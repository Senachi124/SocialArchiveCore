"""Versioned browser Reels queries; transport owns authentication and pacing."""
import re


INITIAL_DOC_ID = '39368442709468666'
# Kept separate because Relay's initial and connection operations take different
# variable names. Never reuse the initial query to pretend pagination succeeded.
CONNECTION_DOC_ID = '28614481434846159'
PROVIDER = '__relay_internal__pv__PolarisShortDramaEnabledrelayprovider'


class ReelsError(ValueError):
    pass


def query(user_id, cursor=None):
    user_id = str(user_id)
    if not re.fullmatch(r'[0-9]{1,30}', user_id):
        raise ReelsError('invalid_reels_user_id')
    data = {'include_feed_video': True, 'page_size': 12,
            'target_user_id': user_id}
    if cursor is None:
        return INITIAL_DOC_ID, {'data': data, 'user_id': user_id, PROVIDER: True}
    if not isinstance(cursor, str) or not cursor:
        raise ReelsError('invalid_reels_cursor')
    return CONNECTION_DOC_ID, {'data': data, 'id': user_id, 'after': cursor,
                               'first': 3, PROVIDER: True}


def parse_page(payload):
    if not isinstance(payload, dict) or payload.get('errors'):
        raise ReelsError('reels_graphql_errors')
    data = payload.get('data')
    user = data.get('fetch__XDTUserDict') if isinstance(data, dict) else None
    connection = user.get('clips_connection') if isinstance(user, dict) else None
    if not isinstance(connection, dict):
        raise ReelsError('missing_reels_connection')
    edges, info = connection.get('edges'), connection.get('page_info')
    if (not isinstance(edges, list) or not isinstance(info, dict)
            or type(info.get('has_next_page')) is not bool):
        raise ReelsError('invalid_reels_page_schema')
    nodes, identities = [], set()
    for edge in edges:
        node = edge.get('node') if isinstance(edge, dict) else None
        media = node.get('media') if isinstance(node, dict) else None
        identity = str(media.get('pk') or media.get('id') or '') if isinstance(media, dict) else ''
        code = media.get('code') if isinstance(media, dict) else None
        if (not re.fullmatch(r'[0-9]{1,30}', identity)
                or not isinstance(code, str) or not re.fullmatch(r'[A-Za-z0-9_-]{1,32}', code)):
            raise ReelsError('invalid_reels_media_identity')
        if identity in identities:
            raise ReelsError('duplicate_reels_media_identity')
        identities.add(identity)
        nodes.append(node)
    cursor = info.get('end_cursor')
    if info['has_next_page'] and (not nodes or not isinstance(cursor, str) or not cursor):
        raise ReelsError('invalid_reels_next_page')
    return nodes, cursor if info['has_next_page'] else None


def pages(context, user_id, username, start_cursor=None, max_pages=120):
    if not isinstance(username, str) or not re.fullmatch(r'[A-Za-z0-9_.]{1,30}', username) or username in ('.', '..'):
        raise ReelsError('invalid_reels_username')
    if type(max_pages) is not int or not 1 <= max_pages <= 120:
        raise ReelsError('reels_page_budget')
    cursor = start_cursor
    seen_cursors = {cursor} if cursor else set()
    seen_media = set()
    for number in range(1, max_pages + 1):
        doc_id, variables = query(user_id, cursor)
        payload = context.doc_id_graphql_query(doc_id, variables,
            referer=f'https://www.instagram.com/{username}/reels/', web_api=True)
        nodes, next_cursor = parse_page(payload)
        if next_cursor and next_cursor in seen_cursors:
            raise ReelsError('repeated_reels_cursor')
        fresh = [node for node in nodes
                 if str(node['media'].get('pk') or node['media']['id']) not in seen_media]
        if nodes and not fresh:
            raise ReelsError('repeated_reels_page')
        seen_media.update(str(node['media'].get('pk') or node['media']['id']) for node in nodes)
        yield {'number': number, 'nodes': fresh, 'next_cursor': next_cursor,
               'complete': next_cursor is None}
        if next_cursor is None:
            return
        seen_cursors.add(next_cursor)
        cursor = next_cursor
    raise ReelsError('reels_page_budget_reached')
