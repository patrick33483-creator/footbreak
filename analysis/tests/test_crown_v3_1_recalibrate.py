from __future__ import annotations

import copy
import json
import math
import unittest
from datetime import datetime, timedelta, timezone

from analysis.crown_v3_1_recalibrate import (
    BUCKET_EDGES,
    FEATURE_NAMES,
    apply_isotonic,
    build_report,
    build_records,
    canonicalize,
    design_matrix,
    feature_medians,
    fit_isotonic,
    league_encoding,
    metric_block,
    split_kickoff_cohorts,
)

STATE = {"sha256": "synthetic", "size": 0, "mtime_ns": 0}


def market_item(code: str, side: str, line: float, probability: float, odds: float) -> dict:
    return {"code": code, "side": side, "line": line, "probability": probability, "odds": odds}


def synthetic_payload(count: int = 100) -> dict:
    """Deterministic fake history with both HDC and OU (HIL) markets."""
    rows = []
    start = datetime(2026, 1, 1, 18, tzinfo=timezone.utc)
    for index in range(count):
        kickoff = start + timedelta(hours=6 * index)
        fixture_id = f"f{index:03d}"
        home, away = f"team{index % 11}", f"team{(index + 5) % 11}"
        league = f"league{index % 4}"
        # Deterministic but varied scorelines so labels are not degenerate.
        home_score = (index * 7) % 4
        away_score = (index * 3) % 3
        strong = 0.58 + (index % 5) * 0.012
        for minutes, stage, drift in ((240, "首預", -0.02), (30, "T-30", -0.01), (5, "T-5", 0.0)):
            rows.append({
                "match_id": fixture_id,
                "stage": stage,
                "home": home,
                "away": away,
                "league": league,
                "kickoff": kickoff.isoformat(),
                "predicted_at": (kickoff - timedelta(minutes=minutes)).isoformat(),
                "market_predictions": [
                    market_item("HDC", "H", -0.5 if index % 2 else 0.25, strong + drift, 1.85 + (index % 3) * 0.05),
                    market_item("HDC", "A", -0.5 if index % 2 else 0.25, 1 - strong - drift, 2.0 - (index % 3) * 0.05),
                    market_item("HIL", "H", 2.5 if index % 3 else 2.25, 0.55 + (index % 4) * 0.01 + drift, 1.9),
                    market_item("HIL", "L", 2.5 if index % 3 else 2.25, 0.45 - (index % 4) * 0.01, 1.95),
                ],
                "result_status": "已核對",
                "result_detail": {"home_score": home_score, "away_score": away_score},
            })
    return {"rows": rows}


