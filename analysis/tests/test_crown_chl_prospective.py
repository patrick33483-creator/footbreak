"""Tests for the Crown CHL frozen prospective-only challenger.

Every check protects a safety property the operator asked for: no auto-apply,
no live change, one row per unique fixture, no stage leakage, an immutable
cutoff, and a candidate that fails closed when its features do not exist.
"""
from __future__ import annotations

import json
import os
import stat
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

from analysis import crown_chl_prospective as chl
from analysis.challenger_model import evaluate_all, public_report, run
from analysis.learning_store import LearningStore

CUTOFF = datetime(2026, 6, 1, tzinfo=timezone.utc)
STAGES = ("首預", "T-30", "T-5")


def payload(
    *,
    side: str = "L",
    line: str = "9.5",
    odds: float = 1.9,
    league: str = "英超",
    team_corners: dict | None = None,
) -> dict:
    body = {
        "league": league,
        "conviction": 60,
        "market_predictions": [{
            "code": "CHL",
            "market": "CHL",
            "condition": line,
            "line": line,
            "side": side,
            "label": f"CHL {line} {side}",
            "odds": odds,
            "probability": 0.55,
            "source": "pinnapi_exact_line",
            "provider": "Crown",
        }],
    }
    if team_corners is not None:
        body["team_corners"] = team_corners
    return body


class Builder:
    """Creates an immutable learning database of Crown CHL fixtures."""

    def __init__(self, directory: Path) -> None:
        self.path = directory / "learning.sqlite"

    def add(
        self,
        index: int,
        kickoff: datetime,
        *,
        stages: tuple[str, ...] = STAGES,
        side: str = "L",
        hit: bool = True,
        probability: float = 0.55,
        team_corners: dict | None = None,
        stage_probability: dict[str, float] | None = None,
    ) -> None:
        fixture_id = f"chl{index:04d}"
        with LearningStore(self.path) as store:
            result = store.record_result(
                "crown", fixture_id, home_score=1, away_score=1, source="titan",
            )
            for order, stage in enumerate(stages):
                generated = kickoff - timedelta(hours=len(stages) - order)
                snapshot = store.record_snapshot(
                    "crown", fixture_id, stage, generated, kickoff,
                    payload(side=side, team_corners=team_corners),
                )
                store.record_grade(
                    snapshot["snapshot_id"], "CHL", f"9.5|{side}", "GRADED",
                    {
                        "probability": (stage_probability or {}).get(stage, probability),
                        "target": 1.0 if hit else 0.0,
                        "hit": hit,
                        "brier": 0.2,
                        "log_loss": 0.6,
                    },
                    result_id=result["result_id"],
                )

    def rows(self) -> list[dict]:
        with LearningStore(self.path) as store:
            rows, _ = store.challenger_rows("crown")
        return [row for row in rows if row["market"] == "CHL"]


