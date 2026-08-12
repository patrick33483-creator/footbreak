"""Focused safety tests for the two isolated prospective condition reports."""
from __future__ import annotations

import json
import os
import stat
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

from analysis.learning_store import LearningStore
from analysis import shadow_conditions as shadow


CUTOFF = datetime(2026, 8, 12, 12, tzinfo=timezone.utc)


def prediction(code: str, side: str, line: float, *, odds: float | None = 2.0,
               closing: float | None = 1.9) -> dict:
    result = {
        "code": code, "market": code, "side": side, "line": line,
        "condition": line, "probability": 0.6,
    }
    if odds is not None:
        result["odds"] = odds
    if closing is not None:
        result["closing_odds"] = closing
    return result


class ShadowConditionBuilder:
    def __init__(self, directory: Path) -> None:
        self.path = directory / "learning.sqlite"

    def add(self, system: str, fixture: str, stage: str, kickoff: datetime,
            item: dict, target: float | None = 1.0, *, post_kickoff: bool = False) -> None:
        generated = kickoff + timedelta(seconds=1) if post_kickoff else kickoff - timedelta(minutes=10)
        with LearningStore(self.path) as store:
            snapshot = store.record_snapshot(
                system, fixture, stage, generated, kickoff,
                {"market_predictions": [item]},
            )
            if target is not None:
                result = store.record_result(system, fixture, home_score=1, away_score=0)
                store.record_grade(
                    snapshot, item["code"], f"{item['line']}|{item['side']}", "GRADED",
                    {"probability": item["probability"], "target": target},
                    result_id=result["result_id"],
                )

    def report(self, state_path: Path) -> dict:
        state = shadow.freeze_once(state_path, CUTOFF)
        with LearningStore(self.path) as store:
            footbreak, _ = store.shadow_condition_rows("footbreak")
            crown, _ = store.shadow_condition_rows("crown")
        return shadow.evaluate({"footbreak": footbreak, "crown": crown}, state)


class ShadowConditionQualificationTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.builder = ShadowConditionBuilder(self.root)
        self.state = self.root / "private" / "state.json"

    def test_footbreak_condition_is_t5_hil_l_only_and_one_fixture_once(self) -> None:
        future = CUTOFF + timedelta(days=1)
        self.builder.add("footbreak", "ok", "T-5", future, prediction("HIL", "L", 2.5))
        self.builder.add("footbreak", "not-under", "T-5", future, prediction("HIL", "H", 2.5))
        self.builder.add("footbreak", "wrong-stage", "T-30", future, prediction("HIL", "L", 2.5))
        self.builder.add("footbreak", "wrong-market", "T-5", future, prediction("HDC", "L", 2.5))
        # The store's immutable stage key prevents replays becoming a second
        # prospective unit for the same fixture.
        self.builder.add("footbreak", "ok", "T-5", future, prediction("HIL", "L", 2.5))
        result = self.builder.report(self.state)["conditions"]["footbreak_hil_t5_under"]
        self.assertEqual(result["progress"]["qualified_unique_fixtures"], 1)
        self.assertEqual(result["metrics"]["counts"]["decided"], 1)

    def test_crown_requires_all_stages_identical_side_and_numeric_line_and_uses_t5(self) -> None:
        future = CUTOFF + timedelta(days=2)
        for stage in shadow.STAGES:
            self.builder.add("crown", "ok", stage, future, prediction("HDC", "H", -0.75), target=0.0 if stage == "T-5" else 1.0)
        for stage, line in zip(shadow.STAGES, (-0.5, -0.75, -0.75)):
            self.builder.add("crown", "line-move", stage, future, prediction("HDC", "H", line))
        for stage, side in zip(shadow.STAGES, ("H", "A", "H")):
            self.builder.add("crown", "side-move", stage, future, prediction("HDC", side, -0.75))
        self.builder.add("crown", "missing", "T-5", future, prediction("HDC", "H", -0.75))
        result = self.builder.report(self.state)["conditions"]["crown_hdc_three_stage_exact"]
        self.assertEqual(result["progress"]["qualified_unique_fixtures"], 1)
        self.assertEqual(result["metrics"]["counts"]["decided"], 1)
        self.assertEqual(result["metrics"]["hit_rate"], 0.0)

    def test_cutoff_excludes_prior_and_exact_boundary_and_late_rows(self) -> None:
        self.builder.add("footbreak", "before", "T-5", CUTOFF - timedelta(seconds=1), prediction("HIL", "L", 2.5))
        self.builder.add("footbreak", "exact", "T-5", CUTOFF, prediction("HIL", "L", 2.5))
        self.builder.add("footbreak", "future", "T-5", CUTOFF + timedelta(seconds=1), prediction("HIL", "L", 2.5))
        self.builder.add("footbreak", "late", "T-5", CUTOFF + timedelta(days=1), prediction("HIL", "L", 2.5), post_kickoff=True)
        result = self.builder.report(self.state)["conditions"]["footbreak_hil_t5_under"]
        self.assertEqual(result["progress"]["qualified_unique_fixtures"], 1)


class ShadowConditionMetricsAndStateTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.builder = ShadowConditionBuilder(self.root)
        self.state = self.root / "private" / "state.json"

    def test_push_half_and_unavailable_counts_and_metrics_are_settlement_aware(self) -> None:
        for index, target in enumerate((1.0, 0.75, 0.5, 0.25, 0.0, None)):
            self.builder.add(
                "footbreak", f"f{index}", "T-5", CUTOFF + timedelta(days=index + 1),
                prediction("HIL", "L", 2.5, odds=2.0, closing=1.8), target,
            )
        metrics = self.builder.report(self.state)["conditions"]["footbreak_hil_t5_under"]["metrics"]
        self.assertEqual(metrics["counts"]["qualified_fixtures"], 6)
        self.assertEqual(metrics["counts"]["settled"], 5)
        self.assertEqual(metrics["counts"]["decided"], 4)
        self.assertEqual(metrics["counts"]["hits"], 2)
        self.assertEqual(metrics["counts"]["half_won"], 1)
        self.assertEqual(metrics["counts"]["half_lost"], 1)
        self.assertEqual(metrics["counts"]["pushes_refunds"], 1)
        self.assertEqual(metrics["counts"]["outcome_unavailable"], 1)
        self.assertEqual(metrics["hit_rate"], 0.5)
        # 1 + .5 + 0 - .5 - 1 over five settled unit stakes.
        self.assertEqual(metrics["roi"], 0.0)
        self.assertAlmostEqual(metrics["brier"], ((.6 - 1) ** 2 + (.6 - .75) ** 2 + (.6 - .5) ** 2 + (.6 - .25) ** 2 + (.6 - 0) ** 2) / 5, places=6)
        self.assertIsNotNone(metrics["clv"])

    def test_roi_and_clv_fail_closed_without_selected_side_odds_or_same_quote(self) -> None:
        self.builder.add("footbreak", "no-entry", "T-5", CUTOFF + timedelta(days=1),
                         prediction("HIL", "L", 2.5, odds=None, closing=1.8))
        self.builder.add("footbreak", "no-close", "T-5", CUTOFF + timedelta(days=2),
                         prediction("HIL", "L", 2.5, odds=2.0, closing=None))
        metrics = self.builder.report(self.state)["conditions"]["footbreak_hil_t5_under"]["metrics"]
        self.assertIsNone(metrics["roi"])
        self.assertEqual(metrics["roi_reason"], "selected_direction_pre_kickoff_odds_unavailable")
        self.assertIsNone(metrics["clv"])
        self.assertEqual(metrics["clv_reason"], "same_market_direction_line_closing_quote_unavailable")

    def test_human_review_threshold_uses_one_hundred_decided_not_just_qualified(self) -> None:
        pending = [{
            "match_id": f"pending-{index}", "stage": "T-5",
            "kickoff": CUTOFF + timedelta(days=index + 1),
            "predicted_at": CUTOFF, "payload": {"market_predictions": [prediction("HIL", "L", 2.5)]},
            "grades": [],
        } for index in range(100)]
        state = shadow.freeze_once(self.state, CUTOFF)
        collecting = shadow.evaluate({"footbreak": pending, "crown": []}, state)
        report = collecting["conditions"]["footbreak_hil_t5_under"]
        self.assertEqual(report["progress"]["qualified_unique_fixtures"], 100)
        self.assertEqual(report["progress"]["decided_unique_fixtures"], 0)
        self.assertEqual(report["status"], "collecting_insufficient")

        decided = []
        for row in pending:
            copy = {**row, "grades": [{
                "market": "HIL", "target_key": "2.5|L", "state": "GRADED",
                "metrics": {"probability": 0.1, "target": 1.0},
            }]}
            decided.append(copy)
        review = shadow.evaluate({"footbreak": decided, "crown": []}, state)
        report = review["conditions"]["footbreak_hil_t5_under"]
        self.assertEqual(report["progress"]["decided_unique_fixtures"], 100)
        self.assertEqual(report["status"], "human_review_ready")
        self.assertTrue(report["human_review_only"])
        self.assertFalse(report["auto_apply"])

    def test_freeze_is_once_private_and_tamper_is_rejected(self) -> None:
        first = shadow.freeze_once(self.state, CUTOFF)
        raw, mtime = self.state.read_bytes(), self.state.stat().st_mtime_ns
        second = shadow.freeze_once(self.state, CUTOFF + timedelta(days=10))
        self.assertEqual(first["freeze_cutoff"], second["freeze_cutoff"])
        self.assertEqual(self.state.read_bytes(), raw)
        self.assertEqual(self.state.stat().st_mtime_ns, mtime)
        self.assertEqual(stat.S_IMODE(self.state.stat().st_mode), 0o600)
        self.assertEqual(stat.S_IMODE(self.state.parent.stat().st_mode), 0o700)
        changed = json.loads(raw)
        changed["freeze_cutoff"] = "2000-01-01T00:00:00Z"
        self.state.write_text(json.dumps(changed), encoding="utf-8")
        with self.assertRaises(ValueError):
            shadow.freeze_once(self.state, CUTOFF)

    def test_public_artifacts_are_system_separated_and_human_review_only(self) -> None:
        foot, crown = self.root / "www-foot.json", self.root / "www-crown.json"
        report = shadow.run(self.builder.path, self.state, foot, crown, now=CUTOFF)
        left, right = json.loads(foot.read_text()), json.loads(crown.read_text())
        self.assertEqual(left["system"], "footbreak")
        self.assertEqual(left["condition_id"], "footbreak_hil_t5_under")
        self.assertEqual(right["system"], "crown")
        self.assertEqual(right["condition_id"], "crown_hdc_three_stage_exact")
        self.assertFalse(report["policy"]["auto_apply"])
        self.assertTrue(report["policy"]["human_review_only"])
        self.assertEqual(stat.S_IMODE(foot.stat().st_mode), 0o644)


if __name__ == "__main__":
    unittest.main()
