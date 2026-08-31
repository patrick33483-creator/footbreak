from __future__ import annotations

import copy
import unittest
from datetime import datetime, timedelta, timezone

from analysis.crown_v3_backtest import (
    build_report,
    canonicalize,
    settle,
    split_kickoff_cohorts,
    unit_return,
)


def pick(market: str, direction: str, line: float, odds: float = 1.9, probability: float = 0.6):
    return {"market": market, "direction": direction, "line": line, "odds": odds, "probability": probability, "ev": probability * odds - 1}


def market_item(code: str, side: str, line: float, probability: float = 0.6, odds: float = 1.9):
    return {"code": code, "side": side, "line": line, "probability": probability, "odds": odds}


def synthetic_payload(count: int = 30) -> dict:
    rows = []
    start = datetime(2026, 1, 1, tzinfo=timezone.utc)
    for index in range(count):
        kickoff = start + timedelta(days=index)
        fixture_id = f"f{index:03d}"
        # Alternating outcomes prevent every selection candidate from being identical.
        home_score, away_score = ((2, 0) if index % 3 else (0, 2))
        for minutes, stage in ((90, "首預"), (30, "T-30"), (5, "T-5")):
            rows.append({
                "match_id": fixture_id,
                "stage": stage,
                "kickoff": kickoff.isoformat(),
                "predicted_at": (kickoff - timedelta(minutes=minutes)).isoformat(),
                "market_predictions": [
                    market_item("HDC", "H", -0.5, 0.60 + (index % 4) * 0.01, 1.9),
                    market_item("HDC", "A", -0.5, 0.40, 1.9),
                    market_item("HIL", "H", 2.5, 0.57 + (index % 3) * 0.01, 1.95),
                    market_item("HIL", "L", 2.5, 0.43, 1.95),
                ],
                "result_status": "已核對",
                "result_detail": {"home_score": home_score, "away_score": away_score},
            })
    return {"rows": rows}


class CrownV3SettlementTests(unittest.TestCase):
    def test_hil_quarter_half_win_half_loss_and_push(self):
        self.assertEqual(settle(pick("HIL", "over", 2.25), {"home_score": 1, "away_score": 1}), "half_loss")
        self.assertEqual(settle(pick("HIL", "under", 2.25), {"home_score": 1, "away_score": 1}), "half_win")
        self.assertEqual(settle(pick("HIL", "over", 2.0), {"home_score": 1, "away_score": 1}), "push")
        self.assertAlmostEqual(unit_return("half_win", 1.9), 0.45)
        self.assertEqual(unit_return("half_loss", 1.9), -0.5)
        self.assertEqual(unit_return("push", 1.9), 0.0)

    def test_hdc_quarter_settlement_both_sides(self):
        result = {"home_score": 1, "away_score": 1}
        self.assertEqual(settle(pick("HDC", "home", -0.25), result), "half_loss")
        self.assertEqual(settle(pick("HDC", "away", -0.25), result), "half_win")
        self.assertEqual(settle(pick("HDC", "home", 0.0), result), "push")
        self.assertEqual(settle(pick("HDC", "away", 0.0), result), "push")


class CrownV3AuditTests(unittest.TestCase):
    def test_canonical_latest_pre_kickoff_and_market_specific_max_ev(self):
        kickoff = datetime(2026, 1, 1, 12, tzinfo=timezone.utc)
        base = {
            "match_id": "x", "stage": "T-5", "kickoff": kickoff.isoformat(),
            "result_status": "已核對", "result_detail": {"home_score": 1, "away_score": 0},
        }
        rows = [
            {**base, "predicted_at": (kickoff - timedelta(minutes=7)).isoformat(), "market_predictions": [market_item("HDC", "H", -0.5, .9, 1.9)]},
            {**base, "predicted_at": (kickoff - timedelta(minutes=4)).isoformat(), "market_predictions": [market_item("HDC", "A", -0.5, .99, 1.9)]},  # latest valid
            {**base, "predicted_at": (kickoff + timedelta(seconds=1)).isoformat(), "market_predictions": [market_item("HDC", "H", -0.5, .99, 1.9)]},
        ]
        fixtures, diagnostics = canonicalize(rows)
        self.assertEqual(fixtures[0]["stages"]["T-5"]["leads"]["HDC"]["direction"], "away")
        self.assertEqual(diagnostics["invalid_or_post_kickoff_snapshot"], 1)

    def test_equal_kickoff_cohort_never_crosses_split(self):
        base = datetime(2026, 1, 1, tzinfo=timezone.utc)
        fixtures = []
        for index in range(10):
            # Fixtures 5-7 form one cohort around a nominal boundary.
            kickoff = base + timedelta(days=5 if 5 <= index <= 7 else index)
            fixtures.append({"fixture_id": str(index), "kickoff": kickoff})
        parts, metadata = split_kickoff_cohorts(fixtures)
        membership = {row["fixture_id"]: part for part, rows in parts.items() for row in rows}
        self.assertEqual(len({membership[str(i)] for i in (5, 6, 7)}), 1)
        self.assertTrue(metadata["no_equal_kickoff_crosses_split"])

    def test_holdout_results_cannot_change_locked_rules(self):
        payload = synthetic_payload(30)
        state = {"sha256": "synthetic", "size": 0, "mtime_ns": 0}
        first = build_report(payload, state, min_discovery=3, min_selection=2, bootstrap_samples=20)
        changed = copy.deepcopy(payload)
        # Last six kickoff cohorts are holdout for 30 singleton cohorts.
        holdout_ids = {f"f{i:03d}" for i in range(24, 30)}
        for row in changed["rows"]:
            if row["match_id"] in holdout_ids:
                row["result_detail"] = {"home_score": 9, "away_score": 0} if row["result_detail"]["home_score"] == 0 else {"home_score": 0, "away_score": 9}
        second = build_report(changed, state, min_discovery=3, min_selection=2, bootstrap_samples=20)
        self.assertEqual(first["locked_rules_selected_without_holdout"], second["locked_rules_selected_without_holdout"])
        self.assertNotEqual(first["comparisons"]["v2_baseline_t5_cross_market_max_ev_always"]["holdout"]["roi"], second["comparisons"]["v2_baseline_t5_cross_market_max_ev_always"]["holdout"]["roi"])

    def test_expected_split_counts_and_metric_fields(self):
        report = build_report(synthetic_payload(20), {"sha256": "x", "size": 1, "mtime_ns": 1}, min_discovery=2, min_selection=2, bootstrap_samples=10)
        self.assertEqual(report["split"]["counts"], {"discovery": 12, "selection": 4, "holdout": 4})
        metrics = report["comparisons"]["v3_portfolio"]["holdout"]
        for key in ("n", "coverage", "hit_rate_excluding_push", "roi", "wilson_hit_rate_95", "fixture_cluster_bootstrap_roi_95", "brier_excluding_push", "log_loss_excluding_push", "calibration_gap_abs_excluding_push"):
            self.assertIn(key, metrics)
        self.assertTrue(report["scope"]["not_upstream_retraining"])


if __name__ == "__main__":
    unittest.main()
