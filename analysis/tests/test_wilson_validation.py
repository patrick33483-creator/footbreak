"""Focused invariants for the isolated Wilson simulation portfolios."""
from __future__ import annotations

import copy
import json
import tempfile
import unittest
import subprocess
from pathlib import Path

from analysis.wilson_validation import (
    DECISION_STAGE, EDGE_BUFFER, FIXED_STAKE, FIXTURE_STAKE_CAP, MIN_DECIDED,
    STARTING_BANKROLL, admission_arithmetic, choose_admission, commit_bet,
    ensure_namespace, portfolio_name, recompute_namespace, wilson95,
)
from analysis.migrate_wilson_strategy import migrate_file
from analysis.wilson_portfolio import _native_t5, _selected


def candidate(market="HDC", side="H", line=-0.25, *, hits=41, decided=59, key="example"):
    return {
        "market": market, "selected_side": side, "selected_line": line,
        "key": ["system=footbreak", f"market={market}", key],
        "path": "首預→T-30→T-5", "direction": "主讓→主讓→主讓",
        "role": "主讓", "line_bucket": "0.25–0.5", "odds_tier": "≥1.70",
        "movement": "不變", "total": {"hits": hits, "decided": decided, "pushes": 0},
        "label": "HDC，首預→T-30→T-5 all 主讓，主隊讓0.25–0.5，T-5 odds >=1.70，方向不變",
        "source_artifact": {"hash": "frozen-artifact", "version": "v7", "as_of": "2026-08-19T22:55:00+08:00"},
    }


def selected(market="HDC", side="H", line=-0.25, odds=1.90):
    return {"market": market, "side": side, "line": line, "odds": odds}


class WilsonAdmissionTest(unittest.TestCase):
    def test_worked_example_formula_and_raw_pass(self):
        arithmetic = admission_arithmetic(41, 59, 1.90)
        self.assertIsNotNone(arithmetic)
        assert arithmetic is not None
        self.assertAlmostEqual(arithmetic["hit_rate_raw"], 41 / 59)
        self.assertAlmostEqual(arithmetic["break_even_rate_raw"], 1 / 1.90)
        self.assertAlmostEqual(arithmetic["required_rate_raw"], 1 / 1.90 + .03)
        self.assertAlmostEqual(
            arithmetic["minimum_acceptable_odds_raw"],
            1 / (arithmetic["wilson95_lower_raw"] - EDGE_BUFFER),
        )
        self.assertTrue(arithmetic["passes"])

    def test_boundary_equality_passes_and_below_fails(self):
        lower = wilson95(41, 59)[0]
        boundary_odds = 1 / (lower - EDGE_BUFFER)
        at_boundary = admission_arithmetic(41, 59, boundary_odds)
        below = admission_arithmetic(41, 59, boundary_odds - 1e-8)
        self.assertTrue(at_boundary["passes"])
        self.assertFalse(below["passes"])

    def test_low_actual_odds_fail_without_discarding_frozen_evidence(self):
        """A high historical hit rate is not enough at an overpriced quote."""
        frozen = candidate()
        before = copy.deepcopy(frozen)
        minimum = admission_arithmetic(41, 59, 1.90)["minimum_acceptable_odds_raw"]
        decision, reason = choose_admission(
            "footbreak", "HDC",
            selected(odds=minimum - 0.01),
            [frozen], stage_at="2026-08-19T22:55:00+08:00",
        )
        self.assertIsNone(decision)
        self.assertEqual(reason, "wilson_gate_not_passed")
        # A quote rejection does not mutate or erase the frozen discovery
        # evidence. A later candidate can use the same historical snapshot;
        # it is never recomputed from prospective portfolio results.
        self.assertEqual(frozen, before)
        later, later_reason = choose_admission(
            "footbreak", "HDC", selected(odds=1.90), [frozen],
            stage_at="2026-08-19T22:55:00+08:00",
        )
        self.assertIsNotNone(later)
        self.assertEqual(later_reason, "wilson_pass")

    def test_minimum_sample_invalid_odds_and_lower_guard(self):
        selected_row = selected()
        rejected, reason = choose_admission("footbreak", "HDC", selected_row,
                                            [candidate(decided=49, hits=40)], stage_at="t")
        self.assertIsNone(rejected)
        self.assertEqual(reason, "no_frozen_historical_condition")
        accepted, reason = choose_admission("footbreak", "HDC", selected_row,
                                            [candidate(decided=50, hits=41)], stage_at="t")
        self.assertIsNotNone(accepted)
        self.assertEqual(reason, "wilson_pass")
        self.assertIsNone(admission_arithmetic(1, 50, 1.9)["minimum_acceptable_odds_raw"])
        self.assertIsNone(admission_arithmetic(41, 59, 1.0))
        self.assertIsNone(admission_arithmetic(41, 59, "NaN"))

    def test_duplicate_and_conflicting_historical_evidence_fail_closed(self):
        first, second = candidate(), candidate(hits=42)
        decision, reason = choose_admission("footbreak", "HDC", selected(), [first, second], stage_at="t")
        self.assertIsNone(decision)
        self.assertEqual(reason, "wilson_gate_not_passed")
        rows = candidate()
        rows["fixture_markets"] = [
            {"fixture_market_id": "x", "won": True},
            {"fixture_market_id": "x", "won": False},
        ]
        decision, reason = choose_admission("footbreak", "HDC", selected(), [rows], stage_at="t")
        self.assertIsNone(decision)
        self.assertEqual(reason, "no_frozen_historical_condition")


