"""Explicit, fail-closed Linux media mount checks; no SSD or /tmp fallback."""
import json
import os
import shutil
import subprocess
import time
from pathlib import Path

RESERVE_BYTES = 5 * 1024 ** 3
_checked = 0.0


def _absolute_directory(name):
    value = os.environ.get(name)
    if not value or not Path(value).is_absolute():
        raise ValueError(f'{name} must be an explicit absolute directory')
    path = Path(value)
    if not path.is_dir() or path.is_symlink():
        raise ValueError(f'{name} must be an existing nonsymlink directory')
    return path.resolve()


def check_storage(*, force=False):
    """Validate mount identity and capacity, at least once each second of writes.

    Call with force=True before startup. Service managers must also bind the worker
    to its media mount so an unmount stops writers rather than exposing a fallback.
    """
    global _checked
    if not force and 0 < time.monotonic() - _checked < 1:
        return
    data = _absolute_directory('XLOADER_DATA')
    mount = _absolute_directory('XLOADER_MEDIA_MOUNT')
    tmp = _absolute_directory('TMPDIR')
    _absolute_directory('XLOADER_CONTROL')
    uuid = os.environ.get('XLOADER_MEDIA_UUID')
    if not uuid:
        raise ValueError('XLOADER_MEDIA_UUID is required')
    if not data.is_relative_to(mount) or not tmp.is_relative_to(data):
        raise ValueError('Media and temporary files must share the configured mount')
    result = subprocess.run(
        ['findmnt', '--json', '--target', str(data), '--output', 'TARGET,UUID,FSROOT,FSTYPE'],
        check=True, capture_output=True, text=True, timeout=5)
    rows = json.loads(result.stdout).get('filesystems', [])
    expected = {'target': str(mount), 'uuid': uuid,
                'fsroot': os.environ.get('XLOADER_MEDIA_FSROOT', '/'),
                'fstype': os.environ.get('XLOADER_MEDIA_FSTYPE', 'ext4')}
    if len(rows) != 1 or any(rows[0].get(k) != v for k, v in expected.items()):
        raise OSError('Configured media filesystem is unavailable or has the wrong identity')
    if data.stat().st_dev != tmp.stat().st_dev:
        raise OSError('Media and temporary directories are on different filesystems')
    for name in ('staging', 'complete', 'session'):
        child = data / name
        if child.is_symlink() or (child.exists() and child.stat().st_dev != data.stat().st_dev):
            raise OSError('Archive subdirectory escapes the media filesystem')
    if shutil.disk_usage(data).free < RESERVE_BYTES:
        raise OSError('Media filesystem has less than the required 5 GiB reserve')
    _checked = time.monotonic()
