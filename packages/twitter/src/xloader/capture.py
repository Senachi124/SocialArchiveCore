"""One cancellable capture child: twscrape metadata, streamed media GET, immutable staging."""
import asyncio
import hashlib
import json
import os
import sys
import uuid
import time
from contextlib import aclosing
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from urllib.parse import urlparse, parse_qs, urlencode
import httpx
from . import db
from .storage import check_storage
from .archive import reusable_post, canonical_post
from .common import CONTROL, DATA, atomic_json, digest_file, HK
from .diagnostics import SafeErrors
from .events import event
from .xsource import Source, SourceFailure


class MediaFetcher:
    def __init__(self, base, progress):
        self.base, self.progress = base, progress
        self.wait_file = DATA / 'session' / 'media-wait.json'

    async def wait(self):
        while self.wait_file.exists():
            until = json.loads(self.wait_file.read_text())['until']
            remaining = until - datetime.now(timezone.utc).timestamp()
            if remaining <= 0: break
            self.progress(wait_until=datetime.fromtimestamp(until, HK).isoformat())
            await asyncio.sleep(min(3, remaining))
        self.progress(wait_until=None)

    async def fetch(self, asset, destination):
        check_storage()
        # URLs only originate in authenticated provider results, never a browser parameter.
        url = urlparse(asset['url'])
        if url.scheme != 'https' or not (url.hostname or '').endswith('.twimg.com'):
            raise SourceFailure('extractor invalid media host')
        query = parse_qs(url.query)
        identity = hashlib.sha256((url.hostname + url.path + '?' + urlencode(
            {key: query[key][0] for key in ('format', 'name') if key in query})).encode()).hexdigest()
        receipts = self.base / '_receipts'
        receipts.mkdir(exist_ok=True)
        receipt_path = receipts / f'{destination.parent.name}-{destination.name}.json'
        if receipt_path.exists() and destination.is_file():
            receipt = json.loads(receipt_path.read_text())
            if receipt.get('identity') == identity and destination.stat().st_size == receipt['size'] and digest_file(destination) == receipt['sha256']:
                return
        parts = self.base / '_parts'
        parts.mkdir(exist_ok=True)
        for attempt in range(3):
            await self.wait()
            started = time.monotonic()
            self.progress(phase='download', file=destination.name, bytes=0, total_bytes=None, elapsed_seconds=0, attempt=attempt+1)
            part = parts / (uuid.uuid4().hex + '.part')
            try:
                async with httpx.AsyncClient(timeout=60, follow_redirects=False, trust_env=False,
                        headers={'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36'}) as client:
                    async with client.stream('GET', asset['url']) as response:
                        if response.status_code == 429:
                            value = response.headers.get('retry-after', '')
                            try: delay = float(value)
                            except ValueError:
                                try: delay = parsedate_to_datetime(value).timestamp() - datetime.now(timezone.utc).timestamp()
                                except (ValueError, TypeError, OverflowError): delay = 60 * (2 ** attempt)
                            until = datetime.now(timezone.utc).timestamp() + max(1, delay)
                            self.wait_file.parent.mkdir(mode=0o700, exist_ok=True)
                            atomic_json(self.wait_file, {'until': until})
                        response.raise_for_status()
                        if 'text/html' in response.headers.get('content-type', ''):
                            raise SourceFailure('extractor invalid media response')
                        size = 0
                        updated = started
                        expected = response.headers.get('content-length')
                        with part.open('wb') as out:
                            async for chunk in response.aiter_bytes():
                                check_storage()
                                out.write(chunk); size += len(chunk)
                                if time.monotonic() - updated >= 1:
                                    updated = time.monotonic()
                                    self.progress(bytes=size, total_bytes=int(expected) if expected else None, elapsed_seconds=round(updated-started, 3))
                        expected = response.headers.get('content-length')
                        if not size or (expected and int(expected) != size):
                            raise OSError('Incomplete media')
                # Keep displaced/unverified bytes; only a completed file becomes visible.
                if destination.exists():
                    destination.rename(parts / (uuid.uuid4().hex + destination.suffix))
                os.replace(part, destination)
                atomic_json(receipt_path, {'identity': identity, 'size': size, 'sha256': digest_file(destination)})
                self.progress(phase='downloaded', bytes=size, elapsed_seconds=round(time.monotonic()-started, 3))
                return
            except (httpx.HTTPError, OSError):
                if attempt == 2: raise
                await asyncio.sleep(2 ** attempt)


