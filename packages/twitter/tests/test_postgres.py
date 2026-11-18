"""Isolated PostgreSQL integration; no provider network calls or real accounts."""
import json
import os
import unittest
from pathlib import Path
from unittest.mock import patch
from psycopg.types.json import Jsonb


@unittest.skipUnless(os.environ.get('XLOADER_TEST_DATABASE') == 'isolated', 'Use tests/run_postgres.py')
class CaptureIntegration(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        if os.environ.get('XLOADER_SCHEMA') != 'xloader_test':
            raise RuntimeError('Refusing a non-test schema')
        from xloader import db, archive, capture, worker
        cls.db, cls.archive, cls.capture, cls.worker = db, archive, capture, worker
        db.migrate()
        cls.patches = [patch.object(module, 'check_storage') for module in (archive, capture, worker)]
        for item in cls.patches:
            item.start()

    @classmethod
    def tearDownClass(cls):
        for item in cls.patches:
            item.stop()

    def setUp(self):
        self.db.execute('TRUNCATE xl_events,xl_job_posts,xl_slices,xl_jobs,xl_posts,xl_profiles,xl_targets,xl_state RESTART IDENTITY CASCADE')

    def target(self):
        from xloader.search import normalize
        spec = normalize({'value':'fixture','since':'2020-01-01','until':'2020-01-02','rolling_end':False})
        return self.db.execute('INSERT INTO xl_targets(label,spec,enabled) VALUES(%s,%s,true) RETURNING *', ('fixture', Jsonb(spec)), one=True)

    def test_automatic_schedule_priority_and_same_target_deduplication(self):
        manual = self.db.create_job(self.target()['spec'])
        target = self.target()
        self.worker.schedule()
        self.assertEqual(self.worker.next_job()['trigger'], 'scheduled')
        first = self.db.execute('SELECT count(*) AS n FROM xl_jobs', one=True)['n']
        self.db.execute('UPDATE xl_targets SET next_run_at=NULL')
        self.worker.schedule()
        self.assertEqual(self.db.execute('SELECT count(*) AS n FROM xl_jobs', one=True)['n'], first)
        self.assertGreater(self.worker.next_job()['id'], manual['id'])

    def test_pause_resume_cancel_and_restart_preserve_slices(self):
        from xloader import jobs
        target = self.target()
        job = self.db.create_job(target['spec'], target['id'])
        self.assertTrue(jobs.claim(job['id']))
        self.db.execute("UPDATE xl_slices SET status='running' WHERE job_id=%s", (job['id'],))
        jobs.control(job['id'], 'pause')
        self.assertEqual(jobs.finish(job['id'], 'queued', 'restart'), 'paused')
        self.assertEqual(self.db.create_job(target['spec'], target['id'])['id'], job['id'])
        jobs.control(job['id'], 'resume')
        self.assertEqual(self.db.execute('SELECT status FROM xl_slices WHERE job_id=%s', (job['id'],), one=True)['status'], 'queued')
        jobs.control(job['id'], 'cancel')
        with self.assertRaises(ValueError):
            jobs.control(job['id'], 'retry')

    def test_mocked_provider_to_media_and_idempotent_local_archive(self):
        from PIL import Image
        import io
        import httpx
        from xloader.common import DATA
        target = self.target()
        job = self.db.create_job(target['spec'])
        row = self.db.execute('SELECT * FROM xl_slices WHERE job_id=%s', (job['id'],), one=True)
        original = io.BytesIO()
        Image.new('RGB', (800, 600), '#79aeb4').save(original, format='JPEG')
        media = original.getvalue()
        metadata = {'tweet_id':'123','author':{'name':'fixture'},'content':'Synthetic archive',
                    'date':'2020-01-01T12:00:00+00:00','count':1}
        class FixtureSource:
            def __init__(self, *args): pass
            async def __aenter__(self): return self
            async def __aexit__(self, *args): pass
            async def posts(self, row):
                yield dict(metadata), [{'url':'https://pbs.twimg.com/fixture.jpg', 'name':'1.jpg'}], 'search'
                yield {**metadata,'tweet_id':'124','count':0,'content':'text only'}, [], 'search'
            async def details(self, post_id): return None
        real_client = httpx.AsyncClient
        calls = []
        def response(request):
            calls.append(request.url.host)
            return httpx.Response(200,content=media,headers={'Content-Type':'image/jpeg'})
        def client(**kwargs):
            return real_client(**kwargs, transport=httpx.MockTransport(response))
        with patch.object(self.capture, 'Source', FixtureSource), patch.object(self.capture.httpx, 'AsyncClient', client):
            self.assertEqual(self.capture.main(row['id']), 0)
            self.assertEqual(self.capture.main(row['id']), 0)
        self.assertEqual(calls, ['pbs.twimg.com'])
        counts = self.db.execute('SELECT added_posts,added_files FROM xl_jobs WHERE id=%s', (job['id'],), one=True)
        self.assertEqual(counts, {'added_posts':2,'added_files':1})
        self.assertEqual(len(self.db.execute('SELECT * FROM xl_posts')), 2)
        post = self.db.execute("SELECT * FROM xl_posts WHERE post_id='123'", one=True)
        root = DATA / 'complete' / post['relative_dir']
        self.assertEqual((root/'1.jpg').read_bytes(), media)
        self.assertTrue((root/'1.jpg.preview.jpg').is_file())
        self.assertNotIn('pbs.twimg.com', json.dumps(post['manifest']))

    def test_failed_capture_can_retry_without_losing_progress(self):
        from xloader import jobs
        job = self.db.create_job(self.target()['spec'])
        self.assertTrue(jobs.claim(job['id']))
        self.db.execute("UPDATE xl_slices SET status='running',seen=9 WHERE job_id=%s", (job['id'],))
        jobs.finish(job['id'],'failed','synthetic failure')
        jobs.control(job['id'],'retry')
        row = self.db.execute('SELECT * FROM xl_slices WHERE job_id=%s', (job['id'],), one=True)
        self.assertEqual((row['seen'],row['status']), (9,'queued'))
