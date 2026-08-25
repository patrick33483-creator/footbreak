"""Static regression contract for Crown service timeout and lock ownership."""
from __future__ import annotations

import unittest
import subprocess
import tempfile
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
            "crown-reverse-t5-drain.service",
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
            "crown-reverse-t5-drain.service",
            "crown-settle.service",
            "crown-sweep.service",
        ):
            unit = (ROOT / "deploy/systemd" / name).read_text(encoding="utf-8")
            self.assertIn("ConditionPathExists=!/run/crown-t5-priority", unit)
        preempt = (ROOT / "deploy/crown-tick-preempt.sh").read_text(encoding="utf-8")
        self.assertIn(
            "systemctl stop --no-block crown-round-update.service crown-sweep.service "
            "crown-settle.service",
            preempt,
        )
        # The hourly native-only repair has an independent short pass budget.
        # Do not kill an already-started run at the same minute as a deadline
        # tick: it must record a terminal result while direct-ID stages run.
        self.assertNotIn(
            "systemctl stop --no-block crown-round-update.service crown-first-look-reconcile.service",
            preempt,
        )
        # The optional bridge shares the state lock for short claim/merge I/O,
        # so urgent native T-5 explicitly stops it and its durable job retries.
        self.assertIn("crown-reverse-t5-drain.service", preempt)

    def test_reverse_t5_worker_timer_is_server_owned_low_priority_and_bounded(self) -> None:
        service = (
            ROOT / "deploy/systemd/crown-reverse-t5-drain.service"
        ).read_text(encoding="utf-8")
        timer = (
            ROOT / "deploy/systemd/crown-reverse-t5-drain.timer"
        ).read_text(encoding="utf-8")
        self.assertIn(
            "ExecStart=/opt/footbreak/deploy/crown-run.sh reverse-t5-drain",
            service,
        )
        self.assertIn("TimeoutStartSec=20", service)
        self.assertIn("TimeoutStopSec=2", service)
        self.assertIn("KillMode=control-group", service)
        self.assertIn("SendSIGKILL=yes", service)
        self.assertIn("Nice=19", service)
        self.assertIn("NoNewPrivileges=true", service)
        self.assertIn("PrivateTmp=true", service)
        self.assertIn("ConditionPathExists=!/run/crown-t5-priority", service)
        self.assertIn(
            "ConditionPathExists=!/run/crown-t5-priority",
            service.split("[Service]", 1)[0],
        )
        self.assertNotIn(
            "ExecStartPre=/opt/footbreak/deploy/crown-tick-preempt.sh", service,
        )
        self.assertIn(
            "ExecCondition=/bin/grep -Fq '\"reverse-t5-drain\"' /opt/footbreak/crown/run.py",
            service,
        )
        self.assertIn("OnUnitInactiveSec=30s", timer)
        self.assertIn("AccuracySec=1s", timer)
        self.assertIn("Persistent=true", timer)
        self.assertIn("Unit=crown-reverse-t5-drain.service", timer)
        self.assertNotIn("perplexity", (service + timer).lower())

    def test_residual_worker_unit_skips_cleanly_on_baseline_parser(self) -> None:
        """A rollback leaves a copied unit inert rather than firing bad argparse."""
        service = (
            ROOT / "deploy/systemd/crown-reverse-t5-drain.service"
        ).read_text(encoding="utf-8")
        # Keep this rollback fixture self-contained. GitHub Actions checks out
        # only the current commit, so an older production commit is not
        # guaranteed to exist in the local object database.
        baseline = """
        parser = argparse.ArgumentParser()
        parser.add_argument(
            "mode",
            choices=("tick", "sweep", "settle", "round-update"),
        )
        """
        with tempfile.TemporaryDirectory() as directory:
            legacy_run = Path(directory) / "run.py"
            legacy_run.write_text(baseline, encoding="utf-8")
            condition = subprocess.run(
                ["/bin/grep", "-Fq", '"reverse-t5-drain"', str(legacy_run)],
                check=False,
            )
        self.assertEqual(condition.returncode, 1)
        self.assertIn("ExecCondition=/bin/grep -Fq", service)

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
        self.assertIn('if [ "$MODE" != "reverse-t5-drain" ]; then', runner)


if __name__ == "__main__":
    unittest.main()
