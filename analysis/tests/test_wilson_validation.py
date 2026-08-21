"""Focused invariants for the isolated Wilson simulation portfolios."""
from __future__ import annotations

import copy
import json
import tempfile
import unittest
import subprocess
from pathlib import Path

from analysis.wilson_validation import (
    DECISION_STAGE, EDGE_BUFFER, FIXED_STAKE, FIXTURE_STAKE_CAP, MIN_DECIDED, STRATEGY,
    STARTING_BANKROLL, admission_arithmetic, choose_admission, commit_bet,
    apply_active_evidence, condition_number, ensure_namespace, freeze_condition,
    matching_admissions, portfolio_name, active_observations, project_granular_ranking_evidence,
    project_dashboard_research_matches, record_match_observation,
    recompute_namespace, wilson95,
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
    def test_kashiwa_style_discovery_matches_are_not_admissions(self):
        """Two research hits must not impersonate the native #8/#9 outcome."""
        rows = [
            {
                **candidate("HIL", "H", 2.75, key=f"kashiwa-{number}"),
                "condition_number": number,
                "condition_rank": number,
                "line_bucket": "2.75–3.0",
                "odds_tier": "≥1.70",
                "selected_odds": 1.84,
                "label": f"Japan HIL research condition {number}",
            }
            for number in (8, 9)
        ]
        projected = project_dashboard_research_matches(rows)
        self.assertEqual(len(projected), 2)
        for index, row in enumerate(projected, start=8):
            self.assertEqual(row["match_class"], "research_only")
            self.assertFalse(row["authoritative"])
            self.assertFalse(row["notification_eligible"])
            self.assertEqual(row["display_label"], "研究吻合／未納入正式 Wilson")
            self.assertEqual(row["research_rank"], index)
            self.assertNotIn("condition_number", row)
            self.assertEqual(row["research_identity"]["selected_line"], 2.75)
            self.assertEqual(row["research_identity"]["line_bucket"], "2.75–3.0")
            self.assertEqual(row["research_identity"]["odds_tier"], "≥1.70")

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

    def test_frozen_condition_numbers_ignore_candidate_and_dictionary_order(self):
        """Public numbers belong to frozen identities, never ranking/UI order."""
        ledger = {"bets": []}
        selected_row = selected()
        first = candidate(key="alpha")
        second = candidate(key="beta")
        admissions, reason = matching_admissions(
            "footbreak", "HDC", selected_row, [second, first],
            stage_at="2026-08-19T22:55:00+08:00",
        )
        self.assertEqual(reason, "wilson_pass")
        self.assertEqual(len(admissions), 2)
        by_key = {row["candidate"]["key"][-1]: row for row in admissions}
        frozen_first = freeze_condition(
            ledger, "footbreak", by_key["alpha"], now="2026-08-19T23:00:00+08:00",
        )
        frozen_second = freeze_condition(
            ledger, "footbreak", by_key["beta"], now="2026-08-19T23:01:00+08:00",
        )
        ns = ledger["wilson_validation"]
        first_signature = by_key["alpha"]["signature"]
        second_signature = by_key["beta"]["signature"]
        self.assertEqual(frozen_first["condition_number"], 1)
        self.assertEqual(frozen_second["condition_number"], 2)
        # Simulate a different dashboard/ranking iteration order.
        ns["conditions"] = dict(reversed(list(ns["conditions"].items())))
        self.assertEqual(condition_number(ns, first_signature), 1)
        self.assertEqual(condition_number(ns, second_signature), 2)
        self.assertEqual(freeze_condition(
            ledger, "footbreak", by_key["alpha"], now="2026-08-20T00:00:00+08:00",
        )["condition_number"], 1)

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


class WilsonBatchRolloverTest(unittest.TestCase):
    """Versioned evidence is intentionally tested independently of betting PnL."""

    def _admission(self, system="footbreak"):
        result, reason = choose_admission(
            system, "HDC", selected(), [candidate()],
            stage_at="2026-08-20T00:00:00+08:00",
        )
        self.assertEqual(reason, "wilson_pass")
        assert result is not None
        return result

    def _settled(
        self, ledger, index, *, result="Won", signature=None,
        stage_at=None, system="footbreak",
    ):
        stage_at = stage_at or f"2026-08-20T{index // 60:02d}:{index % 60:02d}:00+08:00"
        admission, reason = apply_active_evidence(
            ledger, system, self._admission(system), stage_at=stage_at, now=stage_at,
        )
        self.assertIsNone(reason)
        assert admission is not None
        if signature is not None:
            admission["signature"] = signature
        watch = {
            "match_id": f"fixture-{index}", "league": "測試", "home": "主",
            "away": "客", "kickoff": "2026-08-21T00:00:00+08:00",
        }
        row = commit_bet(
            ledger, system, watch, "HDC", selected(), admission, now=stage_at,
            market_label="讓球", selected_label="讓球", selected_role="主讓",
            selected_line=-.25,
        )
        self.assertIsNotNone(row)
        row.update({
            "status": "SETTLED", "result": result,
            "pnl": 450 if result in {"Won", "Half Won"} else -500,
            "settled_at": stage_at,
        })
        ledger["bets"].append(row)
        return row

    def _active(self, ledger):
        frozen = next(iter(ledger["wilson_validation"]["conditions"].values()))
        return frozen, frozen["active_evidence"]

    def test_nineteen_stay_pending_and_twenty_rolls_once(self):
        ledger = {"bets": []}
        for index in range(1, 20):
            self._settled(ledger, index, result="Won" if index % 2 else "Lost")
        recompute_namespace(ledger, "footbreak")
        frozen, active = self._active(ledger)
        self.assertEqual(active["version"], 1)
        self.assertEqual(frozen["pending_rollover_progress"]["display"], "19/20")
        self._settled(ledger, 20, result="Won")
        recompute_namespace(ledger, "footbreak")
        frozen, active = self._active(ledger)
        self.assertEqual(active["version"], 2)
        self.assertEqual(active["cumulative_decided"], 79)
        self.assertEqual(frozen["rollover_audit"][-1]["batch_decided"], 20)
        self.assertEqual(frozen["pending_rollover_progress"]["display"], "0/20")

    def test_low_odds_formal_observation_settles_into_evidence_not_pnl_for_both_systems(self):
        """A native condition match is evidence even when execution is no-bet."""
        for system in ("footbreak", "crown"):
            ledger = {"bets": []}
            stage_at = "2026-08-20T00:01:00+08:00"
            admission, reason = apply_active_evidence(
                ledger, system, self._admission(system), stage_at=stage_at, now=stage_at,
            )
            self.assertIsNone(reason)
            assert admission is not None
            # A price below this condition's Wilson minimum is explicitly
            # matched but cannot create a formal paper execution.
            admission["arithmetic"]["passes"] = False
            admission["arithmetic"]["actual_decimal_odds_raw"] = 1.20
            watch = {
                "match_id": f"{system}-low-odds", "league": "測試",
                "home": "主", "away": "客", "kickoff": "2026-08-21T00:00:00+08:00",
            }
            observation = record_match_observation(
                ledger, system, watch, "HDC", selected(odds=1.20), admission,
                now=stage_at, market_label="讓球", selected_role="主讓",
                selected_line=-.25,
            )
            self.assertIsNotNone(observation)
            assert observation is not None
            self.assertEqual(observation["status"], "PENDING")
            self.assertFalse(observation["formal_bet"])
            self.assertNotIn("stake", observation)
            self.assertEqual(ledger["bets"], [])
            # The native T-5 identity is idempotent: a retry cannot create a
            # duplicate evidence row, even when a counterpart execution later
            # succeeds on the same formal condition.
            self.assertIs(
                observation,
                record_match_observation(
                    ledger, system, watch, "HDC", selected(odds=1.20), admission,
                    now=stage_at, market_label="讓球", selected_role="主讓",
                    selected_line=-.25,
                ),
            )
            self.assertEqual(len(active_observations(ledger, system)), 1)
            # A research/non-frozen row cannot be upgraded by the same
            # settlement/recompute path.
            ensure_namespace(ledger, system)["observations"].append({
                "portfolio": f"{system}_wilson_observations", "strategy": STRATEGY,
                "formal_bet": False, "stage": DECISION_STAGE, "status": "PENDING",
                "match_id": "research-only",
            })
            self.assertEqual(len(active_observations(ledger, system)), 1)
            # This models the result workflow's verified official settlement;
            # it must affect the evidence denominator exactly once and never
            # add a PnL/stake row.
            observation.update({"status": "SETTLED", "result": "Won", "settled_at": "2026-08-21T02:00:00+08:00"})
            recompute_namespace(ledger, system)
            frozen, active = self._active(ledger)
            self.assertEqual(frozen["pending_rollover_progress"]["display"], "1/20")
            self.assertEqual(frozen["prospective"]["decided"], 1)
            self.assertEqual(frozen["prospective"]["pnl"], 0.0)
            self.assertEqual(frozen["prospective"]["turnover"], 0.0)
            self.assertEqual(ledger["wilson_validation"]["stats"]["pnl"], 0.0)
            self.assertEqual(ledger["wilson_validation"]["stats"]["turnover"], 0.0)
            # Recompute/retry remains idempotent.
            recompute_namespace(ledger, system)
            self.assertEqual(frozen["pending_rollover_progress"]["display"], "1/20")

    def test_forty_rolls_two_versions_and_twenty_six_leaves_six(self):
        ledger = {"bets": []}
        for index in range(1, 41):
            self._settled(ledger, index, result="Won")
        recompute_namespace(ledger, "footbreak")
        frozen, active = self._active(ledger)
        self.assertEqual(active["version"], 3)
        self.assertEqual(active["cumulative_decided"], 99)
        self.assertEqual(len(frozen["rollover_audit"]), 2)
        self.assertEqual(frozen["pending_rollover_progress"]["display"], "0/20")

        ledger = {"bets": []}
        for index in range(1, 27):
            self._settled(ledger, index, result="Won")
        recompute_namespace(ledger, "footbreak")
        frozen, active = self._active(ledger)
        self.assertEqual(active["version"], 2)
        self.assertEqual(active["cumulative_decided"], 79)
        self.assertEqual(frozen["pending_rollover_progress"]["display"], "6/20")

    def test_push_duplicate_conflict_and_activation_boundary_fail_closed(self):
        ledger = {"bets": []}
        for index in range(1, 19):
            self._settled(ledger, index)
        push = self._settled(ledger, 19, result="Refunded")
        # A duplicate fixture-market provenance hash is ambiguous, including
        # when its outcome conflicts. Neither copy is allowed in the batch.
        duplicate = self._settled(ledger, 20, result="Lost")
        duplicate["rollover_provenance"]["fixture_market_hash"] = (
            ledger["bets"][0]["rollover_provenance"]["fixture_market_hash"]
        )
        duplicate["result"] = "Won"
        recompute_namespace(ledger, "footbreak")
        frozen, active = self._active(ledger)
        self.assertEqual(active["version"], 1)
        self.assertEqual(frozen["pending_rollover_progress"]["display"], "17/20")
        self.assertGreater(
            frozen["pending_rollover_progress"]["excluded"]["not_binary_decided"], 0,
        )
        self.assertGreater(
            frozen["pending_rollover_progress"]["excluded"]["duplicate_or_conflicting_fixture_market"], 0,
        )
        # A row at the current activation boundary is not retrospectively
        # eligible, even if otherwise fully settled and provenance-complete.
        boundary = active["activation_boundary_at"]
        ledger["bets"][1]["rollover_provenance"]["stage_at"] = boundary
        recompute_namespace(ledger, "footbreak")
        self.assertGreater(
            frozen["pending_rollover_progress"]["excluded"]["before_snapshot_boundary"], 0,
        )

    def test_versions_are_immutable_and_recompute_is_idempotent(self):
        ledger = {"bets": []}
        for index in range(1, 21):
            self._settled(ledger, index)
        recompute_namespace(ledger, "footbreak")
        frozen, active = self._active(ledger)
        first = copy.deepcopy(frozen["evidence_versions"][0])
        second = copy.deepcopy(frozen["evidence_versions"][1])
        first_bet = copy.deepcopy(ledger["bets"][0])
        recompute_namespace(ledger, "footbreak")
        self.assertEqual(frozen["evidence_versions"][0], first)
        self.assertEqual(frozen["evidence_versions"][1], second)
        self.assertEqual(ledger["bets"][0], first_bet)
        self.assertEqual(active["version"], 2)

    def test_initial_migration_merges_full_existing_cohort_once_then_resets(self):
        legacy = {
            "bets": [],
            "wilson_validation": {
                "schema_version": 1,
                "system": "footbreak",
                "activation_at": "2026-08-20T10:00:00+08:00",
                "conditions": {
                    "exact-condition": {
                        "signature": "exact-condition",
                        "frozen_at": "2026-08-20T09:00:00+08:00",
                        "condition_number": 2,
                        "historical_evidence": {"hits": 141, "decided": 231},
                        "prospective": {"hits": 44, "decided": 71, "pushes": 3},
                    },
                },
            },
        }
        namespace = ensure_namespace(
            legacy, "footbreak", now="2026-08-20T22:00:00+08:00",
        )
        frozen = namespace["conditions"]["exact-condition"]
        active = frozen["active_evidence"]
        self.assertEqual((active["cumulative_hits"], active["cumulative_decided"]), (185, 302))
        self.assertEqual(active["version"], 2)
        self.assertTrue(frozen["rollover_audit"][-1]["initial_migration_full_cohort"])
        self.assertEqual(frozen["rollover_audit"][-1]["legacy_prospective_cohort"], {"hits": 44, "decided": 71, "pushes": 3})
        self.assertEqual(frozen["pending_rollover_progress"]["display"], "0/20")
        before = copy.deepcopy(frozen["evidence_versions"])
        ensure_namespace(legacy, "footbreak", now="2026-08-21T00:00:00+08:00")
        self.assertEqual(frozen["evidence_versions"], before)

    def test_granular_card_initial_migration_is_active_evidence_for_both_systems(self):
        """The screenshot card and Wilson admission share one exact identity."""
        for system in ("footbreak", "crown"):
            ranking = [candidate(hits=141, decided=231, key="condition-2")]
            ranking[0]["key"] = [
                f"system={system}", "market=HDC", "path=首預→T-30→T-5",
                "decision=T-5", "tier=≥1.70", "direction=A→A→A",
                "role=主讓", "bucket=0.25–0.5", "movement=不變",
            ]
            ranking[0]["system"] = system
            ranking[0]["observed_path"] = "首預→T-30→T-5"
            ranking[0]["decision_stage"] = "T-5"
            ranking[0]["holdout"] = {
                "hits": 44, "decided": 71, "pushes": 0, "accuracy": 44 / 71,
            }
            ledger = {"bets": []}
            # A namespace may have been installed before the granular history
            # is ready; its old cutover timestamp must not admit a backfill.
            ensure_namespace(ledger, system, now="2026-08-20T10:00:00+08:00")
            projected = project_granular_ranking_evidence(
                ledger, system, ranking, now="2026-08-20T22:00:00+08:00",
            )
            self.assertEqual(len(projected), 1)
            card = projected[0]
            self.assertEqual((card["total"]["hits"], card["total"]["decided"]), (185, 302))
            self.assertEqual(card["active_evidence"]["version"], 2)
            self.assertAlmostEqual(card["active_evidence"]["wilson95_lower_raw"], .557, places=3)
            self.assertAlmostEqual(card["active_evidence"]["minimum_acceptable_odds_raw"], 1.90, places=2)
            self.assertEqual(card["validation_progress"]["display"], "0/20")
            self.assertEqual(
                card["active_evidence"]["activation_boundary_at"],
                "2026-08-20T22:00:00+08:00",
            )
            # The current validation field is reset; the old 44/71 is only in
            # immutable migration audit, never reused as a progress display.
            self.assertEqual(card["holdout"]["decided"], 0)
            frozen = next(iter(ledger["wilson_validation"]["conditions"].values()))
            self.assertEqual(
                frozen["rollover_audit"][-1]["legacy_prospective_cohort"],
                {"hits": 44, "decided": 71, "pushes": 0},
            )
            # A candidate supplied by this displayed card enters through the
            # active version, rather than falling back to its old 141/231.
            admission, reason = apply_active_evidence(
                ledger, system,
                matching_admissions(system, "HDC", selected(), [card],
                                    stage_at="2026-08-20T22:01:00+08:00")[0][0],
                stage_at="2026-08-20T22:01:00+08:00",
                now="2026-08-20T22:01:00+08:00",
            )
            self.assertIsNone(reason)
            assert admission is not None
            self.assertEqual(
                (admission["history"]["hits"], admission["history"]["decided"]),
                (185, 302),
            )

    def test_granular_card_numbers_survive_a_later_ranking_reorder(self):
        def row(key, hits):
            item = candidate(hits=hits, decided=231, key=key)
            item.update({
                "key": [
                    "system=footbreak", "market=HDC", "path=首預→T-30→T-5",
                    "decision=T-5", "tier=≥1.70", f"direction={key}",
                    "role=主讓", "bucket=0.25–0.5",
                ],
                "system": "footbreak", "observed_path": "首預→T-30→T-5",
                "decision_stage": "T-5",
                "holdout": {"hits": 44, "decided": 71, "pushes": 0},
            })
            return item
        ledger = {"bets": []}
        first = project_granular_ranking_evidence(
            ledger, "footbreak", [row("A→A→A", 141), row("A→B→A", 142)],
            now="2026-08-20T22:00:00+08:00",
        )
        self.assertEqual([item["condition_number"] for item in first], [1, 2])
        second = project_granular_ranking_evidence(
            ledger, "footbreak", [row("A→B→A", 142), row("A→A→A", 141)],
            now="2026-08-21T22:00:00+08:00",
        )
        self.assertEqual([item["condition_number"] for item in second], [2, 1])

    def test_crown_uses_the_same_rollover_engine(self):
        ledger = {"bets": []}
        for index in range(1, 21):
            self._settled(ledger, index, system="crown")
        recompute_namespace(ledger, "crown")
        frozen = next(iter(ledger["wilson_validation"]["conditions"].values()))
        self.assertEqual(frozen["active_evidence"]["version"], 2)
        self.assertEqual(frozen["pending_rollover_progress"]["display"], "0/20")

    def test_dashboard_contract_exposes_active_version_and_hash_only_audit(self):
        ledger = {"bets": []}
        for index in range(1, 21):
            self._settled(ledger, index)
        recompute_namespace(ledger, "footbreak")
        frozen, _ = self._active(ledger)
        batch = frozen["rollover_audit"][-1]
        self.assertEqual(len(batch["batch_fixture_market_hashes"]), 20)
        self.assertTrue(all(len(value) == 64 for value in batch["batch_fixture_market_hashes"]))
        self.assertNotIn("fixture-", json.dumps(batch, ensure_ascii=False))
        root = Path(__file__).resolve().parents[2]
        for path in (
            root / "system" / "gen_app_data.py",
            root / "crown" / "dashboard_data.py",
        ):
            source = path.read_text(encoding="utf-8")
            self.assertIn("pending_progress", source)
            self.assertIn("active_evidence", source)
            self.assertIn("project_granular_ranking_evidence", source)
        for path in (
            root / "hkjc-dashboard" / "app.js",
            root / "crown" / "dashboard" / "app.js",
        ):
            source = path.read_text(encoding="utf-8")
            self.assertIn("Wilson 證據版本", source)
            self.assertIn("新前瞻待合併", source)
            self.assertIn("活躍證據 v", source)

    def test_future_formal_odds_gate_uses_active_version_not_old_baseline(self):
        ledger = {"bets": []}
        for index in range(1, 21):
            self._settled(ledger, index, result="Lost")
        recompute_namespace(ledger, "footbreak")
        admission, reason = apply_active_evidence(
            ledger, "footbreak", self._admission(),
            stage_at="2026-08-20T21:00:00+08:00",
            now="2026-08-20T21:00:00+08:00",
        )
        self.assertIsNone(reason)
        assert admission is not None
        self.assertEqual(admission["evidence_version"], 2)
        self.assertFalse(admission["arithmetic"]["passes"])
        self.assertTrue(self._admission()["arithmetic"]["passes"])


if __name__ == "__main__":
    unittest.main()
