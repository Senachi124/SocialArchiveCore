import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch,Mock
from instagram_archive_core import core,catalog,storage,auto_recovery
from instagram_archive_core.redaction import redact


class CoreTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root=Path(self.tmp.name)
        self.env=patch.dict(os.environ,{'IG_CORE_MEDIA_ROOT':str(self.root),'IG_CORE_MOUNT_UUID':'synthetic-test-volume'})
        self.env.start();self.addCleanup(self.env.stop)
        self.guard=patch('instagram_archive_core.core.guard_disk',return_value=self.root)
        self.guard.start();self.addCleanup(self.guard.stop)
        self.catguard=patch('instagram_archive_core.catalog.guard_disk',return_value=self.root)
        self.catguard.start();self.addCleanup(self.catguard.stop)
        for module in ('core','catalog'):
            identity_guard=patch('instagram_archive_core.'+module+'.guard_identity',return_value=self.root)
            identity_guard.start();self.addCleanup(identity_guard.stop)
        core.JOB_QUEUE=core.AutomaticFirstQueue()
        core.configure();core.initialize()
        cookie=self.root/'synthetic-cookies.txt';cookie.write_text('# synthetic fixture only\n');cookie.chmod(0o600)
        self.account=core.register_account('fixture',cookie)

    def test_due_schedule_priority_and_duplicate_prevention(self):
        manual=core.fetch('manualfixture',self.account,['posts'])[0]
        core.set_target('scheduledfixture',self.account,['posts'],24)
        core.schedule_due_targets();core.schedule_due_targets()
        self.assertNotEqual(core.JOB_QUEUE.get_nowait(),manual)
        self.assertEqual(core.JOB_QUEUE.get_nowait(),manual)
        with core.connect() as db:
            self.assertEqual(db.execute('SELECT count(*) FROM jobs').fetchone()[0],2)

    def test_restart_only_worker_recovers_running_jobs(self):
        job=core.fetch('fixture',self.account,['posts'])[0]
        with core.connect() as db:db.execute("UPDATE jobs SET status='running' WHERE id=?",(job,))
        core.initialize()
        with core.connect() as db:self.assertEqual(db.execute('SELECT status FROM jobs').fetchone()[0],'running')
        core.initialize(recover=True)
        with core.connect() as db:self.assertEqual(db.execute('SELECT status FROM jobs').fetchone()[0],'queued')

    def test_cancel_preserves_metadata(self):
        job=core.fetch('fixture',self.account,['posts'])[0]
        core.cancel_job(job)
        with core.connect() as db:
            row=db.execute('SELECT * FROM jobs').fetchone()
            self.assertEqual(row['status'],'cancelled');self.assertEqual(row['username'],'fixture')

    def test_local_catalog_verified_and_modified_files_not_known(self):
        path=core.LIBRARY_DIR/'fixture.jpg';path.write_bytes(b'synthetic media fixture')
        data={'media_id':'123','post_id':'456','archive_target':'fixture','subcategory':'posts','_archive_post_ids':['123']}
        catalog.LocalArchive().store(path,data)
        self.assertTrue(catalog.known_media('123'))
        self.assertTrue(catalog.known_post_id('fixture','456'))
        path.write_bytes(b'changed synthetic bytes')
        self.assertFalse(catalog.known_media('123'))
        self.assertFalse(catalog.known_post_id('fixture','456'))

    def test_incomplete_files_never_get_markers(self):
        path=core.LIBRARY_DIR/'empty.jpg';path.touch()
        with self.assertRaises(ValueError):catalog.LocalArchive().store(path,{'media_id':'123'})
        self.assertFalse(catalog.known_media('123'))

    def test_cookie_file_is_referenced_no_copy(self):
        job=core.fetch('fixture',self.account,['posts'])[0]
        with core.connect() as db:
            config=core.build_config(db.execute('SELECT * FROM jobs WHERE id=?',(job,)).fetchone(),db.execute('SELECT * FROM accounts').fetchone())
        value=json.loads(config.read_text())
        self.assertEqual(value['extractor']['instagram']['cookies'],str(self.root/'synthetic-cookies.txt'))
        self.assertFalse(value['extractor']['instagram']['cookies-update'])
        self.assertFalse((self.root/'cookies').exists())

    def test_auto_cooldown_retry_retains_boundary_and_history(self):
        core.set_target('fixture',self.account,['posts'],24)
        core.schedule_due_targets()
        with core.connect() as db:
            row=db.execute('SELECT * FROM jobs').fetchone()
            row=auto_recovery.prepare(db,row,'2020-01-01T00:00:00+00:00')
            auto_recovery.defer(db,row,'2020-01-01T00:01:00+00:00')
            db.execute("UPDATE jobs SET status='failed' WHERE id=?",(row['id'],))
        core.schedule_due_targets()
        with core.connect() as db:
            rows=db.execute('SELECT * FROM jobs ORDER BY id').fetchall()
        self.assertEqual(len(rows),2)
        self.assertEqual(rows[0]['status'],'failed')
        self.assertEqual(auto_recovery.recovery(rows[1])['boundary_before'],'2020-01-01T00:00:00+00:00')

    def test_invalid_target_rejected(self):
        for value in ('../escape','https://example.com','..'):
            with self.assertRaises(ValueError):core.fetch(value,self.account,['posts'])

    def test_storage_rejects_wrong_uuid_and_low_space(self):
        with patch('instagram_archive_core.storage.subprocess.run',return_value=Mock(stdout=json.dumps({'filesystems':[{'uuid':'wrong','fsroot':'/'}]}))):
            with self.assertRaises(RuntimeError):storage.guard_disk()
        with patch('instagram_archive_core.storage.subprocess.run',return_value=Mock(stdout=json.dumps({'filesystems':[{'uuid':'synthetic-test-volume','fsroot':'/'}]}))),patch('instagram_archive_core.storage.shutil.disk_usage',return_value=Mock(free=1)):
            with self.assertRaises(RuntimeError):storage.guard_disk()

    def test_missing_mount_refuses_queue_open(self):
        with patch.object(core,'guard_identity',side_effect=RuntimeError('mount unavailable')),patch.object(core.sqlite3,'connect') as opened:
            with self.assertRaises(RuntimeError):
                with core.connect():pass
        opened.assert_not_called()

    def test_redaction(self):
        self.assertNotIn('secret',redact('[error] https://cdn.example/a?token=secret'))
        self.assertNotIn('secret',redact('Cookie: sessionid=secret'))

