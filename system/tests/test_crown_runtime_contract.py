"""Static regression contract for Crown service timeout and lock ownership."""
from __future__ import annotations

import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]


class CrownRuntimeContractTests(unittest.TestCase):
    def test_settlement_timeout_kills_entire_control_group_with_bounded_grace(self) -> None:
        settle = (ROOT / "deploy/systemd/crown-settle.service").read_text(encoding="utf-8")
        self.assertIn("ExecStart=/opt/footbreak/deploy/crown-run.sh settle", settle)
        self.assertIn("TimeoutStartSec=900", settle)
        self.assertIn("Environment=CROWN_SETTLE_PASS_DEADLINE_SECONDS=120", settle)
        self.assertIn("Environment=CROWN_SETTLE_PROVIDER_PASS_DEADLINE_SECONDS=90", settle)
        self.assertIn("KillMode=control-group", settle)
        self.assertIn("TimeoutStopSec=30", settle)
        self.assertIn("SendSIGKILL=yes", settle)
        # Do not add any stale-lock or age-based displacement mechanism: a
        # running settlement remains protected until systemd actually stops it.
        self.assertNotIn("lock-age", settle.lower())
        self.assertNotIn("stale", settle.lower())

    def test_all_crown_runner_units_kill_descendants_and_t5_preemption_remains_safe(self) -> None:
        for name in (
            "crown-round-update.service",
            "crown-first-look-reconcile.service",
            "crown-settle.service",
            "crown-sweep.service",
            "crown-tick.service",
        ):
            unit = (ROOT / "deploy/systemd" / name).read_text(encoding="utf-8")
            with self.subTest(unit=name):
                self.assertIn("KillMode=control-group", unit)
                self.assertIn("SendSIGKILL=yes", unit)
        for name in (
            "crown-round-update.service",
            "crown-first-look-reconcile.service",
            "crown-settle.service",
            "crown-sweep.service",
        ):
            unit = (ROOT / "deploy/systemd" / name).read_text(encoding="utf-8")
            self.assertIn("ConditionPathExists=!/run/crown-t5-priority", unit)
        preempt = (ROOT / "deploy/crown-tick-preempt.sh").read_text(encoding="utf-8")
        self.assertIn(
            "systemctl stop --no-block crown-round-update.service crown-first-look-reconcile.service "
            "crown-sweep.service crown-settle.service",
            preempt,
        )

    def test_runner_holds_duplicate_lock_but_does_not_pass_fd_to_python(self) -> None:
        runner = (ROOT / "deploy/crown-run.sh").read_text(encoding="utf-8")
        self.assertIn('exec 9>"$CROWN_LOCK_DIR/footbreak-crown-${MODE}.lock"', runner)
        self.assertIn("flock -n 9", runner)
        self.assertIn("duplicate trigger rejected", runner)
        self.assertIn("exit 75", runner)
        self.assertIn('"$PYTHON" -m crown.run "$MODE" 9>&-', runner)
        self.assertNotIn('exec "$PYTHON" -m crown.run', runner)
        self.assertIn('timeout "${ALERT_TIMEOUT_SECONDS}s"', runner)
        self.assertIn("CROWN_RUNNER_ALERT_TIMEOUT_SECONDS", runner)


if __name__ == "__main__":
    unittest.main()
