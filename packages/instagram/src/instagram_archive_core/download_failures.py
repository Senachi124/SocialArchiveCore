"""Read bounded, sanitized per-file failure evidence for terminal job status."""
from functools import lru_cache
from pathlib import Path
import re

MAX_BYTES = 16 * 1024 * 1024
MAX_DETAILS = 20
ALPHABET = 'ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789-_'
FAILED = re.compile(r'\[download\]\[error\] Failed to download (\d{5,30})_(\d{5,30})\.(jpg|jpeg|png|webp|mp4)\s*$')
HTTP = re.compile(r"\[downloader\.http\]\[warning\] ['\"]([45]\d\d)\s")


def shortcode(value):
    number = int(value)
    result = ''
    while number:
        number, digit = divmod(number, 64)
        result = ALPHABET[digit] + result
    return result or 'A'


@lru_cache(maxsize=256)
def _read(path, size, modified):
    details = []
    seen = set()
    pending_http = None
    with Path(path).open('rb') as stream:
        for raw in stream.read(MAX_BYTES).decode('utf-8', errors='replace').splitlines():
            warning = HTTP.search(raw)
            if warning:
                pending_http = int(warning[1])
                continue
            failure = FAILED.search(raw)
            if failure:
                name = f'{failure[1]}_{failure[2]}.{failure[3]}'
                if name not in seen:
                    seen.add(name)
                    if len(details) < MAX_DETAILS:
                        details.append({'filename': name, 'httpStatus': pending_http,
                                        'postUrl': 'https://www.instagram.com/p/' + shortcode(failure[1]) + '/'})
                pending_http = None
            elif raw.strip():
                # Never attribute an unrelated earlier warning to a later file.
                pending_http = None
    return {'items': details, 'observedCount': len(seen), 'truncated': size > MAX_BYTES or len(seen) > MAX_DETAILS}


def failure_details(row, runtime):
    if row.get('status') in ('queued', 'running'):
        return {'items': [], 'observedCount': 0, 'truncated': False}
    try:
        path = Path(row.get('log_path') or '')
        expected = Path(runtime).resolve() / f"job-{int(row['id'])}.log"
        if not path.is_absolute() or path.is_symlink() or path.resolve() != expected:
            return {'items': [], 'observedCount': 0, 'truncated': False}
        stat = path.stat()
        return _read(str(path), stat.st_size, stat.st_mtime_ns)
    except (OSError, ValueError, TypeError, KeyError):
        return {'items': [], 'observedCount': 0, 'truncated': False}


def with_failure_status(result, runtime):
    details = failure_details(result, runtime)
    result['downloadFailures'] = details
    if details['items']:
        entries = [' '.join((item['filename'], f"HTTP {item['httpStatus']}" if item['httpStatus'] else '下載失敗', item['postUrl'])) for item in details['items']]
        suffix = '；明細超出顯示範圍，請查工作日誌' if details['truncated'] else ''
        result['message'] = (result.get('message') or '') + '；失敗圖片／影片（該次工作紀錄）：' + '；'.join(entries) + suffix
    return result