class CrownChlPrimaryUnitTests(unittest.TestCase):
    def setUp(self) -> None:
        self._directory = tempfile.TemporaryDirectory()
        self.directory = Path(self._directory.name)
        self.addCleanup(self._directory.cleanup)
        self.builder = Builder(self.directory)

    def test_primary_unit_is_one_row_per_unique_fixture(self) -> None:
        for index in range(5):
            self.builder.add(index, CUTOFF + timedelta(days=index + 1))
        rows = self.builder.rows()
        self.assertEqual(len(rows), 15)
        primary = chl.primary_rows(rows)
        self.assertEqual(len(primary), 5)
        self.assertEqual(len({row["match_id"] for row in primary}), 5)
        # The predeclared rule keeps the latest available pre-kickoff stage.
        self.assertEqual({row["stage"] for row in primary}, {"T-5"})

    def test_primary_stage_rule_falls_back_without_a_t5_snapshot(self) -> None:
        self.builder.add(0, CUTOFF + timedelta(days=1), stages=("首預", "T-30"))
        self.builder.add(1, CUTOFF + timedelta(days=2), stages=("首預",))
        self.builder.add(2, CUTOFF + timedelta(days=3), stages=STAGES)
        primary = {row["match_id"]: row["stage"] for row in chl.primary_rows(self.builder.rows())}
        self.assertEqual(primary["chl0000"], "T-30")
        self.assertEqual(primary["chl0001"], "首預")
        self.assertEqual(primary["chl0002"], "T-5")
        self.assertEqual(chl.PRIMARY_STAGE_PRIORITY, ("T-5", "T-30", "首預"))

    def test_stage_diagnostics_are_reported_separately_and_labelled_correlated(self) -> None:
        for index in range(3):
            self.builder.add(index, CUTOFF + timedelta(days=index + 1))
        diagnostics = chl._stage_diagnostics(self.builder.rows())
        self.assertEqual([item["stage"] for item in diagnostics], list(STAGES))
        for item in diagnostics:
            self.assertEqual(item["unique_fixtures"], 3)
            self.assertTrue(item["correlated_secondary_diagnostic"])
            self.assertIn("唔可以相加", item["note"])

    def test_closing_reference_marks_fixtures_without_a_t5_snapshot(self) -> None:
        self.builder.add(0, CUTOFF + timedelta(days=1), stages=("首預", "T-30"))
        self.builder.add(1, CUTOFF + timedelta(days=2), stages=STAGES)
        reference = chl._closing_reference(self.builder.rows())
        self.assertTrue(reference["benchmark_only"])
        self.assertTrue(reference["excluded_from_promotion_gate"])
        self.assertEqual(reference["covered_fixtures"], 1)
        self.assertEqual(reference["fixtures_without_t5"], 1)
        self.assertEqual(reference["coverage"], 0.5)

    def test_closing_reference_is_unavailable_without_any_t5(self) -> None:
        self.builder.add(0, CUTOFF + timedelta(days=1), stages=("首預",))
        reference = chl._closing_reference(self.builder.rows())
        self.assertFalse(reference["available"])
        self.assertEqual(reference["status"], "unavailable_no_t5_snapshot")
        self.assertIsNone(reference["metrics"])

    def test_earlier_stage_metrics_never_borrow_the_t5_direction(self) -> None:
        # A fixture whose 首預 direction differs from its T-5 direction must be
        # scored at 首預 with the 首預 probability, never the later one.
        self.builder.add(
            0, CUTOFF + timedelta(days=1),
            stage_probability={"首預": 0.20, "T-30": 0.50, "T-5": 0.90},
        )
        rows = self.builder.rows()
        first = chl.stage_rows(rows, "首預")
        self.assertEqual(len(first), 1)
        self.assertAlmostEqual(first[0]["probability"], 0.20)
        metrics = chl.strategy_metrics(chl.CHAMPION_STRATEGY, first)
        self.assertEqual(metrics["n"], 1)
        # Brier from the 首預 probability alone: (0.2 - 1)^2 = 0.64
        self.assertAlmostEqual(metrics["brier"], 0.64, places=4)

    def test_always_under_mirrors_probability_and_target(self) -> None:
        self.builder.add(0, CUTOFF + timedelta(days=1), side="L", hit=True, probability=0.7)
        primary = chl.primary_rows(self.builder.rows())
        champion = chl.strategy_metrics(chl.CHAMPION_STRATEGY, primary)
        under = chl.strategy_metrics("always_under", primary)
        self.assertAlmostEqual(champion["brier"], (0.7 - 1.0) ** 2, places=4)
        self.assertAlmostEqual(under["brier"], (0.3 - 0.0) ** 2, places=4)
        self.assertEqual(champion["hits"], 1)
        self.assertEqual(under["hits"], 0)
        self.assertIsNotNone(champion["hit_rate_ci95"])


