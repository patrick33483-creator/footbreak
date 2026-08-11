import unittest
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch

SYSTEM = Path(__file__).resolve().parents[1]
if str(SYSTEM) not in sys.path:
    sys.path.insert(0, str(SYSTEM))

import run_predict
import staking


class FailClosedPredictionTest(unittest.TestCase):
    def test_failed_t5_prediction_is_a_terminal_no_bet_decision(self):
        match = {
            "id": "5001",
            "homeTeam": {"name_ch": "主隊", "name_en": "Home"},
            "awayTeam": {"name_ch": "客隊", "name_en": "Away"},
            "tournament": {"nameCH": "測試聯賽"},
        }
        kickoff = datetime(2026, 8, 9, 14, 0, tzinfo=timezone.utc)
        with patch.object(run_predict.H, "parse_kickoff", return_value=kickoff), \
             patch.object(run_predict, "hk_odds_fingerprint", return_value={"HDC": []}):
            result = run_predict.failed_prediction(
                match, "T-5", 4.5, "PinnAPI 賽事配對失敗，T-5 資料不足，最終決定不下注"
            )
        self.assertTrue(result["can_bet"])
        self.assertIsNone(result["pick"])
        self.assertEqual(result["conviction"], 0.0)
        self.assertIn("最終決定不下注", result["no_bet_reason"])
        self.assertEqual(result["stage"], "T-5")

    def test_missing_pinnapi_fixture_uses_hkjc_full_market_model(self):
        match = {
            "id": "5002",
            "homeTeam": {"name_ch": "主隊", "name_en": "Home"},
            "awayTeam": {"name_ch": "客隊", "name_en": "Away"},
            "tournament": {"nameCH": "測試聯賽"},
            "foPools": [],
        }
        kickoff = datetime.now(timezone.utc) + timedelta(minutes=5)
        hk = {
            "HAD": [{"condition": None, "odds": {"H": 2.4, "D": 3.2, "A": 2.8}}],
            "HDC": [],
            "HIL": [],
            "CHL": [],
        }
        view = {
            "lh": 1.4, "la": 1.1, "rho": -0.03, "rmse": 0.04, "n": 3,
            "total": 2.5, "supremacy": 0.3,
        }
        matrix = [[0.0] * 13 for _ in range(13)]
        matrix[1][0] = 1.0
        with patch.object(run_predict.H, "parse_kickoff", return_value=kickoff), \
             patch.object(run_predict.H, "flatten_odds", return_value=hk), \
             patch.object(run_predict.P, "fit_view", return_value=view), \
             patch.object(run_predict.P, "apply",
                          return_value=(1.4, 1.1, None, {})), \
             patch.object(run_predict.P, "outcome_probs",
                          return_value=(matrix, [], None, 1.0, 0.0, 0.0)), \
             patch.object(run_predict.M, "evaluate", return_value=[]), \
             patch.object(run_predict, "hk_odds_fingerprint", return_value={}), \
             patch.object(run_predict, "conviction", return_value=60.0):
            result = run_predict.analyse_match(match, None, stage_override="T-5")
        self.assertNotIn("skip", result)
        self.assertEqual(result["model_source"], "hkjc_full_market")
        self.assertFalse(result["sharp_reference_available"])
        self.assertIsNone(result["fixture_id"])
        self.assertIsNotNone(result["final"])

    def test_hkjc_only_model_never_creates_a_simulation(self):
        result = {
            "conviction": 65.0,
            "model_source": "hkjc_full_market",
            "sharp_reference_available": False,
            "candidates": [{
                "market": "入球大小", "code": "HIL", "condition": "2.5",
                "side": "H", "label": "大 2.5", "odds": 2.0,
                "fair": 1.94, "prob": 0.515, "push": 0.0,
                "ev": 0.03, "kelly_raw": 0.03, "is_main": True,
            }],
        }
        pick, reason = run_predict.pick_one(result)
        self.assertIsNone(pick)
        self.assertIn("禁止由馬會自身盤面建立模擬注", reason)

    def test_market_policy_tightens_after_thirty_bad_settlements(self):
        bets = [{
            "status": "SETTLED", "code": "CHL", "stake": 100,
            "pnl": -100, "result": "Lost", "model_prob": 0.60,
        } for _ in range(30)]
        policy = staking.market_entry_thresholds({"bets": bets})["CHL"]
        self.assertEqual(policy["min_edge"], 0.035)
        self.assertEqual(policy["confidence_floor"], 62.0)
        self.assertEqual(policy["reason"], "severe_market_underperformance")

    def test_sharp_candidate_uses_its_market_dynamic_threshold(self):
        result = {
            "conviction": 60.0,
            "model_source": "pinnapi",
            "sharp_reference_available": True,
            "candidates": [{
                "market": "入球大小", "code": "HIL", "condition": "2.5",
                "side": "H", "label": "大 2.5", "odds": 2.0,
                "fair": 1.90, "prob": 0.53, "push": 0.0,
                "ev": 0.06, "kelly_raw": 0.06, "is_main": True,
            }],
        }
        staged = {
            "fraction": 1 / 3, "cap": 0.04, "level": 1,
            "label": "測試", "market_mult": {}, "n_settled": 30,
            "slope": None,
            "entry_thresholds": {
                "HIL": {
                    "min_edge": 0.04, "confidence_floor": 62.0,
                    "n_settled": 30, "reason": "severe_market_underperformance",
                },
            },
        }
        with patch.object(run_predict.K, "stage", return_value=staged):
            pick, reason = run_predict.pick_one(result)
        self.assertIsNone(pick)
        self.assertIn("信念 60.0/62", reason)


if __name__ == "__main__":
    unittest.main()
