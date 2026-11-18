"""Keep gallery-dl profile and Posts IDs paired with their versioned variables."""
from functools import wraps
from importlib.metadata import version as gallery_version

PROFILE_QUERY = 'PolarisProfilePageContentQuery'
PROFILE_QUERY_ID = '28649589871350527'
SUPPORTED_GALLERY_VERSION = '1.32.17'
POSTS_QUERY = 'PolarisProfilePostsTabContentQuery_connection'
POSTS_QUERY_ID = '39229011733413759'
COMPATIBLE_QUERIES = {PROFILE_QUERY: PROFILE_QUERY_ID, POSTS_QUERY: POSTS_QUERY_ID}


def profile_query_cache(original):
    @wraps(original)
    def cache(self, func, *args, **kwargs):
        if (getattr(func, '__module__', None) == 'gallery_dl.extractor.instagram'
                and getattr(func, '__name__', None) == '_extract_docid'
                and args and args[0] in COMPATIBLE_QUERIES):
            # The website's newer query requires variables this extractor does
            # not send. Bypass both discovery and stale persistent cache values.
            # Other queries and user metadata still use the original cache.
            return COMPATIBLE_QUERIES[args[0]]
        return original(self, func, *args, **kwargs)
    cache._archive_profile_query_compat = True
    return cache


def install_profile_query_compat():
    from gallery_dl.extractor.instagram import InstagramExtractor, QUERIES
    if (gallery_version('gallery-dl') != SUPPORTED_GALLERY_VERSION
            or any(QUERIES.get(name, (None,))[0] != docid for name, docid in COMPATIBLE_QUERIES.items())):
        raise RuntimeError('Review Instagram profile/Posts query compatibility before upgrading gallery-dl')
    if not getattr(InstagramExtractor.cache, '_archive_profile_query_compat', False):
        InstagramExtractor.cache = profile_query_cache(InstagramExtractor.cache)
