"""Resumable, metadata-only ALT jobs over an explicitly selected saved account."""
import argparse
import asyncio
import json
import re
import sys
import uuid
from pathlib import Path
from psycopg.types.json import Jsonb
from . import db
from .storage import check_storage
from .common import DATA, CONTROL, now, safe_path, digest_file, atomic_json
from .archive import canonical_post
from .alt_metadata import state_key
from .diagnostics import SafeErrors
from .xsource import Source


def schedule(owner):
    owner = owner.lstrip('@').lower()
    if not re.fullmatch(r'[a-z0-9_]{1,15}', owner):
        raise ValueError('Invalid account')
    with db.connection() as c, c.transaction():
        target = c.execute("SELECT * FROM xl_targets WHERE NOT archived AND spec->>'kind'='account' AND lower(ltrim(spec->>'value','@'))=%s", (owner,)).fetchone()
        if not target: raise ValueError('Saved account not found')
        c.execute('SELECT pg_advisory_xact_lock(89272,%s)', (target['id'],))
        target = c.execute("SELECT * FROM xl_targets WHERE id=%s AND NOT archived AND spec->>'kind'='account' AND lower(ltrim(spec->>'value','@'))=%s FOR UPDATE", (target['id'],owner)).fetchone()
        if not target: raise ValueError('Saved account changed during scheduling')
        active = c.execute("SELECT * FROM xl_jobs WHERE target_id=%s AND status IN ('queued','running','paused') ORDER BY id LIMIT 1", (target['id'],)).fetchone()
        if active:
            if active['spec'].get('alt_backfill'): return active
            raise ValueError('Account already has an active capture job')
        rows = c.execute("""SELECT post_id,version FROM xl_posts
            WHERE lower(COALESCE(manifest->'post'->>'owner',author))=%s
            AND EXISTS (SELECT 1 FROM jsonb_array_elements(manifest->'post'->'media') m
                        WHERE m->>'mime' LIKE 'image/%%')
            ORDER BY published_at DESC,post_id,version""", (owner,)).fetchall()
        if not rows: raise ValueError('No saved images for this account')
        spec = {**target['spec'], 'value': owner, 'alt_backfill': True, 'stop_on_known': False,
                'alt_selected_versions': len(rows)}
        job = c.execute("INSERT INTO xl_jobs(target_id,spec,trigger,message) VALUES(%s,%s,'manual',%s) RETURNING *",
                        (target['id'], Jsonb(spec), f'圖片 ALT 補抓：已固定選取 {len(rows)} 筆保存版本；不重新下載圖片')).fetchone()
        for row in rows:
            url = f"https://x.com/i/web/status/{row['post_id']}#xloader-alt-version={row['version']}"
            c.execute('INSERT INTO xl_slices(job_id,url) VALUES(%s,%s)', (job['id'], url))
        return job


def checked_media(row, metadata, assets):
    """Bind descriptions to the same immutable tweet and capture file layout.

    Legacy archives did not retain CDN identities. Require the unchanged tweet
    text/author, identical asset layout and verified saved bytes; never infer ALT.
    """
    post = row['manifest']['post']
    if (str(metadata['tweet_id']) != row['post_id']
            or canonical_post(metadata)['author'].lower() != row['author'].lower()
            or metadata.get('content', '') != row['content']):
        raise ValueError('來源貼文身分或文字已改變，保留原記錄')
    entries = post.get('media', [])
    names = [a['name'] for a in assets]
    if len(names) != len(set(names)) or sorted(names) != sorted(m['name'] for m in entries):
        raise ValueError('來源媒體配置與保存版本不同，保留原記錄')
    if not row['local_available']:
        raise ValueError('本機副本不可驗證；不下載 NAS 或重建媒體')
    found = []
    for entry in entries:
        if not entry['mime'].startswith('image/'): continue
        path = safe_path(DATA / 'complete', row['relative_dir'] + '/' + entry['name'])
        if not path.is_file() or path.stat().st_size != entry['size'] or digest_file(path) != entry['sha256']:
            raise ValueError('本機圖片未通過雜湊驗證，保留原記錄')
        alt = metadata.get('media_alt', {}).get(entry['name'])
        if alt is not None and not isinstance(alt, str):
            raise ValueError('來源 ALT 格式不正確')
        found.append({'name': entry['name'], 'sha256': entry['sha256'], 'alt_text': alt})
    return found


