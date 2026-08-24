"""Regression guard for deadline preemption versus bounded first-look repair."""

from pathlib import Path
import io
import sys
import tempfile
import unittest
from contextlib import redirect_stdout
from dataclasses import replace
from unittest.mock import patch

from crown.config import settings
from crown.run import main


class CrownTickPreemptionTests(unittest.TestCase):
    def test_urgent_tick_does_not_kill_bounded_first_look_reconciliation(self) -> None:
        script = (
            Path(__file__).resolve().parents[2] / "deploy" / "crown-tick-preempt.sh"
        ).read_text(encoding="utf-8")
        stop_line = next(
            line for line in script.splitlines()
            if "/usr/bin/systemctl stop --no-block" in line
        )
        self.assertNotIn("crown-first-look-reconcile.service", stop_line)
        self.assertIn("crown-sweep.service", stop_line)
        self.assertIn("crown-settle.service", stop_line)

    def test_first_look_runner_exits_before_optional_dashboard_publication(self) -> None:
        runner = (
            Path(__file__).resolve().parents[2] / "crown" / "run.py"
        ).read_text(encoding="utf-8")
        reconcile = runner.index('if args.mode == "first-look-reconcile":')
        identity = runner.index(
            "schedule_hkjc_identity_reconciliation", reconcile,
        )
        dashboard = runner.index("write_dashboard_data(config)", reconcile)
        terminal = runner.index("return 0", reconcile)
        self.assertLess(identity, terminal)
        self.assertLess(terminal, dashboard)

    def test_identity_scheduler_failure_does_not_fail_first_look(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            config = replace(
                settings(),
                state_dir=Path(directory) / "state",
                web_root=Path(directory) / "web",
            )
            with patch.object(
                sys, "argv", ["crown.run", "first-look-reconcile"],
            ), patch(
                "crown.run.settings", return_value=config,
            ), patch(
                "crown.run.run",
                return_value={"ok": True, "mode": "first-look-reconcile"},
            ), patch(
                "crown.run.schedule_hkjc_identity_reconciliation",
                side_effect=RuntimeError("isolated worker launch failed"),
            ), redirect_stdout(io.StringIO()) as output:
                self.assertEqual(main(), 0)
            self.assertIn(
                "'hkjc_identity_reconciliation_scheduled': False",
                output.getvalue(),
            )


if __name__ == "__main__":
    unittest.main()
