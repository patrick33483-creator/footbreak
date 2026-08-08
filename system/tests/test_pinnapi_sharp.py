"""Offline regression tests for Footbreak's PinnAPI sharp-provider adapter."""
from __future__ import annotations

import os
import subprocess
import sys
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[2]
SYSTEM = ROOT / "system"
if str(SYSTEM) not in sys.path:
    sys.path.insert(0, str(SYSTEM))

import run_predict
import sharp
from crown.pinnapi import parse_lines


class FootbreakPinnapiSharpTests(unittest.TestCase):
    def test_pinnapi_parser_and_native_structure_preserve_home_handicap_orientation(self) -> None:
        parsed = parse_lines({
            "event_id": "event-1", "source_timestamp": 1786248000,
            "periods": {"num_0": {
                "moneyline": {"home": 2.1, "draw": 3.4, "away": 3.7},
                # PinnAPI hdp is already home perspective: -0.25 = home gives.
                "spreads": [{"hdp": -0.25, "home": 1.91, "away": 1.99, "is_main": True}],
                "totals": [{"points": 2.75, "over": 1.95, "under": 1.95, "is_main": True}],
            }},
        }, "event-1", observed_at=1786248001)
        self.assertFalse(parsed["timestamp_inferred"])
        prices = [dict(row, provider="pinnapi") for row in parsed["prices"]]
        structured = sharp.structure(prices, "Home", "Away")
        self.assertEqual(structured["HDC"][0]["condition"], "-0.25")
        self.assertEqual(structured["HDC"][0]["odds"], {"H": 1.91, "A": 1.99})
        self.assertEqual(structured["HIL"][0]["condition"], "2.75")
        self.assertEqual(structured["HAD"][0]["odds"]["D"], 3.4)
        self.assertEqual(structured["CHL"], [])

    def test_pinnapi_fixture_adapter_preserves_english_identity_and_kickoff(self) -> None:
        fixture = sharp._fixture_from_pinnapi({
            "id": "123", "league": "England - Premier League", "home": "Home FC", "away": "Away FC",
            "kickoff": 1786248000,
        })
        self.assertEqual(fixture["id"], "123")
        self.assertEqual(fixture["home_team_display"], "Home FC")
        self.assertTrue(fixture["start_date"].endswith("Z"))
        self.assertEqual(fixture["league"]["name"], "England - Premier League")

    def test_pinnapi_lines_failure_propagates(self) -> None:
        class BrokenClient:
            def lines(self, event_id):
                raise RuntimeError("upstream outage")

        with patch.object(sharp, "_client", return_value=BrokenClient()):
            with self.assertRaises(sharp.ProviderError):
                sharp.fetch_odds(["123"])

    def test_prediction_failure_does_not_replace_existing_predictions(self) -> None:
        kickoff = datetime.now(timezone.utc) + timedelta(minutes=30)
        match = {
            "id": "m1", "status": "PREEVENT",
            "homeTeam": {"name_ch": "主隊"}, "awayTeam": {"name_ch": "客隊"},
        }
        fixture = {"id": "p1", "home_team_display": "Home", "away_team_display": "Away"}
        with tempfile.TemporaryDirectory() as directory:
            previous_here, previous_snap = run_predict.HERE, run_predict.HK_SNAP
            try:
                run_predict.HERE = directory
                run_predict.HK_SNAP = os.path.join(directory, "hk_snapshots.json")
                target = Path(directory) / "predictions.json"
                target.write_text('[{"match_id":"existing"}]', encoding="utf-8")
                with patch.object(run_predict.H, "fetch_matches", return_value=[match]), \
                     patch.object(run_predict.H, "parse_kickoff", return_value=kickoff), \
                     patch.object(run_predict.S, "list_fixtures", return_value=[fixture]), \
                     patch.object(run_predict.S, "match_fixture", return_value=(fixture, 1.0)), \
                     patch.object(run_predict, "analyse_match", side_effect=sharp.ProviderError("PinnAPI down")):
                    with self.assertRaisesRegex(RuntimeError, "sharp/prediction failed"):
                        run_predict.main(mode="due", horizon_min=90)
                self.assertEqual(target.read_text(encoding="utf-8"), '[{"match_id":"existing"}]')
            finally:
                run_predict.HERE, run_predict.HK_SNAP = previous_here, previous_snap

    def test_unusable_sharp_model_is_a_failure_not_a_silent_skip(self) -> None:
        kickoff = datetime.now(timezone.utc) + timedelta(minutes=30)
        match = {
            "id": "m1", "status": "PREEVENT",
            "homeTeam": {"name_ch": "主隊"}, "awayTeam": {"name_ch": "客隊"},
        }
        fixture = {"id": "p1", "home_team_display": "Home", "away_team_display": "Away"}
        with patch.object(run_predict.H, "fetch_matches", return_value=[match]), \
             patch.object(run_predict.H, "parse_kickoff", return_value=kickoff), \
             patch.object(run_predict.S, "list_fixtures", return_value=[fixture]), \
             patch.object(run_predict.S, "match_fixture", return_value=(fixture, 1.0)), \
             patch.object(run_predict, "analyse_match", return_value={"skip": "no full-match lines"}):
            with self.assertRaisesRegex(RuntimeError, "produced no usable model"):
                run_predict.main(mode="due", horizon_min=90)

    def test_run_all_exits_before_ledger_or_dashboard_steps_on_prediction_failure(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            log = root / "calls.log"
            fake_python = root / "python3"
            fake_python.write_text(
                "#!/usr/bin/env bash\n"
                "printf '%s\\n' \"$*\" >> \"$TEST_CALL_LOG\"\n"
                "if [ \"$1\" = run_predict.py ]; then exit 17; fi\n"
                "exit 0\n",
                encoding="utf-8",
            )
            fake_python.chmod(0o755)
            env = os.environ | {"PATH": f"{root}:{os.environ['PATH']}", "TEST_CALL_LOG": str(log)}
            result = subprocess.run(["bash", "run_all.sh", "tick"], cwd=SYSTEM, env=env,
                                    text=True, capture_output=True)
            self.assertEqual(result.returncode, 17)
            self.assertEqual(log.read_text(encoding="utf-8").splitlines(), ["run_predict.py 90"])


if __name__ == "__main__":
    unittest.main()
