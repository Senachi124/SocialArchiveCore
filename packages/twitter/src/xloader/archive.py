import hashlib
import json
import mimetypes
import os
import re
import shutil
import uuid
from datetime import datetime, timezone
from pathlib import Path
from PIL import Image
from psycopg.types.json import Jsonb
from . import db
from .storage import check_storage
from .common import DATA, atomic_json, digest_file, safe_path
from .paths import account_path, destination_key

def canonical_post(raw):
    user = raw.get('author') or raw.get('user') or {}
    if not isinstance(user, dict): user = {}
    author = user.get('name') or user.get('screen_name') or 'unknown'
    if not re.fullmatch(r'[A-Za-z0-9_]{1,15}', author): author = 'unknown'
    post_id = str(raw.get('tweet_id') or raw.get('id') or '')
    if not re.fullmatch(r'\d{1,25}', post_id): raise ValueError('Missing post ID')
    published = raw.get('date') or raw.get('created_at')
    if published:
        try:
            published = datetime.fromisoformat(str(published).replace('Z', '+00:00'))
            if published.tzinfo is None: published = published.replace(tzinfo=timezone.utc)
            published = published.astimezone(timezone.utc).isoformat()
        except ValueError: published = None
    references = []
    for relation, key in (('repost', 'retweet_id'), ('replied', 'reply_id'), ('quote', 'quoted_id')):
        ref = str(raw.get(key) or '')
        if re.fullmatch(r'\d{1,25}', ref) and ref != '0' and ref != post_id:
            references.append({'kind': relation, 'post_id': ref, 'source_url': f'https://x.com/i/web/status/{ref}'})
    actor = raw.get('xloader_actor') or (raw.get('user') if raw.get('retweet_id') else user) or user
    if not isinstance(actor, dict): actor = user
    actor_name = actor.get('name') or actor.get('screen_name') or author
    if not re.fullmatch(r'[A-Za-z0-9_]{1,15}', actor_name): actor_name = author
    owner = str(raw.get('xloader_owner') or author)
    if not re.fullmatch(r'[A-Za-z0-9_]{1,15}', owner): raise ValueError('Invalid archive owner')
    return {'post_id': post_id, 'author': author, 'display_name': str(user.get('nick') or user.get('display_name') or author),
            'owner': owner, 'is_reference': bool(raw.get('xloader_reference')),
            'archive_version': str(raw.get('xloader_archive_version') or 'v2'),
            'content': str(raw.get('content') or raw.get('full_text') or ''), 'published_at': published,
            'source_url': f'https://x.com/{actor_name}/status/{post_id}',
            'reply_to': raw.get('reply_id'), 'quote_id': raw.get('quoted_id'),
            'retweet_id': raw.get('retweet_id'), 'language': raw.get('lang'),
            'post_type': 'repost' if raw.get('retweet_id') else 'replied' if raw.get('reply_id') else 'quote' if raw.get('quoted_id') else 'post',
            'actor': {'name': actor_name, 'display_name': str(actor.get('nick') or actor.get('display_name') or actor_name)},
            'references': references}

def post_context(post):
    """Legacy manifests are read as-is and classified without rewriting history."""
    refs = post.get('references')
    if refs is None:
        refs = [{'kind': kind, 'post_id': str(post[key]), 'source_url': f'https://x.com/i/web/status/{post[key]}'}
                for kind, key in (('repost', 'retweet_id'), ('replied', 'reply_to'), ('quote', 'quote_id')) if post.get(key)]
    return {'post_type': post.get('post_type') or ('repost' if post.get('retweet_id') else 'replied' if post.get('reply_to') else 'quote' if post.get('quote_id') else 'post'),
            'owner': post.get('owner') or post['author'], 'is_reference': bool(post.get('is_reference')),
            'actor': post.get('actor') or {'name': post['author'], 'display_name': post['display_name']}, 'references': refs}

def reusable_post(post_id, metadata=None, owner=None, reference=None, archive_version=None):
    """Only skip when every immutable local file still matches its manifest."""
    for row in db.execute('SELECT * FROM xl_posts WHERE post_id=%s AND local_available ORDER BY captured_at DESC', (post_id,)):
        post = row['manifest']['post']
        if metadata is not None:
            owner = metadata.get('xloader_owner') or canonical_post(metadata)['author']
            reference = bool(metadata.get('xloader_reference'))
            archive_version = metadata.get('xloader_archive_version') or 'v2'
        if owner is not None and (post.get('owner') or post['author']).lower() != owner.lower(): continue
        if reference is not None and bool(post.get('is_reference')) != reference: continue
        if archive_version is not None and post.get('archive_version') != archive_version: continue
        if metadata is not None:
            if row['content'] != str(metadata.get('content') or metadata.get('full_text') or ''): continue
            if int(metadata.get('count') or 0) > len(post.get('media', [])): continue
        try:
            for entry in row['manifest']['files']:
                path = safe_path(DATA / 'complete', row['relative_dir'] + '/' + entry['name'])
                if not path.is_file() or path.stat().st_size != entry['size'] or digest_file(path) != entry['sha256']: break
            else: return row
        except (OSError, ValueError): continue
    return None

