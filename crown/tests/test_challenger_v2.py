from __future__ import annotations

import copy
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

from crown import challenger_v2 as v2
from crown.dashboard_data import _public_ledger

HKT = timezone(timedelta(hours=8))


def watch() -> tuple[dict, dict]:
    kickoff = datetime(2026, 8, 20, 20, tzinfo=HKT)
    stage = {
        "stage": "T-5", "ts": (kickoff - timedelta(minutes=5)).isoformat(),
        "kickoff_hkt": kickoff.isoformat(),
        "market_predictions": [
            {"code": "HIL", "side": "H", "line": 2.5, "odds": 1.85,
             "probability": .58, "quote_source": "titan007-crown-id-3",
             "observed_at": (kickoff - timedelta(minutes=6)).isoformat()},
            {"code": "HDC", "side": "H", "line": -.25, "odds": 1.95,
             "probability": .56, "quote_source": "titan007-crown-id-3",
             "observed_at": (kickoff - timedelta(minutes=6)).isoformat()},
        ],
    }
    return {
        "match_id": "v2-future", "league": "測試聯賽", "home": "主隊", "away": "客隊",
        "kickoff": kickoff.isoformat(), "stages": [stage],
    }, stage


class CrownV2ChallengerTests(unittest.TestCase):
    def test_cutover_v1_isolation_and_idempotency(self):
        ledger = {
            "bets": [{
                "bet_id": "v1", "portfolio": "crown_independent_validation",
                "strategy": "independent-validation-v1", "status": "SETTLED",
                "stake": 250, "odds": 1.8, "pnl": -250, "result": "Lost",
            }],
            "stats": {"pnl": -250, "roi": -1},
        }
        original = copy.deepcopy(ledger)
        fixture, stage = watch()
        made, audit = v2.evaluate_new_t5(ledger, fixture, stage)
        self.assertEqual(len(made), 4)  # two permitted markets × ablation
        self.assertEqual(ledger["bets"], original["bets"])
        self.assertEqual(ledger["stats"], original["stats"])
        self.assertTrue(ledger[v2.NAMESPACE]["v1_frozen_benchmark"]["read_only"])
        self.assertEqual(
            ledger[v2.NAMESPACE]["v1_frozen_benchmark"]["benchmark_snapshot_at_activation"],
            ledger[v2.NAMESPACE]["activation_at"],
        )
        self.assertTrue(all(row["research_only"] for row in made))
        self.assertTrue(all(not row["actionable_telegram"] for row in made))
        frozen = copy.deepcopy(ledger[v2.NAMESPACE]["v1_frozen_benchmark"])
        # A later raw-ledger change must not rewrite the cutover benchmark.
        ledger["bets"].append({"bet_id": "later-v1", "strategy": "independent-validation-v1"})
        self.assertEqual(v2.ensure_namespace(ledger)["v1_frozen_benchmark"], frozen)
        dashboard_ledger, _ = _public_ledger(ledger)
        self.assertNotIn(v2.NAMESPACE, dashboard_ledger)
        repeat, repeated_audit = v2.evaluate_new_t5(ledger, fixture, stage)
        self.assertEqual(repeat, [])
        self.assertTrue(all(row["reason"] == "v2_idempotent_existing_research_row" for row in repeated_audit))

    def test_candidate_lanes_native_provenance_and_cutover_fail_closed(self):
        fixture, stage = watch()
        ledger = {"bets": []}
        stage["market_predictions"][1]["odds"] = 1.85
        made, audit = v2.evaluate_new_t5(ledger, fixture, stage)
        self.assertEqual(len(made), 2)
        self.assertIn("v2_hdc_1_80_1_89_explicitly_ineligible", {row["reason"] for row in audit})

        old_fixture, old_stage = watch()
        old_stage["ts"] = "2026-08-19T19:59:00+08:00"
        made, audit = v2.evaluate_new_t5({"bets": []}, old_fixture, old_stage)
        self.assertEqual(made, [])
        self.assertEqual(
            audit[0]["reason"],
            "v2_policy_or_activation_or_native_t5_not_eligible",
        )

        bad_fixture, bad_stage = watch()
        bad_stage["market_predictions"][0].pop("quote_source")
        made, audit = v2.evaluate_new_t5({"bets": []}, bad_fixture, bad_stage)
        self.assertEqual(len(made), 2)  # HDC lane survives; HIL fails closed.
        self.assertIn("missing_quote_provenance", {row["reason"] for row in audit})

    def test_activation_boundary_rejects_prior_native_stage_then_accepts_later_one(self):
        fixture, stage = watch()
        before_activation = {"bets": []}
        v2.ensure_namespace(before_activation, now="2026-08-20T19:56:00+08:00")
        # This is a native T-5 after the immutable policy floor but before the
        # namespace was first activated, so it cannot become prospective data.
        made, audit = v2.evaluate_new_t5(before_activation, fixture, stage)
        self.assertEqual(made, [])
        self.assertEqual(
            audit[0]["reason"],
            "v2_policy_or_activation_or_native_t5_not_eligible",
        )

        after_activation = {"bets": []}
        v2.ensure_namespace(after_activation, now="2026-08-20T19:50:00+08:00")
        made, _ = v2.evaluate_new_t5(after_activation, fixture, stage)
        self.assertEqual(len(made), 4)

    def test_activation_boundary_is_immutable_across_reload(self):
        ledger = {"bets": []}
        first = v2.ensure_namespace(ledger, now="2026-08-20T19:50:00+08:00")
        activation = first["activation_at"]
        reloaded = copy.deepcopy(ledger)
        later = v2.ensure_namespace(reloaded, now="2026-08-20T20:30:00+08:00")
        self.assertEqual(later["activation_at"], activation)
        self.assertEqual(
            later["v1_frozen_benchmark"]["benchmark_snapshot_at_activation"],
            activation,
        )

    def test_league_shrinkage_ablation_unique_fixture_and_promotion_blocked(self):
        fixture, stage = watch()
        ledger = {"bets": []}
        namespace = v2.ensure_namespace(ledger)
        namespace["league_effect"] = {
            "status": "frozen_pre_cutover_ready",
            "frozen_at": "2026-08-19T19:00:00+08:00",
            "markets": {"HIL": {
                "global_probability": .50,
                "leagues": {"測試聯賽": {"probability": .70, "fixtures": 3}},
            }},
        }
        made, _ = v2.evaluate_new_t5(ledger, fixture, stage)
        no_league = next(row for row in made if row["market"] == "HIL" and row["variant"] == "no_league")
        pooled = next(row for row in made if row["market"] == "HIL" and row["variant"] == "league_shrunk")
        self.assertEqual(no_league["probability"], .58)
        self.assertGreater(pooled["probability"], .58)
        self.assertLess(pooled["probability"], .70)

        for row in namespace["research_bets"]:
            row.update({"status": "SETTLED", "result": "Won", "pnl": row["stake"] * (row["odds"] - 1)})
        report = v2.recompute(namespace, ledger)
        market = report["by_market"]["HIL"]
        self.assertIsNotNone(next(row for row in namespace["research_bets"] if row["market"] == "HIL")["brier"])
        self.assertIsNotNone(next(row for row in namespace["research_bets"] if row["market"] == "HIL")["calibration_bucket"])
        self.assertEqual(market["no_league_ablation"]["unique_fixtures"], 1)
        self.assertFalse(market["promotion"]["promotion_review_eligible"])
        self.assertIn("unique_fixture_sample_below_100", market["promotion"]["reasons"])
        self.assertIn("v1_champion_probability_metrics_unavailable", market["promotion"]["reasons"])
        self.assertTrue(all(not row["promotion_gate_use"] for row in report["league_odds_market"]))

    def test_chinese_dashboard_contract_and_no_transport_dependency(self):
        report = v2.recompute(v2.ensure_namespace({"bets": []}), {"bets": []})
        self.assertEqual(report["title"], "v2挑戰者研究中")
        self.assertEqual(report["subtitle"], "非正式推介")
        self.assertFalse(report["actionable_telegram_enabled"])
        source = (__import__("pathlib").Path(__file__).resolve().parents[1] / "challenger_v2.py").read_text(encoding="utf-8")
        self.assertNotIn("notify", source)
        self.assertNotIn("sendMessage", source)
        dashboard = (Path(__file__).resolve().parents[1] / "dashboard" / "app.js").read_text(encoding="utf-8")
        self.assertIn("v2挑戰者研究中／非正式推介", dashboard)
        self.assertIn("同市場雙邊、同盤口、同觀測時間及同來源", dashboard)
        self.assertIn("CLV 覆蓋", dashboard)

    def test_no_vig_requires_same_two_sided_quote_and_clv_never_uses_post_kickoff(self):
        fixture, stage = watch()
        selected = stage["market_predictions"][0]
        stage["two_sided_quotes"] = [
            selected | {"fixture_id": fixture["match_id"]},
            {
                "fixture_id": fixture["match_id"], "code": "HIL", "side": "L",
                "line": 2.5, "odds": 2.02, "quote_source": selected["quote_source"],
                "observed_at": selected["observed_at"],
            },
        ]
        # A closing quote after kickoff is evidence-free, not a substitute.
        stage["closing_quotes"] = [{
            "code": "HIL", "side": "H", "line": 2.5, "odds": 1.80,
            "quote_source": selected["quote_source"], "observed_at": stage["kickoff_hkt"],
        }]
        ledger = {"bets": []}
        made, _ = v2.evaluate_new_t5(ledger, fixture, stage)
        hil = next(row for row in made if row["market"] == "HIL" and row["variant"] == "no_league")
        self.assertTrue(hil["market_implied_available"])
        self.assertIsNone(hil["closing_line_value"])
        self.assertFalse(hil["clv_available"])
        self.assertEqual(hil["clv_reason"], "same_market_side_line_pre_kickoff_closing_quote_unavailable")
        # A mismatched observed timestamp makes the two-sided baseline unavailable.
        stage["two_sided_quotes"][1]["observed_at"] = "2026-08-20T19:55:00+08:00"
        second = {"bets": []}
        made, _ = v2.evaluate_new_t5(second, fixture, stage)
        self.assertFalse(next(row for row in made if row["market"] == "HIL")["market_implied_available"])


if __name__ == "__main__":
    unittest.main()
