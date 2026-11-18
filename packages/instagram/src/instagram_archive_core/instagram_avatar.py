"""Resolve avatars through gallery-dl's profile-page and GraphQL lookup."""
import hashlib
from urllib.parse import urlsplit
from gallery_dl.extractor.instagram import shortcode_from_id


def avatar_posts(self):
    username = self.item
    expected_id = None
    if username.startswith('id:'):
        raise ValueError('Use a username for avatar capture')
    # 1.32.17 resolves the profile ID from the webpage, then obtains full user
    # metadata through GraphQL. Never call the retired topsearch endpoint.
    user = dict(self.api.user(username, check_private=False))
    if user.get('username', '').casefold() != username.casefold():
        raise self.exc.NotFoundError('user')
    uid = str(user.get('pk') or user.get('id') or '')
    if not uid.isdigit():
        raise ValueError('Avatar profile has no valid user identity')
    if expected_id and uid != expected_id:
        raise ValueError('Avatar profile identity no longer matches the saved user ID')
    user['pk'] = uid
    candidates = [user.get('hd_profile_pic_url_info') or {}]
    candidates += user.get('hd_profile_pic_versions') or []
    candidates += [{'url': user.get('profile_pic_url_hd'), 'width': 0, 'height': 0},
                   {'url': user.get('profile_pic_url'), 'width': 0, 'height': 0}]
    avatar = next((dict(item) for item in candidates if item.get('url')), None)
    if avatar is None:
        raise ValueError('Instagram returned no avatar URL for this user')
    parsed = urlsplit(avatar['url'])
    host = parsed.hostname or ''
    if parsed.scheme != 'https' or parsed.username or parsed.password or not (
            host.endswith('.cdninstagram.com') or host.endswith('.fbcdn.net')):
        raise ValueError('Instagram returned an invalid avatar media URL')
    avatar.setdefault('width', 0)
    avatar.setdefault('height', 0)
    pk = str(user.get('profile_pic_id') or '').partition('_')[0]
    if not pk.isdigit():
        # CDN query signatures rotate. The immutable image path identifies the
        # version when Instagram omits profile_pic_id; never key by username alone.
        pk = str(int.from_bytes(hashlib.sha256(('avatar:'+uid+':'+parsed.path).encode()).digest()[:16], 'big'))
    self.log.info('頭像來源：個人頁及 GraphQL 資料；採用來源提供的圖片尺寸，保留版本去重')
    return ({'pk': pk, 'code': shortcode_from_id(pk), 'user': user,
             'caption': None, 'like_count': 0,
             'image_versions2': {'candidates': (avatar,)}},)