def restore_local(folder, target, manifest, job_id):
    """Repair this same immutable version, preserving any damaged bytes in staging."""
    check_storage()
    for entry in manifest['files']:
        dest = safe_path(target, entry['name'])
        if dest.is_file() and dest.stat().st_size == entry['size'] and digest_file(dest) == entry['sha256']: continue
        repair = DATA / 'staging' / '_repairs' / str(job_id) / uuid.uuid4().hex
        repair.mkdir(parents=True, exist_ok=True)
        candidate = repair / 'verified-file'
        source = safe_path(folder, entry['name'])
        if source.is_file(): shutil.copyfile(source, candidate)
        elif entry['name'] == 'record.json': atomic_json(candidate, manifest['post'])
        elif entry.get('variant') == 'preview':
            original = next(m for m in manifest['post']['media'] if m.get('preview') == entry['name'])
            with Image.open(safe_path(folder, original['name'])) as image:
                image.thumbnail((480, 480))
                image.convert('RGB').save(candidate, 'JPEG', quality=80)
        else: raise ValueError('Repair source unavailable; staging retained')
        if candidate.stat().st_size != entry['size'] or digest_file(candidate) != entry['sha256']:
            raise ValueError('Repair hash mismatch; staging retained')
        temp = safe_path(target, entry['name'] + '.restore')
        shutil.copyfile(candidate, temp); os.chmod(temp, 0o640)
        if dest.exists(): os.rename(dest, repair / 'damaged-file')
        os.replace(temp, dest)

def publish_post(folder, job_id):
    check_storage()
    folder = Path(folder)
    if folder.is_symlink(): raise ValueError('Unsafe staging')
    raw = json.loads((folder / 'record.json').read_text(encoding='utf-8'))
    post = canonical_post(raw)
    media_alt = raw.get('media_alt') or {}
    if not isinstance(media_alt, dict):
        raise ValueError('Invalid media alt metadata')
    if post['post_type'] == 'repost': return False
    media = []
    for file in sorted(folder.iterdir()):
        if file.is_symlink() or not file.is_file(): raise ValueError('Unsafe staging member')
        if file.name == 'record.json': continue
        if file.suffix.lower() not in ('.jpg', '.jpeg', '.png', '.webp', '.gif', '.mp4', '.webm'):
            raise ValueError('Incomplete or unsupported media; staging retained')
        media.append({'name': file.name, 'size': file.stat().st_size, 'sha256': digest_file(file),
                      'mime': mimetypes.guess_type(file.name)[0] or 'application/octet-stream'})
        if file.name in media_alt and media[-1]['mime'].startswith('image/'):
            alt = media_alt[file.name]
            if alt is not None and not isinstance(alt, str):
                raise ValueError('Invalid image alt text')
            media[-1]['alt_text'] = alt.replace('\x00', '\ufffd') if alt is not None else None
    # Source-only signed URLs and cookies are deliberately excluded from portable metadata.
    content_version = hashlib.sha256(json.dumps({'post': post, 'media': media}, sort_keys=True, ensure_ascii=False).encode()).hexdigest()
    archived = db.execute('SELECT * FROM xl_posts WHERE post_id=%s AND version=%s',
                          (post['post_id'], content_version), one=True)
    post['version'] = content_version
    collection = 'references' if post['is_reference'] else 'posts'
    relative = archived['relative_dir'] if archived else f'users/{account_path(post["owner"])}/{collection}/{post["post_id"]}/{content_version}'
    target = safe_path(DATA / 'complete', relative)
    files = list(media)
    if not target.exists():
        target.parent.mkdir(parents=True, exist_ok=True)
        building = target.with_name(target.name + '.building')
        building.mkdir(exist_ok=True)
        for entry in media:
            check_storage()
            dest = building / entry['name']
            shutil.copyfile(folder / entry['name'], dest)
            os.chmod(dest, 0o640)
            if entry['mime'].startswith('image/'):
                try:
                    with Image.open(dest) as image:
                        image.thumbnail((480, 480))
                        preview = entry['name'] + '.preview.jpg'
                        image.convert('RGB').save(building / preview, 'JPEG', quality=80)
                        os.chmod(building / preview, 0o640)
                        entry['preview'] = preview
                        files.append({'name': preview, 'size': (building / preview).stat().st_size,
                                      'sha256': digest_file(building / preview), 'mime': 'image/jpeg', 'variant': 'preview'})
                except (OSError, ValueError): pass
        post['media'] = media
        atomic_json(building / 'record.json', post)
        files.append({'name': 'record.json', 'size': (building / 'record.json').stat().st_size,
                      'sha256': digest_file(building / 'record.json'), 'mime': 'application/json'})
        atomic_json(building / 'manifest.json', {'format': 'xloader.post.v2', 'post': post, 'files': files})
        os.rename(building, target)
    manifest = json.loads((target / 'manifest.json').read_text(encoding='utf-8'))
    restore_local(folder, target, manifest, job_id)
    with db.connection() as c, c.transaction():
        inserted = c.execute('''INSERT INTO xl_posts(post_id,version,author,display_name,content,published_at,source_url,relative_dir,resource_key,manifest)
            VALUES(%s,%s,%s,%s,%s,%s,%s,%s,%s,%s) ON CONFLICT DO NOTHING RETURNING post_id''',
            (post['post_id'], content_version, post['author'], post['display_name'], post['content'], post['published_at'], post['source_url'], relative,
             archived['resource_key'] if archived else destination_key('post', post['post_id'], content_version, relative), Jsonb(manifest))).fetchone()
        c.execute('INSERT INTO xl_job_posts VALUES(%s,%s,%s) ON CONFLICT DO NOTHING', (job_id, post['post_id'], content_version))
        c.execute('UPDATE xl_posts SET local_available=true WHERE post_id=%s AND version=%s', (post['post_id'],content_version))
        if inserted:
            c.execute('UPDATE xl_jobs SET added_posts=added_posts+1,added_files=added_files+%s WHERE id=%s', (len(media), job_id))
    return bool(inserted)

def portable_catalog(rows):
    return {'format': 'xloader.catalog.v2', 'timezone': 'Asia/Hong_Kong', 'posts': [
        {**r['manifest']['post'], 'relative_dir': r['relative_dir'], 'files': r['manifest']['files']} for r in rows]}
