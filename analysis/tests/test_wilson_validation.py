"""Focused invariants for the isolated Wilson simulation portfolios."""
from __future__ import annotations

import copy
import hashlib
import json
import tempfile
import unittest
import subprocess
from datetime import datetime, timedelta
from unittest.mock import patch
from pathlib import Path

from analysis import wilson_validation as wv
from analysis.wilson_validation import (
    DECISION_STAGE, EDGE_BUFFER, FIXED_STAKE, FIXTURE_STAKE_CAP, MIN_DECIDED, STRATEGY,
    STARTING_BANKROLL, admission_arithmetic, choose_admission, commit_bet,
    apply_active_evidence, condition_number, ensure_namespace, freeze_condition,
    matching_admissions, portfolio_name, active_observations, project_granular_ranking_evidence,
    project_dashboard_research_matches, project_frozen_ranking_evidence,
    record_match_observation,
    recompute_namespace, wilson95,
    validate_formal_row,
    create_production_identity_manifest,
    _expected_production_identity_manifest,
    _fixture_market_hash,
    _quarter_snapshot_binding_valid,
)
from analysis.quarter_line import from_dixon_coles
from analysis.migrate_wilson_strategy import migrate_file
from analysis.wilson_portfolio import _native_t5, _selected


def candidate(
    market="HDC", side="H", line=-0.25, *, hits=41, decided=59,
    key="example", system="footbreak",
):
    matcher_key = (
        [
            f"system={system}", f"market={market}", "path=首預→T-30→T-5",
            "decision=T-5", "tier=≥1.70", "direction=A→A→A",
            "role=主讓", "bucket=0.25–0.5", "movement=不變",
            "tier_path=≥1.70→≥1.70→≥1.70",
        ]
        if key == "example" else [f"system={system}", f"market={market}", key]
    )
    return {
        "market": market, "selected_side": side, "selected_line": line,
        "key": matcher_key,
        "path": "首預→T-30→T-5", "direction": "A→A→A",
        "role": "主讓", "line_bucket": "0.25–0.5", "odds_tier": "≥1.70",
        "odds_trajectory": "≥1.70→≥1.70→≥1.70",
        "movement": "不變", "total": {"hits": hits, "decided": decided, "pushes": 0},
        "label": "HDC，首預→T-30→T-5 all 主讓，主隊讓0.25–0.5，T-5 odds >=1.70，方向不變",
        "source_artifact": {"hash": "frozen-artifact", "version": "v7", "as_of": "2026-08-19T22:55:00+08:00"},
    }


def selected(market="HDC", side="H", line=-0.25, odds=1.90):
    return {"market": market, "side": side, "line": line, "odds": odds}


