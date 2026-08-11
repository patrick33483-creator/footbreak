"""Footbreak confidence-only shadow portfolio regression tests."""
from __future__ import annotations

import json
import sys
import tempfile
import unittest
from datetime import datetime, timedelta
from pathlib import Path
from unittest.mock import patch

SYSTEM = Path(__file__).resolve().parents[1]
ROOT = SYSTEM.parent
if str(SYSTEM) not in sys.path:
    sys.path.insert(0, str(SYSTEM))

import gen_app_data
import record_picks
import settle


def candidate(*, code="HIL", label="大 2.5", probability=.64, push=0.0,
              main=True, odds=1.90):
    return {
        "market": "入球大小", "code": code, "condition": "2.5", "side": "H",
        "label": label, "prob": probability, "push": push, "odds": odds,
        "ev": -.08, "fair": 1.6, "kelly_used": .1, "is_main": main,
    }


class ShadowPortfolioTests(unittest.TestCase):
    def test_t5_no_benchmark_creates_fixed_shadow_only_and_is_idempotent(self):
        kickoff = datetime.now(record_picks.HKT) + timedelta(minutes=20)
        result = {
            "match_id": "shadow-fixture", "stage": "T-5",
            "kickoff_hkt": kickoff.strftime("%Y-%m-%d %H:%M"),
            "league": "測試聯賽", "home": "主隊", "away": "客隊",
            "conviction": record_picks.P.CONF_FLOOR + 1,
            "model_source": "hkjc_full_market",
            "sharp_reference_available": False, "candidates": [
                candidate(main=False, probability=.74, label="外圍"),
                candidate(main=True, probability=.61, label="主線"),
            ],
            "pick": None, "no_bet_reason": "無獨立 PinnAPI 同場基準",
            "can_bet": True, "weather": {}, "final": {}, "open": {}, "now": {},
            "movement": {}, "adjustments": [], "mults": {}, "outcome": {},
        }
        with tempfile.TemporaryDirectory() as directory:
            Path(directory, "predictions.json").write_text(
                json.dumps([result], ensure_ascii=False), encoding="utf-8"
            )
            ledger_path = Path(directory, "sim_ledger.json")
            with patch.object(record_picks, "HERE", directory), \
                 patch.object(record_picks, "LEDGER", str(ledger_path)):
                _, _, first = record_picks.sync()
                _, _, second = record_picks.sync()
        self.assertEqual(first["bets"], [])
        self.assertEqual(len(second["shadow_bets"]), 1)
        shadow = second["shadow_bets"][0]
        self.assertEqual(shadow["label"], "入球大小 主線")
        self.assertEqual(shadow["stake"], 1000.0)
        self.assertIsNone(shadow["ev"])
        self.assertIsNone(shadow["fair"])
        self.assertIsNone(shadow["kelly"])
        self.assertTrue(shadow["confidence_only"])
        self.assertTrue(shadow["shadow_only"])
        self.assertEqual(shadow["portfolio"], "shadow")
        self.assertEqual(second["shadow_stats"]["n_pending"], 1)
        self.assertEqual(second["shadow_stats"]["open_stake"], 1000.0)

    def test_shadow_stats_and_same_period_comparison_leave_official_stats_alone(self):
        ledger = {
            "bankroll": 50000, "watch": {}, "log": [],
            "bets": [
                {"status": "SETTLED", "stake": 100, "pnl": 20, "result": "Won",
                 "market": "讓球", "home": "甲", "away": "乙",
                 "created_at": "2026-08-10T10:00:00+08:00"},
                {"status": "SETTLED", "stake": 100, "pnl": -100, "result": "Lost",
                 "market": "讓球", "home": "甲", "away": "乙",
                 "created_at": "2026-08-11T10:00:00+08:00"},
            ],
            "shadow_bets": [
                {"status": "PENDING", "stake": 1000, "market": "入球大小",
                 "home": "甲", "away": "乙", "created_at": "2026-08-11T09:00:00+08:00"},
                {"status": "SETTLED", "stake": 1000, "pnl": 900, "result": "Won",
                 "market": "入球大小", "home": "甲", "away": "乙",
                 "created_at": "2026-08-11T10:00:00+08:00"},
            ],
        }
        official = settle.recompute(ledger)
        self.assertEqual(official["pnl"], -80)
        self.assertEqual(ledger["shadow_stats"]["pnl"], 900)
        self.assertEqual(ledger["shadow_stats"]["n_pending"], 1)
        self.assertEqual(ledger["shadow_stats"]["open_stake"], 1000)
        comparison = ledger["shadow_stats"]["comparison"]
        self.assertEqual(comparison["definition"], "from_first_shadow_bet")
        self.assertEqual(comparison["official_total_bets"], 1)
        self.assertEqual(comparison["shadow_total_bets"], 2)

    def test_shadow_settlement_uses_same_official_result_flow(self):
        old_kickoff = (datetime.now(record_picks.HKT) - timedelta(hours=4)).strftime("%Y-%m-%d %H:%M")
        ledger = {
            "bankroll": 50000, "bets": [], "watch": {}, "log": [], "stats": {},
            "shadow_bets": [{
                "bet_id": "shadow-1", "match_id": "5001", "home": "主隊", "away": "客隊",
                "kickoff": old_kickoff, "market": "入球大小", "code": "HIL",
                "condition": "2.5", "side": "H", "label": "入球大小 大 2.5",
                "odds": 2.0, "stake": 1000, "status": "PENDING", "portfolio": "shadow",
                "confidence_only": True, "shadow_only": True, "history": [],
            }],
        }
        result = {
            "goals_home": 2, "goals_away": 1, "goals_total": 3,
            "corners_home": None, "corners_away": None, "corners_total": None,
            "source": "hkjc_official_exact_id",
        }
        with tempfile.TemporaryDirectory() as directory:
            ledger_path = Path(directory, "sim_ledger.json")
            ledger_path.write_text(json.dumps(ledger), encoding="utf-8")
            with patch.object(settle, "LEDGER", str(ledger_path)), \
                 patch.object(settle, "fetch_hkjc_results", return_value={"5001": result}), \
                 patch.object(settle, "fetch_hkjc_statuses", return_value={}):
                settle.run(force=True)
            saved = json.loads(ledger_path.read_text(encoding="utf-8"))
        bet = saved["shadow_bets"][0]
        self.assertEqual(bet["status"], "SETTLED")
        self.assertEqual(bet["result"], "Won")
        self.assertEqual(bet["pnl"], 1000)
        self.assertEqual(saved["stats"]["n_settled"], 0)
        self.assertEqual(saved["shadow_stats"]["n_settled"], 1)
        self.assertIn("影子結算", saved["log"][0]["changes"][0])

    def test_dashboard_payload_keeps_history_official_only_and_exposes_shadow(self):
        watch = {
            "m1": {"match_id": "m1", "home": "主", "away": "客", "league": "測試",
                   "kickoff": "2026-08-10 20:00", "stages": [{
                       "prediction_era": record_picks.PREDICTION_ERA, "stage": "T-5",
                       "market_predictions": [{
                           "code": "HIL", "condition": "2.5", "side": "H",
                           "label": "大 2.5", "probability": .64,
                       }], "conviction": 60,
                   }]}
        }
        history = gen_app_data.build_prediction_history(
            watch, [], None
        )
        self.assertFalse(history["rows"][0]["simulated_bet"])
        source = (SYSTEM / "gen_app_data.py").read_text(encoding="utf-8")
        self.assertIn('"shadow_bets": sorted(shadow_bets', source)
        self.assertIn('"shadow_stats": led.get("shadow_stats")', source)


class ShadowDashboardSourceTests(unittest.TestCase):
    def test_shadow_page_and_stage_filters_are_present_on_both_dashboards(self):
        foot_index = (ROOT / "hkjc-dashboard" / "index.html").read_text(encoding="utf-8")
        foot_app = (ROOT / "hkjc-dashboard" / "app.js").read_text(encoding="utf-8")
        self.assertIn('data-view="shadow">影子倉', foot_index)
        self.assertIn('id="viewShadow"', foot_index)
        self.assertIn("function renderShadow()", foot_app)
        self.assertIn("confidence-only，固定 2%", foot_app)
        for dashboard in (ROOT / "hkjc-dashboard", ROOT / "crown/dashboard"):
            app = (dashboard / "app.js").read_text(encoding="utf-8")
            css = (dashboard / "styles.css").read_text(encoding="utf-8")
            self.assertIn("let HISTORY_STAGE = 'all'", app)
            self.assertIn("data-history-stage", app)
            self.assertIn("HISTORY_STAGE === 'all'", app)
            self.assertIn("history-stage-filter", css)
            self.assertIn("min-height: 44px", css)


if __name__ == "__main__":
    unittest.main()
