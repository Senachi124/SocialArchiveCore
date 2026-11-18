"""Immutable avatar originals, stored alongside other complete archive resources."""
import os
import re
import shutil
import uuid
from urllib.parse import urlparse, urlunparse
from PIL import Image
from psycopg.types.json import Jsonb
from . import db
from .storage import check_storage
from .common import DATA, atomic_json, digest_file, safe_path
from .events import event
from .paths import account_path, destination_key

def original_avatar(url):
    parsed = urlparse(url or '')
    if parsed.scheme != 'https' or parsed.hostname != 'pbs.twimg.com' or not parsed.path.startswith('/profile_images/'):
        raise ValueError('Invalid avatar source')
    return urlunparse(parsed._replace(path=re.sub(r'_(normal|bigger|mini)(\.[^/.]+)$', r'\2', parsed.path)))

def publish_profile(username, display_name, source):
    check_storage()
    username = account_path(username)
    if not re.fullmatch(r'[A-Za-z0-9_]{1,15}', username): raise ValueError('Invalid username')
    with Image.open(source) as image:
        image.verify()
        mime = Image.MIME.get(image.format)
    if mime not in ('image/jpeg','image/png','image/gif','image/webp'): raise ValueError('Invalid avatar image')
    version = digest_file(source)
    existing = db.execute('SELECT * FROM xl_profiles WHERE username=%s AND version=%s', (username,version), one=True)
    relative = existing['relative_dir'] if existing else f'users/{username}/profiles/{version}'
    target = safe_path(DATA / 'complete', relative)
    info = {'name':'avatar', 'size':source.stat().st_size, 'sha256':version, 'mime':mime}
    manifest = {'format':'xloader.profile.v1', 'username':username, 'display_name':display_name, 'version':version, 'files':[info]}
    if not target.exists():
        target.parent.mkdir(parents=True, exist_ok=True)
        building = target.with_name(version + '.' + uuid.uuid4().hex + '.building')
        building.mkdir()
        shutil.copyfile(source, building / 'avatar'); os.chmod(building / 'avatar', 0o640)
        atomic_json(building / 'manifest.json', manifest)
        os.rename(building, target)
    if digest_file(target / 'avatar') != version: raise ValueError('Avatar integrity mismatch')
    db.execute('''INSERT INTO xl_profiles(username,version,display_name,relative_dir,resource_key,manifest)
      VALUES(%s,%s,%s,%s,%s,%s) ON CONFLICT(username,version) DO UPDATE SET captured_at=now()''',
      (username,version,display_name,relative,existing['resource_key'] if existing else destination_key('profile',username,version,relative),Jsonb(manifest)))
    return version

async def capture_profile(username, source, fetcher, base, progress, job_id):
    # Refresh at most daily; original versions remain immutable.
    old = db.execute("SELECT 1 FROM xl_profiles WHERE username=%s AND captured_at>now()-interval '1 day'", (username.lower(),), one=True)
    if old: return
    progress(phase='profile', file=None, post_id=None)
    profile = await source.profile(username)
    if not profile or not profile.get('url'):
        event('profile', '用戶資料未提供頭像', job_id=job_id)
        return
    folder = base / '_profiles'; folder.mkdir(exist_ok=True)
    path = folder / (username.lower() + '.image')
    try:
        await fetcher.fetch({'url':original_avatar(profile['url'])}, path)
        publish_profile(username, profile['display_name'], path)
        event('profile', '頭像已保存', job_id=job_id, bytes=path.stat().st_size)
    except Exception as error:
        # Avatar download failure must not discard otherwise usable post progress.
        event('profile_failed', '頭像未完成；保留貼文擷取', job_id=job_id, exception_type=type(error).__name__)
