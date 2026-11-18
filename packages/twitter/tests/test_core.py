import json


import os


import tempfile


import unittest


from datetime import datetime, date


from pathlib import Path


from xloader.common import HK, WindowClosed, next_window, safe_path, window_deadline


from xloader.search import normalize, plan, split_slice


from xloader.cookies import parse_cookies


from unittest.mock import patch


class SearchTests(unittest.TestCase):
    def test_user_first_incremental_ignores_dates(self):
        from datetime import timedelta
        from xloader.search import workflow_spec
        target={'spec':normalize({'value':'example'}),'last_success_at':None}
        spec=workflow_spec(target,{'mode':'incremental','since':'2007-01-01','end_date':'2008-01-01'})
        today=datetime.now(HK).date()
        self.assertEqual(spec['since'],(today-timedelta(days=6)).isoformat())
        self.assertEqual(spec['end_date'],today.isoformat())
        self.assertTrue(spec['stop_on_known'])
        target['last_success_at']=datetime.now(HK)-timedelta(days=1)
        self.assertEqual(workflow_spec(target,{'mode':'incremental'})['since'],(today-timedelta(days=3)).isoformat())

    def test_user_first_full_search_and_post_modes(self):
        from xloader.search import workflow_spec
        target={'spec':normalize({'value':'example'}),'last_success_at':None}
        body={'mode':'full','since':'2020-01-01','end_date':'2020-01-02','stop_on_known':True}
        full=workflow_spec(target,body)
        self.assertEqual(full['until'],'2020-01-03')
        self.assertFalse(full['stop_on_known'])
        search=workflow_spec(target,{**body,'mode':'search','query':'filter:media (live OR music)'})
        self.assertIn('from:example',search['query'])
        self.assertIn('filter:media',search['query'])
        for invalid in ({'mode':''},{'mode':'search','query':''},{'mode':'search','query':'until:2020-01-01'},{'mode':'post','post_url':'example'}):
            with self.subTest(invalid=invalid),self.assertRaises(ValueError):workflow_spec(target,invalid)
        post={'mode':'post','post_url':'https://x.com/example/status/123'}
        self.assertEqual(workflow_spec(target,post)['kind'],'post')
        with self.assertRaises(ValueError):workflow_spec(target,post,scheduled=True)

    def test_inclusive_end_preserves_legacy_until(self):
        spec=normalize({'value':'example','since':'2026-10-02','end_date':'2026-10-02'})
        self.assertEqual(spec['until'],'2026-10-03')
        self.assertEqual(sum((b-a).days for a,b,_ in plan(spec)),1)
        legacy=normalize({'value':'example','since':'2026-10-01','until':'2026-10-02'})
        self.assertEqual(legacy['end_date'],'2026-10-01')
        with self.assertRaises(ValueError):normalize({'kind':'search','value':'from:a until:2026-10-02','end_date':'2026-10-02'})

    def test_full_and_incremental_policies(self):
        from xloader.search import target_spec
        spec=normalize({'value':'example','stop_on_known':True,'media_only':True,'replies':False})
        target={'spec':spec,'last_success_at':datetime.fromisoformat('2026-10-01T12:00:00+08:00')}
        self.assertFalse(target_spec(target,'full')['stop_on_known'])
        incremental=target_spec(target,'incremental')
        self.assertEqual(incremental['since'],'2026-09-29')
        self.assertTrue(incremental['stop_on_known'])
        self.assertIn('-filter:replies',incremental['query'])
        self.assertFalse(target_spec({**target,'last_success_at':None},'incremental')['stop_on_known'])

    def test_historical_coverage_no_gaps(self):
        s = normalize({'value':'@example','since':'2020-01-01','until':'2020-04-03','slice_days':30})
        windows = plan(s)
        self.assertEqual(windows[0][1], date(2020,4,3))
        self.assertEqual(windows[-1][0], date(2020,1,1))
        self.assertEqual(sum((b-a).days for a,b,_ in windows), 93)
        for newer,older in zip(windows,windows[1:]): self.assertEqual(newer[0],older[1])
        self.assertIn('f=live', windows[0][2])

    def test_search_url_and_embedded_dates(self):
        s = normalize({'value':'https://x.com/search?q=from%3Aexample+since%3A2020-01-01+until%3A2020-02-01'})
        self.assertEqual(s['query'],'from:example')
        self.assertEqual(s['since'],'2020-01-01')

    def test_arbitrary_advanced_operators(self):
        s=normalize({'kind':'search','value':'("香港" OR "Japan") from:example -filter:replies filter:media'})
        self.assertIn('OR',s['query'])
        self.assertIn('filter:media',s['query'])

    def test_post_is_exact_url(self):
        s=normalize({'kind':'post','value':'https://twitter.com/example/status/123'})
        self.assertEqual(plan(s),[(None,None,'https://x.com/example/status/123')])

    def test_unsafe_inputs(self):
        for value in ('https://evil.example/search?q=hello','https://x.com@evil.example/search?q=hello','https://x.com:443/example','bad/name'):
            with self.subTest(value=value),self.assertRaises(ValueError): normalize({'value':value})
        with self.assertRaises(ValueError): normalize({'value':'example','since':'2020-02-02','until':'2020-01-01'})
        with self.assertRaises(ValueError): normalize({'kind':'search','value':'from:a max_id:123'})

    def test_dense_split_preserves_both_edges(self):
        s=split_slice('from:a','2020-01-01','2020-02-01')
        self.assertEqual(s[0][1],date(2020,2,1))
        self.assertEqual(s[-1][0],date(2020,1,1))
        self.assertEqual(s[0][0],s[1][1])
        self.assertEqual(split_slice('from:a','2020-01-01','2020-01-02'),[])


