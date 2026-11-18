"""One account path policy; display metadata and immutable bytes stay unchanged."""
import hashlib
import re


def account_path(username):
    if not isinstance(username, str) or not re.fullmatch(r'[A-Za-z0-9_]{1,15}', username):
        raise ValueError('Invalid archive account')
    return username.lower()


def canonical_relative(relative):
    parts = relative.split('/')
    if len(parts) < 4 or parts[0] != 'users' or parts[2] not in ('posts', 'references', 'profiles'):
        raise ValueError('Unknown account resource path')
    if any(not re.fullmatch(r'[A-Za-z0-9_.-]+', p) or p in ('.', '..') for p in parts):
        raise ValueError('Invalid resource path')
    parts[1] = account_path(parts[1])
    return '/'.join(parts)


def destination_key(kind, identity, version, relative):
    if kind not in ('post', 'profile') or canonical_relative(relative) != relative:
        raise ValueError('Noncanonical destination')
    fingerprint = hashlib.sha256(relative.encode('ascii')).hexdigest()
    return f'x:{kind}:{identity}:{version}:path-v1:{fingerprint}'
