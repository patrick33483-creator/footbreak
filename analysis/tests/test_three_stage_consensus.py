from __future__ import annotations

import unittest

from analysis.three_stage_consensus import STAGES, calculate_three_stage_consensus


def row(match: str, stage: str, code: str, side: str, line: float,
        hit: bool | None, status: str = "GRADED", odds: float | None = 1.9) -> dict:
    return {
        "match_id": match,
        "stage": stage,
        "market_grades": [{
            "code": code,
            "side": side,
            "line": line,
            "grade_status": status,
            "hit": hit,
            "odds": odds,
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

    def test_exact_hdc_breakdown_uses_selected_team_and_home_line(self) -> None:
        fixtures = (
            ("hg", "H", -0.5, True, "home_giving"),
            ("hr", "H", 0.5, False, "home_receiving"),
            ("sh", "H", 0.0, True, "scratch_home"),
            ("sa", "A", 0.0, False, "scratch_away"),
            ("ag", "A", 0.5, True, "away_giving"),
            ("ar", "A", -0.5, False, "away_receiving"),
        )
        rows = [
            row(match, stage, "HDC", side, line, hit)
            for match, side, line, hit, _ in fixtures
            for stage in STAGES
        ]

        result = calculate_three_stage_consensus(rows)["markets"]["HDC"]
        breakdown = {
            item["key"]: item
            for item in result["same_direction_and_line"]["breakdown"]
        }

        self.assertEqual(set(breakdown), {item[4] for item in fixtures})
        self.assertEqual(sum(item["fixtures"] for item in breakdown.values()), 6)
        self.assertEqual(sum(item["decided"] for item in breakdown.values()), 6)
        self.assertEqual(breakdown["home_giving"]["accuracy"], 1.0)
        self.assertEqual(breakdown["home_receiving"]["accuracy"], 0.0)
        self.assertEqual(breakdown["scratch_home"]["label"], "平手盤（主）")
        self.assertEqual(breakdown["scratch_away"]["label"], "平手盤（客）")
        self.assertEqual(breakdown["away_giving"]["accuracy"], 1.0)
        self.assertEqual(breakdown["away_receiving"]["accuracy"], 0.0)

    def test_exact_totals_breakdown_separates_over_and_under(self) -> None:
        rows = [
            row(match, stage, "HIL", side, 2.5, hit)
            for match, side, hit in (
                ("over-win", "H", True),
                ("over-loss", "H", False),
                ("under-win", "L", True),
            )
            for stage in STAGES
        ]

        result = calculate_three_stage_consensus(rows)["markets"]["HIL"]
        breakdown = {
            item["key"]: item
            for item in result["same_direction_and_line"]["breakdown"]
        }

        self.assertEqual(breakdown["over"]["fixtures"], 2)
        self.assertEqual(breakdown["over"]["hits"], 1)
        self.assertEqual(breakdown["over"]["accuracy"], 0.5)
        self.assertEqual(breakdown["under"]["fixtures"], 1)
        self.assertEqual(breakdown["under"]["hits"], 1)
        self.assertEqual(breakdown["under"]["accuracy"], 1.0)

    def test_ranking_prioritises_qualified_samples_then_accuracy(self) -> None:
        rows = []
        for index in range(31):
            hit = index < 20
            rows.extend(
                row(f"hil-under-{index}", stage, "HIL", "L", 2.5, hit)
                for stage in STAGES
            )
        for index in range(30):
            hit = index < 25
            rows.extend(
                row(f"chl-under-{index}", stage, "CHL", "L", 9.5, hit)
                for stage in STAGES
            )
        for index in range(2):
            rows.extend(
                row(f"scratch-home-{index}", stage, "HDC", "H", 0.0, True)
                for stage in STAGES
            )

        ranking = calculate_three_stage_consensus(rows)["ranking"]

        self.assertEqual(ranking["minimum_decided"], 30)
        self.assertEqual(ranking["candidate_count"], 3)
        self.assertEqual(
            [(item["market"], item["condition_key"]) for item in ranking["top"]],
            [("CHL", "under"), ("HIL", "under"), ("HDC", "scratch_home")],
        )
        self.assertTrue(ranking["top"][0]["sample_qualified"])
        self.assertTrue(ranking["top"][1]["sample_qualified"])
        self.assertFalse(ranking["top"][2]["sample_qualified"])

    def test_ranking_exposes_low_odds_bias_and_excluded_accuracy(self) -> None:
        rows = []
        fixtures = (
            ("low-win", 1.50, True),
            ("low-loss", 1.69, False),
            ("kept-win", 1.70, True),
            ("kept-loss", 2.00, False),
            ("missing-win", None, True),
            ("missing-nan", float("nan"), False),
        )
        for match, odds, hit in fixtures:
            rows.extend(
                row(match, stage, "HIL", "L", 2.5, hit, odds=odds)
                for stage in STAGES
            )

        under = calculate_three_stage_consensus(rows)["ranking"]["top"][0]
        audit = under["odds_bias"]

        self.assertEqual(under["decided"], 2)
        self.assertEqual(under["hits"], 1)
        self.assertEqual(under["accuracy"], 0.5)
        self.assertEqual(audit["threshold"], 1.70)
        self.assertEqual(audit["decided"], 6)
        self.assertEqual(audit["priced_decided"], 4)
        self.assertEqual(audit["missing_odds"], 2)
        self.assertEqual(audit["average_odds"], 1.722)
        self.assertEqual(audit["low_odds"]["decided"], 2)
        self.assertEqual(audit["low_odds"]["hits"], 1)
        self.assertEqual(audit["low_odds"]["accuracy"], 0.5)
        self.assertEqual(audit["low_odds"]["average_odds"], 1.595)
        self.assertEqual(audit["low_odds"]["share"], 0.5)
        self.assertEqual(audit["at_or_above_threshold"]["decided"], 2)
        self.assertEqual(audit["at_or_above_threshold"]["hits"], 1)
        self.assertEqual(audit["at_or_above_threshold"]["accuracy"], 0.5)
        self.assertEqual(audit["at_or_above_threshold"]["average_odds"], 1.85)

    def test_low_or_missing_odds_never_enter_main_statistics_or_ranking(self) -> None:
        rows = []
        for match, odds, hit in (
            ("low-win", 1.50, True),
            ("low-loss", 1.69, False),
            ("missing-win", None, True),
        ):
            rows.extend(
                row(match, stage, "CHL", "L", 9.5, hit, odds=odds)
                for stage in STAGES
            )

        report = calculate_three_stage_consensus(rows)
        market = report["markets"]["CHL"]
        under = {
            item["key"]: item
            for item in market["same_direction_and_line"]["breakdown"]
        }["under"]

        self.assertEqual(market["same_direction"]["primary"]["decided"], 0)
        self.assertIsNone(market["same_direction"]["primary"]["accuracy"])
        self.assertEqual(under["decided"], 0)
        self.assertEqual(under["all_fixtures"], 3)
        self.assertEqual(under["odds_bias"]["low_odds"]["decided"], 2)
        self.assertEqual(under["odds_bias"]["missing_odds"], 1)
        self.assertEqual(report["ranking"]["candidate_count"], 0)


if __name__ == "__main__":
    unittest.main()
