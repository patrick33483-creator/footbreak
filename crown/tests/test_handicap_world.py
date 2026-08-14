from __future__ import annotations

import copy
import tempfile
import unittest
from contextlib import nullcontext
from dataclasses import replace
from datetime import datetime
from pathlib import Path
from unittest.mock import patch

from crown.config import settings
from crown.dashboard_api import perform_handicap_world_settlement
from crown.handicap_world import (
    FIXED_STAKE,
    KELLY_CAP_PCT,
    PORTFOLIO,
    STARTING_BANKROLL,
    ensure_state,
    record_new_t5,
)
from crown.ledger import recompute_stats, sync_prediction
from crown.settle import _settle, settle_due


KICKOFF = "2099-08-12T20:00:00+08:00"
STAGE_TIMES = {
    "首預": "2099-08-12T18:00:00+08:00",
    "T-30": "2099-08-12T19:30:00+08:00",
    "T-5": "2099-08-12T19:55:00+08:00",
}


def market(
    *, side: str = "H", line: float = -0.25, odds: object = 1.70,
    probability: object = 0.60, source: str = "pinnapi_exact_full_match",
    probability_observed_at: object = "2099-08-12T19:54:00+08:00",
) -> dict:
    return {
        "code": "HDC", "market": "HDC", "side": side, "line": line,
        "condition": f"{line:g}", "odds": odds, "probability": probability,
        "probability_source": source,
        "probability_observed_at": probability_observed_at,
    }


def stage(name: str, quote: dict | None = None) -> dict:
    return {
        "stage": name, "match_id": "fixture-1", "kickoff_hkt": KICKOFF,
        "home": "Alpha", "away": "Beta", "league": "Test League",
        "ts": STAGE_TIMES[name], "market_predictions": [quote or market()],
    }


def watch(quotes: tuple[dict, dict, dict] | None = None) -> dict:
    quotes = quotes or (market(), market(), market())
    return {
        "match_id": "fixture-1", "kickoff": KICKOFF, "kickoff_hkt": KICKOFF,
        "home": "Alpha", "away": "Beta", "league": "Test League",
        "titan_match_id": "fixture-1", "stages": [
            stage("首預", quotes[0]), stage("T-30", quotes[1]), stage("T-5", quotes[2]),
        ],
    }


def ledger() -> dict:
    return {"bankroll": 50000, "bets": [], "shadow_bets": [], "watch": {}, "log": []}


