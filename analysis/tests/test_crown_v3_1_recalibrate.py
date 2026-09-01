from __future__ import annotations

import copy
import json
import math
import unittest
from datetime import datetime, timedelta, timezone

from analysis.crown_v3_1_recalibrate import (
    BUCKET_EDGES,
    FEATURE_NAMES,
    OPPOSITE_DIRECTION,
    PRIMARY_DIRECTION,
    apply_isotonic,
    build_records,
    build_report,
    canonicalize,
    decide,
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


def synthetic_payload(count: int = 100, v2_favours: str = "opposite") -> dict:
    """Deterministic fake history with both HDC and OU (HIL) markets.

    ``v2_favours='opposite'`` publishes V2 probabilities that make the production
    maximum-EV lead the away/under side while the realised scores mostly favour the
    primary (home/over) side, so a genuinely independent model must flip direction.
    """
    rows = []
    start = datetime(2026, 1, 1, 18, tzinfo=timezone.utc)
    for index in range(count):
        kickoff = start + timedelta(hours=6 * index)
        fixture_id = f"f{index:03d}"
        home, away = f"team{index % 11}", f"team{(index + 5) % 11}"
        league = f"league{index % 4}"
        # 70% of fixtures are a clear home win and a clear over.
        primary_wins = index % 10 < 7
        home_score, away_score = (3, 1) if primary_wins else (0, 2)
        primary_probability = 0.40 + (index % 5) * 0.01 if v2_favours == "opposite" else 0.60 + (index % 5) * 0.01
        for minutes, stage, drift in ((240, "首預", -0.02), (30, "T-30", -0.01), (5, "T-5", 0.0)):
            primary = primary_probability + drift
            rows.append({
                "match_id": fixture_id,
                "stage": stage,
                "home": home,
                "away": away,
                "league": league,
                "kickoff": kickoff.isoformat(),
                "predicted_at": (kickoff - timedelta(minutes=minutes)).isoformat(),
                "market_predictions": [
                    market_item("HDC", "H", -0.5 if index % 2 else 0.25, primary, 1.85 + (index % 3) * 0.05),
                    market_item("HDC", "A", -0.5 if index % 2 else 0.25, 1 - primary, 2.00 - (index % 3) * 0.05),
                    market_item("HIL", "H", 2.5 if index % 3 else 2.25, primary, 1.90),
                    market_item("HIL", "L", 2.5 if index % 3 else 2.25, 1 - primary, 1.95),
                ],
                "result_status": "已核對",
                "result_detail": {"home_score": home_score, "away_score": away_score},
            })
    return {"rows": rows}


def sample_record(**overrides) -> dict:
    record = {
        "fixture_id": "x", "market": "HDC", "league": "L",
        "primary_direction": "home", "opposite_direction": "away",
        "primary_line": -0.5, "opposite_line": -0.5,
        "primary_grade": "full_win", "opposite_grade": "full_loss",
        "label": 1.0, "is_push": False,
        "primary_odds": 1.9, "opposite_odds": 2.1,
        "primary_return": 0.9, "opposite_return": -1.0,
        "sides_available": ["primary", "opposite"],
        "v2_primary_side_probability": 0.4, "v2_primary_probability_is_derived": False,
        "v2_lead_direction": "away", "v2_lead_probability": 0.6, "v2_lead_odds": 2.1,
        "v2_lead_grade": "full_loss", "v2_lead_return": -1.0,
        "raw_values": {},
    }
    record.update(overrides)
    return record


class CrownV31TargetTests(unittest.TestCase):
    def test_target_is_fixed_primary_side_and_ignores_v2_lead(self) -> None:
        fixtures_primary, _ = canonicalize(synthetic_payload(40, v2_favours="primary")["rows"])
        fixtures_opposite, _ = canonicalize(synthetic_payload(40, v2_favours="opposite")["rows"])
        for market in ("HDC", "HIL"):
            first = build_records(fixtures_primary, market)
            second = build_records(fixtures_opposite, market)
            self.assertEqual([row["primary_direction"] for row in first], [PRIMARY_DIRECTION[market]] * len(first))
            self.assertEqual([row["opposite_direction"] for row in second], [OPPOSITE_DIRECTION[market]] * len(second))
            # Swapping which side V2 leads must not move a single label.
            self.assertEqual([row["label"] for row in first], [row["label"] for row in second])
            self.assertEqual({row["v2_lead_direction"] for row in first},
                             {PRIMARY_DIRECTION[market]})
            self.assertEqual({row["v2_lead_direction"] for row in second},
                             {OPPOSITE_DIRECTION[market]})

    def test_decision_below_half_takes_opposite_side_odds_and_settlement(self) -> None:
        record = sample_record()
        backed = decide(record, 0.62)
        self.assertEqual(backed["direction"], "home")
        self.assertAlmostEqual(backed["confidence"], 0.62)
        self.assertEqual(backed["odds"], 1.9)
        self.assertEqual(backed["grade"], "full_win")
        self.assertAlmostEqual(backed["return"], 0.9)
        self.assertTrue(backed["flips_v2_lead"])

        flipped = decide(record, 0.38)
        self.assertEqual(flipped["direction"], "away")
        self.assertAlmostEqual(flipped["confidence"], 0.62)
        self.assertEqual(flipped["odds"], 2.1)
        self.assertEqual(flipped["grade"], "full_loss")
        self.assertAlmostEqual(flipped["return"], -1.0)
        self.assertEqual(flipped["label"], 0.0)
        self.assertFalse(flipped["flips_v2_lead"])

    def test_half_win_on_opposite_quarter_line_is_scored_exactly(self) -> None:
        fixtures, _ = canonicalize([
            {
                "match_id": "q", "stage": "T-5", "home": "a", "away": "b", "league": "L",
                "kickoff": "2026-02-01T12:00:00+00:00", "predicted_at": "2026-02-01T11:55:00+00:00",
                "market_predictions": [
                    market_item("HIL", "H", 2.25, 0.55, 1.9),
                    market_item("HIL", "L", 2.25, 0.45, 2.0),
                ],
                "result_status": "已核對", "result_detail": {"home_score": 1, "away_score": 1},
            }
        ])
        record = build_records(fixtures, "HIL")[0]
        self.assertEqual(record["primary_grade"], "half_loss")
        self.assertEqual(record["opposite_grade"], "half_win")
        self.assertAlmostEqual(record["primary_return"], -0.5)
        self.assertAlmostEqual(record["opposite_return"], 0.5)
        self.assertAlmostEqual(decide(record, 0.30)["return"], 0.5)


class CrownV31PipelineTests(unittest.TestCase):
    def test_pipeline_runs_on_hundred_synthetic_fixtures(self) -> None:
        report = build_report(synthetic_payload(100), STATE)
        self.assertEqual(report["split"]["total_fixtures"], 100)
        self.assertEqual(sum(report["split"]["counts"].values()), 100)
        for market in ("hdc", "hil"):
            for variant in ("raw", "isotonic", "platt"):
                block = report["models"][market][variant]["holdout"]
                for key in ("n", "mean_probability", "hit_rate_excluding_push", "brier", "log_loss",
                            "calibration_gap", "buckets", "roi_at_0.55_threshold", "wilson", "decision"):
                    self.assertIn(key, block)
            self.assertEqual(set(report["features"]["importance"][market]), set(FEATURE_NAMES))
            self.assertIn("production_lead_decision", report["v2_baseline"][market])
        self.assertTrue(report["scope"]["retrains_model"])
        self.assertIn("FIXED primary side", report["scope"]["target_definition"])
        json.dumps(report, ensure_ascii=False)

    def test_model_can_flip_the_v2_lead_direction(self) -> None:
        report = build_report(synthetic_payload(100, v2_favours="opposite"), STATE)
        for market in ("hdc", "hil"):
            decision = report["models"][market]["raw"]["holdout"]["decision"]
            production = report["v2_baseline"][market]["production_lead_decision"]["holdout"]["decision"]
            self.assertGreater(decision["direction_flip_rate_vs_v2_lead"], 0.5)
            self.assertEqual(production["direction_flip_rate_vs_v2_lead"], 0.0)
            self.assertIn(PRIMARY_DIRECTION[market.upper()], decision["direction_counts"])

    def test_decision_hit_rates_are_not_hardwired_equal(self) -> None:
        report = build_report(synthetic_payload(100, v2_favours="opposite"), STATE)
        for market in ("hdc", "hil"):
            model_hit = report["models"][market]["raw"]["holdout"]["decision"]["decision_integer_hit_rate_excluding_push"]
            production_hit = (report["v2_baseline"][market]["production_lead_decision"]["holdout"]
                              ["decision"]["decision_integer_hit_rate_excluding_push"])
            self.assertIsNotNone(model_hit)
            self.assertIsNotNone(production_hit)
            self.assertNotAlmostEqual(model_hit, production_hit)
            self.assertGreater(model_hit, production_hit)

    def test_isotonic_calibration_does_not_increase_selection_brier(self) -> None:
        report = build_report(synthetic_payload(100), STATE)
        for market in ("hdc", "hil"):
            raw = report["models"][market]["raw"]["selection"]["brier"]
            isotonic = report["models"][market]["isotonic"]["selection"]["brier"]
            self.assertIsNotNone(raw)
            self.assertIsNotNone(isotonic)
            self.assertLessEqual(isotonic, raw + 1e-12)

    def test_calibration_monotonicity_guard(self) -> None:
        report = build_report(synthetic_payload(100), STATE)
        for market in ("hdc", "hil"):
            guard = report["models"][market]["calibration_meta"]
            self.assertTrue(guard["isotonic_is_non_decreasing"])
            self.assertEqual(guard["fit_split"], "selection")
            self.assertTrue(guard["holdout_never_used_for_fitting"])
            if guard["platt_is_increasing"]:
                self.assertGreater(guard["platt_slope"], 0)
                self.assertFalse(guard["platt_rejected_and_scored_as_raw"])
            else:
                self.assertTrue(guard["platt_rejected_and_scored_as_raw"])
                self.assertEqual(report["models"][market]["platt"]["holdout"]["brier"],
                                 report["models"][market]["raw"]["holdout"]["brier"])
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

        for market in ("HDC", "HIL"):
            every = build_records(fixtures, market)
            holdout_records = [row for row in every if row["fixture_id"] in holdout_ids]
            earlier = [row for row in every if row["fixture_id"] not in holdout_ids]
            reported = report["models"][market.lower()]["raw"]["holdout"]
            self.assertEqual(reported["n_including_push"], len(holdout_records))
            encoding, prior = league_encoding(earlier)
            medians = feature_medians(earlier, encoding, prior)
            self.assertEqual(len(design_matrix(holdout_records, medians, encoding, prior)[0]), len(FEATURE_NAMES))

        mutated = copy.deepcopy(payload)
        for row in mutated["rows"]:
            if row["match_id"] in holdout_ids:
                row["result_detail"] = {"home_score": 0, "away_score": 5}
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
            baseline = dict(report["v2_baseline"][market])
            production = baseline.pop("production_lead_decision")
            blocks.extend(baseline.values())
            blocks.extend(production.values())
        self.assertTrue(blocks)
        for block in blocks:
            self.assertEqual(sum(bucket["n"] for bucket in block["buckets"].values()), block["n"])
            self.assertEqual(len(block["buckets"]), len(BUCKET_EDGES))
            self.assertEqual(block["n"] + block["push_n"], block["n_including_push"])

    def test_metric_block_arithmetic_and_decision_accounting(self) -> None:
        records = [
            sample_record(fixture_id="a"),
            sample_record(fixture_id="b", primary_grade="full_loss", opposite_grade="full_win",
                          label=0.0, primary_return=-1.0, opposite_return=1.1, opposite_odds=2.1),
            sample_record(fixture_id="c", primary_grade="push", opposite_grade="push", label=None,
                          is_push=True, primary_return=0.0, opposite_return=0.0, v2_lead_grade="push",
                          v2_lead_return=0.0),
        ]
        block = metric_block(records, [0.7, 0.6, 0.9])
        self.assertEqual(block["n"], 2)
        self.assertEqual(block["push_n"], 1)
        self.assertAlmostEqual(block["brier"], ((0.7 - 1) ** 2 + 0.6 ** 2) / 2)
        self.assertAlmostEqual(block["log_loss"], (-math.log(0.7) - math.log(0.4)) / 2)
        self.assertAlmostEqual(block["mean_probability"], 0.65)
        self.assertAlmostEqual(block["hit_rate_excluding_push"], 0.5)
        self.assertAlmostEqual(block["calibration_gap"], 0.15)
        # All three probabilities are >= 0.55, so the primary side is backed each time.
        self.assertEqual(block["decision"]["direction_counts"], {"home": 3})
        self.assertAlmostEqual(block["decision"]["direction_flip_rate_vs_v2_lead"], 1.0)
        self.assertEqual(block["roi_at_0.55_threshold"]["n_bets_including_push"], 3)
        self.assertAlmostEqual(block["roi_at_0.55_threshold"]["roi"], (0.9 - 1.0 + 0.0) / 3)
        self.assertAlmostEqual(block["decision"]["decision_accuracy_excluding_push"], 0.5)

        flipped = metric_block(records, [0.2, 0.3, 0.4])
        self.assertEqual(flipped["decision"]["direction_counts"], {"away": 3})
        self.assertAlmostEqual(flipped["roi_at_0.55_threshold"]["roi"], (-1.0 + 1.1 + 0.0) / 3)
        self.assertAlmostEqual(flipped["decision"]["decision_accuracy_excluding_push"], 0.5)
        self.assertAlmostEqual(flipped["brier"], ((0.2 - 1) ** 2 + 0.3 ** 2) / 2)

    def test_two_runs_produce_byte_identical_json(self) -> None:
        payload = synthetic_payload(100)
        first = json.dumps(build_report(payload, STATE), ensure_ascii=False, indent=2, sort_keys=True)
        second = json.dumps(build_report(copy.deepcopy(payload), STATE), ensure_ascii=False, indent=2, sort_keys=True)
        self.assertEqual(first.encode("utf-8"), second.encode("utf-8"))


if __name__ == "__main__":
    unittest.main()
