"""Accept only X session cookies; never serialize values to API responses/logs."""
import http.cookiejar
import io
import json
import time
from http.cookies import SimpleCookie, CookieError

def parse_cookies(value):
    if len(value.encode('utf-8')) > 1024 * 1024:
        raise ValueError('Cookie 檔案過大')
    found = {}
    if value.lstrip().startswith(('[', '{')):
        rows = json.loads(value)
        if isinstance(rows, dict): rows = rows.get('cookies', [])
        for row in rows:
            domain = str(row.get('domain', '')).lstrip('.')
            if domain not in ('x.com', 'twitter.com'): continue
            name, val = row.get('name'), row.get('value')
            if name not in ('auth_token', 'ct0'): continue
            expiry = row.get('expirationDate', row.get('expires', 0)) or 0
            if expiry > 0 and expiry <= time.time(): raise ValueError('X Cookie 已過期')
            found[name] = str(val)
    elif '\t' not in value:
        header = value.strip()
        if header.lower().startswith('cookie:'): header = header[7:].strip()
        if '\n' in header or '\r' in header: raise ValueError('Cookie 標頭須在同一行')
        cookies = SimpleCookie()
        try: cookies.load(header)
        except CookieError: raise ValueError('Cookie 格式無效') from None
        for name in ('auth_token', 'ct0'):
            if name in cookies: found[name] = cookies[name].value
    else:
        for line in value.splitlines():
            line = line.removeprefix('#HttpOnly_')
            if not line or line.startswith('#'): continue
            parts = line.split('\t')
            if len(parts) != 7: continue
            domain, _, _, _, expiry, name, val = parts
            if domain.lstrip('.') not in ('x.com', 'twitter.com') or name not in ('auth_token', 'ct0'): continue
            if int(expiry) > 0 and int(expiry) <= time.time(): raise ValueError('X Cookie 已過期')
            found[name] = val
    if not all(found.get(k) for k in ('auth_token', 'ct0')):
        raise ValueError('需要登入 X 後匯出的 auth_token 與 ct0')
    if any(any(ord(c) < 33 or c in '\t\r\n' for c in val) or len(val) > 4096 for val in found.values()):
        raise ValueError('Cookie 格式無效')
    return '# Netscape HTTP Cookie File\n' + ''.join(f'.x.com\tTRUE\t/\tTRUE\t0\t{k}\t{found[k]}\n' for k in ('auth_token', 'ct0'))