class CrownChlFrozenStateTests(unittest.TestCase):
    def setUp(self) -> None:
        self._directory = tempfile.TemporaryDirectory()
        self.directory = Path(self._directory.name)
        self.addCleanup(self._directory.cleanup)
        self.builder = Builder(self.directory)
        self.state_path = self.directory / "challenger" / "crown_chl_state.json"

    def _history(self, count: int = 70) -> None:
        for index in range(count):
            self.builder.add(
                index,
                CUTOFF - timedelta(days=count - index),
                hit=index % 2 == 0,
                side="L" if index % 3 else "S",
            )

    def test_state_is_frozen_once_and_reloaded_byte_for_byte(self) -> None:
        self._history()
        rows = self.builder.rows()
        first = chl.resolve(rows, self.state_path, now=CUTOFF)
        raw = self.state_path.read_bytes()
        mtime = self.state_path.stat().st_mtime_ns
        second = chl.resolve(rows, self.state_path, now=CUTOFF + timedelta(days=30))
        self.assertEqual(self.state_path.read_bytes(), raw)
        self.assertEqual(self.state_path.stat().st_mtime_ns, mtime)
        self.assertEqual(first["freeze_cutoff"], second["freeze_cutoff"])
        self.assertEqual(first["state_version_hash"], second["state_version_hash"])
        self.assertEqual(first["selected_strategy"], second["selected_strategy"])

    def test_frozen_state_permissions_are_private(self) -> None:
        self._history()
        chl.resolve(self.builder.rows(), self.state_path, now=CUTOFF)
        self.assertEqual(stat.S_IMODE(self.state_path.stat().st_mode), 0o600)
        self.assertEqual(stat.S_IMODE(self.state_path.parent.stat().st_mode), 0o700)

    def test_exact_cutoff_fixture_is_excluded_from_both_sides(self) -> None:
        self._history()
        self.builder.add(900, CUTOFF)          # exactly at the cutoff
        self.builder.add(901, CUTOFF + timedelta(seconds=1))
        rows = self.builder.rows()
        state = chl.build_state(rows, CUTOFF)
        self.assertIsNotNone(state)
        historical = [row for row in chl.primary_rows(rows) if row["kickoff"] < CUTOFF]
        self.assertNotIn("chl0900", {row["match_id"] for row in historical})
        report = chl.evaluate_prospective(rows, state)
        self.assertEqual(report["prospective_fixtures"], 1)
        self.assertIn("exact cutoff excluded", report["cutoff_boundary"])

    def test_selection_excludes_the_already_inspected_recent_holdout(self) -> None:
        self._history(count=100)
        state = chl.build_state(self.builder.rows(), CUTOFF)
        selection = state["selection"]
        self.assertEqual(selection["historical_fixtures_before_cutoff"], 100)
        self.assertEqual(selection["selection_fixtures"], 70)
        self.assertEqual(selection["excluded_recent_holdout_fixtures"], 30)
        self.assertEqual(selection["fold_count"], chl.WALK_FORWARD_FOLDS)
        self.assertIn("walk_forward", selection["method"])

    def test_state_is_not_created_without_enough_pre_freeze_history(self) -> None:
        self._history(count=10)
        report = chl.resolve(self.builder.rows(), self.state_path, now=CUTOFF)
        self.assertFalse(self.state_path.exists())
        self.assertEqual(report["status"], "prospective_shadow_collecting")
        self.assertEqual(
            report["reason"],
            "insufficient_pre_freeze_history_for_three_walk_forward_folds",
        )

    def test_altered_state_is_rejected_instead_of_silently_trusted(self) -> None:
        self._history()
        chl.resolve(self.builder.rows(), self.state_path, now=CUTOFF)
        tampered = json.loads(self.state_path.read_text(encoding="utf-8"))
        tampered["frozen"]["selected_strategy"] = "team_corner_feature"
        self.state_path.write_text(json.dumps(tampered), encoding="utf-8")
        with self.assertRaises(ValueError):
            chl.load_state(self.state_path)


