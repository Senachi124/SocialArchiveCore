"""Build bounded Latest queries; all dates are inclusive since / exclusive until."""
import re
from datetime import date, datetime, timedelta, timezone
from urllib.parse import parse_qs, urlencode, urlparse
from .common import HK

HANDLE = re.compile(r'[A-Za-z0-9_]{1,15}')
STATUS = re.compile(r'/([A-Za-z0-9_]{1,15}|i/web)/status/(\d{1,25})/?')

def normalize(spec):
    options = {k: bool(spec.get(k, default)) for k, default in (
        ('replies', True), ('media_only', False), ('stop_on_known', False), ('rolling_end', True))}
    options['auto_mode'] = spec.get('auto_mode', 'incremental')
    if options['auto_mode'] not in ('incremental', 'full', 'search'): raise ValueError('自動模式無效')
    options['workflow_version'] = int(spec.get('workflow_version', 1))
    options['auto_configured'] = bool(spec.get('auto_configured', False))
    if options['workflow_version'] not in (1, 2): raise ValueError('抓取設定版本無效')
    options['extra_query'] = str(spec.get('extra_query') or '').strip()
    if len(options['extra_query']) > 1000 or any(ord(c) < 32 for c in options['extra_query']): raise ValueError('進階條件過長或格式無效')
    if re.search(r'(?<!\S)(since|until|max_id|since_id|until_id):', options['extra_query']): raise ValueError('日期請使用日期欄位，進階條件不接受日期或 ID 分頁運算子')
    kind = spec.get('kind', 'account')
    value = str(spec.get('value', '')).strip()
    if len(value) > 1500 or any(ord(c) < 32 for c in value):
        raise ValueError('目標過長或含無效字元')
    if value.startswith(('https://', 'http://')):
        u = urlparse(value)
        if u.scheme != 'https' or u.hostname not in ('x.com', 'www.x.com', 'twitter.com', 'www.twitter.com') or u.username or u.port:
            raise ValueError('只接受 HTTPS 的 X / Twitter 網址')
        if u.path.rstrip('/') == '/search':
            kind, value = 'search', parse_qs(u.query).get('q', [''])[0]
        elif STATUS.fullmatch(u.path):
            return {'kind': 'post', 'value': 'https://x.com' + u.path, 'since': None, 'until': None, **options}
        else:
            kind, value = 'account', u.path.strip('/').split('/')[0]
    if kind == 'post':
        raise ValueError('貼文模式請輸入完整 X 貼文網址')
    if kind == 'account':
        value = value.lstrip('@')
        if not HANDLE.fullmatch(value):
            raise ValueError('X 帳號名稱無效')
        query = 'from:' + value
        if not spec.get('replies', True): query += ' -filter:replies'
        if spec.get('media_only', False): query += ' filter:media'
        if options['extra_query']: query += ' (' + options['extra_query'] + ')'
    elif kind == 'search':
        if not value: raise ValueError('請輸入進階搜尋式')
        # Move simple date operators to the planner, preserving quoted text.
        operators = re.findall(r'(?<!\S)(since|until):(\d{4}-\d{2}-\d{2})(?=\s|$)', value)
        if len({k for k, _ in operators}) != len(operators):
            raise ValueError('每個日期條件只能設定一次')
        for key, val in operators:
            if spec.get(key) and spec[key] != val: raise ValueError('搜尋式與日期欄位不一致')
            spec = {**spec, key: val}
        query = re.sub(r'(?<!\S)(since|until):\d{4}-\d{2}-\d{2}(?=\s|$)', '', value).strip()
        if re.search(r'(?<!\S)(since|until):', query): raise ValueError('日期請使用 YYYY-MM-DD')
        if re.search(r'(?<!\S)(max_id|since_id|until_id):', query): raise ValueError('分段搜尋不接受 ID 分頁條件')
    else:
        raise ValueError('目標類型無效')
    since = date.fromisoformat(spec.get('since') or '2006-03-21')
    # New UI uses inclusive end_date. Persisted until and X query operators stay exclusive.
    exclusive = spec.get('until')
    if spec.get('end_date'):
        inclusive_end = date.fromisoformat(spec['end_date']) + timedelta(days=1)
        if exclusive and date.fromisoformat(exclusive) != inclusive_end:
            raise ValueError('搜尋式 until 與包含當天的結束日期不一致')
        exclusive = inclusive_end.isoformat()
    until = date.fromisoformat(exclusive or (datetime.now(HK).date() + timedelta(days=1)).isoformat())
    if since < date(2006, 3, 21) or since >= until or until - since > timedelta(days=365 * 25):
        raise ValueError('日期範圍無效')
    days = int(spec.get('slice_days', 30))
    if not 1 <= days <= 90: raise ValueError('每段日期須為 1–90 天')
    return {'kind': kind, 'value': query if kind == 'search' else value, 'query': query, 'since': since.isoformat(), 'until': until.isoformat(),
            'end_date': (until - timedelta(days=1)).isoformat(), 'slice_days': days, **options}

