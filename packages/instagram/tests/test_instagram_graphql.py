import unittest
from types import SimpleNamespace
from unittest.mock import Mock, patch
from gallery_dl.extractor.instagram import InstagramAPI, InstagramExtractor
from instagram_archive_core.instagram_graphql import (PROFILE_QUERY, PROFILE_QUERY_ID,
    profile_query_cache, install_profile_query_compat, POSTS_QUERY, POSTS_QUERY_ID)


class InstagramGraphQLTests(unittest.TestCase):
    def test_posts_pagination_uses_fixed_id_and_preserves_cursor(self):
        import json
        stale=Mock(return_value='29104886249145907')
        extr=SimpleNamespace(root='https://www.instagram.com',_user=None,csrf_token='synthetic',
            log=Mock(),status=0,_init_cursor=lambda:None,_update_cursor=lambda value:value,
            config=lambda key,default=None:{'lsd':'synthetic','fb-dtsg':'synthetic'}.get(key,default))
        extr.cache=lambda *args,**kwargs:profile_query_cache(stale)(extr,*args,**kwargs)
        seen=[]
        field='xdt_api__v1__feed__user_timeline_graphql_connection'
        def request(url,**kwargs):
            seen.append((kwargs['data']['doc_id'],json.loads(kwargs['data']['variables'])))
            page=len(seen)
            return SimpleNamespace(text=json.dumps({'data':{field:{'edges':[{'node':{'id':str(page)}}],
                'page_info':{'has_next_page':page==1,'end_cursor':'next-page'}}}}))
        extr.request=request
        api=object.__new__(InstagramAPI);api.extractor=extr
        api.user=lambda handle:{'username':handle}
        self.assertEqual([{'id':'1'},{'id':'2'}],list(api.user_feed('example')))
        self.assertEqual([POSTS_QUERY_ID,POSTS_QUERY_ID],[entry[0] for entry in seen])
        self.assertIsNone(seen[0][1]['after']);self.assertEqual('next-page',seen[1][1]['after'])
        self.assertEqual('example',seen[0][1]['username']);stale.assert_not_called()

    def test_posts_upstream_fallback_drift_requires_review(self):
        from gallery_dl.extractor.instagram import QUERIES
        with patch.dict(QUERIES,{POSTS_QUERY:('unexpected','')}):
            with self.assertRaises(RuntimeError):install_profile_query_compat()

    def test_stale_profile_query_bypasses_cache_without_writing(self):
        original=Mock(return_value='28717443601247930')
        extractor=object()
        result=profile_query_cache(original)(extractor,InstagramAPI._extract_docid,PROFILE_QUERY,'/example',_exp=86400,_mem=False)
        self.assertEqual(PROFILE_QUERY_ID,result)
        original.assert_not_called()

    def test_other_queries_keep_cache_and_arguments(self):
        original=Mock(return_value='other-query')
        extractor=object()
        for query in ('PolarisProfileReelsTabContentQuery_connection',
                      'PolarisStoriesV3ReelPageGalleryQuery','PolarisProfileStoryHighlightsTrayContentQuery'):
            self.assertEqual('other-query',profile_query_cache(original)(extractor,InstagramAPI._extract_docid,query,'/example',_exp=86400,_mem=False))
            original.assert_called_with(extractor,InstagramAPI._extract_docid,query,'/example',_exp=86400,_mem=False)

    def test_profile_user_metadata_cache_is_preserved(self):
        original=Mock(return_value={'id':'123'})
        self.assertEqual({'id':'123'},profile_query_cache(original)(object(),InstagramAPI._user_by_id_impl,'123'))
        original.assert_called_once()

    def test_unrelated_same_named_function_is_not_intercepted(self):
        original=Mock(return_value='unchanged')
        def _extract_docid(*args):pass
        self.assertEqual('unchanged',profile_query_cache(original)(object(),_extract_docid,PROFILE_QUERY))
        original.assert_called_once()

    def test_real_graphql_body_uses_compatible_id_and_original_variables(self):
        original=Mock(return_value='28717443601247930')
        extr=SimpleNamespace(root='https://www.instagram.com',_user=None,csrf_token='synthetic-csrf',
            log=Mock(),config=lambda key,default=None:{'lsd':'synthetic-lsd','fb-dtsg':'synthetic-dtsg'}.get(key,default))
        extr.cache=lambda *args,**kwargs:profile_query_cache(original)(extr,*args,**kwargs)
        seen=[]
        def request(url,**kwargs):
            seen.append((url,kwargs))
            return SimpleNamespace(text='{"data":{"user":{"id":"123","username":"example"}}}')
        extr.request=request
        api=object.__new__(InstagramAPI);api.extractor=extr
        result=api._user_by_id_impl('123')
        self.assertEqual('123',result['id']);self.assertEqual(1,len(seen))
        url,request=seen[0]
        self.assertEqual('https://www.instagram.com/api/graphql',url)
        self.assertEqual(PROFILE_QUERY_ID,request['data']['doc_id'])
        import json
        self.assertEqual('123',json.loads(request['data']['variables'])['id'])
        original.assert_not_called()

    def test_failures_are_not_converted_to_success(self):
        original=Mock(side_effect=RuntimeError('upstream unavailable'))
        with self.assertRaises(RuntimeError):profile_query_cache(original)(object(),InstagramAPI._extract_docid,'other-query')

    def test_install_idempotent_and_upgrade_requires_review(self):
        original=InstagramExtractor.cache
        try:
            install_profile_query_compat();first=InstagramExtractor.cache
            install_profile_query_compat();self.assertIs(first,InstagramExtractor.cache)
            with patch('instagram_archive_core.instagram_graphql.gallery_version',return_value='99.0.0'):
                with self.assertRaisesRegex(RuntimeError,'Review Instagram'):install_profile_query_compat()
        finally:InstagramExtractor.cache=original

    def test_vm_worker_installs_compatibility_before_capture(self):
        import instagram_archive_core.gallery_worker as worker
        with patch.object(worker,'install_profile_query_compat',side_effect=RuntimeError('verified invocation')) as install:
            with self.assertRaisesRegex(RuntimeError,'verified invocation'):worker.install()
            install.assert_called_once_with()
