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
            "A-HDC-HHH-SAME-LINE",
        })
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
            conditions["A-HDC-OPEN-AWAY-MINUS-050"]["prospective"]["full_loss"], 1,
        )
        self.assertEqual(
            conditions["A-HDC-HHH-SAME-LINE"]["prospective"]["half_win"], 1,
        )
        self.assertEqual(
            conditions["A-HDC-HHH-SAME-LINE"]["prospective"]["roi"], 0.455,
        )
        self.assertTrue(payload["background_accumulation"]["enabled"])
        self.assertEqual(payload["public_tiers"], ["S", "A"])

    def test_thresholds_are_strict_and_line_change_does_not_match(self) -> None:
        rows = [
            _row("exact-odds", "T-5", _prediction("HIL", "H", 2.5, 1.85)),
            _row("changed", "首預", _prediction("HDC", "H", -0.25, 1.8)),
            _row("changed", "T-30", _prediction("HDC", "H", -0.5, 1.8)),
            _row("changed", "T-5", _prediction("HDC", "H", -0.25, 1.8)),
        ]

        payload = build_segmented_conditions(rows)
        conditions = {item["id"]: item for item in payload["public_conditions"]}

        self.assertEqual(
            conditions["S-HIL-T5-OVER-185"]["prospective"]["qualified"], 0,
        )
        self.assertEqual(
            conditions["A-HDC-HHH-SAME-LINE"]["prospective"]["qualified"], 0,
        )


if __name__ == "__main__":
    unittest.main()
