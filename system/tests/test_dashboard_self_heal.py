from pathlib import Path
import json
import subprocess
import sys
import tempfile
import unittest
from datetime import datetime, timedelta, timezone

from deploy.check_dashboard_stage_projection import projection_is_current


ROOT = Path(__file__).resolve().parents[2]


class DashboardSelfHealTests(unittest.TestCase):
    def test_timer_runs_every_thirty_minutes(self):
        timer = (
            ROOT
            / "deploy"
            / "systemd"
            / "footbreak-dashboard-self-heal.timer"
        ).read_text(encoding="utf-8")

        self.assertIn("OnUnitActiveSec=30min", timer)
        self.assertIn("Persistent=true", timer)

    def test_repair_is_local_and_bounded(self):
        script = (ROOT / "deploy" / "dashboard-self-heal.sh").read_text(
            encoding="utf-8"
        )

        self.assertIn("flock -n", script)
        self.assertIn("api_is_healthy 8766", script)
        self.assertIn("api_is_healthy 8765", script)
        self.assertIn('nginx_status 8081', script)
        self.assertIn('nginx_status 8082', script)
        self.assertIn("public_dashboard_json_is_healthy", script)
        self.assertIn("api/data", script)
        self.assertIn("nginx-unified-dashboard.conf", script)
        self.assertIn("footbreak-dashboard-api.service", script)
        self.assertIn("crown-dashboard-api.service", script)
        self.assertIn("dashboard_projection_is_current", script)
        self.assertIn("ledger-committed timed stage", script)
        self.assertIn("systemctl reload nginx || systemctl restart nginx", script)
        self.assertNotIn("Telegram", script)
        self.assertNotIn("external-tool", script)

    def test_deployment_enables_and_health_checks_timer(self):
        update = (ROOT / "deploy" / "update.sh").read_text(encoding="utf-8")
        health = (ROOT / "deploy" / "health-check.sh").read_text(encoding="utf-8")

        self.assertIn("footbreak-dashboard-self-heal.timer", update)
        self.assertIn("footbreak-dashboard-self-heal.timer", health)

    def test_committed_timed_stages_missing_from_valid_json_trigger_idempotent_repair(self):
        ledger = {
            "watch": {
                "m1": {
                    "match_id": "m1",
                    "stages": [
                        {"stage": "首預"},
                        {"stage": "T-30"},
                        {"stage": "T-5"},
                    ],
                },
            },
        }
        dashboard = {
            "generated_at": "2026-08-28T12:00:00+00:00",
            "ledger": {},
            "matches": [{"match_id": "m1", "stages": [{"stage": "首預"}]}],
        }
        checker = ROOT / "deploy" / "check_dashboard_stage_projection.py"
        with tempfile.TemporaryDirectory() as directory:
            ledger_path = Path(directory, "ledger.json")
            dashboard_path = Path(directory, "data.json")
            ledger_path.write_text(json.dumps(ledger), encoding="utf-8")
            dashboard_path.write_text(json.dumps(dashboard), encoding="utf-8")
            command = [
                sys.executable, str(checker),
                "--system", "footbreak",
                "--ledger", str(ledger_path),
                "--dashboard", str(dashboard_path),
            ]
            first = subprocess.run(command, capture_output=True, text=True, check=False)
            self.assertEqual(first.returncode, 1, first.stderr)

            dashboard["matches"][0]["stages"].extend(
                [{"stage": "T-30"}, {"stage": "T-5"}],
            )
            dashboard_path.write_text(json.dumps(dashboard), encoding="utf-8")
            repaired = subprocess.run(command, capture_output=True, text=True, check=False)
            rerun = subprocess.run(command, capture_output=True, text=True, check=False)
            self.assertEqual(repaired.returncode, 0, repaired.stderr)
            self.assertEqual(rerun.returncode, 0, rerun.stderr)
            self.assertEqual(
                [row["stage"] for row in json.loads(
                    dashboard_path.read_text(encoding="utf-8")
                )["matches"][0]["stages"]],
                ["首預", "T-30", "T-5"],
            )

    def test_historical_hidden_stage_does_not_cause_unsatisfiable_republish(self):
        now = datetime(2026, 8, 28, 12, 0, tzinfo=timezone.utc)
        ledger = {"watch": {"old": {
            "match_id": "old",
            "kickoff": (now - timedelta(days=1)).isoformat(),
            "stages": [{"stage": "T-30"}],
        }}}
        self.assertTrue(projection_is_current(
            ledger, {"matches": []}, system="footbreak", now=now,
        ))

    def test_active_recoverable_missing_card_still_requires_republish(self):
        now = datetime(2026, 8, 28, 12, 0, tzinfo=timezone.utc)
        ledger = {"watch": {"live": {
            "match_id": "live",
            "home": "Home",
            "away": "Away",
            "league": "League",
            "kickoff": (now + timedelta(minutes=20)).isoformat(),
            "stages": [{"stage": "首預"}, {"stage": "T-30"}],
        }}}
        self.assertFalse(projection_is_current(
            ledger, {"matches": []}, system="footbreak", now=now,
        ))

    def test_duplicate_authoritative_timed_stage_is_rejected(self):
        ledger = {"watch": {"m": {
            "match_id": "m",
            "stages": [{"stage": "T-5"}, {"stage": "T-5"}],
        }}}
        dashboard = {
            "matches": [{"match_id": "m", "stages": [{"stage": "T-5"}]}],
        }
        self.assertFalse(projection_is_current(ledger, dashboard))

    def test_footbreak_dashboard_generation_never_writes_ledger(self):
        generator = (ROOT / "system" / "gen_app_data.py").read_text(encoding="utf-8")
        self.assertNotIn("_write_ledger_atomic", generator)
        self.assertNotIn("project_granular_ranking_evidence(", generator)
        self.assertIn("project_frozen_ranking_evidence(", generator)
        self.assertIn("Dashboard generation is a read-only ledger consumer", generator)

    def test_both_stage_ticks_queue_independent_projection(self):
        for name in ("footbreak-tick.service", "crown-tick.service"):
            unit = (ROOT / "deploy" / "systemd" / name).read_text(encoding="utf-8")
            self.assertIn(
                "ExecStartPost=-/usr/bin/systemctl start --no-block "
                "footbreak-dashboard-self-heal.service",
                unit,
            )


if __name__ == "__main__":
    unittest.main()
