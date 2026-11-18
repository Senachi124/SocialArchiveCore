"""Explicit, verified mounted media storage; no fallback directories."""
import json
import os
from pathlib import Path
import shutil
import subprocess

MIN_FREE = 5 * 1024**3


def media_root():
    raw = os.environ.get('IG_CORE_MEDIA_ROOT')
    if not raw or not Path(raw).is_absolute():
        raise RuntimeError('IG_CORE_MEDIA_ROOT must be an existing absolute directory')
    root = Path(raw)
    if root.is_symlink() or not root.is_dir():
        raise RuntimeError('Media root must exist and must not be a symlink')
    return root.resolve(strict=True)


def verify_storage(require_free=True):
    root = media_root()
    expected = os.environ.get('IG_CORE_MOUNT_UUID')
    expected_root = os.environ.get('IG_CORE_FSROOT', '/')
    if not expected:
        raise RuntimeError('IG_CORE_MOUNT_UUID is required; refusing unverified storage')
    result = subprocess.run(['findmnt', '--json', '--target', str(root), '--output', 'UUID,FSROOT'],
                            capture_output=True, text=True, check=True, timeout=10)
    mounts = json.loads(result.stdout).get('filesystems', [])
    if len(mounts) != 1 or mounts[0].get('uuid') != expected or mounts[0].get('fsroot') != expected_root:
        raise RuntimeError('Media filesystem identity mismatch; preserving queue and files')
    for name in ('library','runtime','tmp','archive.sqlite3','archive.sqlite3-wal','archive.sqlite3-shm','media.sqlite3','media.sqlite3-wal','media.sqlite3-shm','worker.lock','gallery-cache.sqlite3','gallery-cache.sqlite3-wal','gallery-cache.sqlite3-shm','instaloader-rate.sqlite3','instaloader-rate.sqlite3-wal','instaloader-rate.sqlite3-shm'):
        child=root/name
        if child.is_symlink() or (child.exists() and child.stat().st_dev != root.stat().st_dev):
            raise RuntimeError('Storage child escapes the verified filesystem')
        if name in ('library','runtime','tmp') and child.exists() and not child.is_dir():
            raise RuntimeError('Invalid storage directory')
    if require_free and shutil.disk_usage(root).free < MIN_FREE:
        raise RuntimeError('Less than 5 GiB free; preserving queue and files')
    return root


def guard_disk():
    return verify_storage(require_free=True)


def guard_identity():
    return verify_storage(require_free=False)
