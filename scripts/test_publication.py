"""Regression checks for the publication gate using synthetic Git repositories."""
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import unittest


class PublicationTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        (self.root / "scripts").mkdir()
        shutil.copyfile(Path(__file__).with_name("check_publication.py"),
                        self.root / "scripts/check_publication.py")
        self.git("init", "-q")

    def git(self, *args):
        return subprocess.run(["git", "-C", str(self.root), *args],
                              capture_output=True, check=True)

    def check(self):
        return subprocess.run([sys.executable, str(self.root / "scripts/check_publication.py")],
                              capture_output=True, text=True)

    def test_clean_source_passes(self):
        (self.root / "README.md").write_text("# Synthetic test repository\n")
        self.git("add", "README.md")
        self.assertEqual(self.check().returncode, 0)

    def test_staged_secret_not_hidden_by_clean_worktree(self):
        path = self.root / "README.md"
        path.write_text("-----BEGIN " + "PRIVATE KEY-----\nsynthetic marker only\n")
        self.git("add", "README.md")
        path.write_text("Clean working tree bytes must not hide indexed content.\n")
        result = self.check()
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("private key", result.stderr)
        self.assertNotIn("synthetic marker", result.stderr)

    def test_symlink_rejected_without_following_it(self):
        (self.root / "reference.md").symlink_to("/nonexistent/external-file")
        self.git("add", "reference.md")
        self.assertNotEqual(self.check().returncode, 0)

    def test_state_file_rejected(self):
        (self.root / "state").mkdir()
        (self.root / "state/queue.json").write_text("{}")
        self.git("add", "state/queue.json")
        self.assertNotEqual(self.check().returncode, 0)


if __name__ == "__main__":
    unittest.main()
