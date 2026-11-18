"""Verified local media and target-membership catalog for gallery-dl.

The adapter deliberately has no NAS or host helper dependencies. The worker
process lock serializes writers; a marker is committed only after the final
file exists, is nonempty, and has been hashed.
"""
from contextlib import contextmanager
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import re
import sqlite3
from .storage import media_root, guard_disk, guard_identity


def stamp():
    return datetime.now(timezone.utc).isoformat()


@contextmanager
def database():
    root=guard_identity()
    db=sqlite3.connect(root/'media.sqlite3',timeout=20)
    db.row_factory=sqlite3.Row
    try:
        with db:
            db.executescript('''CREATE TABLE IF NOT EXISTS media(
                media_id TEXT PRIMARY KEY,path TEXT NOT NULL,size INTEGER NOT NULL,sha256 TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS posts(target TEXT NOT NULL,post_id TEXT NOT NULL,
                media_ids TEXT NOT NULL,updated_at TEXT NOT NULL,PRIMARY KEY(target,post_id));
                CREATE TABLE IF NOT EXISTS memberships(media_id TEXT NOT NULL,target TEXT NOT NULL,
                PRIMARY KEY(media_id,target));''')
            yield db
    finally:
        db.close()


def identity(value):
    value=str(value)
    if not re.fullmatch(r'\d+',value):
        raise ValueError('Invalid stable Instagram media identity')
    return value


def known_media(media_id):
    with database() as db:
        row=db.execute('SELECT * FROM media WHERE media_id=?',(identity(media_id),)).fetchone()
    if not row:return False
    path=media_root()/'library'/row['path']
    try:
        if path.is_symlink() or not path.resolve(strict=True).is_relative_to(media_root()/'library'):
            return False
        if path.stat().st_size!=row['size']:return False
        with path.open('rb') as stream:
            return hashlib.file_digest(stream,'sha256').hexdigest()==row['sha256']
    except OSError:
        return False


def known_post_id(target,post_id,before=None):
    with database() as db:
        row=db.execute('SELECT * FROM posts WHERE target=? AND post_id=?',
                       (target.lower(),identity(post_id))).fetchone()
    return bool(row and (not before or row['updated_at']<=before)
                and json.loads(row['media_ids']) and all(known_media(i) for i in json.loads(row['media_ids'])))


def save_post(target,post):
    ids=post.get('child_ids') or [post['id']]
    if not ids or not all(known_media(i) for i in ids):return False
    with database() as db:
        db.execute('''INSERT INTO posts VALUES(?,?,?,?) ON CONFLICT(target,post_id)
            DO UPDATE SET media_ids=excluded.media_ids,updated_at=excluded.updated_at''',
            (target.lower(),identity(post['id']),json.dumps(ids),stamp()))
    return True


def remember_target(media_ids,data):
    target=data.get('archive_target')
    if not target:return
    with database() as db:
        for media_id in media_ids:
            db.execute('INSERT OR IGNORE INTO memberships VALUES(?,?)',(identity(media_id),target.lower()))


class LocalArchive:
    def __init__(self,pathfmt=None):self.pathfmt=pathfmt
    def keygen(self,data):return 'instagram'+identity(data['media_id'])
    def check(self,data):
        guard_disk()
        if known_media(data['media_id']):
            remember_target([data['media_id']],data)
            return True
        return False
    def add(self,data):
        if self.pathfmt is None:
            if not known_media(data['media_id']):raise RuntimeError('Completed file is required')
            return
        self.store(Path(getattr(self.pathfmt,'realpath',None) or self.pathfmt.path),data)
    def store(self,path,data):
        guard_disk()
        path=Path(path)
        if path.is_symlink():raise ValueError('Symlink media rejected')
        resolved=path.resolve(strict=True)
        root=media_root()/'library'
        if not resolved.is_relative_to(root) or resolved.stat().st_size<=0:
            raise ValueError('Invalid completed media path')
        with resolved.open('rb') as stream:digest=hashlib.file_digest(stream,'sha256').hexdigest()
        with database() as db:
            db.execute('''INSERT INTO media VALUES(?,?,?,?) ON CONFLICT(media_id)
                DO UPDATE SET path=excluded.path,size=excluded.size,sha256=excluded.sha256''',
                (identity(data['media_id']),str(resolved.relative_to(root)),resolved.stat().st_size,digest))
        remember_target([data['media_id']],data)
        if data.get('subcategory') in ('posts','reels') and data.get('_archive_post_ids'):
            save_post(str(data.get('archive_target') or data.get('username','')),
                      {'id':data['post_id'],'child_ids':data['_archive_post_ids']})
    def finalize(self):pass
    def close(self):pass
