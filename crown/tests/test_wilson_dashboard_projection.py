"""Small public-contract guards for Wilson condition explanations."""
from __future__ import annotations

import unittest

from analysis.wilson_validation import admission_arithmetic
from crown.dashboard_data import _wilson_match_projection


class CrownWilsonDashboardProjectionTests(unittest.TestCase):
    def test_projection_uses_persisted_raw_and_display_admission_values(self) -> None:
        arithmetic = admission_arithmetic(41, 59, 1.90)
        assert arithmetic is not None
        source = {
            "condition_number": 4,
            "market": "CHL",
            "market_label": "角球大細",
            "selected_role": "大",
            "selected_line": 9.5,
            # This deliberately differs from the persisted raw admission quote.
            "odds": 9.99,
            "wilson_admission": arithmetic,
        }
        actual = _wilson_match_projection(source, bet_status="NO_BET_LOW_ODDS")
        self.assertEqual(actual["condition_number"], 4)
        self.assertEqual(actual["odds"], arithmetic["actual_decimal_odds_raw"])
        self.assertEqual(actual["minimum_required_odds"], arithmetic["minimum_acceptable_odds_raw"])
        self.assertEqual(
            actual["minimum_required_odds_display"],
            arithmetic["display"]["minimum_acceptable_odds"],
        )
        self.assertEqual(actual["bet_status"], "NO_BET_LOW_ODDS")


if __name__ == "__main__":
    unittest.main()