class HandicapWorldTests(unittest.TestCase):
    def test_boundary_eligibility_creates_two_independent_strategy_legs(self) -> None:
        data, fixture = ledger(), watch()
        data["watch"][fixture["match_id"]] = fixture
        created = record_new_t5(data, fixture)
        world = ensure_state(data)

        self.assertEqual(len(created), 2)
        self.assertEqual(len(world["signals"]), 1)
        self.assertEqual({bet["strategy"] for bet in world["bets"]}, {"fixed_stake", "conservative_kelly"})
        self.assertTrue(all(bet["portfolio"] == PORTFOLIO for bet in world["bets"]))
        self.assertEqual(next(b for b in world["bets"] if b["strategy"] == "fixed_stake")["stake"], FIXED_STAKE)
        kelly = next(b for b in world["bets"] if b["strategy"] == "conservative_kelly")
        self.assertLessEqual(kelly["stake"], STARTING_BANKROLL * KELLY_CAP_PCT)
        self.assertEqual(kelly["parent_signal_id"], world["signals"][0]["signal_id"])
        self.assertEqual(data["bets"], [])
        self.assertEqual(data["shadow_bets"], [])

    def test_odds_below_boundary_rejects_and_never_backfills(self) -> None:
        quotes = (market(), market(), market(odds=1.699))
        data, fixture = ledger(), watch(quotes)
        record_new_t5(data, fixture)
        world = ensure_state(data)
        self.assertEqual(world["bets"], [])
        self.assertEqual(world["signals"], [])
        self.assertEqual(world["audit"][-1]["reason"], "invalid_or_below_1_70_t5_selected_odds")

    def test_direction_line_identity_and_duplicate_stage_fail_closed(self) -> None:
        cases: list[tuple[str, dict]] = []
        cases.append(("direction", watch((market(), market(side="A", line=0.25), market()))))
        cases.append(("line", watch((market(), market(line=-0.5), market()))))
        identity = watch()
        identity["stages"][1]["away"] = "Other"
        cases.append(("identity", identity))
        duplicate = watch()
        duplicate["stages"].append(copy.deepcopy(duplicate["stages"][2]))
        cases.append(("duplicate", duplicate))
        for name, fixture in cases:
            with self.subTest(name=name):
                data = ledger()
                data["watch"][fixture["match_id"]] = fixture
                self.assertEqual(record_new_t5(data, fixture), [])
                self.assertEqual(ensure_state(data)["bets"], [])

    def test_circular_or_invalid_probability_skips_only_kelly_and_keeps_fixed(self) -> None:
        for label, quote in (
            ("circular", market(source="crown_full_market_no_vig")),
            ("invalid", market(probability=1.0)),
            ("missing", market(probability=None)),
        ):
            with self.subTest(label=label):
                data, fixture = ledger(), watch((market(), market(), quote))
                record_new_t5(data, fixture)
                world = ensure_state(data)
                self.assertEqual([bet["strategy"] for bet in world["bets"]], ["fixed_stake"])
                self.assertEqual(world["signals"][0]["kelly"]["status"], "SKIPPED")
                self.assertIn("independent", world["signals"][0]["kelly"]["reason"])

    def test_unproven_or_post_t5_independent_probability_skips_kelly(self) -> None:
        for label, probability_time in (
            ("missing", None),
            ("after_t5", "2099-08-12T19:56:00+08:00"),
            ("after_kickoff", "2099-08-12T20:01:00+08:00"),
        ):
            with self.subTest(label=label):
                data, fixture = ledger(), watch((
                    market(), market(), market(probability_observed_at=probability_time),
                ))
                record_new_t5(data, fixture)
                world = ensure_state(data)
                self.assertEqual([bet["strategy"] for bet in world["bets"]], ["fixed_stake"])
                self.assertEqual(world["signals"][0]["kelly"]["status"], "SKIPPED")
                self.assertIn("independent", world["signals"][0]["kelly"]["reason"])

    def test_replay_does_not_duplicate_signal_or_child_legs(self) -> None:
        data, fixture = ledger(), watch()
        first = record_new_t5(data, fixture)
        second = record_new_t5(data, fixture)
        world = ensure_state(data)
        self.assertEqual(len(first), 2)
        self.assertEqual(second, [])
        self.assertEqual(len(world["signals"]), 1)
        self.assertEqual(len(world["bets"]), 2)

    def test_existing_persisted_t5_is_never_backfilled(self) -> None:
        data, fixture = ledger(), watch()
        data["watch"][fixture["match_id"]] = fixture
        prediction = {
            "match_id": fixture["match_id"], "league": fixture["league"],
            "home": fixture["home"], "away": fixture["away"], "kickoff_hkt": KICKOFF,
            "stage": "T-5", "status": "PREDICTION_READY", "market_sources": {},
            "forecast_candidates": [{
                "code": "HDC", "market": "HDC", "side": "H", "line": -0.25,
                "condition": "-0.25", "odds": 1.70, "prob": .60,
                "reference": "pinnapi_exact_full_match",
                "observed_at": "2099-08-12T19:55:00+08:00",
                "probability_observed_at": "2099-08-12T19:54:00+08:00",
            }],
        }
        self.assertEqual(sync_prediction(data, prediction, settings()), [])
        world = ensure_state(data)
        self.assertEqual(world["signals"], [])
        self.assertEqual(world["bets"], [])

    def test_kelly_uses_only_prior_settled_results_for_its_own_equity(self) -> None:
        data, fixture = ledger(), watch()
        world = ensure_state(data)
        world["bets"].append({
            "strategy": "conservative_kelly", "status": "SETTLED", "pnl": -10_000,
            "settled_at": "2099-08-12T19:50:00+08:00",
        })
        # The fixed strategy's results never alter the Kelly strategy equity.
        world["bets"].append({
            "strategy": "fixed_stake", "status": "SETTLED", "pnl": 20_000,
            "settled_at": "2099-08-12T19:50:00+08:00",
        })
        # This later settlement is deliberately not visible to the T-5 entry.
        world["bets"].append({
            "strategy": "conservative_kelly", "status": "SETTLED", "pnl": 20_000,
            "settled_at": "2099-08-12T19:56:00+08:00",
        })
        record_new_t5(data, fixture)
        kelly = next(
            b for b in world["bets"]
            if b.get("strategy") == "conservative_kelly" and b.get("status") == "PENDING"
        )
        self.assertEqual(kelly["terms"]["kelly_equity_at_entry"], 40_000)
        self.assertLessEqual(kelly["stake"], 1_600)

    def test_independent_world_bankroll_stats_and_all_asian_quarter_outcomes(self) -> None:
        data = ledger()
        world = ensure_state(data)
        outcomes = [
            (-0.25, "H", 1, 0, "Won"),
            (-0.75, "H", 1, 0, "Half Won"),
            (0.0, "H", 1, 1, "Refunded"),
            (-0.25, "H", 1, 1, "Half Lost"),
            (-0.25, "H", 0, 1, "Lost"),
        ]
        for index, (line, side, home, away, expected) in enumerate(outcomes):
            bet = {
                "portfolio": PORTFOLIO, "code": "HDC", "condition": str(line),
                "side": side, "stake": 1000, "odds": 2.0, "status": "PENDING",
                "home": "A", "away": "B", "created_at": f"2099-08-01T00:0{index}:00+08:00",
            }
            self.assertTrue(_settle(bet, {"home_score": home, "away_score": away}, "test"))
            self.assertEqual(bet["result"], expected)
            self.assertEqual(bet["history"][-1]["action"], "讓球世界結算")
            world["bets"].append(bet)
        config = replace(settings(), bankroll=10_000)
        recompute_stats(data, config)
        stats = world["stats"]
        self.assertEqual(stats["n_settled"], 5)
        self.assertEqual(stats["res_counts"], {"Won": 1, "Half Won": 1, "Refunded": 1, "Half Lost": 1, "Lost": 1})
        self.assertEqual(stats["equity"], 50_000)
        self.assertEqual(data["stats"]["equity"], 10_000)
        self.assertGreater(stats["max_drawdown"], 0)
        self.assertEqual(stats["by_strategy"]["conservative_kelly"]["equity"], 50_000)
        self.assertEqual(stats["by_strategy"]["fixed_stake"]["equity"], 50_000)

    def test_settlement_due_counts_world_without_official_or_shadow_contamination(self) -> None:
        config = settings()
        data = ledger()
        world = ensure_state(data)
        world["bets"].append({
            "bet_id": "world-1", "portfolio": PORTFOLIO, "strategy": "fixed_stake",
            "match_id": "x", "titan_match_id": "x", "league": "L", "home": "A", "away": "B",
            "kickoff": "2020-01-01T12:00:00+08:00", "market": "皇冠讓球", "code": "HDC",
            "condition": "-0.25", "side": "H", "odds": 2.0, "stake": 1000, "status": "PENDING",
        })
        titan = [{"id": "x", "league": "L", "home": "A", "away": "B",
                  "kickoff": datetime.fromisoformat("2020-01-01T12:00:00+08:00"), "home_score": 1, "away_score": 0}]
        with patch("crown.settle.load_ledger", return_value=data), \
             patch("crown.settle._refresh_live", return_value={}), \
             patch("crown.settle.fetch_official_results", return_value={}), \
             patch("crown.settle.fetch_official_match_statuses", return_value={}), \
             patch("crown.settle.TitanClient.results", return_value=titan), \
             patch("crown.settle.save_ledger"):
            result = settle_due(config)
        self.assertEqual(result["settled"], 0)
        self.assertEqual(result["shadow_settled"], 0)
        self.assertEqual(result["handicap_world_settled"], 1)
        self.assertEqual(world["bets"][0]["result"], "Won")

    def test_world_only_settlement_does_not_touch_other_portfolios_or_learning(self) -> None:
        config = settings()
        data = ledger()
        data["bets"].append({"bet_id": "official", "status": "PENDING", "kickoff": "2020-01-01T12:00:00+08:00"})
        data["shadow_bets"].append({"bet_id": "shadow", "status": "PENDING", "kickoff": "2020-01-01T12:00:00+08:00"})
        world = ensure_state(data)
        world["bets"].append({
            "bet_id": "world-only", "portfolio": PORTFOLIO, "strategy": "fixed_stake",
            "match_id": "x", "titan_match_id": "x", "league": "L", "home": "A", "away": "B",
            "kickoff": "2020-01-01T12:00:00+08:00", "code": "HDC", "condition": "-0.25",
            "side": "H", "odds": 2.0, "stake": 1000, "status": "PENDING",
        })
        titan = [{"id": "x", "league": "L", "home": "A", "away": "B",
                  "kickoff": datetime.fromisoformat("2020-01-01T12:00:00+08:00"), "home_score": 1, "away_score": 0}]
        with patch("crown.settle.load_ledger", return_value=data), \
             patch("crown.settle._refresh_live", return_value={}), \
             patch("crown.settle.fetch_official_results", return_value={}), \
             patch("crown.settle.fetch_official_match_statuses", return_value={}), \
             patch("crown.settle.TitanClient.results", return_value=titan), \
             patch("crown.settle.save_ledger"):
            result = settle_due(config, handicap_world_only=True)
        self.assertEqual(result["handicap_world_settled"], 1)
        self.assertEqual(data["bets"][0]["status"], "PENDING")
        self.assertEqual(data["shadow_bets"][0]["status"], "PENDING")
        self.assertNotIn("stats", data)
        self.assertNotIn("shadow_stats", data)

    def test_world_only_dashboard_operation_skips_general_settlement_and_history(self) -> None:
        config = settings()
        with patch("crown.dashboard_api.state_lock", return_value=nullcontext()), \
             patch("crown.dashboard_api.settle_due", return_value={
                 "handicap_world_settled": 2, "handicap_world_voided": 1, "handicap_world_pending": 3,
             }) as settle, \
             patch("crown.dashboard_api.write_dashboard_data"), \
             patch("crown.dashboard_api.read_published_data", return_value={"ledger": {}}), \
             patch("crown.dashboard_api.run") as general_run, \
             patch("crown.dashboard_api.update_history") as history:
            response = perform_handicap_world_settlement(config)
        settle.assert_called_once_with(config, handicap_world_only=True)
        general_run.assert_not_called()
        history.assert_not_called()
        self.assertEqual(response["handicap_world_settled_count"], 2)
        self.assertEqual(response["handicap_world_voided_count"], 1)
        self.assertEqual(response["handicap_world_pending_count"], 3)

    def test_dashboard_and_mobile_contract_surface_policy_and_audit(self) -> None:
        root = Path(__file__).parents[1] / "dashboard"
        index = (root / "index.html").read_text(encoding="utf-8")
        app = (root / "app.js").read_text(encoding="utf-8")
        css = (root / "styles.css").read_text(encoding="utf-8")
        self.assertIn('data-view="handicapWorld"', index)
        self.assertIn('id="viewHandicapWorld"', index)
        self.assertIn("function renderHandicapWorld()", app)
        self.assertIn("function bindHandicapWorldSettlementButton()", app)
        self.assertIn("settle-handicap-world", app)
        self.assertIn("handicap-world-only", app)
        self.assertIn("credentials: 'same-origin'", app)
        self.assertIn("登入憑證已失效，請重新整理頁面並重新登入一次", app)
        self.assertIn("獨立勝率", app)
        self.assertIn("凱利未落注紀錄", app)
        self.assertIn("計算後沒有正期望值，凱利注碼為零", app)
        self.assertIn("勝率來自同一皇冠盤價，並非獨立資料", app)
        self.assertNotIn("canonical settlement", app)
        self.assertNotIn(">P&L <", app)
        self.assertIn("function handicapWorldStrategyComparison(s)", app)
        self.assertIn("不互相借用盈虧", app)
        self.assertIn("首預、T-30、T-5", app)
        self.assertIn("max_drawdown", app)
        self.assertIn(".world-policy-grid", css)
        self.assertIn(".world-policy-grid { grid-template-columns: 1fr; }", css)
        self.assertIn(".world-strategy-grid { grid-template-columns: 1fr; }", css)
        self.assertIn(".world-bets td[data-label]", css)


if __name__ == "__main__":
    unittest.main()
