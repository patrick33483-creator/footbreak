from __future__ import annotations

import unittest

from crown.segmented_conditions import build_segmented_conditions


def _prediction(code: str, side: str, line: float, odds: float) -> dict:
    return {
        "code": code,
        "side": side,
        "line": line,
        "odds": odds,
        "probability": 0.6,
    }


def _row(
    match_id: str,
    stage: str,
    prediction: dict,
    *,
    kickoff: str = "2026-09-01T20:00:00+08:00",
    settlement: str | None = None,
) -> dict:
    grades = []
    if settlement:
        grades.append({
            **prediction,
            "grade_status": "GRADED",
            "settlement": settlement,
        })
    return {
        "match_id": match_id,
        "stage": stage,
        "kickoff": kickoff,
        "predicted_at": "2026-08-31T12:00:00+08:00",
        "league": "測試聯賽",
        "home": f"{match_id} 主隊",
        "away": f"{match_id} 客隊",
        "market_predictions": [prediction],
        "market_grades": grades,
        "result_status": "已核對" if settlement else "待賽果",
        "score": "2-1" if settlement else None,
    }


class SegmentedConditionTests(unittest.TestCase):
    def test_projects_only_frozen_s_and_a_conditions_after_cutoff(self) -> None:
        over_open = _prediction("HIL", "H", 2.75, 1.81)
        over_t5 = _prediction("HIL", "H", 3.0, 1.90)
        away_open = _prediction("HDC", "A", 0.5, 1.88)
        home_open = _prediction("HDC", "H", -0.25, 1.75)
        home_t30 = _prediction("HDC", "H", -0.25, 1.82)
        home_t5 = _prediction("HDC", "H", -0.25, 1.91)
        rows = [
            _row("over", "首預", over_open),
            _row("over", "T-5", over_t5, settlement="Won"),
            _row("away", "首預", away_open, settlement="Lost"),
            _row("home", "首預", home_open),
            _row("home", "T-30", home_t30),
            _row("home", "T-5", home_t5, settlement="Half Won"),
            _row(
                "old",
                "T-5",
                over_t5,
                kickoff="2026-08-30T20:00:00+08:00",
                settlement="Won",
            ),
        ]

        payload = build_segmented_conditions(rows)
        conditions = {item["id"]: item for item in payload["public_conditions"]}

        self.assertEqual(set(conditions), {
            "S-HIL-T5-OVER-185",
            "A-HIL-OPEN-T5-OVER-180",
            "A-HDC-OPEN-AWAY-MINUS-050",
            "S-HIL-OPEN-OVER-3-180",
        })
        # A-HDC-HHH-SAME-LINE tier lowered from A to B (backfilled sample val-30% ROI -5.7%);
        # continues to accumulate in background pool but is no longer public.
        all_ids = {item["condition_id"] for item in payload["matching_observations"]}
        self.assertIn("A-HDC-HHH-SAME-LINE", all_ids)
        self.assertEqual(
            conditions["S-HIL-T5-OVER-185"]["prospective"]["qualified"], 1,
        )
        self.assertEqual(
            conditions["S-HIL-T5-OVER-185"]["prospective"]["hit_rate"], 1.0,
        )
        self.assertEqual(
            conditions["S-HIL-T5-OVER-185"]["prospective"]["roi"], 0.9,
        )
        self.assertEqual(
            conditions["S-HIL-T5-OVER-185"]["historical"]["sample"], 77,
        )
        self.assertEqual(
            conditions["S-HIL-T5-OVER-185"]["combined"]["qualified"], 78,
        )
        self.assertAlmostEqual(
            conditions["S-HIL-T5-OVER-185"]["combined"]["roi"], 0.306538,
        )
        self.assertEqual(
            conditions["A-HDC-OPEN-AWAY-MINUS-050"]["prospective"]["full_loss"], 1,
        )
        self.assertEqual(
            conditions["A-HDC-OPEN-AWAY-MINUS-050"]["historical"]["sample"], 39,
        )
        self.assertEqual(
            conditions["A-HDC-OPEN-AWAY-MINUS-050"]["excluded_odds"], [1.85],
        )
        self.assertEqual(
            conditions["A-HDC-OPEN-AWAY-MINUS-050"]["combined"]["qualified"], 40,
        )
        self.assertTrue(payload["background_accumulation"]["enabled"])
        self.assertEqual(payload["public_tiers"], ["S", "A"])

    def test_thresholds_are_strict_and_line_change_does_not_match(self) -> None:
        rows = [
            _row("exact-odds", "T-5", _prediction("HIL", "H", 2.5, 1.85)),
            _row("excluded-line-200", "T-5", _prediction("HIL", "H", 2.0, 1.90)),
            _row("excluded-line-225", "T-5", _prediction("HIL", "H", 2.25, 1.90)),
            _row("excluded-away-odds-185", "首預", _prediction("HDC", "A", 0.5, 1.85)),
            _row("included-away-odds-184", "首預", _prediction("HDC", "A", 0.5, 1.84)),
            _row("changed", "首預", _prediction("HDC", "H", -0.25, 1.8)),
            _row("changed", "T-30", _prediction("HDC", "H", -0.5, 1.8)),
            _row("changed", "T-5", _prediction("HDC", "H", -0.25, 1.8)),
            # S-HIL-OPEN-OVER-3-180 threshold checks
            _row("open-line-3.25", "首預", _prediction("HIL", "H", 3.25, 1.90)),
            _row("open-odds-boundary", "首預", _prediction("HIL", "H", 3.0, 1.80)),
        ]

        payload = build_segmented_conditions(rows)
        conditions = {item["id"]: item for item in payload["public_conditions"]}

        self.assertEqual(
            conditions["S-HIL-T5-OVER-185"]["prospective"]["qualified"], 0,
        )
        self.assertEqual(
            conditions["S-HIL-T5-OVER-185"]["excluded_lines"], [2.0, 2.25],
        )
        self.assertEqual(
            conditions["A-HDC-OPEN-AWAY-MINUS-050"]["prospective"]["qualified"], 1,
        )
        # A-HDC-HHH-SAME-LINE is no longer public (tier B)
        # Line 3.25 or exact 1.80 odds must not match S-HIL-OPEN-OVER-3-180
        self.assertEqual(
            conditions["S-HIL-OPEN-OVER-3-180"]["prospective"]["qualified"], 0,
        )

    def test_s_hil_open_over_3_180_matches_only_line_3_with_odds_above_180(self) -> None:
        rows = [
            # Exact match: line 3, odds 1.85, Won
            _row(
                "match1",
                "首預",
                _prediction("HIL", "H", 3.0, 1.85),
                settlement="Won",
            ),
            # Odds too low
            _row("skip-odds", "首預", _prediction("HIL", "H", 3.0, 1.79)),
            # Wrong side (細)
            _row("skip-side", "首預", _prediction("HIL", "L", 3.0, 1.90)),
            # Wrong line
            _row("skip-line", "首預", _prediction("HIL", "H", 2.75, 1.90)),
        ]
        payload = build_segmented_conditions(rows)
        conditions = {item["id"]: item for item in payload["public_conditions"]}
        cond = conditions["S-HIL-OPEN-OVER-3-180"]
        self.assertEqual(cond["prospective"]["qualified"], 1)
        self.assertEqual(cond["prospective"]["full_win"], 1)
        self.assertEqual(cond["prospective"]["hit_rate"], 1.0)


if __name__ == "__main__":
    unittest.main()
