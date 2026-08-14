from __future__ import annotations

import unittest
from datetime import datetime, timedelta, timezone

from analysis.condition_mining_report import build_report


class ConditionMiningReportTests(unittest.TestCase):
    def test_single_stage_crosses_handicap_role_with_line_bucket(self) -> None:
        start = datetime(2026, 1, 1, tzinfo=timezone.utc)
        rows = []
        for index in range(40):
            kickoff = (start + timedelta(days=index)).isoformat()
            hit = index % 3 != 0
            rows.append({
                "match_id": f"fixture-{index}",
                "kickoff": kickoff,
                "predicted_at": kickoff,
                "stage": "T-5",
                "market_grades": [{
                    "code": "HDC",
                    "side": "H",
                    "line": -0.5,
                    "odds": 1.8,
                    "grade_status": "GRADED",
                    "hit": hit,
                    "settlement": "Won" if hit else "Lost",
                }],
            })
        payload = {"prediction_history": {"rows": rows}}
        report = build_report(payload, payload)["systems"]["crown"]
        conditions = {
            row["condition"]: row for row in report["top_conditions"]
        }
        self.assertIn("T-5｜淺盤≤0.5｜主讓", conditions)
        coverage = report["coverage_by_role_line_bucket"]
        segment = next(
            row for row in coverage
            if row["market"] == "HDC"
            and row["decision_stage"] == "T-5"
            and row["odds_tier"] == "≥1.70"
            and row["role"] == "主讓"
            and row["line_bucket"] == "淺盤≤0.5"
        )
        self.assertEqual(segment["total"]["decided"], 40)

    def test_finds_supported_a_b_a_and_keeps_odds_tiers_separate(self) -> None:
        rows = []
        start = datetime(2026, 1, 1, tzinfo=timezone.utc)
        for index in range(100):
            match_id = f"m{index:03d}"
            kickoff = (start + timedelta(days=index)).isoformat()
            # Both chronological partitions contain both price tiers.  This
            # prevents a condition from appearing qualified merely because one
            # tier exists only in development or only in holdout.
            high = index % 10 < 7
            odds = 1.80 if high else 1.60
            hit = (index % 10) != 0 if high else (index % 2) == 0
            for stage, side in (("首預", "H"), ("T-30", "L"), ("T-5", "H")):
                rows.append({
                    "match_id": match_id,
                    "kickoff": kickoff,
                    "predicted_at": kickoff,
                    "stage": stage,
                    "home": "Home",
                    "away": "Away",
                    "market_grades": [{
                        "code": "HIL",
                        "side": side,
                        "line": 2.5,
                        "odds": odds,
                        "grade_status": "GRADED",
                        "hit": hit,
                        "settlement": "Won" if hit else "Lost",
                        "probability": 0.61,
                    }],
                })
        payload = {"prediction_history": {"rows": rows}}
        report = build_report(payload, payload)
        top = report["systems"]["footbreak"]["top_conditions"]
        aba = [
            item for item in top
            if item["odds_tier"] == "≥1.70"
            and "方向 A→B→A" in item["condition"]
        ]
        self.assertTrue(aba)
        self.assertEqual(aba[0]["total"]["decided"], 70)
        self.assertTrue(all(item["odds_tier"] in {"≥1.70", "<1.70"} for item in top))
        coverage = report["systems"]["footbreak"]["coverage_by_market_stage_tier"]
        coverage_keys = {
            (item["market"], item["decision_stage"], item["odds_tier"])
            for item in coverage
        }
        self.assertIn(("HIL", "T-5", "≥1.70"), coverage_keys)
        self.assertIn(("HIL", "T-5", "<1.70"), coverage_keys)
        t5_high = next(
            item for item in coverage
            if (item["market"], item["decision_stage"], item["odds_tier"])
            == ("HIL", "T-5", "≥1.70")
        )
        self.assertEqual(t5_high["total"]["decided"], 70)
        self.assertTrue(report["read_only"])
        self.assertTrue(report["aggregate_only"])

    def test_push_is_not_counted_as_decision(self) -> None:
        rows = []
        start = datetime(2026, 1, 1, tzinfo=timezone.utc)
        for index in range(40):
            hit = None if index == 0 else True
            rows.append({
                "match_id": f"m{index:03d}",
                "kickoff": (start + timedelta(days=index)).isoformat(),
                "predicted_at": (start + timedelta(days=index)).isoformat(),
                "stage": "T-5",
                "market_grades": [{
                    "code": "CHL", "side": "L", "line": 10.5, "odds": 1.8,
                    "grade_status": "GRADED", "hit": hit,
                    "settlement": "Refunded" if hit is None else "Won",
                }],
            })
        payload = {"prediction_history": {"rows": rows}}
        report = build_report(payload, payload)
        condition = next(
            item for item in report["systems"]["footbreak"]["top_conditions"]
            if item["condition"] == "T-5 全部" and item["market"] == "CHL"
        )
        self.assertEqual(condition["total"]["settled"], 40)
        self.assertEqual(condition["total"]["decided"], 39)
        self.assertEqual(condition["total"]["pushes"], 1)


if __name__ == "__main__":
    unittest.main()
