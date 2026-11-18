"""Contextual gallery-dl logs without cookies, request bodies or signed queries."""
import logging
import time
from urllib.parse import urlsplit, urlunsplit


def safe_url(value):
    try:
        url = urlsplit(str(value))
        if url.scheme not in ('https', 'http') or not url.hostname:
            return '[invalid-url]'
        path = ''.join(c for c in url.path if ord(c) >= 32 and ord(c) != 127)
        return urlunsplit((url.scheme, url.hostname, path, '', ''))[:500]
    except ValueError:
        return '[invalid-url]'


def trace_request(original):
    def request(self, url, **kwargs):
        # Fail before sending if a future adapter accidentally restores search.
        path = urlsplit(str(url)).path.rstrip('/')
        if path in ('/web/search/topsearch', '/api/v1/web/search/topsearch'):
            raise RuntimeError('Deprecated Instagram user-search endpoint is disabled')
        started = time.monotonic()
        category = self.subcategory
        source = safe_url(url)
        target = safe_url(self.url)
        method = kwargs.get('method', 'GET')
        self.log.info('request category=%s target=%s method=%s url=%s', category, target, method, source)
        try:
            response = original(self, url, **kwargs)
        except Exception as error:
            status = getattr(error, 'status', None)
            status = status if isinstance(status, int) and 100 <= status <= 599 else 'unknown'
            self.log.error('request failed category=%s target=%s method=%s url=%s status=%s exception=%s elapsed=%.2fs',
                           category, target, method, source, status, type(error).__name__, time.monotonic() - started)
            raise
        self.log.info('response category=%s status=%s url=%s elapsed=%.2fs', category,
                      response.status_code, safe_url(response.url), time.monotonic() - started)
        return response
    return request


class ErrorCounter(logging.Handler):
    def __init__(self):
        super().__init__(logging.ERROR)
        self.count = 0

    def emit(self, record):
        self.count += 1


def trace_job(original):
    def run(self):
        extractor = self.extractor
        if extractor.category != 'instagram':
            return original(self)
        counter = ErrorCounter()
        logger = logging.getLogger()
        logger.addHandler(counter)
        started = time.monotonic()
        extractor.log.info('category start category=%s target=%s', extractor.subcategory, safe_url(extractor.url))
        try:
            result = original(self)
            # AbortExtraction can carry code=0, even after logging an error.
            if counter.count:
                self.status |= 1
                result = (result or 0) | 1
            return result
        finally:
            logger.removeHandler(counter)
            extractor.log.info('category end category=%s status=%s errors=%s elapsed=%.2fs',
                               extractor.subcategory, self.status, counter.count, time.monotonic() - started)
    return run
