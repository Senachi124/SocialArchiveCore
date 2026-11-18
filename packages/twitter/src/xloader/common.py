import hashlib
import json
import os
from datetime import datetime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

HK = ZoneInfo('Asia/Hong_Kong')
# Missing configuration stays unset; writer entry points fail closed.
DATA = Path(os.environ['XLOADER_DATA']) if os.environ.get('XLOADER_DATA') else None
CONTROL = Path(os.environ['XLOADER_CONTROL']) if os.environ.get('XLOADER_CONTROL') else None

def now():
    return datetime.now(timezone.utc)

def window_deadline(trigger, at=None):
    if trigger not in ('manual', 'scheduled'):
        raise ValueError('Invalid backup provenance')
    if trigger == 'manual':
        return None
    at = (at or now()).astimezone(HK)
    if not 3 <= at.hour < 9:
        raise WindowClosed(next_window(at))
    return at.replace(hour=9, minute=0, second=0, microsecond=0)

def next_window(at=None):
    at = (at or now()).astimezone(HK)
    start = at.replace(hour=3, minute=0, second=0, microsecond=0)
    return start if at < start else start + timedelta(days=1)

class WindowClosed(Exception):
    def __init__(self, next_run):
        self.next_run = next_run

def safe_path(root, relative):
    parts = relative.split('/')
    if not relative or '\\' in relative or any(p in ('', '.', '..') for p in parts):
        raise ValueError('Unsafe archive path')
    root = Path(root).resolve()
    path = root
    for part in parts:
        path = path / part
        if path.is_symlink():
            raise ValueError('Symlinks are not archive files')
    if not path.resolve().is_relative_to(root):
        raise ValueError('Archive path escapes root')
    return path

def digest_file(path):
    h = hashlib.sha256()
    with open(path, 'rb') as f:
        while block := f.read(1024 * 1024):
            h.update(block)
    return h.hexdigest()

def atomic_json(path, value, mode=0o640):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_name(path.name + '.building')
    with open(temp, 'w', encoding='utf-8') as f:
        json.dump(value, f, ensure_ascii=False, indent=2, default=str)
        f.flush()
        os.fsync(f.fileno())
    os.chmod(temp, mode)
    os.replace(temp, path)
