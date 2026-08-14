from __future__ import annotations

import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

import yaml


ROOT = Path(__file__).resolve().parents[2]
SCRIPT = ROOT / "deploy" / "audit-crown-stage-state.py"
WORKFLOW = ROOT / ".github" / "workflows" / "crown-stage-diagnose.yml"


class CrownStageReportTests(unittest.TestCase):
    def test_report_is_bounded_and_redacts_provider_identifiers_and_payloads(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            state = Path(directory)
            state.joinpath("predictions.json").write_text(json.dumps([{
                "match_id": "titan-provider-id-123",
                "league": "測試聯賽",
                "home": "甲隊",
                "away": "乙隊",
                "kickoff_hkt": "2026-08-13T22:00:00+08:00",
                "generated_at": "2026-08-13T20:00:00+08:00",
                "status": "DATA_MISSING",
                "no_bet_reason": '{"token":"must-not-appear"}',
                "provider_payload": {"token": "must-not-appear"},
            }], ensure_ascii=False), encoding="utf-8")
            state.joinpath("ledger.json").write_text(json.dumps({
                "watch": {
                    "titan-provider-id-123": {
                        "match_id": "titan-provider-id-123",
                        "home": "甲隊",
                        "away": "乙隊",
                        "kickoff": "2026-08-13T22:00:00+08:00",
                        "discovered_at": "2026-08-13T20:00:00+08:00",
                        "stages": [{"stage": "T-30", "status": "DATA_MISSING",
                                    "ts": "2026-08-13T21:30:00+08:00",
                                    "no_bet_reason": '{"token":"must-not-appear"}',
                                    "market_predictions": [
                                        {"code": "HDC", "side": "A", "line": -0.5,
                                         "odds": 1.88, "observed_at": 1786622400},
                                        {"code": "PRIVATE", "side": "H", "line": 1,
                                         "odds": 9.99, "secret": "must-not-appear"},
                                    ]}],
                        "token": "must-not-appear",
                    },
                },
            }, ensure_ascii=False), encoding="utf-8")
            completed = subprocess.run(
                [
                    sys.executable, str(SCRIPT), "--state-dir", str(state),
                    "--future-hours", "6", "--current-grace-minutes", "30",
                    "--limit", "25", "--now", "2026-08-13T21:30:00+08:00",
                ],
                cwd=ROOT,
                env={"FOOTBREAK_APP_DIR": str(ROOT)},
                check=True,
                capture_output=True,
                text=True,
            )
        report = json.loads(completed.stdout)
        self.assertTrue(report["read_only"])
        self.assertFalse(report["provider_requests"])
        self.assertEqual(report["state"]["fixtures_emitted"], 1)
        fixture = report["fixtures"][0]
        self.assertEqual(fixture["fixture"]["home"], "甲隊")
        self.assertEqual(fixture["fixture"]["away"], "乙隊")
        self.assertNotIn("match_id", fixture)
        self.assertNotIn('"provider_payload":', completed.stdout)
        self.assertNotIn("titan-provider-id-123", completed.stdout)
        self.assertNotIn("must-not-appear", completed.stdout)
        self.assertTrue(fixture["first_look"]["should_have_run"])
        self.assertEqual(fixture["first_look"]["reason"], "fixture_known_pre_kickoff_first_look_missing")
        self.assertEqual(fixture["scheduler"]["next_due_stage"], "首預")
        self.assertEqual(fixture["completed_stages"], [])
        markets = fixture["stage_status"][0]["markets"]
        self.assertEqual(markets, [{
            "code": "HDC",
            "side": "A",
            "home_line": -0.5,
            "selected_line": 0.5,
            "odds": 1.88,
            "observed_at": "2026-08-13T20:00:00+08:00",
        }])

    def test_manual_workflow_is_read_only_and_validates_bounded_inputs(self) -> None:
        text = WORKFLOW.read_text(encoding="utf-8")
        parsed = yaml.load(text, Loader=yaml.BaseLoader)
        trigger = parsed.get("on", parsed.get(True))
        inputs = trigger["workflow_dispatch"]["inputs"]
        self.assertEqual(inputs["future_hours"]["options"], ["6", "12", "24"])
        self.assertEqual(inputs["current_grace_minutes"]["options"], ["15", "30", "60"])
        self.assertEqual(inputs["fixture_limit"]["options"], ["25", "50", "100"])
        self.assertEqual(parsed["permissions"]["contents"], "read")
        self.assertNotIn("upload-artifact", text)
        self.assertNotIn("journalctl", text)
        self.assertNotIn("cat /etc/footbreak", text)
        self.assertNotIn("printenv", text)
        self.assertIn("case \"$FUTURE_HOURS\" in 6|12|24)", text)
        self.assertIn("case \"$CURRENT_GRACE_MINUTES\" in 15|30|60)", text)
        self.assertIn("case \"$FIXTURE_LIMIT\" in 25|50|100)", text)
        self.assertIn("raw_provider_ids_emitted", SCRIPT.read_text(encoding="utf-8"))
        self.assertIn("provider_payloads_emitted", SCRIPT.read_text(encoding="utf-8"))


if __name__ == "__main__":
    unittest.main()
