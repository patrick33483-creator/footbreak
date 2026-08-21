from __future__ import annotations

import json
import os
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch


SYSTEM = Path(__file__).resolve().parents[1]
ROOT = SYSTEM.parent
if str(SYSTEM) not in sys.path:
    sys.path.insert(0, str(SYSTEM))

import disk_guard


class DiskGuardTests(unittest.TestCase):
    def test_cleanup_removes_only_old_allowlisted_regular_temp_files(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            old = root / ".ledger.json.orphan"
            fresh = root / ".ledger.json.active"
            canonical = root / "ledger.json"
            unknown = root / ".unknown.large"
            target = root / "target"
            for path in (old, fresh, canonical, unknown, target):
                path.write_bytes(b"x" * 20)
            link = root / ".ledger.json.symlink"
            link.symlink_to(target)
            timestamp = time.time() - 7200
            os.utime(old, (timestamp, timestamp))
            os.utime(unknown, (timestamp, timestamp))
            count, reclaimed = disk_guard.cleanup_stale_atomic_temps(
                (root,), now=time.time(), stale_seconds=3600,
            )
            self.assertEqual((count, reclaimed), (1, 20))
            self.assertFalse(old.exists())
            self.assertTrue(fresh.exists())
            self.assertTrue(canonical.exists())
            self.assertTrue(unknown.exists())
            self.assertTrue(link.is_symlink())
            self.assertTrue(target.exists())

    def test_sidecar_pruning_preserves_current_and_newest_overlap(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            names = [f"history-{value:020x}.json" for value in range(8)]
            for index, name in enumerate(names):
                path = root / name
                path.write_bytes(b"x" * (index + 1))
                os.utime(path, (100 + index, 100 + index))
            current = names[1]
            (root / "data.json").write_text(json.dumps({
                "history_data_url": current,
            }), encoding="utf-8")
            removed, _bytes = disk_guard.prune_crown_history_sidecars(
                root, keep_generations=3,
            )
            remaining = {path.name for path in root.glob("history-*.json")}
            self.assertEqual(removed, 4)
            self.assertEqual(remaining, {current, names[5], names[6], names[7]})

    def test_sidecar_pruning_fails_closed_without_valid_public_pointer(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            sidecar = root / "history-00000000000000000001.json"
            sidecar.write_bytes(b"history")
            (root / "data.json").write_text("{bad json", encoding="utf-8")
            self.assertEqual(
                disk_guard.prune_crown_history_sidecars(root, keep_generations=1),
                (0, 0),
            )
            self.assertTrue(sidecar.exists())

    def test_reserve_is_released_at_critical_and_rebuilt_only_with_margin(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            reserve = Path(directory) / ".disk-reserve"
            reserve.write_bytes(b"x" * 16)
            low = disk_guard.DiskStatus(total=1000, used=950, free=50)
            high = disk_guard.DiskStatus(total=1000, used=100, free=900)
            with patch.object(disk_guard, "warning_free_bytes", return_value=500):
                self.assertTrue(disk_guard.release_reserve(reserve))
                self.assertFalse(disk_guard.ensure_reserve(low, reserve, 100))
                with patch.object(os, "posix_fallocate", side_effect=lambda fd, _off, size: os.ftruncate(fd, size)):
                    self.assertTrue(disk_guard.ensure_reserve(high, reserve, 100))
            self.assertEqual(reserve.stat().st_size, 100)

    def test_docker_cleanup_is_build_cache_only_and_age_bounded(self) -> None:
        with patch.object(disk_guard.shutil, "which", return_value="/usr/bin/docker"), \
             patch.object(disk_guard.subprocess, "run") as run:
            run.return_value.returncode = 0
            self.assertTrue(disk_guard.prune_old_docker_build_cache())
        command = run.call_args.args[0]
        self.assertEqual(command[:3], ["/usr/bin/docker", "builder", "prune"])
        self.assertIn("until=168h", command)
        self.assertNotIn("system", command)
        self.assertNotIn("image", command)
        self.assertNotIn("volume", command)

    def test_tick_services_run_disk_preflight_before_stage_preemption(self) -> None:
        for name, preempt in (
            ("footbreak-tick.service", "footbreak-tick-preempt.sh"),
            ("crown-tick.service", "crown-tick-preempt.sh"),
        ):
            unit = (ROOT / "deploy/systemd" / name).read_text(encoding="utf-8")
            self.assertLess(unit.index("disk_guard.py --preflight"), unit.index(preempt))

    def test_deploy_paths_release_reserve_and_prune_build_cache_without_runtime_prune(self) -> None:
        workflow = (ROOT / ".github/workflows/deploy.yml").read_text(encoding="utf-8")
        update = (ROOT / "deploy/update.sh").read_text(encoding="utf-8")
        setup = (ROOT / "deploy/setup.sh").read_text(encoding="utf-8")
        self.assertIn("rm -f /var/lib/footbreak/.disk-reserve", workflow)
        self.assertIn("docker builder prune -af --filter until=168h", workflow)
        self.assertNotIn("docker system prune", workflow)
        self.assertIn('system/disk_guard.py"', update)
        self.assertIn('system/disk_guard.py"', setup)


if __name__ == "__main__":
    unittest.main()
