"""Liveness contract for the enabled reverse T-5 worker."""
from __future__ import annotations

import tempfile
import unittest
from datetime import datetime, timedelta
from pathlib import Path

from crown.common import HKT, write_json_atomic
from crown import reverse_t5_bridge_health as health


class ReverseT5BridgeHealthTests(unittest.TestCase):
    def setUp(self) -> None:
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.state_dir = Path(self.directory.name)
        self.now = datetime.now(HKT)

    def _enable(self, *, at: datetime | None = None) -> None:
        self.assertTrue(health.mark_enabled(self.state_dir, now=at or self.now))

    def _telemetry(
        self, *, completed_at: datetime | None, timeouts: int = 0,
    ) -> None:
        payload: dict[str, object] = {
            "last_status": "complete" if timeouts == 0 else "timeout",
            "consecutive_timeouts": timeouts,
        }
        if completed_at is not None:
            payload["last_completed"] = completed_at.isoformat()
        write_json_atomic(health.telemetry_path(self.state_dir), payload)

    def test_enabled_missing_telemetry_is_healthy_only_within_bounded_grace(self) -> None:
        self._enable()
        self.assertTrue(health.liveness_status(self.state_dir, now=self.now)[0])
        after_grace = self.now + timedelta(
            seconds=health.ENABLEMENT_GRACE_SECONDS + 1,
        )
        ok, message = health.liveness_status(self.state_dir, now=after_grace)
        self.assertFalse(ok)
        self.assertIn("completion telemetry missing", message)

    def test_condition_skipped_timer_does_not_refresh_enablement_grace(self) -> None:
        self._enable()
        marker = health.enablement_marker_path(self.state_dir).read_text(
            encoding="utf-8",
        )
        # A priority-marker Condition skip does not invoke Python or rewrite
        # this durable marker, so expiry is based on the original enablement.
        later = self.now + timedelta(seconds=health.ENABLEMENT_GRACE_SECONDS + 1)
        ok, _message = health.liveness_status(self.state_dir, now=later)
        self.assertFalse(ok)
        self.assertEqual(
            health.enablement_marker_path(self.state_dir).read_text(encoding="utf-8"),
            marker,
        )

    def test_stale_completion_fails_after_timer_service_window(self) -> None:
        self._enable(at=self.now - timedelta(minutes=10))
        self._telemetry(
            completed_at=self.now - timedelta(
                seconds=health.WORKER_COMPLETION_MAX_AGE_SECONDS + 1,
            ),
        )
        ok, message = health.liveness_status(self.state_dir, now=self.now)
        self.assertFalse(ok)
        self.assertIn("completion telemetry is stale", message)

    def test_one_timeout_remains_healthy_after_a_recent_completion(self) -> None:
        self._enable(at=self.now - timedelta(seconds=30))
        self._telemetry(completed_at=self.now - timedelta(seconds=5), timeouts=1)
        self.assertTrue(health.liveness_status(self.state_dir, now=self.now)[0])

    def test_two_consecutive_timeouts_fail(self) -> None:
        self._enable(at=self.now - timedelta(seconds=30))
        self._telemetry(completed_at=self.now - timedelta(seconds=5), timeouts=2)
        ok, message = health.liveness_status(self.state_dir, now=self.now)
        self.assertFalse(ok)
        self.assertIn("consecutive worker timeouts", message)

    def test_successful_no_job_completion_is_lively(self) -> None:
        self._enable(at=self.now - timedelta(seconds=30))
        self._telemetry(completed_at=self.now, timeouts=0)
        self.assertTrue(health.liveness_status(self.state_dir, now=self.now)[0])

    def test_aged_retryable_job_fails_even_with_recent_completion(self) -> None:
        self._enable(at=self.now - timedelta(seconds=30))
        self._telemetry(completed_at=self.now)
        write_json_atomic(self.state_dir / "ledger.json", {
            "crown_reverse_t5_bridge": {"jobs": [{
                "state": "RUNNING",
                "stage_at": (
                    self.now - timedelta(
                        seconds=health.MAX_RETRYABLE_STAGE_AGE_SECONDS + 1,
                    )
                ).isoformat(),
            }]},
        })
        ok, message = health.liveness_status(self.state_dir, now=self.now)
        self.assertFalse(ok)
        self.assertIn("aged retryable work", message)

    def test_disable_clears_marker_and_old_telemetry(self) -> None:
        self._enable()
        self._telemetry(completed_at=self.now)
        health.mark_disabled(self.state_dir)
        self.assertFalse(health.enablement_marker_path(self.state_dir).exists())
        self.assertFalse(health.telemetry_path(self.state_dir).exists())


if __name__ == "__main__":
    unittest.main()
