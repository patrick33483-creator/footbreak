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

    def test_market_prediction_scores_without_poisson_snapshot(self) -> None:
        score = accuracy.score_stage(
            {
                "stage": "T-5",
                "conviction": 63,
                "market_predictions": [{
                    "code": "HIL", "condition": "2.5", "side": "H",
                    "probability": 0.59, "label": "大 2.5 球",
                }],
            },
            {
                "goals_home": 2, "goals_away": 1,
                "corners_total": None, "source": "hkjc_official",
            },
        )
        self.assertIsNotNone(score)
        self.assertEqual(score["score_act"], "2-1")
        self.assertIsNone(score["wdl_pick"])
        self.assertEqual(score["market_grades"][0]["settlement"], "Won")
        self.assertTrue(score["market_grades"][0]["hit"])

    def test_official_result_with_missing_corners_uses_exact_fixture_fallback(self) -> None:
        kickoff = datetime.now(accuracy.HKT) - timedelta(hours=3)
        corner_prediction = {
            "code": "CHL", "condition": "9.5", "side": "H",
            "probability": 0.61, "label": "角球大 9.5",
        }
        ledger = {
            "bets": [],
            "watch": {
                "m-corner": {
                    "match_id": "m-corner", "fixture_id": "fx-corner",
                    "home": "主隊", "away": "客隊", "league": "聯賽",
                    "kickoff": kickoff.strftime("%Y-%m-%d %H:%M"),
                    "stages": [{
                        "prediction_era": accuracy.PREDICTION_ERA,
                        "stage": "T-30", "conviction": 60,
                        "market_predictions": [corner_prediction],
                    }],
                },
            },
        }
        official = {
            "goals_home": 1, "goals_away": 1, "goals_total": 2,
            "corners_total": None, "source": "hkjc_official",
        }
        merged = {
            **official, "corners_home": 6, "corners_away": 5,
            "corners_total": 11,
            "source": "hkjc_official+opticodds_exact_fixture_id",
        }
        with tempfile.TemporaryDirectory() as directory:
            ledger_path = Path(directory) / "sim_ledger.json"
            output_path = Path(directory) / "accuracy.json"
            history_path = Path(directory) / "accuracy_history.json"
            ledger_path.write_text(json.dumps(ledger), encoding="utf-8")
            with patch.object(accuracy, "LEDGER", str(ledger_path)), \
                 patch.object(accuracy, "OUT", str(output_path)), \
                 patch.object(accuracy, "HISTORY_OUT", str(history_path)), \
                 patch.object(accuracy.S, "fetch_hkjc_results",
                              return_value={"m-corner": official}), \
                 patch.object(accuracy.S, "merge_missing_corners",
                              return_value=merged) as merge:
                result = accuracy.run(fetch=True)
        merge.assert_called_once_with(official, "fx-corner")
        grade = result["matches"][0]["stages"][0]["market_grades"][0]
        self.assertEqual(grade["grade_status"], "GRADED")
        self.assertEqual(grade["settlement"], "Won")


if __name__ == "__main__":
    unittest.main()
