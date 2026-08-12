from __future__ import annotations

import copy
import json
import sqlite3
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

from analysis.challenger_model import (
    HIL_FEATURE_SCHEMA_VERSION,
    HIL_MODEL_VERSION,
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

    def _record_market_rows(
        self, database: Path, count: int, *, system: str = "footbreak", market: str = "HDC"
    ) -> None:
        with LearningStore(database) as store:
            for index in range(count):
                kickoff = datetime(2026, 1, 1, tzinfo=timezone.utc) + timedelta(days=index)
                snapshot = store.record_snapshot(
                    system, f"m{index:03d}", "T-5",
                    kickoff - timedelta(minutes=5), kickoff,
                    {
                        "league": "League A", "final": {"lh": 1.2, "la": 0.9},
                        "market_predictions": [{
                            "code": market,
                            "condition": "2.5" if market == "HIL" else "-0.5",
                            "line": 2.5 if market == "HIL" else -.5,
                            "side": "H", "odds": 1.9, "probability": 0.7,
                        }],
                    },
                )
                result = store.record_result(
                    system, f"m{index:03d}", home_score=1, away_score=0, source="test"
                )
                target = float(index % 2 == 0)
                store.record_grade(
                    snapshot, market, "2.5|H" if market == "HIL" else "-0.5|H", "GRADED",
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

    def test_hil_v2_uses_only_compact_pre_kickoff_line_price_stage_schema(self) -> None:
        rows = []
        for index, stage, line, odds, probability in (
            (1, "首預", 2.5, 1.91, .53),
            (1, "T-30", 2.75, 1.82, .56),
            (1, "T-5", 2.75, 1.74, .59),
        ):
            row = self._source_row(index, market="HIL", stage=stage, target=1.0)
            row["target_key"] = f"{line}|H"
            row["probability"] = probability
            row["payload"]["market_predictions"][0].update({
                "condition": str(line), "line": line, "odds": odds, "side": "H",
            })
            row["payload"]["outcome"] = {"home": .40, "draw": .31, "away": .29}
            # These fields are deliberately present in the payload to prove
            # that HIL v2's compact encoder cannot use them.
            row["payload"]["result_detail"] = {"home_score": 77, "away_score": 66}
            row["payload"]["actual"] = "post-kickoff-result"
            rows.append(row)
        featured = build_feature_rows(rows)
        latest = featured[-1]["numeric"]
        self.assertAlmostEqual(latest["market_implied_probability"], 1 / 1.74)
        self.assertEqual(latest["stage_line_delta"], 0.0)
        self.assertAlmostEqual(latest["stage_odds_delta"], -0.08)
        self.assertAlmostEqual(latest["stage_implied_probability_delta"], (1 / 1.74) - (1 / 1.82))

        report = evaluate_market(
            [
                self._hil_source_row(index, target=float(index % 2))
                for index in range(100)
            ],
            "crown", "HIL", {},
        )
        self.assertEqual(report["model_version"], HIL_MODEL_VERSION)
        self.assertEqual(report["feature_schema_version"], HIL_FEATURE_SCHEMA_VERSION)
        self.assertNotIn("league", report["numeric_features"])
        self.assertNotIn("league", report["categorical_features"])
        self.assertIn("stage_odds_delta", report["numeric_features"])
        self.assertFalse(any(
            forbidden in item["feature"]
            for item in report["coefficient_importance"]
            for forbidden in ("result", "actual", "footbreak_", "league")
        ))
        hdc = evaluate_market(
            [self._source_row(index) for index in range(100)], "crown", "HDC", {}
        )
        self.assertEqual(hdc["model_version"], "challenger-logit-v1")
        self.assertIn("league", hdc["categorical_features"])
        footbreak_hil = evaluate_market(
            [self._hil_source_row(index, target=float(index % 2)) for index in range(100)],
            "footbreak", "HIL", {},
        )
        self.assertEqual(footbreak_hil["model_version"], "challenger-logit-v1")
        self.assertIn("league", footbreak_hil["categorical_features"])

    def _hil_source_row(self, index: int, *, target: float) -> dict:
        row = self._source_row(index, market="HIL", target=target)
        line = 2.25 + .25 * (index % 4)
        odds = 1.72 + .03 * (index % 5)
        row["target_key"] = f"{line}|H"
        row["probability"] = .51 + .01 * (index % 7)
        row["payload"]["market_predictions"][0].update({
            "condition": str(line), "line": line, "odds": odds, "side": "H",
        })
        row["payload"]["outcome"] = {
            "home": .37, "draw": .25 + .01 * (index % 3), "away": .38 - .01 * (index % 3),
        }
        return row

    def test_hil_calibration_is_deterministic_and_never_uses_locked_holdout(self) -> None:
        rows = [
            self._hil_source_row(index, target=float(index % 2 == 0))
            for index in range(100)
        ]
        first = evaluate_market(rows, "crown", "HIL", {})
        second = evaluate_market(copy.deepcopy(rows), "crown", "HIL", {})
        self.assertEqual(first["train_fixtures"], 70)
        self.assertEqual(first["holdout_fixtures"], 30)
        self.assertEqual(first["holdout_rows"], second["holdout_rows"])
        self.assertEqual(first["calibration"], second["calibration"])
        self.assertEqual(first["challenger"]["metrics"], second["challenger"]["metrics"])
        self.assertEqual(
            first["calibration"]["fit_fixtures"] + first["calibration"]["calibration_fixtures"],
            first["train_fixtures"],
        )
        self.assertLess(
            first["calibration"]["fit_fixtures"] + first["calibration"]["calibration_fixtures"],
            first["train_fixtures"] + first["holdout_fixtures"],
        )
        self.assertFalse(first["auto_apply"])
        self.assertFalse(first["challenger"]["probability_artifact_written"])

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
            self._record_market_rows(database, 100, system="crown", market="HIL")
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
            # Even a synthetic candidate that clears offline gates remains an
            # isolated report and never changes a live probability or ledger.
            self.assertFalse(first["policy"]["auto_apply"])
            self.assertEqual(first["systems"], second["systems"])
            hil = first["systems"]["crown"]["tests"]["HIL"]
            self.assertEqual(hil["model_version"], HIL_MODEL_VERSION)
            self.assertFalse(hil["auto_apply"])
            self.assertFalse(hil["challenger"]["probability_artifact_written"])


if __name__ == "__main__":
    unittest.main()