def target_spec(target, mode='full'):
    spec = dict(target['spec'])
    if mode == 'incremental' and spec.get('workflow_version') == 2:
        today = datetime.now(HK).date()
        baseline = target['last_success_at']
        # Recent capture has no user-entered dates. Historical backfill is explicit.
        start = baseline.astimezone(HK).date() - timedelta(days=2) if baseline else today - timedelta(days=6)
        spec.update(since=min(start, today).isoformat(), until=(today + timedelta(days=1)).isoformat(), end_date=today.isoformat())
        return spec
    if spec['kind'] != 'post':
        if spec.get('rolling_end', True):
            spec['until'] = (datetime.now(HK).date() + timedelta(days=1)).isoformat()
            spec['end_date'] = datetime.now(HK).date().isoformat()
        if mode == 'incremental' and target['last_success_at']:
            spec['since'] = max(spec['since'], (target['last_success_at'].astimezone(HK) - timedelta(days=2)).date().isoformat())
    if mode in ('full', 'search') or not target['last_success_at']: spec['stop_on_known'] = False
    return spec

def workflow_spec(target, body, scheduled=False):
    mode = body.get('mode', 'incremental')
    if mode not in ('incremental', 'full', 'search', 'post') or (scheduled and mode == 'post'):
        raise ValueError('請選擇有效的抓取模式')
    if mode == 'post':
        spec = normalize({'kind': 'post', 'value': body.get('post_url', '')})
        if spec['kind'] != 'post': raise ValueError('單則模式需要 X 貼文網址')
        return spec
    old = target['spec']
    spec = {'kind': old['kind'], 'value': old.get('query', old['value']) if old['kind'] == 'search' else old['value'],
            'workflow_version': 2, 'auto_configured': scheduled, 'auto_mode': mode, 'replies': body.get('replies', True),
            'media_only': body.get('media_only', False), 'stop_on_known': mode == 'incremental' and body.get('stop_on_known', True),
            'rolling_end': mode == 'incremental' or (scheduled and body.get('rolling_end', True))}
    if old['kind'] == 'post': raise ValueError('請先新增 X 用戶，再設定自動或批次抓取')
    if mode == 'search':
        query = str(body.get('query') or '').strip()
        if not query: raise ValueError('請填寫進階搜尋條件')
        if old['kind'] == 'account': spec['extra_query'] = query
        else: spec['value'] = query
    if mode in ('full', 'search'):
        spec.update(since=body.get('since') or '2006-03-21', end_date=body.get('end_date') or datetime.now(HK).date().isoformat(), slice_days=body.get('slice_days', 30))
    normalized = normalize(spec)
    return normalized if scheduled else target_spec({**target, 'spec': normalized}, mode)

def plan(spec):
    if spec['kind'] == 'post': return [(None, None, spec['value'])]
    start, end = date.fromisoformat(spec['since']), date.fromisoformat(spec['until'])
    result = []
    while end > start:
        left = max(start, end - timedelta(days=spec['slice_days']))
        result.append((left, end, search_url(spec['query'], left, end)))
        end = left
    return result

def search_url(query, since, until):
    return 'https://x.com/search?' + urlencode({'q': f'({query}) since:{since} until:{until}', 'f': 'live'})

def split_slice(query, start, end):
    start, end = date.fromisoformat(str(start)), date.fromisoformat(str(end))
    if (end - start).days <= 1: return []
    mid = start + timedelta(days=(end - start).days // 2)
    return [(mid, end, search_url(query, mid, end)), (start, mid, search_url(query, start, mid))]
