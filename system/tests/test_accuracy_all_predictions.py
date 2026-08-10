"""Regression: pure-prediction accuracy must not depend on simulated bets."""
from __future__ import annotations

import json
import sys
import tempfile
import unittest
from datetime import datetime, timedelta
from pathlib import Path
from unittest.mock import patch

SYSTEM_DIR = Path(__file__).resolve().parents[1]
if str(SYSTEM_DIR) not in sys.path:
    sys.path.insert(0, str(SYSTEM_DIR))

import accuracy


class AccuracyAllPredictionsTests(unittest.TestCase):
    def test_watch_prediction_is_scored_when_bets_are_empty(self) -> None:
        kickoff = datetime.now(accuracy.HKT) - timedelta(hours=3)
        ledger = {
            "bets": [],
            "watch": {
                "m1": {
                    "match_id": "m1",
                    "home": "主隊",
                    "away": "客隊",
                    "league": "聯賽",
                    "kickoff": kickoff.strftime("%Y-%m-%d %H:%M"),
                    "stages": [{
                        "prediction_era": accuracy.PREDICTION_ERA,
                        "stage": "T-30",
                        "conviction": 60,
                        "final": {"lh": 1.6, "la": 0.9, "rho": 0.0, "mu": None},
                        "now": {},
                        "market_predictions": [{
                            "code": "HDC", "condition": "-0.5", "side": "H",
                            "probability": 0.61, "label": "主 -0.5",
                        }],
                    }],
                }
            },
        }
        result = {
            "goals_home": 2, "goals_away": 1, "goals_total": 3,
            "corners_total": None, "source": "hkjc_official",
        }
        with tempfile.TemporaryDirectory() as directory:
            ledger_path = Path(directory) / "sim_ledger.json"
            output_path = Path(directory) / "accuracy.json"
            history_path = Path(directory) / "accuracy_history.json"
            ledger_path.write_text(json.dumps(ledger, ensure_ascii=False), encoding="utf-8")
            with patch.object(accuracy, "LEDGER", str(ledger_path)), \
                 patch.object(accuracy, "OUT", str(output_path)), \
                 patch.object(accuracy, "HISTORY_OUT", str(history_path)), \
                 patch.object(accuracy.S, "fetch_hkjc_results", return_value={"m1": result}):
                scored = accuracy.run(fetch=True)
            history_count = len(
                json.loads(history_path.read_text(encoding="utf-8"))["matches"]
            )
        self.assertEqual(scored["n_matches"], 1)
        self.assertEqual(scored["n_preds"], 1)
        self.assertEqual(scored["missing_results"], [])
        self.assertEqual(scored["latest"]["n"], 1)
        self.assertEqual(history_count, 1)

    def test_wdl_only_snapshot_is_not_a_learning_sample(self) -> None:
        self.assertFalse(accuracy.has_scoreable_market_prediction({
            "prediction_era": accuracy.PREDICTION_ERA,
            "final": {"lh": 1.5, "la": 1.0},
        }))


if __name__ == "__main__":
    unittest.main()
