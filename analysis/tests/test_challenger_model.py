from __future__ import annotations

import copy
import json
import sqlite3
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

from analysis.challenger_model import (
    TrainOnlyEncoder,
    build_feature_rows,
    chronological_fixture_split,
    evaluate_market,
    fit_logistic,
    predict,
    promotion_gate,
    run,
)
from analysis.learning_store import LearningStore


class ChallengerModelTests(unittest.TestCase):
    def _source_row(
        self,
        index: int,
        *,
        market: str = "HDC",
        stage: str = "T-5",
        league: str = "League A",
        target: float = 1.0,
    ) -> dict:
        kickoff = datetime(2026, 1, 1, tzinfo=timezone.utc) + timedelta(days=index)
        return {
            "match_id": f"m{index:03d}",
            "kickoff": kickoff,
            "predicted_at": kickoff - timedelta(minutes=5),
            "stage": stage,
            "market": market,
            "target_key": "-0.5|H",
            "probability": 0.75 if target else 0.25,
            "target": target,
            "payload": {
                "league": league,
                "home": f"H{index}",
                "away": f"A{index}",
                "final": {"lh": 1.4, "la": 0.9, "total": 2.3, "supremacy": 0.5},
                "movement": {"d_total": 0.1, "d_sup": 0.05},
                "info": {"hk_max_move_pct": 0.03, "hk_n_lines_moved": 2},
                # Must never be a feature even when a malformed payload has it.
                "actual": "post-kickoff-result",
                "result_detail": {"home_score": 99},
                "market_predictions": [{
                    "code": market, "condition": "-0.5", "side": "H",
                    "probability": 0.75 if target else 0.25,
                }],
            },
        }

    def _record_market_rows(self, database: Path, count: int) -> None:
        with LearningStore(database) as store:
            for index in range(count):
                kickoff = datetime(2026, 1, 1, tzinfo=timezone.utc) + timedelta(days=index)
                snapshot = store.record_snapshot(
                    "footbreak", f"m{index:03d}", "T-5",
                    kickoff - timedelta(minutes=5), kickoff,
                    {
                        "league": "League A", "final": {"lh": 1.2, "la": 0.9},
                        "market_predictions": [{
                            "code": "HDC", "condition": "-0.5", "side": "H",
                            "probability": 0.7,
                        }],
                    },
                )
                result = store.record_result(
                    "footbreak", f"m{index:03d}", home_score=1, away_score=0, source="test"
                )
                target = float(index % 2 == 0)
                store.record_grade(
                    snapshot, "HDC", "-0.5|H", "GRADED",
                    {
                        "probability": 0.7, "target": target, "hit": bool(target),
                        "brier": (0.7 - target) ** 2, "log_loss": 0.5,
                    },
                    result_id=result["result_id"],
                )

    def test_feature_extraction_uses_only_whitelisted_pre_kickoff_fields_and_missing_indicators(self) -> None:
        row = self._source_row(1)
        row["payload"]["movement"] = {}
        featured = build_feature_rows([row])
        self.assertEqual(len(featured), 1)
        numeric = featured[0]["numeric"]
        self.assertIsNone(numeric["footbreak_movement_total"])
        self.assertEqual(featured[0]["categorical"]["selection_side"], "H")
        encoder = TrainOnlyEncoder().fit(featured)
        values = encoder.transform_one(featured[0])
        self.assertEqual(len(values), len(encoder.feature_names))
        self.assertIn("footbreak_movement_total__missing", encoder.feature_names)
        self.assertFalse(any("actual" in name or "result" in name for name in encoder.feature_names))

    def test_encoder_fit_does_not_learn_holdout_categories_or_statistics(self) -> None:
        train = build_feature_rows([
            self._source_row(1, league="Train League", target=0.0),
            self._source_row(3, league="Train League", target=0.0),
        ])
        holdout = build_feature_rows([self._source_row(2, league="Holdout Only", target=1.0)])
        encoder = TrainOnlyEncoder().fit(train)
        self.assertNotIn("Holdout Only", encoder.categories["league"])
        self.assertEqual(encoder.medians["base_probability"], 0.25)
        transformed = encoder.transform_one(holdout[0])
        league_start = encoder.feature_names.index("league=Train League")
        self.assertEqual(transformed[league_start], 0.0)

    def test_fixture_split_keeps_all_stages_of_a_fixture_in_one_partition(self) -> None:
        rows = []
        for index in range(10):
            rows.extend([
                self._source_row(index, stage="首預", target=float(index % 2)),
                self._source_row(index, stage="T-30", target=float(index % 2)),
                self._source_row(index, stage="T-5", target=float(index % 2)),
            ])
        train, holdout, _ = chronological_fixture_split(rows)
        self.assertTrue(train.isdisjoint(holdout))
        for index in range(10):
            fixture = f"m{index:03d}"
            self.assertTrue((fixture in train) ^ (fixture in holdout))

    def test_challenger_rows_exclude_post_kickoff_snapshots(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            database = Path(directory) / "learning.sqlite"
            with LearningStore(database) as store:
                kickoff = "2026-02-01T12:00:00+00:00"
                valid = store.record_snapshot(
                    "crown", "late-test", "T-5", "2026-02-01T11:55:00+00:00", kickoff,
                    {"market_predictions": [{"code": "HIL", "condition": 2.5, "side": "H"}]},
                )
                late = store.record_snapshot(
                    "crown", "late-test", "T-30", kickoff, kickoff,
                    {"market_predictions": [{"code": "HIL", "condition": 2.5, "side": "H"}]},
                )
                result = store.record_result("crown", "late-test", home_score=1, away_score=0, source="test")
                for snapshot in (valid, late):
                    store.record_grade(
                        snapshot, "HIL", "2.5|H", "GRADED",
                        {"probability": 0.6, "target": 1.0, "hit": True, "brier": 0.16, "log_loss": 0.5},
                        result_id=result["result_id"],
                    )
                rows, diagnostics = store.challenger_rows("crown")
            self.assertEqual(len(rows), 1)
            self.assertEqual(rows[0]["stage"], "T-5")
            self.assertEqual(diagnostics["quarantined_snapshots"], 1)

    def test_insufficient_samples_explicitly_leave_champion_unchanged(self) -> None:
        report = evaluate_market(
            [self._source_row(index) for index in range(99)], "footbreak", "HDC", {}
        )
        self.assertEqual(report["status"], "insufficient_data")
        self.assertEqual(report["remaining_fixtures"], 1)
        self.assertFalse(report["separate_model_trained"])
        self.assertEqual(report["fallback"], "champion_unchanged_no_pooled_or_cross_system_fallback")
        self.assertFalse(report["auto_apply"])

    def test_fit_and_predictions_are_deterministic(self) -> None:
        source = [
            self._source_row(index, target=float(index % 2 == 0), league="League A" if index % 3 else "League B")
            for index in range(80)
        ]
        rows = build_feature_rows(source)
        first_encoder, first_weights = fit_logistic(rows)
        second_encoder, second_weights = fit_logistic(copy.deepcopy(rows))
        self.assertEqual(first_encoder.feature_names, second_encoder.feature_names)
        self.assertEqual(first_weights, second_weights)
        self.assertEqual(predict(first_encoder, first_weights, rows), predict(second_encoder, second_weights, rows))

    def test_promotion_gate_requires_all_metric_and_accuracy_conditions(self) -> None:
        champion = {"n": 30, "brier": .30, "log_loss": .70, "accuracy": .60}
        good = {"n": 30, "brier": .289, "log_loss": .69, "accuracy": .59}
        bad_accuracy = {"n": 30, "brier": .289, "log_loss": .69, "accuracy": .57}
        self.assertTrue(promotion_gate(champion, good, 30)["passed"])
        failed = promotion_gate(champion, bad_accuracy, 30)
        self.assertFalse(failed["passed"])
        self.assertIn("accuracy_not_materially_worse", failed["rejection_reasons"])

    def test_daily_pipeline_writes_only_isolated_artifacts_and_does_not_mutate_learning_store(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            database, output, public = root / "learning.sqlite", root / "challenger.json", root / "public.json"
            ledger = root / "official-ledger.json"
            ledger.write_text(json.dumps({"bets": [{"stake": 100}], "shadow_bets": [{"stake": 2}]}), encoding="utf-8")
            self._record_market_rows(database, 3)
            before_ledger = ledger.read_bytes()
            connection = sqlite3.connect(database)
            before_rows = connection.execute("SELECT COUNT(*) FROM prediction_snapshots").fetchone()[0]
            connection.close()
            first = run(database, output, [public])
            second = run(database, output, [public])
            connection = sqlite3.connect(database)
            after_rows = connection.execute("SELECT COUNT(*) FROM prediction_snapshots").fetchone()[0]
            connection.close()
            self.assertEqual(before_rows, after_rows)
            self.assertEqual(ledger.read_bytes(), before_ledger)
            self.assertTrue(output.exists())
            self.assertTrue(public.exists())
            self.assertEqual(oct(public.stat().st_mode & 0o777), "0o644")
            self.assertFalse(first["review_required"])
            self.assertEqual(first["systems"], second["systems"])


if __name__ == "__main__":
    unittest.main()