class CrownChlProspectiveWindowTests(unittest.TestCase):
    def setUp(self) -> None:
        self._directory = tempfile.TemporaryDirectory()
        self.directory = Path(self._directory.name)
        self.addCleanup(self._directory.cleanup)
        self.builder = Builder(self.directory)
        self.state_path = self.directory / "challenger" / "crown_chl_state.json"
        for index in range(70):
            self.builder.add(
                index, CUTOFF - timedelta(days=70 - index),
                hit=index % 2 == 0, side="L" if index % 3 else "S",
            )

    def _future(self, count: int) -> None:
        for index in range(count):
            self.builder.add(
                1000 + index, CUTOFF + timedelta(days=index + 1),
                hit=index % 2 == 0,
            )

    def test_twenty_nine_fixtures_keep_collecting_and_thirty_are_evaluated(self) -> None:
        self._future(29)
        collecting = chl.resolve(self.builder.rows(), self.state_path, now=CUTOFF)
        self.assertEqual(collecting["status"], "prospective_shadow_collecting")
        self.assertEqual(collecting["prospective_fixtures"], 29)
        self.assertEqual(collecting["remaining_fixtures"], 1)

        self.builder.add(1029, CUTOFF + timedelta(days=30), hit=True)
        evaluated = chl.resolve(self.builder.rows(), self.state_path, now=CUTOFF)
        self.assertEqual(evaluated["prospective_fixtures"], 30)
        self.assertIn(
            evaluated["status"],
            {"prospective_tested_no_safe_upgrade", "candidate_passed_human_review_required"},
        )
        self.assertEqual(evaluated["remaining_fixtures"], 0)
        self.assertEqual(evaluated["champion"]["metrics"]["unique_fixtures"], 30)
        self.assertEqual(evaluated["champion"]["metrics"]["n"], 30)

    def test_prospective_rows_count_all_stages_but_fixtures_stay_unique(self) -> None:
        self._future(30)
        report = chl.resolve(self.builder.rows(), self.state_path, now=CUTOFF)
        self.assertEqual(report["prospective_fixtures"], 30)
        self.assertEqual(report["prospective_rows"], 90)
        self.assertEqual(report["primary_unit"], "one_row_per_unique_fixture")

    def test_sample_warning_below_one_hundred_unique_fixtures(self) -> None:
        self._future(35)
        report = chl.resolve(self.builder.rows(), self.state_path, now=CUTOFF)
        self.assertEqual(report["sample_warning"], "below_strong_sample")
        self.assertEqual(report["strong_sample_fixtures"], 100)

    def test_no_upgrade_when_the_selected_strategy_equals_the_champion(self) -> None:
        self._future(30)
        report = chl.resolve(self.builder.rows(), self.state_path, now=CUTOFF)
        if report["selected_strategy"] == chl.CHAMPION_STRATEGY:
            self.assertEqual(report["status"], "prospective_tested_no_safe_upgrade")
            self.assertIn("candidate_differs_from_champion", report["rejection_reasons"])
            self.assertFalse(report["checks"]["candidate_differs_from_champion"])

    def test_shadow_returns_never_claim_edge_and_clv_is_unavailable(self) -> None:
        self._future(30)
        report = chl.resolve(self.builder.rows(), self.state_path, now=CUTOFF)
        shadow = report["shadow_returns"]
        self.assertIsNone(shadow["clv"])
        self.assertEqual(shadow["reason"], "closing_odds_unavailable")
        self.assertIn("唔係優勢或 +EV", shadow["note"])

    def test_report_never_authorises_any_live_change(self) -> None:
        self._future(30)
        report = chl.resolve(self.builder.rows(), self.state_path, now=CUTOFF)
        self.assertFalse(report["auto_apply"])
        self.assertFalse(report["retraining"])
        self.assertFalse(report["probability_artifact_written"])
        self.assertEqual(report["live_integration"], "none")


