"""Regression tests for the Crown-style Footbreak prediction history payload."""
from __future__ import annotations

import sys
import unittest
from pathlib import Path

SYSTEM_DIR = Path(__file__).resolve().parents[1]
if str(SYSTEM_DIR) not in sys.path:
    sys.path.insert(0, str(SYSTEM_DIR))

import gen_app_data
from record_picks import PREDICTION_ERA

ERA = PREDICTION_ERA


def market_prediction(code="HDC", condition="-0.5", side="H"):
    return {
        "code": code,
        "condition": condition,
        "side": side,
        "probability": 0.61,
        "label": "測試市場方向",
    }


class PredictionHistoryPayloadTests(unittest.TestCase):
    def test_all_stages_are_kept_and_results_are_joined(self) -> None:
        stage = {
            "prediction_era": ERA,
            "stage": "T-30",
            "ts": "2026-08-09T19:30:00+08:00",
            "conviction": 61.2,
            "final": {"lh": 1.7, "la": 0.8, "rho": 0.0, "mu": None},
            "now": {},
            "market_predictions": [market_prediction()],
            "no_bet_reason": "未到唯一落注時點 T-5",
        }
        watch = {
            "m1": {
                "match_id": "m1", "home": "主隊", "away": "客隊",
                "league": "測試聯賽", "kickoff": "2026-08-09 20:00",
                "stages": [stage],
            }
        }
        accuracy = {
            "matches": [{
                "match_id": "m1", "home": "主隊", "away": "客隊",
                "league": "測試聯賽", "kickoff": "2026-08-09 20:00",
                "score": "2-0", "result_source": "hkjc_official",
                "stages": [{
                    "stage": "T-30", "conf": 61.2, "wdl_pick": 0,
                    "wdl_act": 0, "wdl_hit": 1, "wdl_pmax": 0.58,
                    "score_top": "1-0",
                    "market_predictions": [market_prediction()],
                }],
            }]
        }

        payload = gen_app_data.build_prediction_history(watch, [], accuracy)

        self.assertEqual(payload["stats"]["matches"], 1)
        self.assertEqual(payload["stats"]["predictions"], 1)
        self.assertEqual(payload["stats"]["graded"], 1)
        self.assertEqual(payload["stats"]["hits"], 1)
        self.assertEqual(payload["stats"]["accuracy"], 1.0)
        self.assertEqual(payload["stats"]["by_stage"]["T-30"]["accuracy"], 1.0)
        row = payload["rows"][0]
        self.assertEqual(row["forecast"], "主勝")
        self.assertEqual(row["actual"], "主勝")
        self.assertEqual(row["score"], "2-0")
        self.assertEqual(row["result_source"], "hkjc_official")
        self.assertTrue(row["correct"])
        self.assertFalse(row["simulated_bet"])

    def test_simulated_bet_is_only_attached_to_its_stage(self) -> None:
        watch = {
            "m1": {
                "match_id": "m1", "home": "主隊", "away": "客隊",
                "league": "測試聯賽", "kickoff": "2026-08-09 20:00",
                "stages": [
                    {"prediction_era": ERA, "stage": "T-30", "conviction": 60,
                     "market_predictions": [market_prediction()]},
                    {"prediction_era": ERA, "stage": "T-5", "conviction": 62,
                     "market_predictions": [market_prediction("HIL", "2.5", "L")]},
                ],
            }
        }
        bets = [{
            "match_id": "m1", "first_stage": "T-5",
            "label": "讓球 主隊 -0.5", "status": "PENDING",
        }]

        rows = gen_app_data.build_prediction_history(watch, bets, None)["rows"]
        by_stage = {row["stage"]: row for row in rows}
        self.assertFalse(by_stage["T-30"]["simulated_bet"])
        self.assertTrue(by_stage["T-5"]["simulated_bet"])
        self.assertEqual(by_stage["T-5"]["bet_label"], "讓球 主隊 -0.5")

    def test_suspended_match_is_excluded_not_left_pending(self) -> None:
        watch = {
            "m2": {
                "match_id": "m2", "home": "主隊", "away": "客隊",
                "league": "測試聯賽", "kickoff": "2026-08-09 05:30",
                "stages": [{
                    "prediction_era": ERA, "stage": "首預", "conviction": 55,
                    "market_predictions": [market_prediction()],
                }],
            }
        }
        accuracy = {
            "matches": [],
            "excluded_results": [{
                "match_id": "m2", "status": "MATCHSUSPENDED",
            }],
        }
        payload = gen_app_data.build_prediction_history(watch, [], accuracy)
        self.assertEqual(payload["stats"]["pending"], 0)
        self.assertEqual(payload["stats"]["excluded"], 1)
        self.assertEqual(payload["rows"][0]["result_status"], "不計")

    def test_wdl_only_rows_are_removed_and_later_kickoff_is_first(self) -> None:
        watch = {
            "old-wdl": {
                "home": "舊主", "away": "舊客", "kickoff": "2026-08-09 18:00",
                "stages": [{"prediction_era": ERA, "stage": "T-30"}],
            },
            "valid-early": {
                "home": "早主", "away": "早客", "kickoff": "2026-08-09 19:00",
                "stages": [{
                    "prediction_era": ERA, "stage": "T-30",
                    "market_predictions": [market_prediction()],
                }],
            },
            "valid-late": {
                "home": "後主", "away": "後客", "kickoff": "2026-08-10 01:00",
                "stages": [{
                    "prediction_era": ERA, "stage": "T-5",
                    "market_predictions": [market_prediction("HIL", "3.0", "H")],
                }],
            },
        }
        rows = gen_app_data.build_prediction_history(watch, [], None)["rows"]
        self.assertEqual([row["match_id"] for row in rows], ["valid-late", "valid-early"])


if __name__ == "__main__":
    unittest.main()
