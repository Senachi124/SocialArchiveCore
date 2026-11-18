import contextlib
import io
import json
import os
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch
from xloader import storage, cli


class StorageTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(dir=os.environ['XLOADER_TEST_TMP'])
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        for name in ('media', 'media/tmp', 'control'):
            (self.root / name).mkdir()
        self.env = {'XLOADER_DATA': str(self.root / 'media'),
                    'XLOADER_CONTROL': str(self.root / 'control'),
                    'XLOADER_MEDIA_MOUNT': str(self.root),
                    'XLOADER_MEDIA_UUID': 'fixture-volume',
                    'TMPDIR': str(self.root / 'media/tmp')}
        self.row = {'target': str(self.root), 'uuid': 'fixture-volume', 'fsroot': '/', 'fstype': 'ext4'}

    def guard(self, row=None, free=6 * 1024**3):
        with patch.dict(os.environ, self.env, clear=True), \
             patch.object(storage.subprocess, 'run', return_value=SimpleNamespace(stdout=json.dumps({'filesystems':[row or self.row]}))), \
             patch.object(storage.shutil, 'disk_usage', return_value=SimpleNamespace(free=free)):
            storage.check_storage(force=True)

    def test_verified_mount_and_reserve(self):
        self.guard()

    def test_wrong_uuid_root_type_and_mount_rejected(self):
        for field in ('uuid', 'fsroot', 'fstype', 'target'):
            with self.subTest(field=field), self.assertRaises(OSError):
                self.guard({**self.row, field:'wrong'})

    def test_low_space_rejected(self):
        with self.assertRaises(OSError):
            self.guard(free=5*1024**3-1)

    def test_missing_configuration_never_creates_storage(self):
        with patch.dict(os.environ, {}, clear=True), self.assertRaises(ValueError):
            storage.check_storage(force=True)

    def test_temporary_directory_outside_media_rejected(self):
        self.env['TMPDIR'] = str(self.root / 'control')
        with self.assertRaises(ValueError):
            self.guard()

    def test_symlink_staging_rejected(self):
        (self.root / 'media/staging').symlink_to(self.root / 'control')
        with self.assertRaises(OSError):
            self.guard()


class CommandTests(unittest.TestCase):
    def test_help_requires_no_credentials(self):
        with patch.dict(os.environ, {}, clear=True), contextlib.redirect_stdout(io.StringIO()) as output:
            with self.assertRaises(SystemExit) as exit:
                cli.main(['--help'])
            self.assertEqual(exit.exception.code, 0)
            self.assertIn('worker', output.getvalue())

    def test_exported_entrypoint_suppresses_raw_errors(self):
        with patch.object(cli, 'run', side_effect=ValueError('synthetic-session-DO-NOT-LOG')):
            with self.assertRaises(SystemExit) as exit:
                cli.main(['jobs'])
        self.assertIn('ValueError', str(exit.exception))
        self.assertNotIn('DO-NOT-LOG', str(exit.exception))
