from __future__ import annotations

import unittest

from analysis.three_stage_consensus import calculate_three_stage_consensus


def row(match: str, stage: str, code: str, side: str, line: float,
        hit: bool | None, status: str = "GRADED") -> dict:
    return {
        "match_id": match,
        "stage": stage,
        "market_grades": [{
            "code": code,
            "side": side,
            "line": line,
            "grade_status": status,
            "hit": hit,
        }],
    }


class ThreeStageConsensusTests(unittest.TestCase):
    def test_counts_one_fixture_once_at_t5_and_tracks_line_changes(self) -> None:
        rows = [
            row("a", "首預", "HDC", "H", -0.5, True),
            row("a", "T-30", "HDC", "H", -0.75, False),
            row("a", "T-5", "HDC", "H", -0.75, True),
            row("b", "首預", "HDC", "A", 0.5, False),
            row("b", "T-30", "HDC", "A", 0.5, False),
            row("b", "T-5", "HDC", "A", 0.5, False),
        ]

        result = calculate_three_stage_consensus(rows)["markets"]["HDC"]

        self.assertEqual(result["same_direction"]["fixtures"], 2)
        self.assertEqual(result["same_direction"]["line_changed_fixtures"], 1)
        self.assertEqual(result["same_direction"]["primary"]["decided"], 2)
        self.assertEqual(result["same_direction"]["primary"]["hits"], 1)
        self.assertEqual(result["same_direction"]["primary"]["accuracy"], 0.5)
        self.assertEqual(result["same_direction_and_line"]["fixtures"], 1)
        self.assertEqual(
            result["same_direction_and_line"]["primary"]["accuracy"], 0.0
        )

    def test_rejects_changed_direction_or_missing_stage(self) -> None:
        rows = [
            row("a", "首預", "HIL", "H", 2.5, True),
            row("a", "T-30", "HIL", "L", 2.5, False),
            row("a", "T-5", "HIL", "H", 2.5, True),
            row("b", "首預", "HIL", "L", 2.5, True),
            row("b", "T-5", "HIL", "L", 2.5, True),
        ]

        result = calculate_three_stage_consensus(rows)["markets"]["HIL"]

        self.assertEqual(result["same_direction"]["fixtures"], 0)
        self.assertIsNone(result["same_direction"]["primary"]["accuracy"])

    def test_push_is_excluded_from_accuracy_denominator(self) -> None:
        rows = [
            row("a", stage, "CHL", "H", 10.0, None)
            for stage in ("首預", "T-30", "T-5")
        ]

        result = calculate_three_stage_consensus(rows)["markets"]["CHL"]

        self.assertEqual(result["same_direction"]["fixtures"], 1)
        self.assertEqual(result["same_direction"]["primary"]["decided"], 0)
        self.assertIsNone(result["same_direction"]["primary"]["accuracy"])


if __name__ == "__main__":
    unittest.main()