if __name__=='__main__':unittest.main()

class FailureSafetyTests(unittest.TestCase):
    def test_cli_sanitizes_exception(self):
        import io
        from contextlib import redirect_stderr
        from instagram_archive_core.cli import main
        output=io.StringIO()
        with patch('instagram_archive_core.cli._main',side_effect=RuntimeError('synthetic-secret')),redirect_stderr(output),self.assertRaises(SystemExit):
            main(['init'])
        self.assertNotIn('synthetic-secret',output.getvalue())

    def test_symlink_child_rejected_before_writes(self):
        with tempfile.TemporaryDirectory() as root,tempfile.TemporaryDirectory() as outside:
            Path(root,'tmp').symlink_to(outside,target_is_directory=True)
            with patch.dict(os.environ,{'IG_CORE_MEDIA_ROOT':root,'IG_CORE_MOUNT_UUID':'fixture'}),patch('instagram_archive_core.storage.subprocess.run',return_value=Mock(stdout=json.dumps({'filesystems':[{'uuid':'fixture','fsroot':'/'}]}))):
                with self.assertRaises(RuntimeError):storage.guard_disk()
            self.assertEqual(list(Path(outside).iterdir()),[])

class WorkerExecutionTests(CoreTests):
    # Inherit fixture only, suppress duplicated inherited tests via load_tests below.
    def test_worker_uses_module_and_records_success(self):
        import io
        job=core.fetch('fixture',self.account,['posts'])[0]
        process=Mock(stdout=io.StringIO('[info] done\n'))
        process.wait.return_value=0;process.poll.return_value=0
        with patch.object(core,'gallery_dl_command',return_value=(['python','-m','gallery_dl'],'1.32.17')),patch.object(core.subprocess,'Popen',return_value=process) as spawn,patch.object(core.job_control,'monitor',return_value=None):
            core.run_job(job)
        self.assertEqual(spawn.call_args.args[0][1:3],['-m','instagram_archive_core.gallery_worker'])
        with core.connect() as db:self.assertEqual(db.execute('SELECT status FROM jobs').fetchone()[0],'completed')

    def test_worker_cooldown_defers_auto_without_network(self):
        core.set_target('fixture',self.account,['posts'],24);core.schedule_due_targets()
        with core.connect() as db:
            job=db.execute('SELECT id FROM jobs').fetchone()[0]
            db.execute("INSERT INTO safety_state VALUES(1,'2999-01-01T00:00:00+00:00')")
        with patch.object(core.subprocess,'Popen') as spawn:core.run_job(job)
        spawn.assert_not_called()
        with core.connect() as db:
            row=db.execute('SELECT * FROM jobs').fetchone()
        self.assertEqual(row['status'],'failed');self.assertTrue(auto_recovery.recovery(row)['pending'])


def load_tests(loader,standard_tests,pattern):
    suite=unittest.TestSuite()
    suite.addTests(loader.loadTestsFromTestCase(CoreTests))
    suite.addTests(loader.loadTestsFromTestCase(FailureSafetyTests))
    suite.addTest(WorkerExecutionTests('test_worker_uses_module_and_records_success'))
    suite.addTest(WorkerExecutionTests('test_worker_cooldown_defers_auto_without_network'))
    return suite
