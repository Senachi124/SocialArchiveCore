"""Closed-set diagnostics: never persist upstream text, URLs or credentials."""
import logging
import re
import traceback
from pathlib import Path

MESSAGES = {
    'rate_limit': 'X 暫時限制請求，請稍後再續做',
    'authentication': 'X 登入已失效或需要重新驗證，請更新 Cookie',
    'account_locked': 'X 登入帳戶被暫時鎖定，請先在 X 完成驗證',
    'not_found': '用戶或貼文不存在／無法存取，請核對名稱與權限',
    'network': '連線逾時或網絡暫時失敗，請稍後續做',
    'upstream': 'X 服務回應錯誤，請稍後續做',
    'extractor': 'X 回應格式無法解析，需要檢查擷取器',
    'capture': '擷取器未能完成，已保存診斷代碼與分段進度',
}

_SAFE_NAME = re.compile(r'^[A-Za-z_][A-Za-z0-9_]*$')


def _safe_exception_type(error):
    name = type(error).__name__
    return name if _SAFE_NAME.fullmatch(name) else 'Exception'


def _safe_where(error):
    """Program location only: xloader-relative path, line, and function. No messages."""
    frames = traceback.extract_tb(error.__traceback__) if error.__traceback__ else []
    if not frames:
        return None
    chosen = None
    for frame in frames:
        parts = Path(frame.filename).parts
        if 'xloader' in parts:
            chosen = frame
    if chosen is None:
        chosen = frames[-1]
    parts = Path(chosen.filename).parts
    if 'xloader' in parts:
        rel = '/'.join(parts[parts.index('xloader'):])
    else:
        rel = Path(chosen.filename).name
    func = chosen.name if _SAFE_NAME.fullmatch(chosen.name or '') else '?'
    return f'{rel}:{chosen.lineno}:{func}'


class SafeErrors(logging.Handler):
    def __init__(self):
        super().__init__(logging.WARNING)
        self.detail = {'category': 'capture'}

    def observe(self, text, http_status=None):
        value = str(text).lower()
        category = ('rate_limit' if http_status == 429 or 'rate_limit' in value or 'rate limit' in value or 'ratelimit' in value or re.search(r'\b429\b', value) else
                    'account_locked' if 'locked' in value else
                    'authentication' if http_status == 401 or 'authenticat' in value or 'authorizationerror' in value or 'login required' in value or 'authrequired' in value else
                    'network' if any(s in value for s in ('timeout','connectionerror','connection error','timed out')) else
                    'upstream' if http_status and http_status >= 500 else
                    'extractor' if any(s in value for s in ('extractor','keyerror','attributeerror','unexpected error','jsondecodeerror','xclid','web scripts')) else
                    'not_found' if http_status == 404 or 'notfound' in value or 'not found' in value else 'capture')
        if category != 'capture' or self.detail['category'] == 'capture':
            keep = {key: self.detail[key] for key in ('exception_type', 'where') if key in self.detail}
            self.detail = {'category': category, **keep}
            if http_status is not None: self.detail['http_status'] = int(http_status)

    def capture_exception(self, error):
        """Classify from type/message; persist only category, type name, and code location."""
        response = getattr(error, 'response', None)
        status = getattr(response, 'status_code', None)
        # Message is used only for closed-set classification and is never stored.
        self.observe(f'{type(error).__name__} {error}', status)
        # SourceFailure('not_found') has no space, so the substring classifier misses it.
        label = str(error)
        if label in MESSAGES:
            self.detail['category'] = label
        self.detail['exception_type'] = _safe_exception_type(error)
        where = _safe_where(error)
        if where:
            self.detail['where'] = where
        return self.detail

    def emit(self, record):
        self.observe(record.getMessage())


def failure_message(detail, code):
    category = detail.get('category', 'capture')
    http = detail.get('http_status')
    suffix = f'；HTTP {http}' if type(http) is int and 100 <= http <= 599 else ''
    kind = detail.get('exception_type')
    where = detail.get('where')
    if isinstance(kind, str) and _SAFE_NAME.fullmatch(kind):
        suffix += f'；{kind}'
    if isinstance(where, str) and re.fullmatch(r'[A-Za-z0-9_./:-]{1,200}', where):
        suffix += f' @ {where}'
    return f'{MESSAGES.get(category, MESSAGES["capture"])}（退出碼 {int(code)}{suffix}）'