class WilsonPortfolioTest(unittest.TestCase):
    def _admission(self, market, side, line):
        output, reason = choose_admission("footbreak", market, selected(market, side, line), [
            candidate(market, side, line, key=market)
        ], stage_at="2026-08-19T22:55:00+08:00")
        self.assertEqual(reason, "wilson_pass")
        return output

    def _watch(self):
        return {"match_id": "fixture-1", "league": "測試聯賽", "home": "主隊", "away": "客隊",
                "kickoff": "2026-08-20T01:00:00+08:00"}

    def test_isolated_cutover_archive_and_frozen_prospective_stats(self):
        legacy = {"bet_id": "old", "portfolio": "footbreak_independent_validation",
                  "strategy": "independent-validation-v1", "status": "PENDING", "stake": 250}
        ledger = {"bets": [legacy], "independent_validation": {"stats": {"starting_bankroll": 10000}}}
        ns = ensure_namespace(ledger, "footbreak", now="2026-08-19T23:00:00+08:00")
        self.assertEqual(ns["activation_at"], "2026-08-19T23:00:00+08:00")
        self.assertTrue(ns["retired_v1"]["new_entries_disabled"])
        self.assertEqual(ns["retired_v1"]["legacy_bet_count"], 1)
        self.assertEqual(portfolio_name("footbreak"), "footbreak_wilson_test")
        self.assertEqual(portfolio_name("crown"), "crown_wilson_test")
        admission = self._admission("HDC", "H", -.25)
        bet = commit_bet(ledger, "footbreak", self._watch(), "HDC", selected(), admission,
                         now="2026-08-19T23:01:00+08:00", market_label="讓球",
                         selected_label="讓球 · 主讓 -0.25", selected_role="主讓", selected_line=-.25)
        ledger["bets"].append(bet)
        frozen_before = copy.deepcopy(bet["frozen_historical_evidence"])
        bet.update({"status": "SETTLED", "result": "Won", "pnl": 450})
        recompute_namespace(ledger, "footbreak")
        self.assertEqual(bet["frozen_historical_evidence"], frozen_before)
        self.assertEqual(ledger["wilson_validation"]["stats"]["starting_bankroll"], STARTING_BANKROLL)
        self.assertEqual(ledger["wilson_validation"]["stats"]["hits"], 1)
        self.assertEqual(ledger["wilson_validation"]["stats"]["decided"], 1)

    def test_one_per_market_and_three_market_fixture_cap(self):
        ledger = {"bets": []}
        watch = self._watch()
        choices = [("HDC", "H", -.25), ("HIL", "H", 2.5), ("CHL", "L", 9.5)]
        for market, side, line in choices:
            bet = commit_bet(ledger, "footbreak", watch, market, selected(market, side, line),
                             self._admission(market, side, line), now="2026-08-19T23:01:00+08:00",
                             market_label=market, selected_label=market, selected_role=side, selected_line=line)
            self.assertIsNotNone(bet)
            ledger["bets"].append(bet)
        self.assertEqual(sum(row["stake"] for row in ledger["bets"]), FIXTURE_STAKE_CAP)
        self.assertEqual(len(ledger["bets"]), 3)
        self.assertIsNone(commit_bet(ledger, "footbreak", watch, "HDC", selected("HDC", "A", .25),
                                     self._admission("HDC", "H", -.25), now="x",
                                     market_label="讓球", selected_label="客受讓", selected_role="客受讓", selected_line=.25))
        self.assertTrue(all(row["stake"] == FIXED_STAKE for row in ledger["bets"]))
        self.assertEqual(MIN_DECIDED, 50)

    def test_file_migration_is_idempotent_and_non_destructive(self):
        original = {"bets": [{"bet_id": "old", "portfolio": "crown_independent_validation",
                              "strategy": "independent-validation-v1", "status": "PENDING"}]}
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory, "ledger.json")
            path.write_text(json.dumps(original), encoding="utf-8")
            first = migrate_file(path, "crown")
            second = migrate_file(path, "crown")
        self.assertEqual(first["bets"], original["bets"])
        self.assertEqual(second["bets"], original["bets"])
        self.assertEqual(first["wilson_validation"]["activation_at"], second["wilson_validation"]["activation_at"])

    def test_native_t5_and_quote_provenance_gates(self):
        from system.condition_portfolio import parse_time
        kickoff = "2099-01-01T20:00:00+08:00"
        stage = {
            "stage": "T-5", "ts": "2099-01-01T19:55:00+08:00", "kickoff": kickoff,
            "market_predictions": [{"code": "HDC", "side": "H", "line": -.25, "odds": 1.9,
                                    "quote_source": "provider", "observed_at": "2099-01-01T19:54:00+08:00"}],
        }
        watch = {"stages": [stage], "kickoff": kickoff}
        self.assertTrue(_native_t5(watch, stage, parse_time))
        self.assertIsNotNone(_selected(stage, "HDC", parse_time)[0])
        late = copy.deepcopy(stage)
        late["market_predictions"][0]["observed_at"] = kickoff
        self.assertEqual(_selected(late, "HDC", parse_time)[1], "selected_quote_not_provably_pre_kickoff")
        replay = copy.deepcopy(stage)
        replay["post_hoc_backfill"] = True
        self.assertFalse(_native_t5({"stages": [replay], "kickoff": kickoff}, replay, parse_time))
        self.assertFalse(_native_t5({"stages": [stage, copy.deepcopy(stage)], "kickoff": kickoff}, stage, parse_time))

    def test_dashboard_contracts_and_radar_are_not_in_strategy_files(self):
        root = Path(__file__).resolve().parents[2]
        for path in (root / "hkjc-dashboard" / "app.js", root / "crown" / "dashboard" / "app.js"):
            source = path.read_text(encoding="utf-8")
            self.assertIn("Wilson 測試攻略", source)
            self.assertIn("最低可接受賠率", source)
            self.assertIn("已封存／退役 previous strategy", source)
        changed = subprocess.check_output(
            ["git", "diff", "--name-only"], cwd=root, text=True,
        ).lower()
        self.assertNotIn("radar", changed)


if __name__ == "__main__":
    unittest.main()
