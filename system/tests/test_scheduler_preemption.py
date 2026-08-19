import tempfile
import unittest
import json
import datetime as dt
import os
import subprocess
import sys
from pathlib import Path
from unittest.mock import patch

from system import record_picks
from system import settle


ROOT = Path(__file__).resolve().parents[2]


class SchedulerPreemptionTests(unittest.TestCase):
    def test_deploy_update_enables_full_board_sweep_timer(self):
        update = (ROOT / "deploy/update.sh").read_text(encoding="utf-8")
        self.assertRegex(
            update,
            r"systemctl enable --now\s+\\?\s*"
            r"footbreak-tick\.timer footbreak-sweep\.timer footbreak-settle\.timer",
        )

    def test_incremental_full_board_refresh_runs_every_fifteen_minutes_and_yields_to_tick(self):
        timer = (ROOT / "deploy/systemd/footbreak-sweep.timer").read_text(
            encoding="utf-8"
        )
        self.assertIn("OnCalendar=*:0/15", timer)
        self.assertIn("Persistent=true", timer)
        self.assertIn("AccuracySec=1s", timer)

        service = (ROOT / "deploy/systemd/footbreak-sweep.service").read_text(
            encoding="utf-8"
        )
        self.assertIn("ExecStart=/opt/footbreak/deploy/run.sh sweep", service)
        self.assertIn(
            "ExecCondition=/opt/footbreak/deploy/footbreak-tick-preempt.sh --yield-if-urgent",
            service,
        )
        self.assertIn("ConditionPathExists=!/run/footbreak-t5-priority", service)
        self.assertIn("SuccessExitStatus=75", service)
        self.assertIn("TimeoutStartSec=900", service)

        helper = (ROOT / "deploy/footbreak-tick-preempt.sh").read_text(encoding="utf-8")
        self.assertIn('if [ "$MODE" = "--yield-if-urgent" ]; then', helper)
        self.assertIn("full-board refresh yielded", helper)
        self.assertIn("/usr/bin/systemctl stop footbreak-sweep.service footbreak-settle.service", helper)

    def test_refresh_precondition_yields_for_urgent_or_unreadable_ledger(self):
        helper = ROOT / "deploy/footbreak-tick-preempt.sh"
        with tempfile.TemporaryDirectory() as tmp:
            ledger = Path(tmp, "sim_ledger.json")
            env = os.environ | {
                "FOOTBREAK_LEDGER": str(ledger),
                "FOOTBREAK_PYTHON": sys.executable,
                "FOOTBREAK_PRIORITY_MARKER": str(Path(tmp, "priority")),
            }
            kickoff = (dt.datetime.now(record_picks.HKT) + dt.timedelta(minutes=8)).strftime(
                "%Y-%m-%d %H:%M"
            )
            ledger.write_text(
                json.dumps({"watch": {"urgent": {"kickoff": kickoff, "stages": []}}}),
                encoding="utf-8",
            )
            urgent = subprocess.run(
                ["bash", str(helper), "--yield-if-urgent"],
                env=env,
                text=True,
                capture_output=True,
            )
            self.assertEqual(urgent.returncode, 1, urgent.stderr)
            self.assertIn("full-board refresh yielded", urgent.stdout)

            safe_kickoff = (
                dt.datetime.now(record_picks.HKT) + dt.timedelta(minutes=90)
            ).strftime("%Y-%m-%d %H:%M")
            ledger.write_text(
                json.dumps({"watch": {"safe": {"kickoff": safe_kickoff, "stages": []}}}),
                encoding="utf-8",
            )
            safe = subprocess.run(
                ["bash", str(helper), "--yield-if-urgent"],
                env=env,
                text=True,
                capture_output=True,
            )
            self.assertEqual(safe.returncode, 0, safe.stderr)
            self.assertIn("may run", safe.stdout)

            ledger.unlink()
            unreadable = subprocess.run(
                ["bash", str(helper), "--yield-if-urgent"],
                env=env,
                text=True,
                capture_output=True,
            )
            self.assertEqual(unreadable.returncode, 2)
            self.assertIn("state unavailable", unreadable.stderr)

    def test_tick_service_conditionally_preempts_slow_jobs_before_running(self):
        unit = (ROOT / "deploy/systemd/footbreak-tick.service").read_text(
            encoding="utf-8"
        )
        preempt = unit.index("ExecStartPre=/opt/footbreak/deploy/footbreak-tick-preempt.sh")
        run = unit.index("ExecStart=/opt/footbreak/deploy/run.sh tick")
        self.assertLess(preempt, run)
        self.assertIn("TimeoutStartSec=60", unit)
        self.assertIn("TimeoutStopSec=3", unit)
        self.assertIn("Environment=FOOTBREAK_TICK_LOCK_WAIT_SECONDS=5", unit)
        self.assertIn("SuccessExitStatus=75", unit)
        self.assertIn("ExecStopPost=-/usr/bin/rm -f /run/footbreak-t5-priority", unit)
        for name in ("footbreak-t30.service", "footbreak-sweep.service", "footbreak-settle.service"):
            slow = (ROOT / "deploy/systemd" / name).read_text(encoding="utf-8")
            self.assertIn(
                "ConditionPathExists=!/run/footbreak-t5-priority",
                slow,
            )

        helper = (ROOT / "deploy/footbreak-tick-preempt.sh").read_text(encoding="utf-8")
        self.assertIn('t30_due = 5.0 < minutes <= 30.5 and "T-30" not in stages', helper)
        self.assertIn('t5_due = 0.0 < minutes <= 10.5 and "T-5" not in stages', helper)
        self.assertIn("/usr/bin/systemctl stop footbreak-sweep.service footbreak-settle.service", helper)

    def test_crown_tick_preempts_only_when_a_local_t5_is_due(self):
        unit = (ROOT / "deploy/systemd/crown-tick.service").read_text(encoding="utf-8")
        self.assertLess(
            unit.index("ExecStartPre=/opt/footbreak/deploy/crown-tick-preempt.sh"),
            unit.index("ExecStart=/opt/footbreak/deploy/crown-run.sh tick"),
        )
        self.assertIn("Environment=CROWN_TICK_PASS_DEADLINE_SECONDS=40", unit)
        self.assertIn("TimeoutStartSec=55", unit)
        self.assertIn("TimeoutStopSec=5", unit)
        self.assertIn("ExecStopPost=-/usr/bin/rm -f /run/crown-t5-priority", unit)
        helper = (ROOT / "deploy/crown-tick-preempt.sh").read_text(encoding="utf-8")
        self.assertIn('if 0.0 < minutes <= 10.5 and not t5_complete:', helper)
        self.assertIn(
            "/usr/bin/systemctl stop --no-block crown-sweep.service crown-settle.service",
            helper,
        )
        for name in ("crown-sweep.service", "crown-settle.service"):
            slow = (ROOT / "deploy/systemd" / name).read_text(encoding="utf-8")
            self.assertIn("ConditionPathExists=!/run/crown-t5-priority", slow)

    def test_slow_jobs_yield_when_t5_priority_marker_exists(self):
        wrapper = (ROOT / "deploy/run.sh").read_text(encoding="utf-8")
        self.assertIn('elif [ -e "$PRIORITY_MARKER" ]; then', wrapper)
        self.assertIn('TICK_LOCK_WAIT_SECONDS:-2', wrapper)
        self.assertIn(
            'export PYTHONPATH="$APP_DIR${PYTHONPATH:+:$PYTHONPATH}"',
            wrapper,
        )
        self.assertLess(
            wrapper.index('export PYTHONPATH="$APP_DIR${PYTHONPATH:+:$PYTHONPATH}"'),
            wrapper.index('cd "$APP_DIR/system"'),
        )
        timer = (ROOT / "deploy/systemd/footbreak-settle.timer").read_text(
            encoding="utf-8"
        )
        self.assertIn("OnCalendar=*:0/5:15", timer)
        self.assertIn("AccuracySec=1s", timer)

    def test_record_picks_save_atomically_replaces_ledger(self):
        with tempfile.TemporaryDirectory() as tmp:
            ledger = Path(tmp, "sim_ledger.json")
            with patch.object(record_picks, "LEDGER", str(ledger)):
                record_picks.save({"bets": [{"match_id": "safe"}]})
            self.assertEqual(
                ledger.read_text(encoding="utf-8").count('"match_id": "safe"'),
                1,
            )
            self.assertEqual(list(Path(tmp).glob(".sim-ledger-*")), [])

    def test_settlement_save_atomically_replaces_ledger(self):
        with tempfile.TemporaryDirectory() as tmp:
            ledger = Path(tmp, "sim_ledger.json")
            settle.write_json_atomic(str(ledger), {"bets": []})
            self.assertTrue(ledger.is_file())
            self.assertEqual(list(Path(tmp).glob(".settle-*")), [])

    def test_t5_is_rejected_at_final_ledger_commit_after_safe_time(self):
        with tempfile.TemporaryDirectory() as tmp:
            ledger = Path(tmp, "sim_ledger.json")
            predictions = Path(tmp, "predictions.json")
            now = dt.datetime.now(record_picks.HKT)
            predictions.write_text(
                json.dumps(
                    [
                        {
                            "match_id": "late",
                            "stage": "T-5",
                            "league": "L",
                            "home": "A",
                            "away": "B",
                            "kickoff_hkt": (now + dt.timedelta(seconds=2)).isoformat(),
                            "conviction": 90,
                            "pick": {
                                "market": "讓球",
                                "code": "HDC",
                                "condition": "-0.5",
                                "side": "H",
                                "label": "主 -0.5",
                                "odds": 2.0,
                                "prob": 0.6,
                                "push": 0.0,
                                "ev": 0.2,
                                "kelly_used": 0.01,
                                "stake": 500,
                            },
                            "candidates": [],
                            "final": {},
                        }
                    ]
                ),
                encoding="utf-8",
            )
            with patch.object(record_picks, "HERE", tmp), patch.object(
                record_picks, "LEDGER", str(ledger)
            ):
                _, notes, saved = record_picks.sync("predictions.json")
            self.assertEqual(saved["bets"], [])
            self.assertTrue(any("安全落注時間" in note for note in notes))


if __name__ == "__main__":
    unittest.main()