def quarter_profile(side="H", line=2.75, **_unused):
    profile = from_dixon_coles(
        line=line, side=side, lh=1.5, la=1.2, rho=-.03,
    )
    assert profile is not None
    return profile


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

    def test_quarter_line_half_win_requires_higher_odds_than_binary(self):
        profile = quarter_profile("H", 2.75)
        adjusted = admission_arithmetic(
            41, 59, 2.20, settlement_profile=profile,
        )
        binary = admission_arithmetic(41, 59, 2.20)
        self.assertTrue(adjusted["settlement_adjusted"])
        self.assertEqual(adjusted["settlement_profile"], profile)
        self.assertGreater(
            adjusted["minimum_acceptable_odds_raw"],
            binary["minimum_acceptable_odds_raw"],
        )
        q = adjusted["wilson95_lower_raw"] - EDGE_BUFFER
        expected = 1 + (1 - q) / (q * profile["win_fraction_raw"])
        self.assertAlmostEqual(
            adjusted["minimum_acceptable_odds_raw"], expected,
        )

    def test_quarter_line_half_loss_credits_reduced_boundary_loss(self):
        profile = quarter_profile("H", 2.25, hit_probability=.58)
        adjusted = admission_arithmetic(
            41, 59, 1.90, settlement_profile=profile,
        )
        binary = admission_arithmetic(41, 59, 1.90)
        self.assertLess(
            adjusted["minimum_acceptable_odds_raw"],
            binary["minimum_acceptable_odds_raw"],
        )
        self.assertEqual(profile["boundary_result"], "half_loss")

    def test_quarter_line_directions_have_correct_boundary_settlement(self):
        self.assertEqual(
            quarter_profile("L", 2.25)["boundary_result"], "half_win",
        )
        self.assertEqual(
            quarter_profile("L", 2.75)["boundary_result"], "half_loss",
        )

    def test_quarter_line_match_fails_closed_without_snapshot_profile(self):
        row = selected("HIL", "H", 2.75, 2.20)
        matches, reason = matching_admissions(
            "footbreak", "HIL", row,
            [candidate("HIL", "H", 2.75, key="HIL")],
            stage_at="2026-08-19T22:55:00+08:00",
        )
        self.assertEqual(matches, [])
        self.assertEqual(reason, "quarter_line_settlement_profile_unavailable")
        row["quarter_line_settlement"] = quarter_profile("H", 2.75)
        matches, reason = matching_admissions(
            "footbreak", "HIL", row,
            [candidate("HIL", "H", 2.75, key="HIL")],
            stage_at="2026-08-19T22:55:00+08:00",
        )
        self.assertEqual(reason, "wilson_pass")
        self.assertEqual(len(matches), 1)
        self.assertTrue(matches[0]["arithmetic"]["settlement_adjusted"])

    def test_quarter_formal_row_is_bound_to_exact_native_snapshot(self):
        stage_at = "2026-08-19T23:00:00+08:00"
        kickoff = "2026-08-19T23:30:00+08:00"
        profile = quarter_profile("H", 2.75)
        selected_row = {
            **selected("HIL", "H", 2.75, 2.20),
            "code": "HIL",
            "quote_source": "provider",
            "observed_at": "2026-08-19T22:59:50+08:00",
            "quarter_line_settlement": profile,
        }
        stage = {
            "stage": DECISION_STAGE,
            "ts": stage_at,
            "kickoff": kickoff,
            "market_predictions": [copy.deepcopy(selected_row)],
        }
        snapshot_hash = hashlib.sha256(json.dumps(
            stage, ensure_ascii=False, sort_keys=True, separators=(",", ":"),
            default=str,
        ).encode()).hexdigest()
        stage.update({
            "native_snapshot_id": "snapshot:fixture-quarter:T-5:2026-08-19T23:00:00+08:00",
            "native_snapshot_hash": snapshot_hash,
        })
        selected_row["native_snapshot_binding"] = {
            "schema_version": 1,
            "system": "footbreak",
            "snapshot_id": stage["native_snapshot_id"],
            "snapshot_hash": snapshot_hash,
        }
        ledger = {"bets": [], "watch": {}}
        watch = {
            "match_id": "fixture-quarter",
            "league": "測試",
            "home": "主",
            "away": "客",
            "kickoff": kickoff,
            "stages": [stage],
        }
        ledger["watch"][watch["match_id"]] = watch
        ensure_namespace(
            ledger, "footbreak", now="2026-08-19T22:00:00+08:00",
        )
        matches, reason = matching_admissions(
            "footbreak", "HIL", selected_row,
            [candidate("HIL", "H", 2.75, key="HIL")],
            stage_at=stage_at,
        )
        self.assertEqual(reason, "wilson_pass")
        admission, reason = apply_active_evidence(
            ledger, "footbreak", matches[0], stage_at=stage_at, now=stage_at,
        )
        self.assertIsNone(reason)
        row = commit_bet(
            ledger, "footbreak", watch, "HIL", selected_row, admission,
            now=stage_at, market_label="入球大細", selected_label="大 2.75",
            selected_role="大", selected_line=2.75,
        )
        self.assertIsNotNone(row)
        ledger["bets"].append(row)
        frozen = ledger["wilson_validation"]["conditions"][
            row["frozen_condition_signature"]
        ]
        self.assertTrue(
            _quarter_snapshot_binding_valid(ledger, row, "footbreak"),
        )
        admitted, reason = validate_formal_row(
            row, system="footbreak",
            signature=row["frozen_condition_signature"], frozen=frozen,
            projection_time=datetime.fromisoformat(stage_at), ledger=ledger,
        )
        self.assertIsNotNone(admitted, reason)
        self.assertIsNone(reason)

        corrupted = copy.deepcopy(ledger)
        corrupted["watch"]["fixture-quarter"]["stages"][0][
            "market_predictions"
        ][0]["quarter_line_settlement"]["boundary_probability_raw"] += .01
        admitted, reason = validate_formal_row(
            corrupted["bets"][0], system="footbreak",
            signature=row["frozen_condition_signature"],
            frozen=corrupted["wilson_validation"]["conditions"][
                row["frozen_condition_signature"]
            ],
            projection_time=datetime.fromisoformat(stage_at),
            ledger=corrupted,
        )
        self.assertIsNone(admitted)
        self.assertEqual(reason, "invalid_formal_admission_binding")

        legacy_row = copy.deepcopy(row)
        legacy_row.pop("quarter_line_settlement", None)
        legacy_row.pop("native_snapshot_binding", None)
        legacy_row.pop("quarter_line_settlement_schema_version", None)
        legacy_row["wilson_admission"] = admission_arithmetic(
            hits=41, decided=59, odds=legacy_row["odds"],
        )
        for key in (
            "binary_minimum_acceptable_odds_raw",
            "settlement_adjusted",
            "settlement_profile",
        ):
            legacy_row["wilson_admission"].pop(key, None)
        ledger["wilson_validation"][
            "quarter_settlement_activation_at"
        ] = "2026-08-20T00:00:00+08:00"
        legacy_admitted, legacy_reason = validate_formal_row(
            legacy_row, system="footbreak",
            signature=row["frozen_condition_signature"], frozen=frozen,
            projection_time=datetime.fromisoformat(stage_at),
            ledger=ledger,
        )
        self.assertIsNotNone(legacy_admitted)
        self.assertIsNone(legacy_reason)

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
    def setUp(self):
        self._activation_temp = tempfile.TemporaryDirectory()
        marker = Path(self._activation_temp.name) / "condition17-activation.json"
        module_path = Path(wv.__file__)
        marker.write_text(json.dumps({
            "schema": wv.CONDITION17_ACTIVATION_SCHEMA,
            "wilson_validation_sha256": hashlib.sha256(
                module_path.read_bytes(),
            ).hexdigest(),
            "quarter_line_sha256": hashlib.sha256(
                module_path.with_name("quarter_line.py").read_bytes(),
            ).hexdigest(),
        }), encoding="utf-8")
        marker.chmod(0o400)
        self._activation_patch = patch.object(
            wv, "CONDITION17_ACTIVATION_MARKER", marker,
        )
        self._activation_patch.start()

    def tearDown(self):
        self._activation_patch.stop()
        self._activation_temp.cleanup()

    """Versioned evidence is intentionally tested independently of betting PnL."""

    def _admission(self, system="footbreak"):
        result, reason = choose_admission(
            system, "HDC", selected(), [candidate(system=system)],
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
            "away": "客",
            "kickoff": (
                datetime.fromisoformat(stage_at) + timedelta(hours=1)
            ).isoformat(),
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
            "settled_at": (
                datetime.fromisoformat(stage_at) + timedelta(hours=2)
            ).isoformat(),
        })
        ledger["bets"].append(row)
        return row

    def _active(self, ledger):
        frozen = next(iter(ledger["wilson_validation"]["conditions"].values()))
        return frozen, frozen["active_evidence"]

    def _authorize_production_manifest(self, ledger, system="footbreak"):
        expected, _validated, reason = _expected_production_identity_manifest(
            ledger["wilson_validation"], system,
        )
        self.assertIsNone(reason)
        self.assertIsNotNone(expected)
        return create_production_identity_manifest(
            ledger, system, authorized_manifest=expected,
        )

    def test_pre_patch_binary_formal_row_remains_valid(self):
        ledger = {"bets": []}
        row = self._settled(ledger, 1)
        frozen, _active = self._active(ledger)
        for key in (
            "binary_minimum_acceptable_odds_raw",
            "settlement_adjusted",
            "settlement_profile",
        ):
            row["wilson_admission"].pop(key, None)
        admitted, reason = validate_formal_row(
            row,
            system="footbreak",
            signature=row["frozen_condition_signature"],
            frozen=frozen,
            projection_time=datetime.fromisoformat(row["settled_at"]),
            require_settled=True,
        )
        self.assertIsNotNone(admitted)
        self.assertIsNone(reason)

    def test_nineteen_stay_pending_and_twenty_rolls_once(self):
        ledger = {"bets": []}
        for index in range(1, 20):
            self._settled(ledger, index, result="Won" if index % 2 else "Lost")
        recompute_namespace(ledger, "footbreak")
        frozen, active = self._active(ledger)
        self.assertEqual(active["version"], 1)
        self.assertEqual(frozen["pending_rollover_progress"]["display"], "19/20")
        self.assertEqual(frozen["pending_rollover_progress"]["eligible_hits"], 10)
        self.assertAlmostEqual(
            frozen["pending_rollover_progress"]["accuracy"], 10 / 19,
        )
        projected = project_granular_ranking_evidence(
            ledger, "footbreak", [candidate()],
            now="2026-08-20T01:00:00+08:00",
        )
        self.assertEqual(projected[0]["validation_progress"]["pending_decided"], 19)
        self.assertEqual(projected[0]["validation_progress"]["pending_hits"], 10)
        self.assertAlmostEqual(
            projected[0]["validation_progress"]["pending_accuracy"], 10 / 19,
        )
        forecast = projected[0]["validation_progress"]["if_rate_holds"]
        self.assertEqual(forecast["projected_batch_hits"], 11)
        self.assertEqual(forecast["projected_batch_decided"], 20)
        self.assertEqual(forecast["projected_cumulative_hits"], 52)
        self.assertEqual(forecast["projected_cumulative_decided"], 79)
        self.assertAlmostEqual(
            forecast["projected_minimum_acceptable_odds_raw"],
            admission_arithmetic(52, 79, 1.90)[
                "minimum_acceptable_odds_raw"
            ],
        )
        self._settled(ledger, 20, result="Won")
        recompute_namespace(ledger, "footbreak")
        frozen, active = self._active(ledger)
        self.assertEqual(active["version"], 2)
        self.assertEqual(active["cumulative_decided"], 79)
        self.assertEqual(frozen["rollover_audit"][-1]["batch_decided"], 20)
        self.assertEqual(frozen["pending_rollover_progress"]["display"], "0/20")
        self.assertEqual(frozen["pending_rollover_progress"]["eligible_hits"], 0)
        self.assertIsNone(frozen["pending_rollover_progress"]["accuracy"])

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
            admission["arithmetic"] = admission_arithmetic(
                admission["history"]["hits"],
                admission["history"]["decided"],
                1.20,
            )
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

    def test_v2_evidence_is_immutable_after_v3_rollover_and_decision_id_boundary_holds(self):
        """Gap #6 from bilateral_wilson_audit_report.md: V2->V3 evidence-version
        transition isolation. Reuses the exact 40-bet rollover fixture from
        ``test_forty_rolls_two_versions_and_twenty_six_leaves_six`` (which
        rolls from v1 -> v2 at bet 20, and v2 -> v3 at bet 40) but additionally
        snapshots v2 before/after the v3 rollover to prove it is never mutated
        in place, and confirms a decision keyed to v2's condition_signature +
        evidence_version cannot collide with or be reinterpreted under v3.
        """
        from analysis import bilateral_decision as bilateral

        ledger = {"bets": []}
        for index in range(1, 21):
            self._settled(ledger, index, result="Won")
        with patch(
            "analysis.wilson_validation._now",
            return_value="2026-08-25T12:00:00+08:00",
        ):
            recompute_namespace(ledger, "footbreak")
        frozen, active = self._active(ledger)
        self.assertEqual(active["version"], 2)
        signature = frozen["signature"]
        # Byte-identical snapshot of v2 taken the moment it becomes active,
        # before any v3 rollover has run.
        v2_before = copy.deepcopy(frozen["evidence_versions"][1])
        self.assertEqual(v2_before["version"], 2)

        # A decision recorded while v2 was authoritative embeds v2's
        # condition_signature + evidence_version in its idempotency key.
        v2_decision_id = bilateral.decision_id(
            system="footbreak", fixture="fixture-20", market="HDC", side="H",
            line=-0.25, condition_signature=v2_before["condition_signature"],
            evidence_version=v2_before["version"],
        )

        # Roll forward to v3 with 20 more settled bets on the SAME condition.
        for index in range(21, 41):
            self._settled(
                ledger, index, result="Won",
                stage_at=(
                    datetime.fromisoformat(v2_before["created_at"])
                    + timedelta(minutes=index)
                ).isoformat(),
            )
        recompute_namespace(ledger, "footbreak")
        frozen, active = self._active(ledger)
        self.assertEqual(active["version"], 3)
        self.assertEqual(frozen["signature"], signature)

        # v2's row in evidence_versions must be byte-identical to the snapshot
        # taken before v3 existed -- the rollover only ever appends.
        v2_after = frozen["evidence_versions"][1]
        self.assertEqual(v2_after, v2_before)
        self.assertEqual(v2_after["evidence_hash"], v2_before["evidence_hash"])
        # v3 is a distinct, newly appended row referencing v2 as its parent,
        # never overwriting v2's slot.
        v3 = frozen["evidence_versions"][2]
        self.assertEqual(v3["version"], 3)
        self.assertEqual(v3["prior_version"], 2)
        self.assertEqual(v3["prior_evidence_hash"], v2_before["evidence_hash"])
        self.assertNotEqual(v3["evidence_hash"], v2_before["evidence_hash"])
        self.assertEqual(len(frozen["evidence_versions"]), 3)

        # condition_signature is identical across versions (same immutable
        # condition), but the active pointer now reads v3, not v2.
        self.assertEqual(v3["condition_signature"], v2_before["condition_signature"])
        self.assertEqual(frozen["active_evidence_version"], 3)
        self.assertEqual(frozen["active_evidence_hash"], v3["evidence_hash"])
        self.assertNotEqual(frozen["active_evidence_hash"], v2_before["evidence_hash"])

        # The v2-era decision_id is a pure function of (signature, version);
        # recomputing it after the v3 rollover yields the exact same id
        # (v2's record is never reinterpreted), while a same-fixture decision
        # made under the new active version produces a DIFFERENT id -- the
        # condition_signature+evidence_version boundary prevents any
        # collision between the two evidence eras.
        v2_decision_id_recomputed = bilateral.decision_id(
            system="footbreak", fixture="fixture-20", market="HDC", side="H",
            line=-0.25, condition_signature=v2_before["condition_signature"],
            evidence_version=v2_before["version"],
        )
        self.assertEqual(v2_decision_id, v2_decision_id_recomputed)
        v3_decision_id = bilateral.decision_id(
            system="footbreak", fixture="fixture-20", market="HDC", side="H",
            line=-0.25, condition_signature=v3["condition_signature"],
            evidence_version=v3["version"],
        )
        self.assertNotEqual(v2_decision_id, v3_decision_id)

    def test_push_duplicate_conflict_and_activation_boundary_fail_closed(self):
        ledger = {"bets": []}
        for index in range(1, 19):
            self._settled(ledger, index)
        push = self._settled(ledger, 19, result="Refunded")
        # A duplicate fixture-market provenance hash is ambiguous, including
        # when its outcome conflicts. Neither copy is allowed in the batch.
        duplicate = self._settled(ledger, 20, result="Lost")
        duplicate["match_id"] = "fixture-1"
        duplicate["bet_id"] = (
            "fixture-1|HDC|T-5|wilson-test-strategy-v1"
        )
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
        before = copy.deepcopy(frozen)
        ledger["bets"][1]["rollover_provenance"]["stage_at"] = boundary
        recompute_namespace(ledger, "footbreak")
        self.assertEqual(frozen["evidence_versions"], before["evidence_versions"])
        self.assertEqual(
            frozen["pending_rollover_progress"],
            before["pending_rollover_progress"],
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
            self.assertEqual(card["validation_progress"]["pending_hits"], 0)
            self.assertIsNone(card["validation_progress"]["pending_accuracy"])
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
        for path, projection in (
            (root / "system" / "gen_app_data.py", "project_granular_ranking_evidence"),
            # Crown's browser must project only the already-durable registry;
            # it may not initialize or persist condition evidence itself.
            (root / "crown" / "dashboard_data.py", "project_frozen_ranking_evidence"),
        ):
            source = path.read_text(encoding="utf-8")
            self.assertIn("pending_progress", source)
            self.assertIn("active_evidence", source)
            self.assertIn(projection, source)
        for path in (
            root / "hkjc-dashboard" / "app.js",
            root / "crown" / "dashboard" / "app.js",
        ):
            source = path.read_text(encoding="utf-8")
            self.assertIn("Wilson 證據版本", source)
            self.assertIn("新前瞻待合併", source)
            self.assertIn("暫時命中 ${pendingHits}/${pendingDecided}", source)
            self.assertIn("暫時未有已判定命中率", source)
            self.assertIn("按目前命中率推算：整批約", source)
            self.assertIn("合併後 Wilson 最低要求賠率預計", source)
            self.assertIn("（未合併估算）", source)
            self.assertIn("活躍證據 v", source)

    def test_last_merged_batch_projects_exact_twenty_rows_and_thirteen_hits(self):
        ledger = {"bets": []}
        for index in range(1, 21):
            self._settled(
                ledger, index, result="Won" if index <= 13 else "Lost",
            )
        recompute_namespace(ledger, "footbreak")

        projected = project_granular_ranking_evidence(
            ledger, "footbreak", [candidate()],
            now="2026-08-22T00:00:00+08:00",
        )
        self.assertEqual(len(projected), 1)
        detail = projected[0]["last_merged_evidence"]
        self.assertTrue(detail["complete"])
        self.assertEqual(detail["version"], 2)
        self.assertEqual(detail["expected_decided"], 20)
        self.assertEqual(detail["expected_hits"], 13)
        self.assertEqual(len(detail["rows"]), 20)
        self.assertEqual(sum(row["hit"] for row in detail["rows"]), 13)
        self.assertEqual(detail["rows"][0]["home"], "主")
        self.assertEqual(detail["rows"][0]["away"], "客")
        self.assertEqual(detail["rows"][0]["market"], "HDC")
        self.assertEqual(detail["rows"][0]["selected_role"], "主讓")
        self.assertEqual(detail["rows"][0]["selected_line"], -.25)
        self.assertEqual(detail["rows"][0]["odds"], 1.90)
        serialized = json.dumps(detail, ensure_ascii=False)
        self.assertNotIn("fixture-", serialized)
        self.assertNotIn("fixture_market_hash", serialized)
        self.assertNotIn("rollover_provenance", serialized)

    def test_pending_batch_projects_exact_seventeen_rows_and_nine_hits_for_both_systems(self):
        for system in ("footbreak", "crown"):
            ledger = {"bets": []}
            for index in range(1, 18):
                self._settled(
                    ledger, index, system=system,
                    result="Won" if index <= 9 else "Lost",
                )
            recompute_namespace(ledger, system)
            frozen, _active = self._active(ledger)
            projected_candidate = (
                candidate(system=system)
                if system == "footbreak"
                else copy.deepcopy(frozen["definition"])
            )
            if system == "crown":
                projected_candidate["key"] = copy.deepcopy(
                    projected_candidate.pop("miner_key"),
                )
                projected = project_frozen_ranking_evidence(
                    ledger, system, [projected_candidate],
                )
            else:
                projected = project_granular_ranking_evidence(
                    ledger, system, [projected_candidate],
                    now="2026-08-22T00:00:00+08:00",
                )

            detail = projected[0]["pending_rollover_evidence"]
            self.assertTrue(detail["complete"])
            self.assertEqual(detail["expected_decided"], 17)
            self.assertEqual(detail["expected_hits"], 9)
            self.assertEqual(detail["required"], 20)
            self.assertEqual(len(detail["rows"]), 17)
            self.assertEqual(sum(row["hit"] for row in detail["rows"]), 9)
            identities = [
                row["evidence_identity"] for row in detail["rows"]
            ]
            self.assertEqual(len(set(identities)), 17)
            self.assertEqual(detail["rows"][0]["home"], "主")
            self.assertEqual(detail["rows"][0]["away"], "客")
            serialized = json.dumps(detail, ensure_ascii=False)
            self.assertNotIn("fixture-", serialized)
            self.assertNotIn("fixture_market_hash", serialized)
            self.assertNotIn("rollover_provenance", serialized)

    def test_pending_batch_detail_fails_closed_when_summary_does_not_match_rows(self):
        ledger = {"bets": []}
        for index in range(1, 14):
            self._settled(
                ledger, index, result="Won" if index <= 9 else "Lost",
            )
        recompute_namespace(ledger, "footbreak")
        frozen, _active = self._active(ledger)
        frozen["pending_rollover_progress"]["eligible_hits"] = 10

        projected = project_granular_ranking_evidence(
            ledger, "footbreak", [candidate()],
            now="2026-08-22T00:00:00+08:00",
        )
        detail = projected[0]["pending_rollover_evidence"]
        self.assertFalse(detail["complete"])
        self.assertEqual(detail["rows"], [])
        self.assertEqual(
            detail["unavailable_reason"], "pending_row_identity_mismatch",
        )

    def test_footbreak_seven_pending_detail_filters_legacy_identity_only(self):
        fixture = json.loads(
            (
                Path(__file__).with_name("fixtures")
                / "footbreak_condition_7_pending_17.json"
            ).read_text(encoding="utf-8")
        )
        ledger = {"bets": []}
        seeds = []
        for index in range(1, 7):
            seed = copy.deepcopy(candidate())
            seed["line_bucket"] = f"seed-{index}"
            seed["key"] = [
                (
                    f"bucket=seed-{index}"
                    if value.startswith("bucket=") else value
                )
                for value in seed["key"]
            ]
            seeds.append(seed)
        project_granular_ranking_evidence(
            ledger, fixture["system"], [*seeds, candidate()],
            now="2026-08-20T00:00:00+08:00",
        )
        for item in fixture["fixtures"]:
            self._settled(
                ledger, item["index"], result=item["result"],
                system=fixture["system"],
            )
        recompute_namespace(ledger, fixture["system"])
        frozen = next(
            row for row in ledger["wilson_validation"]["conditions"].values()
            if row.get("condition_number") == fixture["condition_number"]
        )
        self.assertEqual(
            frozen["pending_rollover_progress"]["display"], "17/20",
        )

        # Reproduce the production-shaped compatibility conflict: a preserved
        # same-signature row has rollover-looking provenance but cannot pass
        # the immutable formal identity/version validator. It must be ignored,
        # never mixed into the exact 17-row pending cohort.
        legacy = copy.deepcopy(ledger["bets"][0])
        legacy.update({
            "match_id": "legacy-condition-seven-unverifiable",
            "bet_id": "legacy-unverifiable-id",
            "evidence_version": 999,
        })
        legacy["rollover_provenance"]["fixture_market_hash"] = "f" * 64
        legacy["rollover_provenance"]["admitted_evidence_version"] = 999
        ledger["bets"].append(legacy)

        projected = project_frozen_ranking_evidence(
            ledger, fixture["system"], [candidate()],
        )
        detail = projected[0]["pending_rollover_evidence"]
        self.assertTrue(detail["complete"])
        self.assertEqual(
            (detail["expected_decided"], detail["expected_hits"]),
            (fixture["expected_decided"], fixture["expected_hits"]),
        )
        self.assertEqual(len(detail["rows"]), fixture["expected_decided"])
        self.assertEqual(
            sum(row["hit"] for row in detail["rows"]),
            fixture["expected_hits"],
        )
        identities = [row["evidence_identity"] for row in detail["rows"]]
        self.assertEqual(len(set(identities)), fixture["expected_decided"])
        self.assertNotIn(
            "legacy-condition-seven-unverifiable",
            json.dumps(detail, ensure_ascii=False),
        )
        repeated = project_frozen_ranking_evidence(
            ledger, fixture["system"], [candidate()],
        )[0]["pending_rollover_evidence"]
        self.assertEqual(
            identities,
            [row["evidence_identity"] for row in repeated["rows"]],
        )

        # Tampering one of the 17 authoritative rows drops the exact join to
        # 16; the adapter must remain unavailable rather than fill from legacy.
        tampered = copy.deepcopy(ledger)
        tampered["bets"][1]["evidence_version"] = 999
        blocked = project_frozen_ranking_evidence(
            tampered, fixture["system"], [candidate()],
        )[0]["pending_rollover_evidence"]
        self.assertFalse(blocked["complete"])
        self.assertEqual(blocked["rows"], [])
        self.assertEqual(
            blocked["unavailable_reason"], "pending_row_identity_mismatch",
        )

        # A valid-looking row from another condition cannot fill this cohort.
        cross_condition = copy.deepcopy(ledger)
        cross_condition["bets"][0]["frozen_condition_signature"] = "0" * 24
        blocked = project_frozen_ranking_evidence(
            cross_condition, fixture["system"], [candidate()],
        )[0]["pending_rollover_evidence"]
        self.assertFalse(blocked["complete"])
        self.assertEqual(blocked["rows"], [])

    def test_footbreak_seven_projects_exact_pre_binding_legacy_cohort(self):
        ledger = {"bets": []}
        seeds = []
        for index in range(1, 7):
            seed = copy.deepcopy(candidate())
            seed["line_bucket"] = f"seed-{index}"
            seed["key"] = [
                (
                    f"bucket=seed-{index}"
                    if value.startswith("bucket=") else value
                )
                for value in seed["key"]
            ]
            seeds.append(seed)
        project_granular_ranking_evidence(
            ledger, "footbreak", [*seeds, candidate()],
            now="2026-08-20T00:00:00+08:00",
        )
        rows = [
            self._settled(
                ledger, index,
                result="Won" if index <= 9 else "Lost",
                system="footbreak",
            )
            for index in range(1, 18)
        ]
        recompute_namespace(ledger, "footbreak")
        frozen = next(
            row for row in ledger["wilson_validation"]["conditions"].values()
            if row.get("condition_number") == 7
        )
        self.assertEqual(
            frozen["pending_rollover_progress"]["display"], "17/20",
        )

        # Model the exact production compatibility shape: the rows retain the
        # original formal identity and rollover provenance used to persist the
        # 17/20 counter, but predate the later immutable admission binding.
        for row in rows:
            row["frozen_condition_definition"] = {}

        # A later row rejected for a different reason must not be allowed to
        # expand or fill the persisted compatibility cohort.
        later = self._settled(
            ledger, 18, result="Won", system="footbreak",
        )
        later["settled_at"] = later["created_at"]

        projected = project_frozen_ranking_evidence(
            ledger, "footbreak", [candidate()],
        )
        detail = projected[0]["pending_rollover_evidence"]
        self.assertTrue(detail["complete"])
        self.assertEqual(len(detail["rows"]), 17)
        self.assertEqual(sum(row["hit"] for row in detail["rows"]), 9)
        self.assertEqual(
            len({row["evidence_identity"] for row in detail["rows"]}), 17,
        )

        tampered = copy.deepcopy(ledger)
        tampered["bets"][0]["result"] = "Lost"
        blocked = project_frozen_ranking_evidence(
            tampered, "footbreak", [candidate()],
        )[0]["pending_rollover_evidence"]
        self.assertFalse(blocked["complete"])
        self.assertEqual(blocked["rows"], [])

        extra = copy.deepcopy(ledger)
        extra_row = copy.deepcopy(extra["bets"][0])
        original_match_id = extra_row["match_id"]
        extra_row["match_id"] = "extra-pre-binding-row"
        extra_row["bet_id"] = extra_row["bet_id"].replace(
            original_match_id, extra_row["match_id"], 1,
        )
        extra_row["rollover_provenance"]["fixture_market_hash"] = (
            _fixture_market_hash(
                "footbreak", extra_row["match_id"], extra_row["market"],
            )
        )
        extra["bets"].append(extra_row)
        blocked = project_frozen_ranking_evidence(
            extra, "footbreak", [candidate()],
        )[0]["pending_rollover_evidence"]
        self.assertFalse(blocked["complete"])
        self.assertEqual(blocked["rows"], [])

    def test_condition_fourteen_projects_exact_mixed_binding_pending_cohort(self):
        ledger = {"bets": []}
        seeds = []
        for index in range(1, 14):
            seed = copy.deepcopy(candidate())
            seed["line_bucket"] = f"seed-{index}"
            seed["key"] = [
                (
                    f"bucket=seed-{index}"
                    if value.startswith("bucket=") else value
                )
                for value in seed["key"]
            ]
            seeds.append(seed)
        projected = project_granular_ranking_evidence(
            ledger, "footbreak", [*seeds, candidate()],
            now="2026-08-20T00:00:00+08:00",
        )
        target = next(
            row for row in projected
            if row.get("line_bucket") == candidate()["line_bucket"]
        )
        self.assertEqual(target["condition_number"], 14)

        rows = [
            self._settled(
                ledger, index,
                result="Won" if index <= 4 else "Lost",
                system="footbreak",
            )
            for index in range(1, 8)
        ]
        recompute_namespace(ledger, "footbreak")
        frozen = next(
            row for row in ledger["wilson_validation"]["conditions"].values()
            if row.get("condition_number") == 14
        )
        self.assertEqual(
            frozen["pending_rollover_progress"]["display"], "7/20",
        )
        self.assertEqual(
            frozen["pending_rollover_progress"]["eligible_hits"], 4,
        )

        # Three rows retain the production legacy shape: their exact native
        # stage remains in rollover provenance but the later top-level mirror
        # is absent.  The rest already use the current immutable schema.
        for row in rows[:3]:
            row.pop("native_stage_at")

        detail = project_granular_ranking_evidence(
            ledger, "footbreak", [*seeds, candidate()],
            now="2026-08-22T00:00:00+08:00",
        )[-1]["pending_rollover_evidence"]
        self.assertTrue(detail["complete"])
        self.assertEqual(
            (detail["expected_decided"], detail["expected_hits"]), (7, 4),
        )
        self.assertEqual(len(detail["rows"]), 7)
        self.assertEqual(sum(row["hit"] for row in detail["rows"]), 4)

        # Repairing the definition must not hide any second defect.  A changed
        # quote no longer matches the immutable Wilson arithmetic and the
        # projection therefore stays unavailable.
        tampered = copy.deepcopy(ledger)
        tampered["bets"][0]["odds"] = 9.99
        blocked = project_granular_ranking_evidence(
            tampered, "footbreak", [*seeds, candidate()],
            now="2026-08-22T00:00:00+08:00",
        )[-1]["pending_rollover_evidence"]
        self.assertFalse(blocked["complete"])
        self.assertEqual(blocked["rows"], [])
        self.assertEqual(
            blocked["unavailable_reason"], "pending_row_identity_mismatch",
        )

        conflicting_stage = copy.deepcopy(ledger)
        conflicting_stage["bets"][0]["native_stage_at"] = (
            "2026-08-20T23:59:59+08:00"
        )
        blocked = project_granular_ranking_evidence(
            conflicting_stage, "footbreak", [*seeds, candidate()],
            now="2026-08-22T00:00:00+08:00",
        )[-1]["pending_rollover_evidence"]
        self.assertFalse(blocked["complete"])
        self.assertEqual(blocked["rows"], [])

        # A duplicate losing fixture plus a unique losing replacement keeps
        # the same 7/20 and 4/7 aggregates.  The changed selector exclusions
        # must still block the projection.
        duplicate_substitution = copy.deepcopy(ledger)
        duplicate_substitution["bets"].append(
            copy.deepcopy(duplicate_substitution["bets"][-1]),
        )
        replacement = copy.deepcopy(duplicate_substitution["bets"][-1])
        old_match_id = replacement["match_id"]
        replacement["match_id"] = "aggregate-preserving-replacement"
        replacement["bet_id"] = replacement["bet_id"].replace(
            old_match_id, replacement["match_id"], 1,
        )
        replacement["rollover_provenance"]["fixture_market_hash"] = (
            _fixture_market_hash(
                "footbreak", replacement["match_id"], replacement["market"],
            )
        )
        duplicate_substitution["bets"].append(replacement)
        blocked = project_granular_ranking_evidence(
            duplicate_substitution, "footbreak", [*seeds, candidate()],
            now="2026-08-22T00:00:00+08:00",
        )[-1]["pending_rollover_evidence"]
        self.assertFalse(blocked["complete"])
        self.assertEqual(blocked["rows"], [])

    def test_footbreak_seventeen_projects_exact_schema1_timestamp_anomaly(self):
        fixture = json.loads(
            (
                Path(__file__).with_name("fixtures")
                / "footbreak_condition_17_pending_18.json"
            ).read_text(encoding="utf-8")
        )
        ledger = {"bets": []}
        seeds = []
        for index in range(1, fixture["condition_number"]):
            seed = copy.deepcopy(candidate())
            seed["line_bucket"] = f"seed-{index}"
            seed["key"] = [
                (
                    f"bucket=seed-{index}"
                    if value.startswith("bucket=") else value
                )
                for value in seed["key"]
            ]
            seeds.append(seed)
        ranking = [*seeds, candidate()]
        project_granular_ranking_evidence(
            ledger, fixture["system"], ranking,
            now="2026-08-20T00:00:00+08:00",
        )
        rows = [
            self._settled(
                ledger, item["index"], result=item["result"],
                system=fixture["system"],
            )
            for item in fixture["fixtures"]
        ]
        recompute_namespace(ledger, fixture["system"])
        self._authorize_production_manifest(ledger, fixture["system"])
        frozen = next(
            item for item in ledger["wilson_validation"]["conditions"].values()
            if item.get("condition_number") == fixture["condition_number"]
        )
        self.assertEqual(
            frozen["pending_rollover_progress"]["display"], "18/20",
        )
        self.assertEqual(
            frozen["pending_rollover_progress"]["eligible_hits"], 10,
        )

        # Exact production storage shape: immutable schema-1 admission remains
        # complete, but the top-level stage mirror is absent and the settlement
        # timestamp was stored after creation yet before kickoff.
        for row in rows:
            row.pop("native_stage_at")
            row["settled_at"] = row["created_at"]
            self.assertEqual(row["rollover_provenance"]["schema_version"], 1)
            self.assertEqual(row["evidence_version"], 1)
        admitted, reason = validate_formal_row(
            rows[0], system=fixture["system"],
            signature=rows[0]["frozen_condition_signature"], frozen=frozen,
            projection_time=datetime.fromisoformat(rows[0]["kickoff"]),
            require_settled=True, ledger=ledger,
        )
        self.assertIsNone(admitted)
        self.assertEqual(reason, "invalid_formal_admission_binding")

        # Nearby same-signature activity must remain hidden: five malformed
        # settled rows and one pending row cannot enter or fill the cohort.
        for index in range(5):
            malformed = copy.deepcopy(rows[index])
            old_match_id = malformed["match_id"]
            malformed["match_id"] = f"malformed-condition-17-{index}"
            malformed["bet_id"] = malformed["bet_id"].replace(
                old_match_id, malformed["match_id"], 1,
            )
            malformed["rollover_provenance"].pop("native_pre_kickoff_t5")
            ledger["bets"].append(malformed)
        pending_row = copy.deepcopy(rows[-1])
        old_match_id = pending_row["match_id"]
        pending_row["match_id"] = "pending-condition-17"
        pending_row["bet_id"] = pending_row["bet_id"].replace(
            old_match_id, pending_row["match_id"], 1,
        )
        pending_row.update({"status": "PENDING", "result": None})
        ledger["bets"].append(pending_row)

        before_projection_object = copy.deepcopy(ledger)
        before_projection_bytes = json.dumps(
            ledger, ensure_ascii=False, sort_keys=True, separators=(",", ":"),
        )
        target = next(
            card for card in project_frozen_ranking_evidence(
                ledger, fixture["system"], ranking,
            )
            if card.get("condition_number") == fixture["condition_number"]
        )
        self.assertEqual(ledger, before_projection_object)
        self.assertEqual(
            json.dumps(
                ledger, ensure_ascii=False, sort_keys=True, separators=(",", ":"),
            ),
            before_projection_bytes,
        )
        detail = target["pending_rollover_evidence"]
        self.assertTrue(detail["complete"])
        self.assertEqual(
            (detail["expected_decided"], detail["expected_hits"]),
            (fixture["expected_decided"], fixture["expected_hits"]),
        )
        self.assertEqual(len(detail["rows"]), fixture["expected_decided"])
        self.assertEqual(
            sum(row["hit"] for row in detail["rows"]),
            fixture["expected_hits"],
        )
        self.assertEqual(
            sum(not row["hit"] for row in detail["rows"]),
            fixture["expected_misses"],
        )
        identities = [row["evidence_identity"] for row in detail["rows"]]
        self.assertEqual(len(set(identities)), fixture["expected_decided"])
        repeated = next(
            card for card in project_frozen_ranking_evidence(
                ledger, fixture["system"], ranking,
            )
            if card.get("condition_number") == fixture["condition_number"]
        )["pending_rollover_evidence"]
        self.assertEqual(
            identities,
            [row["evidence_identity"] for row in repeated["rows"]],
        )
        serialized = json.dumps(detail, ensure_ascii=False)
        self.assertNotIn("malformed-condition-17", serialized)
        self.assertNotIn("pending-condition-17", serialized)

        # Read-only projection must neither repair nor expose a card whose
        # durable active pointer disagrees with its immutable evidence chain.
        pointer_mutations = (
            lambda item: item.update(active_evidence_version=2),
            lambda item: item.update(active_evidence_hash="f" * 64),
            lambda item: item["active_evidence"].update(evidence_hash="f" * 64),
            lambda item: item.pop("active_evidence_version"),
            lambda item: item.pop("active_evidence_hash"),
            lambda item: item.pop("active_evidence"),
        )
        for mutate in pointer_mutations:
            corrupted = copy.deepcopy(ledger)
            corrupted_frozen = next(
                item for item in
                corrupted["wilson_validation"]["conditions"].values()
                if item.get("condition_number") == fixture["condition_number"]
            )
            mutate(corrupted_frozen)
            before_object = copy.deepcopy(corrupted)
            before_bytes = json.dumps(
                corrupted, ensure_ascii=False, sort_keys=True,
                separators=(",", ":"),
            )
            projected = project_frozen_ranking_evidence(
                corrupted, fixture["system"], ranking,
            )
            self.assertFalse(any(
                card.get("condition_signature")
                == frozen["signature"]
                for card in projected
            ))
            self.assertEqual(corrupted, before_object)
            self.assertEqual(
                json.dumps(
                    corrupted, ensure_ascii=False, sort_keys=True,
                    separators=(",", ":"),
                ),
                before_bytes,
            )

    def test_footbreak_seventeen_timestamp_compatibility_fails_closed(self):
        ledger = {"bets": []}
        seeds = []
        for index in range(1, 17):
            seed = copy.deepcopy(candidate())
            seed["line_bucket"] = f"seed-{index}"
            seed["key"] = [
                (
                    f"bucket=seed-{index}"
                    if value.startswith("bucket=") else value
                )
                for value in seed["key"]
            ]
            seeds.append(seed)
        ranking = [*seeds, candidate()]
        project_granular_ranking_evidence(
            ledger, "footbreak", ranking,
            now="2026-08-20T00:00:00+08:00",
        )
        rows = [
            self._settled(
                ledger, index, result="Won" if index <= 10 else "Lost",
            )
            for index in range(1, 19)
        ]
        recompute_namespace(ledger, "footbreak")
        self._authorize_production_manifest(ledger)
        for row in rows:
            row.pop("native_stage_at")
            row["settled_at"] = row["created_at"]

        def detail_for(candidate_ledger):
            return next(
                card for card in project_frozen_ranking_evidence(
                    candidate_ledger, "footbreak", ranking,
                )
                if card.get("condition_number") == 17
            )["pending_rollover_evidence"]

        mutations = (
            lambda row: row.update(frozen_condition_signature="0" * 24),
            lambda row: row.update(condition_number=16),
            lambda row: row.update(frozen_condition_definition={}),
            lambda row: row["rollover_provenance"].update(schema_version=2),
            lambda row: row["rollover_provenance"].update(
                admitted_evidence_version=2,
            ),
            lambda row: row.update(evidence_version=2),
            lambda row: row.update(evidence_hash="f" * 64),
            lambda row: row["rollover_provenance"].update(
                fixture_market_hash="f" * 64,
            ),
            lambda row: row.update(result="Refunded"),
            lambda row: row.update(settled_at=(
                datetime.fromisoformat(row["created_at"]) - timedelta(seconds=1)
            ).isoformat()),
            lambda row: row.update(post_hoc_backfill=True),
            lambda row: row.update(exclude_from_simulation=True),
        )
        for mutate in mutations:
            tampered = copy.deepcopy(ledger)
            mutate(tampered["bets"][0])
            blocked = detail_for(tampered)
            self.assertFalse(blocked["complete"])
            self.assertEqual(blocked["rows"], [])
            self.assertEqual(
                blocked["unavailable_reason"],
                "pending_row_identity_mismatch",
            )

        duplicate = copy.deepcopy(ledger)
        duplicate["bets"].append(copy.deepcopy(duplicate["bets"][0]))
        blocked = detail_for(duplicate)
        self.assertFalse(blocked["complete"])
        self.assertEqual(blocked["rows"], [])

        wrong_hits = copy.deepcopy(ledger)
        wrong_hits["bets"][0]["result"] = "Lost"
        blocked = detail_for(wrong_hits)
        self.assertFalse(blocked["complete"])
        self.assertEqual(blocked["rows"], [])

        halfway = copy.deepcopy(ledger)
        for row in halfway["bets"]:
            created = datetime.fromisoformat(row["created_at"])
            kickoff = datetime.fromisoformat(row["kickoff"])
            row["settled_at"] = (
                created + (kickoff - created) / 2
            ).isoformat()
        blocked = detail_for(halfway)
        self.assertFalse(blocked["complete"])
        self.assertEqual(blocked["rows"], [])

        naive_equal = copy.deepcopy(ledger)
        for row in naive_equal["bets"]:
            row["created_at"] = datetime.fromisoformat(
                row["created_at"],
            ).replace(tzinfo=None).isoformat()
            row["settled_at"] = row["created_at"]
        blocked = detail_for(naive_equal)
        self.assertFalse(blocked["complete"])
        self.assertEqual(blocked["rows"], [])

        # Current canonical rows retain their existing path and result.
        current = copy.deepcopy(ledger)
        for row in current["bets"]:
            row["native_stage_at"] = row["rollover_provenance"]["stage_at"]
            row["settled_at"] = (
                datetime.fromisoformat(row["kickoff"]) + timedelta(hours=1)
            ).isoformat()
        current_detail = detail_for(current)
        self.assertTrue(current_detail["complete"])
        self.assertEqual(len(current_detail["rows"]), 18)
        self.assertEqual(sum(row["hit"] for row in current_detail["rows"]), 10)

    def test_footbreak_seventeen_legacy_subset_progresses_to_nineteen_and_rollover(self):
        seeds = []
        for index in range(1, 17):
            seed = copy.deepcopy(candidate())
            seed["line_bucket"] = f"seed-{index}"
            seed["key"] = [
                f"bucket=seed-{index}" if value.startswith("bucket=") else value
                for value in seed["key"]
            ]
            seeds.append(seed)
        ranking = [*seeds, candidate()]
        ledger = {"bets": []}
        project_granular_ranking_evidence(
            ledger, "footbreak", ranking,
            now="2026-08-20T00:00:00+08:00",
        )
        legacy_rows = [
            self._settled(
                ledger, index, result="Won" if index <= 10 else "Lost",
            )
            for index in range(1, 19)
        ]
        recompute_namespace(ledger, "footbreak")
        self._authorize_production_manifest(ledger)
        frozen = next(
            item for item in ledger["wilson_validation"]["conditions"].values()
            if item.get("condition_number") == 17
        )
        self.assertEqual(frozen["pending_rollover_progress"]["display"], "18/20")
        for row in legacy_rows:
            row.pop("native_stage_at")
            row["settled_at"] = row["created_at"]
        immutable_legacy = copy.deepcopy(legacy_rows)

        row_19 = self._settled(ledger, 19, result="Lost")
        recompute_namespace(ledger, "footbreak")
        self.assertEqual(legacy_rows, immutable_legacy)
        self.assertEqual(frozen["active_evidence_version"], 1)
        self.assertEqual(
            (
                frozen["pending_rollover_progress"]["eligible_decided"],
                frozen["pending_rollover_progress"]["eligible_hits"],
            ),
            (19, 10),
        )
        detail_19 = next(
            card for card in project_frozen_ranking_evidence(
                ledger, "footbreak", ranking,
            )
            if card.get("condition_number") == 17
        )["pending_rollover_evidence"]
        self.assertTrue(detail_19["complete"])
        self.assertEqual(len(detail_19["rows"]), 19)
        self.assertEqual(sum(row["hit"] for row in detail_19["rows"]), 10)
        self.assertEqual(
            len({row["evidence_identity"] for row in detail_19["rows"]}), 19,
        )

        substituted = copy.deepcopy(ledger)
        restored = substituted["bets"][0]
        restored["native_stage_at"] = restored["rollover_provenance"]["stage_at"]
        restored["settled_at"] = (
            datetime.fromisoformat(restored["kickoff"]) + timedelta(hours=1)
        ).isoformat()
        substituted_frozen = next(
            item for item in
            substituted["wilson_validation"]["conditions"].values()
            if item.get("condition_number") == 17
        )
        before_substituted_progress = copy.deepcopy(
            substituted_frozen["pending_rollover_progress"],
        )
        recompute_namespace(substituted, "footbreak")
        self.assertEqual(
            substituted_frozen["pending_rollover_progress"],
            before_substituted_progress,
        )
        substituted_detail = next(
            card for card in project_frozen_ranking_evidence(
                substituted, "footbreak", ranking,
            )
            if card.get("condition_number") == 17
        )["pending_rollover_evidence"]
        self.assertFalse(substituted_detail["complete"])
        self.assertEqual(substituted_detail["rows"], [])

        row_20 = self._settled(ledger, 20, result="Lost")
        recompute_namespace(ledger, "footbreak")
        self.assertEqual(legacy_rows, immutable_legacy)
        self.assertEqual(frozen["active_evidence_version"], 2)
        self.assertEqual(frozen["pending_rollover_progress"]["display"], "0/20")
        self.assertEqual(len(frozen["rollover_audit"]), 1)
        rolled = frozen["rollover_audit"][0]
        self.assertEqual((rolled["batch_decided"], rolled["batch_hits"]), (20, 10))
        self.assertEqual(
            rolled["batch_fixture_market_hashes"],
            [
                row["rollover_provenance"]["fixture_market_hash"]
                for row in [*legacy_rows, row_19, row_20]
            ],
        )
        card_20 = next(
            card for card in project_frozen_ranking_evidence(
                ledger, "footbreak", ranking,
            )
            if card.get("condition_number") == 17
        )
        self.assertTrue(card_20["pending_rollover_evidence"]["complete"])
        self.assertEqual(card_20["pending_rollover_evidence"]["rows"], [])
        merged = card_20["last_merged_evidence"]
        self.assertTrue(merged["complete"])
        self.assertEqual(len(merged["rows"]), 20)
        self.assertEqual(sum(row["hit"] for row in merged["rows"]), 10)

    def test_footbreak_seventeen_requires_exact_registry_position_and_signature(self):
        ledger = {"bets": []}
        seeds = []
        for index in range(1, 17):
            seed = copy.deepcopy(candidate())
            seed["line_bucket"] = f"seed-{index}"
            seed["key"] = [
                (
                    f"bucket=seed-{index}"
                    if value.startswith("bucket=") else value
                )
                for value in seed["key"]
            ]
            seeds.append(seed)
        ranking = [*seeds, candidate()]
        project_granular_ranking_evidence(
            ledger, "footbreak", ranking,
            now="2026-08-20T00:00:00+08:00",
        )
        rows = [
            self._settled(
                ledger, index, result="Won" if index <= 10 else "Lost",
            )
            for index in range(1, 19)
        ]
        recompute_namespace(ledger, "footbreak")
        self._authorize_production_manifest(ledger)
        for row in rows:
            row.pop("native_stage_at")
            row["settled_at"] = row["created_at"]
        signature = rows[0]["frozen_condition_signature"]

        def target_detail(candidate_ledger):
            cards = project_frozen_ranking_evidence(
                candidate_ledger, "footbreak", ranking,
            )
            target = next(
                (
                    card for card in cards
                    if card.get("condition_signature") == signature
                ),
                None,
            )
            return (
                target.get("pending_rollover_evidence")
                if isinstance(target, dict) else None
            )

        moved = copy.deepcopy(ledger)
        order = moved["wilson_validation"]["condition_order"]
        order[0], order[16] = order[16], order[0]
        detail = target_detail(moved)
        self.assertIsNotNone(detail)
        self.assertFalse(detail["complete"])
        self.assertEqual(detail["rows"], [])

        duplicated = copy.deepcopy(ledger)
        duplicated["wilson_validation"]["condition_order"][0] = signature
        detail = target_detail(duplicated)
        self.assertIsNotNone(detail)
        self.assertFalse(detail["complete"])
        self.assertEqual(detail["rows"], [])

        missing_manifest = copy.deepcopy(ledger)
        missing_manifest["wilson_validation"].pop("production_identity_manifest")
        before_missing_manifest = copy.deepcopy(missing_manifest)
        detail = target_detail(missing_manifest)
        self.assertIsNotNone(detail)
        self.assertFalse(detail["complete"])
        self.assertEqual(detail["rows"], [])
        self.assertEqual(missing_manifest, before_missing_manifest)

        definition_tampered = copy.deepcopy(ledger)
        definition_tampered["wilson_validation"]["conditions"][signature][
            "definition"
        ]["movement"] = "tampered"
        self.assertIsNone(target_detail(definition_tampered))

        # Even a completely coherent registry rewrite cannot authorize itself.
        # The independently authorized pre-swap manifest remains stale while
        # condition #1 is moved to slot #17 and all mutable numbers are changed.
        coherent = {"bets": []}
        attack_ranking = [candidate(), *seeds]
        project_granular_ranking_evidence(
            coherent, "footbreak", attack_ranking,
            now="2026-08-20T00:00:00+08:00",
        )
        coherent_rows = [
            self._settled(
                coherent, index, result="Won" if index <= 10 else "Lost",
            )
            for index in range(1, 19)
        ]
        recompute_namespace(coherent, "footbreak")
        stale_manifest = self._authorize_production_manifest(coherent)
        coherent_ns = coherent["wilson_validation"]
        coherent_signature = coherent_rows[0]["frozen_condition_signature"]
        coherent_order = coherent_ns["condition_order"]
        coherent_order[0], coherent_order[16] = (
            coherent_order[16], coherent_order[0],
        )
        for number, ordered_signature in enumerate(coherent_order, start=1):
            coherent_ns["conditions"][ordered_signature]["condition_number"] = number
        for row in coherent_rows:
            row["condition_number"] = 17
            row.pop("native_stage_at")
            row["settled_at"] = row["created_at"]
        self.assertEqual(
            coherent_ns["production_identity_manifest"], stale_manifest,
        )
        coherent_card = next(
            card for card in project_frozen_ranking_evidence(
                coherent, "footbreak", attack_ranking,
            )
            if card.get("condition_signature") == coherent_signature
        )
        coherent_detail = coherent_card["pending_rollover_evidence"]
        self.assertFalse(coherent_detail["complete"])
        self.assertEqual(coherent_detail["rows"], [])
        before_progress = copy.deepcopy(
            coherent_ns["conditions"][coherent_signature][
                "pending_rollover_progress"
            ],
        )
        self._settled(coherent, 19, result="Lost")
        recompute_namespace(coherent, "footbreak")
        self.assertEqual(
            coherent_ns["conditions"][coherent_signature][
                "pending_rollover_progress"
            ],
            before_progress,
        )

        # A genuine condition #1 coherently relabeled in its frozen record and
        # all rows is still not production condition #17.
        relabeled = {"bets": []}
        project_granular_ranking_evidence(
            relabeled, "footbreak", [candidate()],
            now="2026-08-20T00:00:00+08:00",
        )
        relabeled_rows = [
            self._settled(
                relabeled, index, result="Won" if index <= 10 else "Lost",
            )
            for index in range(1, 19)
        ]
        recompute_namespace(relabeled, "footbreak")
        relabeled_frozen = next(
            iter(relabeled["wilson_validation"]["conditions"].values()),
        )
        relabeled_frozen["condition_number"] = 17
        for row in relabeled_rows:
            row["condition_number"] = 17
            row.pop("native_stage_at")
            row["settled_at"] = row["created_at"]
        card = project_frozen_ranking_evidence(
            relabeled, "footbreak", [candidate()],
        )[0]
        detail = card["pending_rollover_evidence"]
        self.assertFalse(detail["complete"])
        self.assertEqual(detail["rows"], [])

    def test_crown_condition_fourteen_projects_mixed_binding_pending_cohort(self):
        ledger = {"bets": []}
        seeds = []
        for index in range(1, 14):
            seed = copy.deepcopy(candidate(system="crown"))
            seed["line_bucket"] = f"seed-{index}"
            seed["key"] = [
                (
                    f"bucket=seed-{index}"
                    if value.startswith("bucket=") else value
                )
                for value in seed["key"]
            ]
            seeds.append(seed)
        project_granular_ranking_evidence(
            ledger, "crown", [*seeds, candidate(system="crown")],
            now="2026-08-20T00:00:00+08:00",
        )
        rows = [
            self._settled(
                ledger, index,
                result="Won" if index <= 4 else "Lost",
                system="crown",
            )
            for index in range(1, 8)
        ]
        recompute_namespace(ledger, "crown")
        for row in rows[:3]:
            row.pop("native_stage_at")

        frozen_rows = list(
            ledger["wilson_validation"]["conditions"].values(),
        )
        published_candidates = []
        for frozen in frozen_rows:
            item = copy.deepcopy(frozen["definition"])
            item["key"] = copy.deepcopy(item.pop("miner_key"))
            published_candidates.append(item)
        target = next(
            row for row in project_frozen_ranking_evidence(
                ledger, "crown", published_candidates,
            )
            if row.get("condition_number") == 14
        )
        detail = target["pending_rollover_evidence"]
        self.assertTrue(detail["complete"])
        self.assertEqual(
            (detail["expected_decided"], detail["expected_hits"]), (7, 4),
        )
        self.assertEqual(len(detail["rows"]), 7)
        self.assertEqual(sum(row["hit"] for row in detail["rows"]), 4)

        duplicate_substitution = copy.deepcopy(ledger)
        duplicate_substitution["bets"].append(
            copy.deepcopy(duplicate_substitution["bets"][-1]),
        )
        replacement = copy.deepcopy(duplicate_substitution["bets"][-1])
        old_match_id = replacement["match_id"]
        replacement["match_id"] = "crown-aggregate-preserving-replacement"
        replacement["bet_id"] = replacement["bet_id"].replace(
            old_match_id, replacement["match_id"], 1,
        )
        replacement["rollover_provenance"]["fixture_market_hash"] = (
            _fixture_market_hash(
                "crown", replacement["match_id"], replacement["market"],
            )
        )
        duplicate_substitution["bets"].append(replacement)
        blocked_target = next(
            row for row in project_frozen_ranking_evidence(
                duplicate_substitution, "crown", published_candidates,
            )
            if row.get("condition_number") == 14
        )
        self.assertFalse(
            blocked_target["pending_rollover_evidence"]["complete"],
        )
        self.assertEqual(
            blocked_target["pending_rollover_evidence"]["rows"], [],
        )

    def test_crown_read_only_projection_includes_batch_rows_without_mutating_ledger(self):
        ledger = {"bets": []}
        for index in range(1, 21):
            self._settled(
                ledger, index, system="crown",
                result="Won" if index <= 13 else "Lost",
            )
        recompute_namespace(ledger, "crown")
        before = copy.deepcopy(ledger)
        frozen, _active = self._active(ledger)
        crown_candidate = copy.deepcopy(frozen["definition"])
        crown_candidate["key"] = copy.deepcopy(
            crown_candidate.pop("miner_key"),
        )

        projected = project_frozen_ranking_evidence(
            ledger, "crown", [crown_candidate],
        )

        self.assertEqual(ledger, before)
        self.assertEqual(len(projected), 1)
        detail = projected[0]["last_merged_evidence"]
        self.assertTrue(detail["complete"])
        self.assertEqual(detail["version"], 2)
        self.assertEqual(detail["expected_decided"], 20)
        self.assertEqual(detail["expected_hits"], 13)
        self.assertEqual(len(detail["rows"]), 20)
        self.assertEqual(sum(row["hit"] for row in detail["rows"]), 13)

    def test_last_merged_batch_detail_fails_closed_on_identity_mismatch(self):
        ledger = {"bets": []}
        for index in range(1, 21):
            self._settled(
                ledger, index, result="Won" if index <= 13 else "Lost",
            )
        recompute_namespace(ledger, "footbreak")
        frozen, _ = self._active(ledger)
        frozen["rollover_audit"][-1]["batch_fixture_market_hashes"][7] = "f" * 64

        projected = project_granular_ranking_evidence(
            ledger, "footbreak", [candidate()],
            now="2026-08-22T00:00:00+08:00",
        )
        detail = projected[0]["last_merged_evidence"]
        self.assertFalse(detail["complete"])
        self.assertEqual(detail["rows"], [])
        self.assertEqual(
            detail["unavailable_reason"], "batch_row_identity_mismatch",
        )

    def test_future_formal_odds_gate_uses_active_version_not_old_baseline(self):
        ledger = {"bets": []}
        for index in range(1, 21):
            self._settled(ledger, index, result="Lost")
        recompute_namespace(ledger, "footbreak")
        frozen, active = self._active(ledger)
        decision_at = (
            datetime.fromisoformat(active["created_at"]) + timedelta(minutes=1)
        ).isoformat()
        admission, reason = apply_active_evidence(
            ledger, "footbreak", self._admission(),
            stage_at=decision_at,
            now=decision_at,
        )
        self.assertIsNone(reason)
        assert admission is not None
        self.assertEqual(admission["evidence_version"], 2)
        self.assertFalse(admission["arithmetic"]["passes"])
        self.assertTrue(self._admission()["arithmetic"]["passes"])


if __name__ == "__main__":
    unittest.main()