class CookieTests(unittest.TestCase):
    def test_pasted_header(self):
        result=parse_cookies('Cookie: auth_token=synthetic-auth; ignored=secret; ct0=synthetic-csrf')
        self.assertIn('synthetic-auth',result)
        self.assertNotIn('secret',result)
        with self.assertRaises(ValueError):parse_cookies('auth_token=a;\nct0=b')
    def test_json_filters_other_credentials(self):
        rows=[{'domain':'.x.com','name':k,'value':'synthetic'} for k in ('auth_token','ct0')]+[{'domain':'other.com','name':'password','value':'exclude-me'}]
        result=parse_cookies(json.dumps(rows))
        self.assertNotIn('exclude-me',result)
        self.assertIn('auth_token',result)
    def test_netscape(self):
        value='# Netscape HTTP Cookie File\n'+''.join(f'#HttpOnly_.x.com\tTRUE\t/\tTRUE\t0\t{k}\tsynthetic\n' for k in ('auth_token','ct0'))
        self.assertIn('ct0',parse_cookies(value))
    def test_missing_or_expired(self):
        with self.assertRaises(ValueError):parse_cookies('not a cookie')
        with self.assertRaises(ValueError):parse_cookies(json.dumps([{'domain':'x.com','name':'auth_token','value':'test','expires':1}]))


class PathsTests(unittest.TestCase):
    def test_paths(self):
        with tempfile.TemporaryDirectory() as temp:
            root=Path(temp)
            for relative in ('../secret','/etc/passwd','a//b','a/./b','a\\b'):
                with self.assertRaises(ValueError):safe_path(root,relative)
            self.assertEqual(safe_path(root,'users/example/123'),root/'users/example/123')
    @unittest.skipIf(os.name=='nt','Windows symlink privileges vary')
    def test_symlink_escape(self):
        with tempfile.TemporaryDirectory() as temp:
            root=Path(temp);(root/'escape').symlink_to('/etc',target_is_directory=True)
            with self.assertRaises(ValueError):safe_path(root,'escape/passwd')


class MetadataTests(unittest.TestCase):
    def test_repost_actor_and_original_ids(self):
        from xloader.archive import canonical_post
        post=canonical_post({'tweet_id':100,'retweet_id':99,'content':'RT @original: 保留原文','author':{'name':'original'},'xloader_actor':{'name':'actor'}})
        self.assertEqual(post['post_type'],'repost')
        self.assertEqual(post['actor']['name'],'actor')
        self.assertEqual(post['references'][0]['post_id'],'99')
        self.assertIn('/actor/status/100',post['source_url'])
        reply=canonical_post({'tweet_id':102,'reply_id':99,'quoted_id':98,'quote_id':777,'author':{'name':'actor'}})
        self.assertEqual(reply['post_type'],'replied')
        self.assertEqual([r['post_id'] for r in reply['references']],['99','98'])
        self.assertEqual(reply['quote_id'],98)

    def test_quote_belongs_to_capturing_user(self):
        from xloader.archive import canonical_post, post_context
        quote=canonical_post({'tweet_id':103,'quoted_id':98,'author':{'name':'actor'},'content':'自己的引用文字','xloader_owner':'actor'})
        self.assertEqual(quote['post_type'],'quote')
        self.assertEqual(quote['owner'],'actor')
        self.assertFalse(quote['is_reference'])
        original=canonical_post({'tweet_id':98,'author':{'name':'original'},'xloader_owner':'actor','xloader_reference':True})
        self.assertEqual(original['owner'],'actor')
        self.assertTrue(post_context(original)['is_reference'])


class DiagnosticTests(unittest.TestCase):
    def test_safe_error_categories_never_keep_secrets(self):
        from xloader.diagnostics import SafeErrors, failure_message
        cases=[('RateLimitError auth_token=secret',429,'rate_limit'),('Could not authenticate you ct0=secret',401,'authentication'),('User not found https://example/?secret',404,'not_found'),('KeyError signed_url=secret',None,'extractor'),('XClIdParseError: X web scripts not found',None,'extractor')]
        for message,status,category in cases:
            handler=SafeErrors();handler.observe(message,status)
            self.assertEqual(handler.detail['category'],category)
            self.assertNotIn('secret',json.dumps(handler.detail)+failure_message(handler.detail,1))

    def test_capture_exception_records_type_and_where_without_secrets(self):
        from xloader.diagnostics import SafeErrors, failure_message
        def boom():
            raise KeyError('auth_token=secret&ct0=secret')
        try:
            boom()
        except KeyError as error:
            detail = SafeErrors().capture_exception(error)
        self.assertEqual(detail['category'], 'extractor')
        self.assertEqual(detail['exception_type'], 'KeyError')
        self.assertIn('test_core.py:', detail['where'])
        self.assertIn(':boom', detail['where'])
        blob = json.dumps(detail) + failure_message(detail, 70)
        self.assertNotIn('secret', blob)
        self.assertNotIn('auth_token', blob)
        self.assertIn('KeyError', failure_message(detail, 70))
        self.assertIn('@', failure_message(detail, 70))


    def test_known_category_token_survives_capture_exception(self):
        from xloader.diagnostics import SafeErrors
        class SourceFailure(Exception):
            pass
        detail=SafeErrors().capture_exception(SourceFailure('not_found'))
        self.assertEqual(detail['category'],'not_found')
        self.assertEqual(detail['exception_type'],'SourceFailure')