class CrownV31PipelineTests(unittest.TestCase):
    def test_pipeline_runs_on_hundred_synthetic_fixtures(self) -> None:
        report = build_report(synthetic_payload(100), STATE)
        self.assertEqual(report["split"]["total_fixtures"], 100)
        self.assertEqual(sum(report["split"]["counts"].values()), 100)
        for market in ("hdc", "hil"):
            for variant in ("raw", "isotonic", "platt"):
                self.assertIn("holdout", report["models"][market][variant])
            for part in ("discovery", "selection", "holdout"):
                block = report["v2_baseline"][market][part]
                for key in ("n", "mean_probability", "hit_rate_excluding_push", "brier", "log_loss",
                            "calibration_gap", "buckets", "roi_at_0.55_threshold", "wilson"):
                    self.assertIn(key, block)
            self.assertEqual(set(report["features"]["importance"][market]), set(FEATURE_NAMES))
        self.assertTrue(report["scope"]["retrains_model"])
        json.dumps(report, ensure_ascii=False)

    def test_isotonic_calibration_does_not_increase_selection_brier(self) -> None:
        report = build_report(synthetic_payload(100), STATE)
        for market in ("hdc", "hil"):
            raw = report["models"][market]["raw"]["selection"]["brier"]
            isotonic = report["models"][market]["isotonic"]["selection"]["brier"]
            self.assertIsNotNone(raw)
            self.assertIsNotNone(isotonic)
            self.assertLessEqual(isotonic, raw + 1e-12)

    def test_isotonic_fit_is_monotone_and_bounded(self) -> None:
        model = fit_isotonic([0.1, 0.2, 0.3, 0.4, 0.6, 0.8], [0.0, 1.0, 0.0, 1.0, 1.0, 1.0])
        predicted = apply_isotonic(model, [0.05, 0.1, 0.25, 0.5, 0.9])
        self.assertEqual(predicted, sorted(predicted))
        self.assertTrue(all(0.0 - 1e-9 <= value <= 1.0 + 1e-9 for value in predicted))

    def test_holdout_scores_use_holdout_rows_only(self) -> None:
        payload = synthetic_payload(100)
        report = build_report(payload, STATE)
        fixtures, _ = canonicalize(payload["rows"])
        parts, _ = split_kickoff_cohorts(fixtures)
        holdout_ids = {row["fixture_id"] for row in parts["holdout"]}

        # Holdout record counts must match the number of holdout fixtures with a pick.
        for market in ("HDC", "HIL"):
            records = [row for row in build_records(fixtures, market) if row["fixture_id"] in holdout_ids]
            reported = report["models"][market.lower()]["raw"]["holdout"]
            self.assertEqual(reported["n_including_push"], len(records))
            encoding, prior = league_encoding([r for r in build_records(fixtures, market)
                                               if r["fixture_id"] not in holdout_ids])
            medians = feature_medians([r for r in build_records(fixtures, market)
                                      if r["fixture_id"] not in holdout_ids], encoding, prior)
            self.assertEqual(len(design_matrix(records, medians, encoding, prior)[0]), len(FEATURE_NAMES))

        # Mutating holdout results may not move discovery/selection metrics at all.
        mutated = copy.deepcopy(payload)
        for row in mutated["rows"]:
            if row["match_id"] in holdout_ids:
                row["result_detail"] = {"home_score": 5, "away_score": 0}
        second = build_report(mutated, STATE)
        for market in ("hdc", "hil"):
            for part in ("discovery", "selection"):
                self.assertEqual(report["models"][market]["raw"][part], second["models"][market]["raw"][part])
                self.assertEqual(report["v2_baseline"][market][part], second["v2_baseline"][market][part])
            self.assertEqual(report["features"]["importance"][market], second["features"]["importance"][market])
            self.assertNotEqual(report["models"][market]["raw"]["holdout"]["brier"],
                                second["models"][market]["raw"]["holdout"]["brier"])

    def test_bucket_counts_sum_to_block_n(self) -> None:
        report = build_report(synthetic_payload(100), STATE)
        blocks = []
        for market in ("hdc", "hil"):
            for variant in ("raw", "isotonic", "platt"):
                blocks.extend(report["models"][market][variant].values())
            blocks.extend(report["v2_baseline"][market].values())
        self.assertTrue(blocks)
        for block in blocks:
            self.assertEqual(sum(bucket["n"] for bucket in block["buckets"].values()), block["n"])
            self.assertEqual(len(block["buckets"]), len(BUCKET_EDGES))
            self.assertEqual(block["n"] + block["push_n"], block["n_including_push"])

    def test_metric_block_arithmetic_on_known_rows(self) -> None:
        records = [
            {"grade": "full_win", "label": 1.0, "is_push": False, "return": 0.9, "pick": {}, "fixture_id": "a"},
            {"grade": "full_loss", "label": 0.0, "is_push": False, "return": -1.0, "pick": {}, "fixture_id": "b"},
            {"grade": "push", "label": None, "is_push": True, "return": 0.0, "pick": {}, "fixture_id": "c"},
        ]
        block = metric_block(records, [0.7, 0.6, 0.9])
        self.assertEqual(block["n"], 2)
        self.assertEqual(block["push_n"], 1)
        self.assertAlmostEqual(block["brier"], ((0.7 - 1) ** 2 + 0.6 ** 2) / 2)
        self.assertAlmostEqual(block["log_loss"], (-math.log(0.7) - math.log(0.4)) / 2)
        self.assertAlmostEqual(block["mean_probability"], 0.65)
        self.assertAlmostEqual(block["hit_rate_excluding_push"], 0.5)
        self.assertAlmostEqual(block["calibration_gap"], 0.15)
        self.assertEqual(block["roi_at_0.55_threshold"]["n_bets_including_push"], 3)
        self.assertAlmostEqual(block["roi_at_0.55_threshold"]["roi"], (0.9 - 1.0 + 0.0) / 3)

    def test_two_runs_produce_byte_identical_json(self) -> None:
        payload = synthetic_payload(100)
        first = json.dumps(build_report(payload, STATE), ensure_ascii=False, indent=2, sort_keys=True)
        second = json.dumps(build_report(copy.deepcopy(payload), STATE), ensure_ascii=False, indent=2, sort_keys=True)
        self.assertEqual(first.encode("utf-8"), second.encode("utf-8"))


if __name__ == "__main__":
    unittest.main()
