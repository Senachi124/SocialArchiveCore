"""Supplemental ALT metadata; original records, hashes and media stay immutable."""
import copy
from . import db


def state_key(row):
    return 'image_alt:' + str(row['post_id']) + ':' + row['version']


def load(rows, connection=None):
    keys = list({state_key(row) for row in rows})
    if not keys:
        return {}
    sql = 'SELECT key,value FROM xl_state WHERE key=ANY(%s)'
    records = connection.execute(sql, (keys,)).fetchall() if connection else db.execute(sql, (keys,))
    return {r['key']: r['value'] for r in records}


def merge(row, supplements):
    extra = supplements.get(state_key(row))
    owner = row['manifest']['post'].get('owner') or row['author']
    if (not extra or extra.get('post_id') != row['post_id'] or extra.get('version') != row['version']
            or extra.get('owner', '').lower() != owner.lower()):
        return row
    result = copy.deepcopy(row)
    by_name = {entry['name']: entry for entry in extra.get('media', [])}
    applied = False
    for media in result['manifest']['post'].get('media', []):
        saved = by_name.get(media['name'], {})
        alt = saved.get('alt_text')
        if (not media.get('alt_text') and isinstance(alt, str) and alt
                and saved.get('sha256') == media['sha256'] and media['mime'].startswith('image/')):
            media['alt_text'] = alt
            applied = True
    if applied:
        result['manifest']['post']['alt_enrichment'] = {
            'checked_at': extra['checked_at'], 'source': 'x_api',
            'original_record_unchanged': True}
    return result


def enrich(rows, connection=None):
    extras = load(rows, connection)
    return [merge(row, extras) for row in rows]
