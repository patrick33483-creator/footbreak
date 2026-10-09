import collections
import fcntl
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
import browser_tmp_cleanup as core
import browser_tmp_schedule as runner
from browser_tmp_schedule_ops import SERVICE, TIMER

Disk = collections.namedtuple("Disk", "total used free")


class ScheduleTests(unittest.TestCase):
    def test_capacity_above_and_equal_skip(self):
        for free in (runner.TARGET, runner.TARGET+1):
            with tempfile.TemporaryDirectory() as tmp, \
                 patch.object(runner, "STATE", Path(tmp)/"status.json"), \
                 patch.object(runner.shutil, "disk_usage", return_value=Disk(200, 1, free)), \
                 patch.object(runner, "cleanup") as cleanup:
                self.assertEqual(runner.run()["status"], "skipped_capacity_sufficient")
                cleanup.assert_not_called()
                self.assertEqual(json.loads((Path(tmp)/"status.json").read_text())["free_before"], free)

    def test_low_capacity_calls_only_existing_safe_cleaner(self):
        with tempfile.TemporaryDirectory() as tmp, \
             patch.object(runner, "STATE", Path(tmp)/"status.json"), \
             patch.object(runner.shutil, "disk_usage", return_value=Disk(200, 1, runner.TARGET-1)), \
             patch.object(runner, "cleanup", return_value={"summary":{
                 "stop_reason":"free_space_safety_target_reached",
                 "deleted_directories":2,"deleted_allocated_bytes":42}}) as cleanup:
            result = runner.run()
            self.assertEqual(result["deleted_directories"],2)
            cleanup.assert_called_once_with()

    def test_shared_lock_skips_instead_of_second_delete(self):
        with tempfile.TemporaryDirectory() as tmp, \
             patch.object(core, "LOCK", Path(tmp)/"lock"), \
             patch.object(core, "_cleanup_unlocked") as cleanup:
            with core.LOCK.open("a") as f:
                fcntl.flock(f,fcntl.LOCK_EX | fcntl.LOCK_NB)
                self.assertEqual(core.cleanup()["summary"]["stop_reason"],"another_cleanup_running")
                cleanup.assert_not_called()
            core.cleanup()
            cleanup.assert_called_once_with()

    def test_failure_not_reported_as_success(self):
        with tempfile.TemporaryDirectory() as tmp, \
             patch.object(runner, "STATE", Path(tmp)/"status.json"), \
             patch.object(runner.shutil, "disk_usage", return_value=Disk(200, 1, 0)), \
             patch.object(runner, "cleanup", side_effect=RuntimeError("inventory incomplete")):
            with self.assertRaises(RuntimeError):
                runner.run()
            self.assertFalse((Path(tmp)/"status.json").exists())

    def test_approved_schedule_and_host_temp(self):
        self.assertIn("00/2:00:00 Asia/Hong_Kong", TIMER)
        self.assertIn("Persistent=true", TIMER)
        self.assertIn("PrivateTmp=no", SERVICE)
        self.assertIn("TimeoutStartSec=8min", SERVICE)
        self.assertNotIn("Restart=always", SERVICE)
