"""Strict regression coverage for selected-odds market scorecards."""
from __future__ import annotations

import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
import sys

from analysis.data_health import build_reports, public_view
from analysis.learning_store import LearningStore
from analysis.market_statistics import market_metrics, odds_bucket
from crown.prediction_history import calculate_stats

SYSTEM_DIR = Path(__file__).resolve().parents[2] / "system"
if str(SYSTEM_DIR) not in sys.path:
    sys.path.insert(0, str(SYSTEM_DIR))
from gen_app_data import _prediction_history_stats


def grade(odds, hit, status="GRADED"):
    return {
        "code": "HDC",
        "odds": odds,
        "grade_status": status,
        "hit": hit,
        "brier": .2,
        "log_loss": .5,
    }


class MarketStatisticsTests(unittest.TestCase):
    def test_corner_metrics_split_over_and_under_with_independent_odds_tiers(self):
        def corner(side, odds, hit):
            return {
                "code": "CHL",
                "side": side,
                "odds": odds,
                "grade_status": "GRADED",
                "hit": hit,
            }

        metrics = market_metrics([
            {"market_grades": [corner("H", 1.80, True)]},
            {"market_grades": [corner("H", 1.75, False)]},
            {"market_grades": [corner("H", 1.60, True)]},
            {"market_grades": [corner("L", 1.90, True)]},
            {"market_grades": [corner("L", 1.65, False)]},
        ], "CHL")

        over = metrics["by_selection"]["H"]
        under = metrics["by_selection"]["L"]
        self.assertEqual((over["hits"], over["decided"]), (1, 2))
        self.assertEqual(
            (
                over["odds_groups"]["below_1_70"]["hits"],
                over["odds_groups"]["below_1_70"]["decided"],
            ),
            (1, 1),
        )
        self.assertEqual((under["hits"], under["decided"]), (1, 1))
        self.assertEqual(
            (
                under["odds_groups"]["below_1_70"]["hits"],
                under["odds_groups"]["below_1_70"]["decided"],
            ),
            (0, 1),
        )

    def test_odds_boundary_missing_push_and_pending_are_exclusive(self):
        rows = [
            {"market_grades": [grade(1.70, True)]},
            {"market_grades": [grade(1.699, False)]},
            {"market_grades": [grade(None, True)]},
            {"market_grades": [grade(float("nan"), False)]},
            {"market_grades": [grade(2.00, None)]},  # push: no denominator
            {"market_grades": [grade(2.00, False, "NOT_APPLICABLE")]},  # pending/excluded
        ]
        metrics = market_metrics(rows, "HDC")
        groups = metrics["odds_groups"]

        self.assertEqual(odds_bucket(1.70), "at_or_above_1_70")
        self.assertEqual(odds_bucket(1.699), "below_1_70")
        self.assertEqual(odds_bucket(None), "missing")
        self.assertEqual(odds_bucket(float("nan")), "missing")
        self.assertEqual(metrics["odds_scope"], "selected_odds_at_or_above_1_70")
        self.assertEqual((metrics["graded"], metrics["decided"], metrics["hits"]), (2, 1, 1))
        self.assertEqual(metrics["pushes"], 1)
        self.assertEqual((groups["below_1_70"]["graded"], groups["below_1_70"]["decided"]), (1, 1))
        self.assertNotIn("missing", groups)
        self.assertEqual(metrics["excluded_missing_odds"], 2)
        self.assertEqual((metrics["all_odds"]["graded"], metrics["all_odds"]["decided"], metrics["all_odds"]["hits"]), (3, 2, 1))
        self.assertEqual(
            sum(group["graded"] for group in groups.values()),
            metrics["all_odds"]["graded"],
        )
        self.assertEqual(
            sum(group["decided"] for group in groups.values()),
            metrics["all_odds"]["decided"],
        )

    def test_footbreak_and_crown_use_the_same_market_contract(self):
        rows = [
            {
                "prediction_era": "current",
                "match_id": "fixture",
                "stage": stage,
                "actual": "主勝",
                "forecast": "主勝",
                "correct": True,
                "market_grades": [grade(1.70 if stage != "T-30" else 1.699, stage != "T-30")],
            }
            for stage in ("首預", "T-30", "T-5")
        ]
        footbreak = _prediction_history_stats(rows)
        crown = calculate_stats(rows)
        for payload in (footbreak, crown):
            self.assertEqual(payload["by_market"]["HDC"]["all_odds"]["graded"], 3)
            self.assertEqual(payload["by_stage_market"]["首預"]["HDC"]["graded"], 1)
            self.assertEqual(payload["by_stage_market"]["T-30"]["HDC"]["odds_groups"]["below_1_70"]["graded"], 1)
            self.assertEqual(payload["market_overall"]["odds_groups"]["at_or_above_1_70"]["decided"], 2)
        self.assertEqual(footbreak["by_market"], crown["by_market"])
        self.assertEqual(footbreak["by_stage_market"], crown["by_stage_market"])
        self.assertEqual(footbreak["market_overall"], crown["market_overall"])
        for payload in (footbreak, crown):
            market = payload["by_market"]["HDC"]
            self.assertEqual(
                sum(group["graded"] for group in market["odds_groups"].values()),
                market["all_odds"]["graded"],
            )
            self.assertEqual(
                sum(group["decided"] for group in market["odds_groups"].values()),
                market["all_odds"]["decided"],
            )
            self.assertEqual(
                sum(
                    payload["by_stage_market"][stage]["HDC"]["all_odds"]["graded"]
                    for stage in ("首預", "T-30", "T-5")
                ),
                market["all_odds"]["graded"],
            )

    def test_data_health_current_era_scope_reconciles_and_retains_all_history_audit(self):
        now = datetime(2026, 8, 13, tzinfo=timezone.utc)
        with tempfile.TemporaryDirectory() as directory:
            db = Path(directory) / "learning.sqlite"
            with LearningStore(db) as store:
                for index, version in enumerate((
                    "2026-08-10-market-learning-v2",
                    "2026-08-10-market-learning-v2",
                    "legacy-market-v1",
                )):
                    snapshot = store.record_snapshot(
                        "footbreak", f"f-{index}", "T-5",
                        now - timedelta(hours=2), now - timedelta(hours=1),
                        {"league": "test", "market_predictions": [{
                            "code": "HDC", "condition": "-0.5", "side": "H",
                            "probability": .6, "odds": 1.9,
                        }]},
                        model_version=version, schema_version="2",
                    )
                    store.record_grade(snapshot, "HDC", "-0.5|H", "GRADED", {
                        "hit": index != 1, "target": 1 if index != 1 else 0,
                    })
            report = build_reports(db, now=now)["footbreak"]
        self.assertEqual(report["scope"]["model_version"], "2026-08-10-market-learning-v2")
        self.assertEqual(report["baseline"]["graded_rows"], 2)
        self.assertEqual(report["completeness"]["overall"]["all_history_audit"]["graded_rows"], 3)
        self.assertEqual(public_view(report)["scope"], report["scope"])
        dashboard = _prediction_history_stats([
            {
                "prediction_era": "2026-08-10-market-learning-v2",
                "match_id": f"f-{index}",
                "stage": "T-5",
                "market_grades": [grade(1.9, index != 1)],
            }
            for index in range(2)
        ])
        # Comparable counts use all odds for reconciliation; the visible
        # market hit rate remains the separate >=1.70 primary cohort.
        self.assertEqual(
            dashboard["market_overall"]["all_odds"]["graded"],
            report["baseline"]["graded_rows"],
        )


if __name__ == "__main__":
    unittest.main()
