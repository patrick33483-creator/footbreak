"""Focused recomputation and corruption tests for Asian totals profiles."""
from __future__ import annotations

import copy
import unittest

from analysis.quarter_line import (
    from_dixon_coles, from_two_sided_market, validate,
)


class QuarterLineProfileTest(unittest.TestCase):
    def test_all_four_quarter_settlements(self) -> None:
        expected = {
            ("H", 2.75): "half_win",
            ("L", 2.75): "half_loss",
            ("H", 2.25): "half_loss",
            ("L", 2.25): "half_win",
        }
        for (side, line), result in expected.items():
            with self.subTest(side=side, line=line):
                profile = from_dixon_coles(
                    line=line, side=side, lh=1.5, la=1.2, rho=-.03,
                )
                self.assertIsNotNone(profile)
                self.assertEqual(profile["boundary_result"], result)
                self.assertEqual(validate(profile, market="HIL", side=side, line=line), profile)

    def test_one_complete_275_line_needs_no_neighbouring_line(self) -> None:
        profile = from_two_sided_market(
            line=2.75, side="H", over_odds=1.66, under_odds=2.18,
        )
        self.assertIsNotNone(profile)
        self.assertEqual(
            profile["method"], "native_t5_market_implied_poisson",
        )
        self.assertGreater(profile["source"]["fitted_total_mean"], 0)
        self.assertEqual(validate(profile), profile)

    def test_single_sided_market_fails_closed(self) -> None:
        self.assertIsNone(from_two_sided_market(
            line=2.75, side="H", over_odds=1.66, under_odds=None,
        ))

    def test_corruption_cannot_pass_recomputation(self) -> None:
        original = from_dixon_coles(
            line=2.75, side="H", lh=1.5, la=1.2, rho=-.03,
        )
        self.assertIsNotNone(original)
        for field, value in (
            ("boundary_probability_raw", .01),
            ("win_fraction_raw", .99),
            ("profile_hash", "0" * 64),
        ):
            corrupted = copy.deepcopy(original)
            corrupted[field] = value
            self.assertIsNone(validate(corrupted), field)
        corrupted_model = copy.deepcopy(original)
        corrupted_model["source"]["lh"] = 1.9
        self.assertIsNone(validate(corrupted_model))


if __name__ == "__main__":
    unittest.main()
