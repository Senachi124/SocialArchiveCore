import sqlite3
import unittest
from contextlib import closing
from instagram_archive_core.core import auto_success_times
from instagram_archive_core.public_time import public_times


class AutoSuccessTest(unittest.TestCase):
    def test_only_whole_successful_auto_batches_count(self):
        with closing(sqlite3.connect(':memory:')) as db:
            db.row_factory=sqlite3.Row
            db.execute('CREATE TABLE jobs(target_id INTEGER,batch_id TEXT,status TEXT,finished_at TEXT)')
            db.executemany('INSERT INTO jobs VALUES(?,?,?,?)',[
                (1,'auto-old','completed','2026-09-28T01:00:00+00:00'),
                (1,'auto-old','completed','2026-09-28T02:00:00+00:00'),
                (1,'auto-new','completed','2026-09-28T03:00:00+00:00'),
                (1,'auto-new','partial','2026-09-28T04:00:00+00:00'),
                (1,'batch-manual','completed','2026-09-28T05:00:00+00:00'),
                (2,'auto-active','completed','2026-09-28T05:00:00+00:00'),
                (2,'auto-active','running',None),
                (3,'auto-failed','failed','2026-09-28T06:00:00+00:00'),
            ])
            self.assertEqual(auto_success_times(db),{1:'2026-09-28T02:00:00+00:00'})
            db.execute("UPDATE jobs SET status='completed' WHERE batch_id='auto-new'")
            self.assertEqual(auto_success_times(db)[1],'2026-09-28T04:00:00+00:00')
            self.assertEqual(public_times({'auto_last_success_at':auto_success_times(db)[1]}),{'auto_last_success_at':'2026-09-28T12:00:00+08:00'})
