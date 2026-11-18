"""Replace Instagram discovery only; preserve gallery-dl's media/archive pipeline."""
import copy
import os
from pathlib import Path
from importlib.metadata import version
import instaloader

try:
    from .instagram_web_client import WebClient, WebStop
    from .instagram_web_timeline import pages, TimelineError
    from .instagram_web_reels import pages as reels_pages, ReelsError
    from .instagram_policy import RequestWindow
except ImportError:
    from instagram_web_client import WebClient, WebStop
    from instagram_web_timeline import pages, TimelineError
    from instagram_web_reels import pages as reels_pages, ReelsError
    from instagram_archive_core.instagram_policy import RequestWindow


def install_web_graphql(rate_path=None):
    from gallery_dl.extractor.instagram import InstagramAPI, InstagramExtractor, QUERIES
    if getattr(InstagramAPI, '_vm_web_graphql', False):
        return
    if version('gallery-dl') != '1.32.17':
        raise RuntimeError('Review web adapter before changing gallery-dl version')
    if rate_path is None:
        data_dir = os.environ.get('ARCHIVE_DATA_DIR')
        if not data_dir:
            raise RuntimeError('Web adapter requires explicit state directory')
        rate_path = Path(data_dir)/'instaloader-rate.sqlite3'
    limiter = RequestWindow(rate_path, policy={'windowMinutes':1,'maxRequests':10,'minIntervalSeconds':6})
    original_init = InstagramExtractor._init
    original_finalize = InstagramExtractor.finalize

    def initialize(extractor):
        original_cookies = [copy.copy(c) for c in extractor.cookies]
        original_init(extractor)
        extractor.cookies.clear()
        for cookie in original_cookies:
            extractor.cookies.set_cookie(cookie)
        extractor.csrf_token = extractor.cookies.get_dict().get('csrftoken', '')

    def client(api):
        if not hasattr(api, '_vm_web_client'):
            cookie_values = api.extractor.cookies.get_dict()
            if not cookie_values.get('sessionid') or not cookie_values.get('csrftoken'):
                raise api.exc.AbortExtraction('AuthRequired: complete Cookie snapshot required')
            loader = instaloader.Instaloader(quiet=True, sleep=False, max_connection_attempts=1)
            loader.load_session('local-cookie-session', cookie_values)
            loader.context._session.cookies.clear()
            for cookie in api.extractor.cookies:
                loader.context._session.cookies.set_cookie(copy.copy(cookie))
            api._vm_web_loader = loader
            api._vm_web_client = WebClient(loader.context._session, limiter.reserve, limiter.block)
            api.extractor.log.info('自建 Web GraphQL 掃描；保留 gallery-dl 媒體下載與封存去重')
        return api._vm_web_client

    def call(api, doc_id, variables, web_api=True):
        try:
            return client(api).doc_id_graphql_query(doc_id, variables, web_api=web_api)
        except WebStop as error:
            raise api.exc.AbortExtraction(str(error)) from None

    def feed(api, handle):
        if handle.startswith('id:'):
            handle = api.user_by_id(handle[3:])['username']
        cursor = api.extractor._init_cursor()
        seen = {cursor} if cursor else set()
        try:
            while True:
                for page in pages(client(api), handle, max_pages=10, start_cursor=cursor):
                    if page['optional_location_errors']:
                        api.extractor.log.info('略過 %s 個地點頭像選填欄位錯誤', page['optional_location_errors'])
                    yield from page['nodes']
                    cursor = page['next_cursor']
                    if cursor is None:
                        return
                    if cursor in seen:
                        raise TimelineError('repeated_cursor')
                    seen.add(cursor)
                    api.extractor._cursor = cursor
        except (WebStop, TimelineError) as error:
            raise api.exc.AbortExtraction(str(error)) from None

    def reels(api, handle):
        user = api.user(handle)
        username = user.get('username')
        if not username and not handle.startswith('id:'):
            username = handle
        try:
            for page in reels_pages(client(api), user['id'], username,
                                    start_cursor=api.extractor._init_cursor()):
                yield from page['nodes']
                if page['next_cursor'] is not None:
                    api.extractor._update_cursor(page['next_cursor'])
        except (WebStop, ReelsError) as error:
            raise api.exc.AbortExtraction(str(error)) from None

    def by_web(api, username):
        # Only accept a user node whose username matches; collaboration owners
        # may differ from the account whose timeline was requested.
        try:
            first = next(pages(client(api), username, max_pages=1))
            owner = next((node.get('user') for node in first['nodes']
                if (node.get('user') or {}).get('username', '').lower() == username.lower()), None)
            if not owner or not str(owner.get('pk', '')).isdigit():
                raise TimelineError('target_identity_unresolved')
            return {**owner, 'id': str(owner['pk'])}
        except (WebStop, TimelineError) as error:
            raise api.exc.AbortExtraction(str(error)) from None

    def by_id(api, user_id):
        variables = {'id':str(user_id),'render_surface':'PROFILE','enable_integrity_filters':True,
            '__relay_internal__pv__PolarisCannesGuardianExperienceEnabledrelayprovider':True,
            '__relay_internal__pv__PolarisCASB976ProfileEnabledrelayprovider':False,
            '__relay_internal__pv__PolarisRepostsConsumptionEnabledrelayprovider':False,
            '__relay_internal__pv__PolarisWebSchoolsEnabledrelayprovider':False}
        payload = call(api, '27937681195819736', variables, web_api=False)
        user = (payload.get('data') or {}).get('user')
        if payload.get('errors') or not isinstance(user, dict) or str(user.get('pk') or user.get('id')) != str(user_id):
            raise api.exc.AbortExtraction('profile_identity_or_schema_error')
        return {**user, 'id': str(user_id)}

    def graphql(api, opname, variables, **kwargs):
        payload = call(api, QUERIES[opname][0], variables)
        if payload.get('errors') or not isinstance(payload.get('data'), dict):
            raise api.exc.AbortExtraction('graphql_schema_error')
        return payload['data']

    def paginate(api, opname, variables, fieldname, route=None):
        seen = set()
        variables = dict(variables)
        variables['after'] = api.extractor._init_cursor()
        while True:
            data = graphql(api, opname, variables).get(fieldname)
            if isinstance(data, dict) and 'edges' not in data:
                data = next((value for value in data.values() if isinstance(value, dict) and 'edges' in value), None)
            if not isinstance(data, dict) or not isinstance(data.get('edges'), list):
                raise api.exc.AbortExtraction('missing_scope_connection')
            for edge in data['edges']:
                yield edge['node']
            info = data.get('page_info') or {}
            if info.get('has_next_page') is False:
                return
            cursor = info.get('end_cursor')
            if info.get('has_next_page') is not True or not cursor or cursor in seen or not data['edges']:
                raise api.exc.AbortExtraction('invalid_scope_pagination')
            seen.add(cursor)
            variables['after'] = api.extractor._update_cursor(cursor)

    def media(api, shortcode):
        payload = call(api, '27128499623469141', {'shortcode':shortcode,
            '__relay_internal__pv__PolarisAIGMMediaWebLabelEnabledrelayprovider':False}, web_api=True)
        items = ((payload.get('data') or {}).get('xdt_api__v1__media__shortcode__web_info') or {}).get('items')
        if payload.get('errors') or not items:
            raise api.exc.AbortExtraction('missing_post_media')
        return iter(items)

    def finalize(extractor, status):
        try:
            return original_finalize(extractor, status)
        finally:
            loader = getattr(getattr(extractor, 'api', None), '_vm_web_loader', None)
            if loader:
                loader.close()

    InstagramExtractor._init = initialize
    InstagramExtractor.finalize = finalize
    InstagramAPI.user_feed = feed
    InstagramAPI.user_reels = reels
    InstagramAPI._user_by_web_impl = by_web
    InstagramAPI._user_by_id_impl = by_id
    InstagramAPI._call_graphql = graphql
    InstagramAPI._pagination_graphql = paginate
    InstagramAPI.media = media
    InstagramAPI._vm_web_graphql = True