class CrownChlTeamFeatureTests(unittest.TestCase):
    def setUp(self) -> None:
        self._directory = tempfile.TemporaryDirectory()
        self.directory = Path(self._directory.name)
        self.addCleanup(self._directory.cleanup)
        self.builder = Builder(self.directory)
        self.state_path = self.directory / "challenger" / "crown_chl_state.json"

    def test_team_feature_candidate_fails_closed_when_features_do_not_exist(self) -> None:
        for index in range(70):
            self.builder.add(index, CUTOFF - timedelta(days=70 - index), hit=index % 2 == 0)
        state = chl.build_state(self.builder.rows(), CUTOFF)
        coverage = state["selection"]["team_feature_coverage"]
        self.assertFalse(coverage["eligible"])
        self.assertEqual(coverage["minimum_observed_coverage"], 0.0)
        candidate = next(
            item for item in state["selection"]["candidates"]
            if item["id"] == "team_corner_feature"
        )
        self.assertEqual(candidate["status"], "insufficient_feature_coverage")
        self.assertIsNone(candidate["metrics"])
        self.assertNotEqual(state["frozen"]["selected_strategy"], "team_corner_feature")
        self.assertIsNone(state["private_model"])
        # Nothing was invented from post-match data.
        self.assertIn("never imputed", coverage["policy"])

    def test_team_feature_candidate_is_scored_when_features_genuinely_exist(self) -> None:
        for index in range(70):
            self.builder.add(
                index, CUTOFF - timedelta(days=70 - index), hit=index % 2 == 0,
                team_corners={
                    "home_for_avg": 5.0 + (index % 5) * 0.3,
                    "away_for_avg": 4.5 + (index % 4) * 0.2,
                    "home_against_avg": 4.8,
                    "away_against_avg": 5.1,
                    "sample_matches": 12,
                },
            )
        state = chl.build_state(self.builder.rows(), CUTOFF)
        coverage = state["selection"]["team_feature_coverage"]
        self.assertTrue(coverage["eligible"])
        candidate = next(
            item for item in state["selection"]["candidates"]
            if item["id"] == "team_corner_feature"
        )
        self.assertEqual(candidate["status"], "scored")
        self.assertIsNotNone(candidate["metrics"]["brier"])


