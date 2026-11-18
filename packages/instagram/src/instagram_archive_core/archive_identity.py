"""Keep the profile being archived separate from the post's original author."""
import re


def username(value):
    if isinstance(value, str) and re.fullmatch(r'[A-Za-z0-9._]{1,30}', value) and value not in ('.', '..'):
        return value.lower()
    return ''


def bind_target(extractor, post):
    requested = extractor.config('archive-target')
    if not requested:
        return post
    profile = getattr(extractor, '_user', None) or {}
    target = username(profile.get('username')) or username(requested)
    if not target and str(requested).startswith('id:') and str(post.get('owner_id')) == requested[3:]:
        target = username(post.get('username'))
    if not target:
        raise ValueError('無法識別封存目標帳戶，未開始下載')
    post['archive_target'] = target
    post['archive_target_fullname'] = (profile.get('full_name') or '') if username(profile.get('username')) == target else ''
    post['owner_username'] = username(post.get('username'))
    post['owner_fullname'] = post.get('fullname') or ''
    return post
