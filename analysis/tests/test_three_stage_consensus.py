from __future__ import annotations

import unittest

from analysis.three_stage_consensus import (
    STAGES,
    calculate_three_stage_consensus,
    calculate_three_stage_transitions,
)


def row(match: str, stage: str, code: str, side: str, line: float,
        hit: bool | None, status: str = "GRADED", odds: float | None = 1.9,
        home: str | None = None, away: str | None = None,
        predicted_at: str | None = None) -> dict:
    return {
        "match_id": match,
        "stage": stage,
        "home": home,
        "away": away,
        "predicted_at": predicted_at,
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

    def test_transition_same_actual_hdc_team_allows_feed_side_swap_but_rejects_token_only_match(self) -> None:
        rows = [
            row("same-team", "首預", "HDC", "H", -0.25, True,
                home="Alpha", away="Beta"),
            # This feed has the fixture teams reversed.  "A" is still Alpha.
            row("same-team", "T-30", "HDC", "A", 0.50, False,
                home="Beta", away="Alpha"),
            row("same-team", "T-5", "HDC", "H", -0.75, True,
                home="Alpha", away="Beta"),
            # H at each stage is not a stable actual team after this reversal.
            row("token-only", "首預", "HDC", "H", -0.25, True,
                home="Alpha", away="Beta"),
            row("token-only", "T-30", "HDC", "H", -0.50, False,
                home="Beta", away="Alpha"),
            row("token-only", "T-5", "HDC", "H", -0.75, True,
                home="Alpha", away="Beta"),
            # A raw home-line sign change caused only by the side swap is not
            # a numeric move for the selected Alpha team.
            row("normalised-line-stable", "首預", "HDC", "H", -0.50, True,
                home="Alpha", away="Beta"),
            row("normalised-line-stable", "T-30", "HDC", "A", 0.50, False,
                home="Beta", away="Alpha"),
            row("normalised-line-stable", "T-5", "HDC", "H", -0.50, True,
                home="Alpha", away="Beta"),
        ]

        report = calculate_three_stage_transitions(rows)
        hdc = report["conditions"]["same_direction_line_moved"]["markets"]["HDC"]
        high = hdc["aggregate"]["tiers"]["at_or_above_1_70"]
        categories = {item["key"]: item for item in hdc["breakdown"]}

        self.assertEqual(high["fixtures"], 1)
        self.assertEqual(high["decided"], 1)
        self.assertEqual(high["hits"], 1)
        self.assertEqual(categories["home_giving"]["tiers"]["at_or_above_1_70"]["fixtures"], 1)
        self.assertEqual(
            [item["label"] for item in hdc["breakdown"]],
            ["主讓", "主受讓", "平手盤（主）", "平手盤（客）", "客讓", "客受讓"],
        )

    def test_transition_first_missing_flip_and_stability_rules(self) -> None:
        rows = [
            # Condition B: first stage has no valid direction; T-30/T-5 agree.
            row("missing-first", "首預", "HIL", "?", 2.5, True),
            row("missing-first", "T-30", "HIL", "H", 2.5, False),
            row("missing-first", "T-5", "HIL", "H", 2.5, True),
            # Condition C: first direction flips at T-30 and remains at T-5.
            row("flip-stable", "首預", "HIL", "H", 2.5, True),
            row("flip-stable", "T-30", "HIL", "L", 2.5, False),
            row("flip-stable", "T-5", "HIL", "L", 2.5, True),
            # A flip that reverses again at T-5 must not qualify.
            row("flip-back", "首預", "HIL", "H", 2.5, True),
            row("flip-back", "T-30", "HIL", "L", 2.5, False),
            row("flip-back", "T-5", "HIL", "H", 2.5, True),
        ]

        report = calculate_three_stage_transitions(rows)["conditions"]
        missing = report["first_missing_then_stable"]["markets"]["HIL"]
        flipped = report["flip_then_stable"]["markets"]["HIL"]

        self.assertEqual(missing["aggregate"]["tiers"]["at_or_above_1_70"]["hits"], 1)
        self.assertEqual(missing["breakdown"][0]["label"], "大")
        self.assertEqual(flipped["aggregate"]["tiers"]["at_or_above_1_70"]["fixtures"], 1)
        self.assertEqual(flipped["aggregate"]["tiers"]["at_or_above_1_70"]["hits"], 1)
        self.assertEqual(flipped["breakdown"][1]["label"], "細")

    def test_transition_odds_boundary_invalid_prices_and_pushes_are_scoped_auditably(self) -> None:
        rows = []
        for match, odds, hit in (
            ("boundary", 1.70, True),
            ("low", 1.69, False),
            ("missing", None, True),
            ("invalid", 1.0, True),
            ("push", 1.80, None),
        ):
            rows.extend([
                row(match, "T-30", "CHL", "H", 9.5, False, odds=1.9),
                row(match, "T-5", "CHL", "H", 10.5, hit, odds=odds),
            ])

        chl = calculate_three_stage_transitions(rows)["conditions"][
            "first_missing_then_stable"
        ]["markets"]["CHL"]
        high = chl["aggregate"]["tiers"]["at_or_above_1_70"]
        low = chl["aggregate"]["tiers"]["below_1_70"]
        categories = {item["key"]: item for item in chl["breakdown"]}

        self.assertEqual(high, {
            "fixtures": 2, "settled": 2, "pushes": 1,
            "decided": 1, "hits": 1, "accuracy": 1.0,
        })
        self.assertEqual(low["fixtures"], 1)
        self.assertEqual(low["decided"], 1)
        self.assertEqual(low["hits"], 0)
        self.assertEqual(categories["over"]["label"], "角球大")
        self.assertEqual(categories["under"]["label"], "角球細")
        self.assertEqual(categories["over"]["tiers"]["at_or_above_1_70"]["pushes"], 1)

    def test_transition_dedupes_duplicate_stage_rows_deterministically(self) -> None:
        rows = [
            row("duplicate", "首預", "HIL", "H", 2.5, True, predicted_at="2026-01-01T00:00:00"),
            row("duplicate", "T-30", "HIL", "L", 2.5, False, predicted_at="2026-01-01T00:30:00"),
            row("duplicate", "T-5", "HIL", "L", 2.5, False, predicted_at="2026-01-01T00:54:00"),
            # The latest persisted T-5 record wins deterministically.
            row("duplicate", "T-5", "HIL", "L", 2.5, True, predicted_at="2026-01-01T00:55:00"),
        ]

        high = calculate_three_stage_transitions(rows)["conditions"][
            "flip_then_stable"
        ]["markets"]["HIL"]["aggregate"]["tiers"]["at_or_above_1_70"]

        self.assertEqual(high["fixtures"], 1)
        self.assertEqual(high["hits"], 1)
        self.assertEqual(high["decided"], 1)


if __name__ == "__main__":
    unittest.main()