class CrownChlIntegrationTests(unittest.TestCase):
    def setUp(self) -> None:
        self._directory = tempfile.TemporaryDirectory()
        self.directory = Path(self._directory.name)
        self.addCleanup(self._directory.cleanup)
        self.builder = Builder(self.directory)

    def test_report_is_nested_under_crown_chl_only(self) -> None:
        for index in range(6):
            self.builder.add(index, CUTOFF + timedelta(days=index + 1))
        state_path = self.directory / "challenger" / "crown_chl_state.json"
        with LearningStore(self.builder.path) as store:
            report = evaluate_all(store, chl_state_path=state_path, now=CUTOFF)
        crown = report["systems"]["crown"]["tests"]
        self.assertIn("prospective_chl", crown["CHL"])
        self.assertNotIn("prospective_chl", crown["HIL"])
        footbreak = report["systems"]["footbreak"]["tests"]
        for market in ("HDC", "HIL", "CHL"):
            self.assertNotIn("prospective_chl", footbreak[market])
        self.assertIn("CHL_prospective", report["policy"])
        self.assertFalse(report["policy"]["auto_apply"])

    def test_public_report_excludes_private_model_and_coefficients(self) -> None:
        for index in range(6):
            self.builder.add(index, CUTOFF + timedelta(days=index + 1))
        private_out = self.directory / "private" / "latest.json"
        public_out = self.directory / "www" / "challenger-status.json"
        run(
            self.builder.path,
            private_out,
            [public_out],
            hil_v3_state_path=self.directory / "challenger" / "hil.json",
            chl_state_path=self.directory / "challenger" / "chl.json",
            now=CUTOFF,
        )
        public_text = public_out.read_text(encoding="utf-8")
        for forbidden in (
            "private_model", "coefficient_importance", "training_source", "coefficients",
        ):
            self.assertNotIn(forbidden, public_text)
        self.assertEqual(oct(os.stat(public_out).st_mode & 0o777), "0o644")
        self.assertEqual(oct(os.stat(private_out).st_mode & 0o777), "0o600")
        self.assertIn("prospective_chl", public_text)

    def test_public_projection_keeps_the_private_report_intact(self) -> None:
        report = {
            "systems": {
                "crown": {
                    "tests": {
                        "CHL": {
                            "coefficient_importance": [{"feature": "x", "coefficient": 1.0}],
                            "prospective_chl": {"private_model": {"coefficients": [1.0]}},
                        }
                    }
                }
            }
        }
        public = public_report(report)
        self.assertNotIn("coefficient_importance", public["systems"]["crown"]["tests"]["CHL"])
        self.assertNotIn(
            "private_model",
            public["systems"]["crown"]["tests"]["CHL"]["prospective_chl"],
        )
        self.assertIn("coefficient_importance", report["systems"]["crown"]["tests"]["CHL"])

    def test_notification_only_fires_after_the_final_gate_passes(self) -> None:
        import importlib.util

        root = Path(__file__).resolve().parents[2]
        spec = importlib.util.spec_from_file_location(
            "footbreak_notify", root / "system" / "notify.py"
        )
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)

        def build(status: str) -> dict:
            return {
                "systems": {
                    "crown": {
                        "tests": {
                            "CHL": {
                                "status": "insufficient_data",
                                "prospective_chl": {
                                    "status": status,
                                    "state_version_hash": "abc123",
                                    "prospective_fixtures": 30,
                                    "prospective_rows": 90,
                                    "selected_strategy": "always_under",
                                    "delta": {"brier": -0.02, "log_loss": -0.03, "accuracy": 0.01},
                                },
                            }
                        }
                    }
                }
            }

        for status in (
            "prospective_shadow_collecting",
            "insufficient_feature_coverage",
            "prospective_tested_no_safe_upgrade",
        ):
            events = module.review_events(build(status))
            self.assertEqual(
                [event for event in events if "chl" in event["key"]], [], status
            )
        events = module.review_events(build("candidate_passed_human_review_required"))
        keys = [event["key"] for event in events]
        self.assertIn("prospective-chl:crown:CHL:abc123", keys)
        # The key is stable, so the daily timer cannot repeat one candidate.
        self.assertEqual(
            keys, [event["key"] for event in module.review_events(
                build("candidate_passed_human_review_required"))]
        )


if __name__ == "__main__":
    unittest.main()


def shadow_row(
    index: int,
    *,
    side: str = "L",
    target: float = 1.0,
    odds: float | None = 1.9,
    line: str = "9.5",
    alternates: list[dict] | None = None,
    extra: dict | None = None,
) -> dict:
    """A minimal immutable CHL row for shadow-return alignment tests.

    ``side`` is the side that was actually selected and priced; ``target`` is
    the settlement of *that* side.  ``alternates`` adds sibling quotes exactly
    as a richer immutable payload would carry them.
    """
    selected: dict = {
        "code": "CHL",
        "market": "CHL",
        "condition": line,
        "line": line,
        "side": side,
        "odds": odds,
        "probability": 0.55,
    }
    selected.update(extra or {})
    return {
        "match_id": f"shadow{index:03d}",
        "market": "CHL",
        "stage": "T-5",
        "target_key": f"{line}|{side}",
        "probability": 0.55,
        "target": target,
        "payload": {"market_predictions": [selected, *(alternates or [])]},
    }


