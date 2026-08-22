from __future__ import annotations

import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest


ROOT = Path(__file__).resolve().parents[2]
SCRIPT = ROOT / "deploy" / "check-crown-first-look.py"
TIMER = ROOT / "deploy" / "systemd" / "crown-sweep.timer"
SERVICE = ROOT / "deploy" / "systemd" / "crown-sweep.service"


class CrownFirstLookHealthTests(unittest.TestCase):
    def _run(self, stage_rows: list[dict], *, valid_quote: bool = True) -> subprocess.CompletedProcess[str]:
        with tempfile.TemporaryDirectory() as directory:
            state = Path(directory)
            card = {
                "match_id": "private-provider-id",
                "kickoff_hkt": "2026-08-14T17:30:00+08:00",
                "book_odds": {
                    "crown": [{
                        "market": "HDC",
                        "selection": "H",
                        "line": -0.5,
                        "odds": 1.88,
                    }] if valid_quote else [],
                },
            }
            state.joinpath("predictions.json").write_text(
                json.dumps([card]), encoding="utf-8"
            )
            state.joinpath("ledger.json").write_text(
                json.dumps({
                    "watch": {
                        "private-provider-id": {
                            "matching_version": "2026-08-09-crown-v5-board-source",
                            "prediction_era": "2026-08-12-hkjc-corner-forecast-v4",
                            "stages": stage_rows,
                        },
                    },
                }),
                encoding="utf-8",
            )
            return subprocess.run(
                [
                    sys.executable,
                    str(SCRIPT),
                    "--state-dir",
                    str(state),
                    "--now",
                    "2026-08-14T16:50:00+08:00",
                ],
                cwd=ROOT,
                capture_output=True,
                text=True,
            )

    def test_missing_eligible_first_look_fails_without_exposing_provider_id(self) -> None:
        completed = self._run([])
        self.assertEqual(completed.returncode, 1)
        report = json.loads(completed.stdout)
        self.assertEqual(report["eligible_future_cards"], 1)
        self.assertEqual(report["missing_first_look"], 1)
        self.assertFalse(report["healthy"])
        self.assertNotIn("private-provider-id", completed.stdout)

    def test_completed_first_look_is_healthy(self) -> None:
        completed = self._run([{
            "stage": "首預",
            "status": "PREDICTION_READY",
        }])
        self.assertEqual(completed.returncode, 0)
        self.assertTrue(json.loads(completed.stdout)["healthy"])

    def test_card_without_valid_crown_quote_is_not_called_a_missed_prediction(self) -> None:
        completed = self._run([], valid_quote=False)
        report = json.loads(completed.stdout)
        self.assertEqual(completed.returncode, 0)
        self.assertEqual(report["eligible_future_cards"], 0)
        self.assertEqual(report["missing_first_look"], 0)

    def test_sweep_runs_at_hourly_00_and_checks_after_success(self) -> None:
        timer = TIMER.read_text(encoding="utf-8")
        service = SERVICE.read_text(encoding="utf-8")
        self.assertIn("OnCalendar=*-*-* *:00:00", timer)
        self.assertIn("ExecStartPost=", service)
        self.assertIn("check-crown-first-look.py", service)


if __name__ == "__main__":
    unittest.main()