async def capture(row, base, archive_version):
    owner = row['spec']['value'] if row['spec']['kind'] == 'account' else None
    seen, references, excluded, boundaries = set(), {}, set(), set()
    report = {'seen': 0, 'stopped_on_known': False, 'missing_references': [], 'excluded_reposts': 0,
              'engine': 'twscrape', 'phase': 'search', 'wait_until': None}
    last_event = [None]
    def progress(**values):
        report.update(values)
        report['seen'] = len(seen)
        atomic_json(base / 'progress.json', report)
        identity = tuple(report.get(key) for key in ('phase', 'post_id', 'file', 'wait_until', 'account', 'endpoint', 'batch'))
        if identity != last_event[0]:
            last_event[0] = identity
            labels = {'search':'搜尋貼文','timeline':'補抓時間線／回覆','download':'下載檔案','downloaded':'檔案下載完成',
                      'references':'擷取引用／回覆原文','publish':'驗證並保存貼文','profile':'擷取用戶頭像','complete':'分段擷取完成'}
            phase = 'cooldown' if report.get('wait_until') else report['phase']
            event(phase, '限流等待' if phase == 'cooldown' else labels.get(phase, '擷取進度'), job_id=row['job_id'],
                  slice_id=row.get('id'), **{k: report[k] for k in ('post_id','file','bytes','total_bytes','elapsed_seconds','attempt','account','endpoint','wait_until','seen') if k in report})
    progress()
    fetcher = MediaFetcher(base, progress)

    async def save(metadata, assets, reference=False):
        nonlocal owner
        post_id = str(metadata['tweet_id'])
        progress(post_id=post_id, file=None, bytes=0, total_bytes=None, elapsed_seconds=None)
        author = canonical_post(metadata)['author']
        if not reference and owner is None and row['spec']['kind'] == 'post': owner = author
        archive_owner = owner or author
        metadata.update(xloader_owner=archive_owner, xloader_reference=reference, xloader_archive_version=archive_version)
        existing = reusable_post(post_id, metadata)
        if existing:
            belongs = db.execute('SELECT 1 FROM xl_job_posts WHERE job_id=%s AND post_id=%s AND version=%s', (row['job_id'],post_id,existing['version']), one=True)
            db.execute('INSERT INTO xl_job_posts VALUES(%s,%s,%s) ON CONFLICT DO NOTHING', (row['job_id'], post_id, existing['version']))
            return not belongs
        folder = base / post_id
        folder.mkdir(exist_ok=True)
        # Preserve a previous record if the revised capture is interrupted.
        if (folder / 'record.json').exists():
            parts = base / '_parts'; parts.mkdir(exist_ok=True)
            (folder / 'record.json').rename(parts / (uuid.uuid4().hex + '.json'))
        names = {asset['name'] for asset in assets}
        for file in folder.iterdir():
            if file.name not in names:
                parts = base / '_parts'; parts.mkdir(exist_ok=True)
                file.rename(parts / (uuid.uuid4().hex + file.suffix))
        for asset in assets:
            await fetcher.fetch(asset, folder / asset['name'])
        # Worker cannot publish a partial post: write metadata after all media completed.
        atomic_json(folder / 'record.json', metadata)
        # Make complete posts available immediately, before the whole job finishes.
        progress(phase='publish', file=None)
        from .archive import publish_post
        publish_post(folder, row['job_id'])
        return False

    async with Source(DATA, CONTROL / 'cookies.txt', progress) as source:
        if owner and hasattr(source, 'profile'):
            from .profiles import capture_profile
            await capture_profile(owner, source, fetcher, base, progress, row['job_id'])
        async with aclosing(source.posts(row)) as stream:
            async for metadata, assets, phase in stream:
                post_id = str(metadata['tweet_id'])
                if metadata.get('retweet_id'):
                    excluded.add(post_id); progress(excluded_reposts=len(excluded)); continue
                author = canonical_post(metadata)['author']
                if owner and author.lower() != owner.lower(): continue
                duplicate = post_id in seen
                seen.add(post_id)
                for ref in canonical_post(metadata)['references']:
                    references[(owner or author, ref['post_id'])] = ref
                # Feed observations must remain candidates for the search stop boundary.
                known = post_id in boundaries if duplicate else await save(metadata, assets)
                if known: boundaries.add(post_id)
                progress(phase=phase)
                if known and phase != 'timeline' and row['spec'].get('stop_on_known'):
                    progress(stopped_on_known=True)
                    break
        progress(phase='references')
        for (ref_owner, post_id), ref in sorted(references.items()):
            if post_id in seen: continue
            existing = reusable_post(post_id, owner=ref_owner, reference=True, archive_version=archive_version)
            if existing:
                db.execute('INSERT INTO xl_job_posts VALUES(%s,%s,%s) ON CONFLICT DO NOTHING', (row['job_id'], post_id, existing['version']))
                continue
            progress(phase='references', post_id=str(post_id), file=None, bytes=0, total_bytes=None, elapsed_seconds=None)
            result = await source.details(post_id)
            if result:
                previous, owner = owner, ref_owner
                await save(*result, reference=True)
                owner = previous
            else:
                report['missing_references'].append(post_id)
            progress()
    progress(phase='complete', wait_until=None)


def main(slice_id):
    check_storage()
    row = db.execute('SELECT s.*,j.spec FROM xl_slices s JOIN xl_jobs j ON j.id=s.job_id WHERE s.id=%s', (slice_id,), one=True)
    base = DATA / 'staging' / str(row['job_id']) / str(slice_id)
    base.mkdir(parents=True, exist_ok=True)
    error_file = base / 'error.json'; error_file.unlink(missing_ok=True)
    dataset = db.execute("SELECT value FROM xl_state WHERE key='dataset'", one=True)
    try:
        asyncio.run(capture(row, base, dataset['value']['version'] if dataset else 'v2'))
        return 0
    except Exception as error:
        errors = SafeErrors()
        errors.capture_exception(error)
        atomic_json(error_file, {**errors.detail, 'exit_code': 70})
        return 70


if __name__ == '__main__':
    try: sys.exit(main(int(sys.argv[1])))
    except Exception: sys.exit(70)