class CrownChlShadowAlignmentTests(unittest.TestCase):
    """The shadow return must follow the direction the strategy actually takes.

    The stored odds and target always describe the *selected* side.  Reusing
    them for a different direction would silently invert the book, so anything
    that cannot be verified against a genuinely quoted opposite price has to
    fail closed with a precise reason instead of a wrong ROI.
    """

    def test_champion_uses_the_selected_side_price_and_target(self) -> None:
        rows = [
            shadow_row(0, side="L", target=1.0, odds=2.0),
            shadow_row(1, side="S", target=0.0, odds=2.0),
        ]
        shadow = chl._shadow_returns(rows, chl.CHAMPION_STRATEGY)
        self.assertEqual(shadow["strategy"], chl.CHAMPION_STRATEGY)
        self.assertEqual(shadow["direction_flips"], 0)
        self.assertEqual(shadow["aligned_rows"], 2)
        # One unit won at 2.00 (+1) and one lost (-1).
        self.assertEqual(shadow["roi"], 0.0)
        self.assertEqual(shadow["reason"], "closing_odds_unavailable")

    def test_always_under_on_already_under_rows_stays_aligned(self) -> None:
        rows = [shadow_row(index, side="S", target=1.0, odds=1.8) for index in range(3)]
        shadow = chl._shadow_returns(rows, "always_under")
        self.assertEqual(shadow["direction_flips"], 0)
        self.assertEqual(shadow["roi"], 0.8)

    def test_always_under_never_reuses_the_over_side_price(self) -> None:
        """The dangerous case: the stored quote is for over, the strategy buys under."""
        rows = [shadow_row(index, side="L", target=1.0, odds=1.9) for index in range(3)]
        shadow = chl._shadow_returns(rows, "always_under")
        self.assertEqual(shadow["direction_flips"], 3)
        self.assertIsNone(shadow["roi"])
        self.assertEqual(shadow["reason"], "opposite_side_price_unavailable")
        self.assertEqual(shadow["aligned_rows"], 0)

    def test_always_under_uses_a_genuinely_quoted_opposite_price(self) -> None:
        alternate = {
            "code": "CHL", "market": "CHL", "condition": "9.5",
            "line": "9.5", "side": "S", "odds": 2.5,
        }
        rows = [
            shadow_row(0, side="L", target=1.0, odds=1.5, alternates=[alternate]),
            shadow_row(1, side="L", target=0.0, odds=1.5, alternates=[alternate]),
        ]
        shadow = chl._shadow_returns(rows, "always_under")
        self.assertEqual(shadow["direction_flips"], 2)
        self.assertEqual(shadow["aligned_rows"], 2)
        # Targets mirror: the over win becomes an under loss (-1) and the over
        # loss becomes an under win at 2.50 (+1.5).
        self.assertEqual(shadow["roi"], 0.25)

    def test_opposite_price_must_match_the_same_line(self) -> None:
        mismatched = {
            "code": "CHL", "market": "CHL", "condition": "10.5",
            "line": "10.5", "side": "S", "odds": 2.5,
        }
        rows = [shadow_row(0, side="L", target=1.0, odds=1.5, alternates=[mismatched])]
        shadow = chl._shadow_returns(rows, "always_under")
        self.assertIsNone(shadow["roi"])
        self.assertEqual(shadow["reason"], "opposite_side_price_unavailable")

    def test_a_single_missing_alternate_price_fails_the_whole_book_closed(self) -> None:
        alternate = {
            "code": "CHL", "market": "CHL", "condition": "9.5",
            "line": "9.5", "side": "S", "odds": 2.5,
        }
        rows = [
            shadow_row(0, side="L", target=1.0, odds=1.5, alternates=[alternate]),
            shadow_row(1, side="L", target=0.0, odds=1.5),
        ]
        shadow = chl._shadow_returns(rows, "always_under")
        self.assertIsNone(shadow["roi"])
        self.assertEqual(shadow["aligned_rows"], 1)
        self.assertEqual(shadow["reason"], "opposite_side_price_unavailable")

    def test_model_flip_without_an_opposite_price_yields_null_roi(self) -> None:
        rows = [shadow_row(index, side="L", target=1.0, odds=1.9) for index in range(2)]
        # The fitted model disagrees with the stored selection on every row.
        shadow = chl._shadow_returns(rows, "team_corner_feature", [0.2, 0.3])
        self.assertEqual(shadow["direction_flips"], 2)
        self.assertIsNone(shadow["roi"])
        self.assertEqual(shadow["reason"], "opposite_side_price_unavailable")

    def test_model_agreeing_with_the_selection_scores_the_selected_price(self) -> None:
        rows = [
            shadow_row(0, side="L", target=1.0, odds=2.0),
            shadow_row(1, side="L", target=0.0, odds=2.0),
        ]
        shadow = chl._shadow_returns(rows, "team_corner_feature", [0.7, 0.6])
        self.assertEqual(shadow["direction_flips"], 0)
        self.assertEqual(shadow["roi"], 0.0)

    def test_model_without_probabilities_cannot_resolve_a_direction(self) -> None:
        rows = [shadow_row(0, side="L", target=1.0, odds=2.0)]
        shadow = chl._shadow_returns(rows, "team_corner_feature")
        self.assertIsNone(shadow["roi"])
        self.assertEqual(shadow["reason"], "model_probability_unavailable")

    def test_pushes_and_half_outcomes_settle_at_their_real_value(self) -> None:
        rows = [
            shadow_row(0, side="S", target=0.5, odds=2.0),   # refunded
            shadow_row(1, side="S", target=0.75, odds=2.0),  # half won
            shadow_row(2, side="S", target=0.25, odds=2.0),  # half lost
        ]
        shadow = chl._shadow_returns(rows, chl.CHAMPION_STRATEGY)
        # 0 + 0.5 - 0.5 = 0 across three unit stakes.
        self.assertEqual(shadow["roi"], 0.0)

    def test_mirrored_half_outcomes_are_not_treated_as_full_outcomes(self) -> None:
        alternate = {
            "code": "CHL", "market": "CHL", "condition": "9.5",
            "line": "9.5", "side": "S", "odds": 3.0,
        }
        rows = [shadow_row(0, side="L", target=0.75, odds=1.5, alternates=[alternate])]
        # An over half-win mirrors to an under half-loss: -0.5, never -1.
        shadow = chl._shadow_returns(rows, "always_under")
        self.assertEqual(shadow["roi"], -0.5)

    def test_an_unresolvable_side_never_scores(self) -> None:
        rows = [shadow_row(0, side="?", target=1.0, odds=2.0)]
        rows[0]["target_key"] = "9.5|?"
        shadow = chl._shadow_returns(rows, chl.CHAMPION_STRATEGY)
        self.assertIsNone(shadow["roi"])
        self.assertEqual(shadow["reason"], "direction_not_resolvable")

    def test_a_missing_selected_price_fails_closed(self) -> None:
        rows = [shadow_row(0, side="S", target=1.0, odds=None)]
        shadow = chl._shadow_returns(rows, "always_under")
        self.assertIsNone(shadow["roi"])
        self.assertEqual(shadow["reason"], "selected_side_price_unavailable")

    def test_explicit_opposite_odds_field_is_honoured(self) -> None:
        rows = [
            shadow_row(0, side="L", target=0.0, odds=1.5, extra={"under_odds": 2.4}),
        ]
        shadow = chl._shadow_returns(rows, "always_under")
        self.assertEqual(shadow["roi"], 1.4)

    def test_shadow_return_never_claims_edge(self) -> None:
        rows = [shadow_row(0, side="S", target=1.0, odds=2.0)]
        shadow = chl._shadow_returns(rows, chl.CHAMPION_STRATEGY)
        self.assertIn("+EV", shadow["note"])
        self.assertIsNone(shadow["clv"])
