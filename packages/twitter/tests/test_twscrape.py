"""Offline twscrape contracts and real HTTP client downloads against synthetic transports."""
import asyncio
import json
import tempfile
import os
import unittest
from contextlib import aclosing
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace as N
from unittest.mock import patch
import httpx
from xloader import capture
from xloader.xsource import Source, SessionPool, SourceFailure, tweet_record, cookie_dict
from xloader.cookies import parse_cookies
from xloader.search import normalize, plan
from xloader.common import HK


def tweet(post_id=123, **kw):
    return N(**({'id':post_id,'user':N(username='owner',displayname='Owner'),
                'date':datetime(2020,1,1,tzinfo=timezone.utc),'rawContent':'text',
                'retweetedTweet':None,'quotedTweet':None,'inReplyToTweetId':None,
                'media':N(photos=[],videos=[],animated=[])} | kw))


class SourceTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory(dir=os.environ['XLOADER_TEST_TMP']); self.addCleanup(self.temp.cleanup)
        self.root=Path(self.temp.name); self.cookie=self.root/'cookies.txt'
        self.cookie.write_text(parse_cookies('auth_token=synthetic; ct0=synthetic'))

    def test_model_media_and_relations(self):
        media=N(photos=[N(fullUrl='https://pbs.twimg.com/a?format=png&name=orig')],
                videos=[N(variants=[N(contentType='video/mp4',bitrate=10,url='https://video.twimg.com/low.mp4'),
                                    N(contentType='video/mp4',bitrate=20,url='https://video.twimg.com/high.mp4')])],animated=[])
        record,assets=tweet_record(tweet(media=media,inReplyToTweetId=100,quotedTweet=tweet(99)))
        self.assertEqual(record['reply_id'],100); self.assertEqual(record['quoted_id'],'99')
        self.assertEqual(record['author']['nick'],'Owner');self.assertEqual(record['count'],2)
        self.assertEqual(assets[0]['name'],'1.png');self.assertIn('high.mp4',assets[1]['url'])
        self.assertIsNone(tweet_record(tweet(retweetedTweet=tweet(1))))
        self.assertEqual(cookie_dict(self.cookie),{'auth_token':'synthetic','ct0':'synthetic'})

    def test_embedded_quotes_cannot_advance_search_max_id(self):
        from xloader.xsource import primary_tweets
        response=httpx.Response(200,json={'entries':[
            {'content':{'itemContent':{'itemType':'TimelineTweet','tweet_results':{'result':{'rest_id':'900'}}}}},
            {'content':{'itemContent':{'itemType':'TimelineTweet','tweet_results':{'result':{'rest_id':'899'}}}}}]})
        with patch('xloader.xsource.models.parse_tweets',return_value=[tweet(900,quotedTweet=tweet(10)),tweet(10),tweet(899)]):
            self.assertEqual([t.id for t in primary_tweets(response)],[900,899])

    async def test_parser_failure_is_safe_and_does_not_dump_payload(self):
        from xloader.xsource import models
        async with Source(self.root,self.cookie,lambda **kw:None) as source:
            models._write_dump('tweet',ValueError('NEVER_LOG'),{'token':'NEVER_LOG'},{})
            with self.assertRaisesRegex(SourceFailure,'extractor'):source.check()
            self.assertNotIn('NEVER_LOG',json.dumps(source.errors.detail))

    async def test_session_locks_survive_new_source_and_cookie_refresh(self):
        reset=int((datetime.now(timezone.utc)+timedelta(minutes=15)).timestamp())
        async with Source(self.root,self.cookie,lambda **kw:None) as source:
            await source.pool.lock_until('primary','SearchTimeline',reset)
        self.cookie.write_text(parse_cookies('auth_token=refreshed; ct0=refreshed'))
        async with Source(self.root,self.cookie,lambda **kw:None) as source:
            account=await source.pool.get('primary')
            self.assertEqual(int(account.locks['SearchTimeline'].timestamp()),reset)
            self.assertEqual(account.cookies['auth_token'],'refreshed')
            await source.pool.mark_inactive('primary','authentication required')
        # Same Cookie must not reactivate a rejected login on every date slice.
        async with Source(self.root,self.cookie,lambda **kw:None) as source:
            self.assertFalse((await source.pool.get('primary')).active)
            with self.assertRaisesRegex(SourceFailure,'authentication'):
                await source.pool.get_for_queue_or_wait('SearchTimeline')

    async def test_explicit_same_cookie_import_revalidates_without_unlocking(self):
        import os
        reset=int((datetime.now(timezone.utc)+timedelta(minutes=15)).timestamp())
        async with Source(self.root,self.cookie,lambda **kw:None) as source:
            await source.pool.lock_until('primary','SearchTimeline',reset)
            await source.pool.mark_inactive('primary','authentication required')
        stamp=self.cookie.stat().st_mtime_ns+1000000000
        os.utime(self.cookie,ns=(stamp,stamp))
        async with Source(self.root,self.cookie,lambda **kw:None) as source:
            account=await source.pool.get('primary')
            self.assertTrue(account.active)
            self.assertEqual(int(account.locks['SearchTimeline'].timestamp()),reset)

    async def test_endpoint_wait_is_cancellable_and_does_not_clear_lock(self):
        changes=[]
        async with Source(self.root,self.cookie,lambda **kw:changes.append(kw)) as source:
            reset=int((datetime.now(timezone.utc)+timedelta(minutes=15)).timestamp())
            await source.pool.lock_until('primary','SearchTimeline',reset)
            with self.assertRaises(asyncio.TimeoutError):
                await asyncio.wait_for(source.pool.get_for_queue_or_wait('SearchTimeline'),timeout=.15)
            self.assertIn('+08:00',next(v['wait_until'] for v in changes if v.get('wait_until')))
            self.assertEqual(int((await source.pool.get('primary')).locks['SearchTimeline'].timestamp()),reset)

    async def test_native_429_locks_endpoint_and_resumes_after_reset(self):
        from twscrape.queue_client import QueueClient, HandledError
        from twscrape.http import Response
        changes=[]
        async with Source(self.root,self.cookie,lambda **kw:changes.append(kw)) as source:
            reset=int((datetime.now(timezone.utc)+timedelta(minutes=15)).timestamp())
            queue=QueueClient(source.pool,'SearchTimeline')
            async def close(): pass
            queue.ctx=N(acc=await source.pool.get('primary'),req_count=1,aclose=close)
            response=Response(httpx.Response(429,json={'errors':[{'code':88,'message':'Rate limit exceeded'}]},
                              headers={'x-rate-limit-remaining':'0','x-rate-limit-reset':str(reset)},
                              request=httpx.Request('GET','https://x.com/synthetic')))
            with self.assertRaises(HandledError): await queue._check_rep(response)
            self.assertEqual(int((await source.pool.get('primary')).locks['SearchTimeline'].timestamp()),reset)
            await source.pool.lock_until('primary','SearchTimeline',1)
            account=await source.pool.get_for_queue_or_wait('SearchTimeline')
            self.assertEqual(account.username,'primary')
            await source.pool.unlock('primary','SearchTimeline')

    def row(self, **kw):
        spec=normalize({'value':'owner','since':'2020-01-01','until':'2020-01-02',**kw})
        start,end,url=plan(spec)[0]
        return {'spec':spec,'since_date':start,'until_date':end,'url':url}

    async def test_search_batches_use_latest_native_cursor_and_max_id(self):
        source=Source(self.root,self.cookie,lambda **kw:None);queries=[]
        async def search(query,limit):
            queries.append((query,limit))
            for i in (range(1000,800,-1) if len(queries)==1 else [800,799]): yield tweet(i)
        source.api=N(search=search)
        values=[v async for v in source.posts(self.row())]
        self.assertEqual(len(values),202);self.assertEqual(queries[0][1],200)
        self.assertIn('max_id:800',queries[1][0]);self.assertIn('since:2020-01-01',queries[0][0])

    async def test_latest_account_uses_both_feeds_then_search(self):
        source=Source(self.root,self.cookie,lambda **kw:None);calls=[]
        async def user(name):calls.append('user');return N(id=1)
        async def posts(*a,**kw):calls.append('posts');yield tweet(date=datetime.now(timezone.utc))
        async def replies(*a,**kw):calls.append('replies');yield tweet(124,date=datetime.now(timezone.utc),inReplyToTweetId=120)
        async def search(*a,**kw):calls.append('search');yield tweet(125,date=datetime.now(timezone.utc))
        source.api=N(user_by_login=user,user_tweets=posts,user_tweets_and_replies=replies,search=search)
        today=datetime.now(HK).date()
        row=self.row(since=(today-timedelta(days=2)).isoformat(),until=(today+timedelta(days=1)).isoformat())
        values=[v async for v in source.posts(row)]
        self.assertEqual(calls,['user','posts','replies','search']);self.assertEqual(len(values),3)

    async def test_dense_window_reports_cap_even_when_results_are_reposts(self):
        progress=[];source=Source(self.root,self.cookie,lambda **kw:progress.append(kw));page=0
        async def search(*a,**kw):
            nonlocal page
            page+=1
            for i in range(2000-page*200,1800-page*200,-1):yield tweet(i,retweetedTweet=tweet(1))
        source.api=N(search=search)
        self.assertEqual([v async for v in source.posts(self.row())],[])
        self.assertTrue(any(p.get('capped') for p in progress))

    async def test_single_post_and_silent_upstream_error(self):
        source=Source(self.root,self.cookie,lambda **kw:None)
        async def detail(post_id):return tweet(post_id)
        source.api=N(tweet_details=detail)
        values=[v async for v in source.posts({'spec':normalize({'value':'https://x.com/owner/status/456'})})]
        self.assertEqual(values[0][0]['tweet_id'],'456')
        source.observe(N(record={'message':'API unknown error: secret_token=NEVER_LOG','level':N(no=30)}))
        with self.assertRaises(SourceFailure) as error: source.check()
        self.assertNotIn('NEVER_LOG',str(error.exception))


class MediaTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory(dir=os.environ['XLOADER_TEST_TMP']);self.addCleanup(self.temp.cleanup)
        self.root=Path(self.temp.name);self.folder=self.root/'123';self.folder.mkdir()
        self.patch=patch.object(capture,'DATA',self.root);self.patch.start();self.addCleanup(self.patch.stop)
        self.asset={'url':'https://pbs.twimg.com/synthetic.jpg','name':'1.jpg'}
        self.destination=self.folder/'1.jpg';self.fetcher=capture.MediaFetcher(self.root,lambda **kw:None)
        self.client=httpx.AsyncClient
        guard=patch.object(capture,'check_storage');guard.start();self.addCleanup(guard.stop)

    def client_for(self,handler):
        return patch.object(capture.httpx,'AsyncClient',lambda **kw:self.client(**kw,transport=httpx.MockTransport(handler)))

    async def test_get_stream_receipt_dedupe_and_corrupt_repair(self):
        calls=[]
        def respond(request):calls.append(request.method);return httpx.Response(200,content=b'complete synthetic bytes')
        with self.client_for(respond):
            await self.fetcher.fetch(self.asset,self.destination)
            await self.fetcher.fetch(self.asset,self.destination)
            self.assertEqual(calls,['GET'])
            self.destination.write_bytes(b'corrupt')
            await self.fetcher.fetch(self.asset,self.destination)
        self.assertEqual(calls,['GET','GET']);self.assertTrue(list((self.root/'_parts').iterdir()))
        self.assertEqual(self.destination.read_bytes(),b'complete synthetic bytes')

    async def test_cdn_429_persists_retry_after_before_retry(self):
        calls=[]
        def respond(request):calls.append(1);return httpx.Response(429,headers={'Retry-After':'600'})
        async def interrupt(_):raise asyncio.CancelledError()
        with self.client_for(respond),patch.object(capture.asyncio,'sleep',interrupt):
            with self.assertRaises(asyncio.CancelledError):await self.fetcher.fetch(self.asset,self.destination)
        wait=json.loads(self.fetcher.wait_file.read_text())['until']
        self.assertGreater(wait,datetime.now(timezone.utc).timestamp()+590)
        with patch.object(capture.asyncio,'sleep',interrupt):
            with self.assertRaises(asyncio.CancelledError):await capture.MediaFetcher(self.root,lambda **kw:None).wait()
        self.assertEqual(len(calls),1);self.assertFalse(self.destination.exists())

    async def test_incomplete_media_never_published(self):
        async def no_sleep(_):pass
        with self.client_for(lambda req:httpx.Response(200,content=b'broken',headers={'Content-Length':'100'})),patch.object(capture.asyncio,'sleep',no_sleep):
            with self.assertRaises(OSError):await self.fetcher.fetch(self.asset,self.destination)
        self.assertFalse(self.destination.exists());self.assertEqual(len(list((self.root/'_parts').glob('*.part'))),3)


if __name__=='__main__':unittest.main()