def persist(row, media, job_id):
    key = state_key(row)
    with db.connection() as c, c.transaction():
        job = c.execute('SELECT status,cancel_requested,pause_requested FROM xl_jobs WHERE id=%s FOR UPDATE', (job_id,)).fetchone()
        if not job or job['status'] != 'running' or job['cancel_requested'] or job['pause_requested']:
            raise InterruptedError('ALT job no longer active')
        current = c.execute('SELECT manifest,author FROM xl_posts WHERE post_id=%s AND version=%s FOR SHARE', (row['post_id'],row['version'])).fetchone()
        if not current or current['manifest'] != row['manifest'] or current['author'] != row['author']:
            raise ValueError('Saved index changed during ALT check')
        # Never erase an earlier valid description when an API omits it later.
        c.execute('SELECT pg_advisory_xact_lock(hashtextextended(%s,0))', (key,))
        previous = c.execute('SELECT value FROM xl_state WHERE key=%s', (key,)).fetchone()
        old = {m['name']: m for m in previous['value'].get('media', [])} if previous else {}
        for entry in media:
            prior = old.get(entry['name'], {})
            if not entry['alt_text'] and prior.get('sha256') == entry['sha256'] and prior.get('alt_text'):
                entry['alt_text'] = prior['alt_text']
        value = {'post_id': row['post_id'], 'version': row['version'], 'checked_at': now().isoformat(),
                 'owner': row['manifest']['post'].get('owner') or row['author'],
                 'job_id': job_id, 'source': 'x_api', 'mapping': 'immutable-tweet-and-capture-filename-v1',
                 'local_sha256_verified': True, 'media': media}
        c.execute('INSERT INTO xl_state(key,value) VALUES(%s,%s) ON CONFLICT(key) DO UPDATE SET value=excluded.value', (key, Jsonb(value)))
        if any(m.get('alt_text') for m in media) and (not previous or previous['value'].get('media') != media):
            c.execute("INSERT INTO xl_state VALUES('image_alt_revision',%s) ON CONFLICT(key) DO UPDATE SET value=excluded.value", (Jsonb({'revision':uuid.uuid4().hex}),))
        c.execute('INSERT INTO xl_job_posts VALUES(%s,%s,%s) ON CONFLICT DO NOTHING', (job_id,row['post_id'],row['version']))


async def capture(row, base):
    match = re.fullmatch(r'https://x\.com/i/web/status/(\d{1,25})#xloader-alt-version=([a-f0-9]{64})', row['url'])
    if not match or not row['spec'].get('alt_backfill'): raise ValueError('Invalid ALT selection')
    post = db.execute('SELECT * FROM xl_posts WHERE post_id=%s AND version=%s', match.groups(), one=True)
    report = {'seen': 0, 'phase': 'alt_lookup', 'post_id': match[1], 'alt_count': 0, 'alt_warning': ''}
    def progress(**values):
        report.update(values)
        atomic_json(base / 'progress.json', report)
    progress()
    if not post or (post['manifest']['post'].get('owner') or post['author']).lower() != row['spec']['value'].lower():
        progress(seen=1, alt_warning='保存版本不在固定帳戶範圍內')
        return
    async with Source(DATA, CONTROL / 'cookies.txt', progress) as source:
        result = await source.details(post['post_id'])
    if result is None:
        progress(seen=1, phase='complete', alt_warning='X 未回傳貼文：可能已刪除或存取受限', wait_until=None)
        return
    try:
        media = checked_media(post, *result)
    except (ValueError, OSError):
        progress(seen=1, phase='complete', alt_warning='來源／本機驗證不符；未修改原記錄', wait_until=None)
        return
    persist(post, media, row['job_id'])
    progress(seen=1, phase='complete', alt_count=sum(bool(m['alt_text']) for m in media),
             checked_images=len(media), wait_until=None)


def main(slice_id):
    check_storage()
    row = db.execute('SELECT s.*,j.spec FROM xl_slices s JOIN xl_jobs j ON j.id=s.job_id WHERE s.id=%s', (slice_id,), one=True)
    base = DATA / 'staging' / str(row['job_id']) / str(slice_id)
    base.mkdir(parents=True, exist_ok=True)
    try:
        asyncio.run(capture(row, base))
        return 0
    except Exception as error:
        errors = SafeErrors(); errors.capture_exception(error)
        atomic_json(base / 'error.json', {**errors.detail, 'exit_code': 70})
        return 70


if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('slice_id', nargs='?', type=int)
    parser.add_argument('--schedule')
    args = parser.parse_args()
    if args.schedule:
        job = schedule(args.schedule)
        print(json.dumps({'job_id': job['id'], 'status': job['status'], 'selected_versions': job['spec']['alt_selected_versions']}))
    else:
        sys.exit(main(args.slice_id))
