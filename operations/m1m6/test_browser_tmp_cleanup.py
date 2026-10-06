import os
from pathlib import Path
import tempfile
import time
import unittest
from browser_tmp_cleanup import eligible, names_in


class SafetyTests(unittest.TestCase):
    def test_alias_reference(self):
        self.assertEqual(names_in("--user-data-dir=/tmp/playwright_chromiumdev_profile-AbC123/Default"),
                         {"playwright_chromiumdev_profile-AbC123"})

    def test_wrong_name(self):
        with tempfile.TemporaryDirectory() as root:
            self.assertEqual(eligible(Path(root), time.time()+1, set(), os.stat(root).st_dev)[1],
                             "unapproved_name")

    def test_active_recent_and_valid(self):
        with tempfile.TemporaryDirectory() as root:
            p = Path(root)/"playwright_chromiumdev_profile-AbC123"
            p.mkdir()
            (p/"test").write_text("test")
            dev = p.stat().st_dev
            self.assertEqual(eligible(p, time.time()+1, {p.name}, dev)[1], "active_process")
            self.assertEqual(eligible(p, time.time()-86400*7, set(), dev)[1], "recent_content")
            self.assertIsNone(eligible(p, time.time()+1, set(), dev)[1])

    def test_root_symlink_and_mount_guard(self):
        with tempfile.TemporaryDirectory() as root:
            p = Path(root)/"playwright_chromiumdev_profile-AbC123"
            p.symlink_to(root, target_is_directory=True)
            self.assertEqual(eligible(p, time.time()+1, set(), p.lstat().st_dev)[1],
                             "not_plain_same_device_directory")
            p.unlink()
            p.mkdir()
            self.assertEqual(eligible(p, time.time()+1, set(), -1)[1],
                             "not_plain_same_device_directory")

    def test_live_singleton(self):
        with tempfile.TemporaryDirectory() as root:
            p = Path(root)/"playwright_chromiumdev_profile-AbC123"
            p.mkdir()
            (p/"SingletonLock").symlink_to(f"host-{os.getpid()}")
            self.assertEqual(eligible(p, time.time()+1, set(), p.stat().st_dev)[1], "live_singleton_pid")
