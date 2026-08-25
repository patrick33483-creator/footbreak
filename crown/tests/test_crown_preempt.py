"""Regression guard for deadline preemption versus bounded first-look repair."""

from pathlib import Path
import io
import sys
import tempfile
import time
import unittest
from contextlib import redirect_stdout
from dataclasses import replace
from unittest.mock import patch

from crown.config import settings
from crown.run import main


def _hang_dashboard_projection(_config) -> None:
    time.sleep(2)


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

    def test_first_look_runner_uses_bounded_lightweight_dashboard_projection(self) -> None:
        runner = (
            Path(__file__).resolve().parents[2] / "crown" / "run.py"
        ).read_text(encoding="utf-8")
        reconcile = runner.index('if args.mode == "first-look-reconcile":')
        identity = runner.index(
            "schedule_hkjc_identity_reconciliation", reconcile,
        )
        projection = runner.index("_run_dashboard_projection(", reconcile)
        terminal = runner.index("return 0", reconcile)
        full_dashboard = runner.index("write_dashboard_data(config)", reconcile)
        self.assertLess(identity, projection)
        self.assertLess(projection, terminal)
        self.assertLess(terminal, full_dashboard)

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
            ), patch(
                "crown.run._run_dashboard_projection",
                return_value=("complete", None),
            ), redirect_stdout(io.StringIO()) as output:
                self.assertEqual(main(), 0)
            self.assertIn(
                "'hkjc_identity_reconciliation_scheduled': False",
                output.getvalue(),
            )
            self.assertIn(
                "'dashboard_projection': 'post_first_look_local_state'",
                output.getvalue(),
            )

    def test_dashboard_projection_failure_does_not_fail_first_look(self) -> None:
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
                return_value=True,
            ), patch(
                "crown.run._run_dashboard_projection",
                return_value=("deferred", None),
            ), redirect_stdout(io.StringIO()) as output:
                self.assertEqual(main(), 0)
            self.assertIn(
                "'dashboard_projection_warning': "
                "'deferred_first_look_projection_deadline'",
                output.getvalue(),
            )

    def test_first_look_dashboard_projection_is_hard_bounded(self) -> None:
        import crown.run as crown_run

        with tempfile.TemporaryDirectory() as directory:
            config = replace(
                settings(),
                state_dir=Path(directory) / "state",
                web_root=Path(directory) / "web",
            )
            started = time.monotonic()
            with patch(
                "crown.run.write_tick_dashboard_projection",
                side_effect=_hang_dashboard_projection,
            ):
                status, detail = crown_run._run_dashboard_projection(
                    config, 0.10,
                )
            self.assertEqual((status, detail), ("deferred", None))
            self.assertLess(time.monotonic() - started, 0.75)


if __name__ == "__main__":
    unittest.main()
