from __future__ import annotations

import unittest

from analysis.quarter_line import validate
from analysis.recover_crown_condition2_history import _with_quarter_line_profile


class CrownCondition2HistoryRecoveryTest(unittest.TestCase):
    def test_reconstructs_quarter_profile_from_same_stage_two_sided_quote(self) -> None:
        selected = {"code": "HIL", "side": "H", "line": 2.75, "odds": 1.81}
        source = {
            "market_predictions": [
                {"code": "HIL", "side": "H", "line": 2.75, "odds": 1.81},
                {"code": "HIL", "side": "L", "line": 2.75, "odds": 2.05},
            ],
        }

        recovered = _with_quarter_line_profile(selected, source)

        self.assertNotIn("quarter_line_settlement", selected)
        self.assertEqual(
            validate(
                recovered["quarter_line_settlement"],
                market="HIL", side="H", line=2.75,
            ),
            recovered["quarter_line_settlement"],
        )

    def test_never_guesses_profile_without_one_exact_quote_per_side(self) -> None:
        selected = {"code": "HIL", "side": "H", "line": 2.75, "odds": 1.81}
        source = {
            "market_predictions": [
                {"code": "HIL", "side": "H", "line": 2.75, "odds": 1.81},
            ],
        }

        recovered = _with_quarter_line_profile(selected, source)

        self.assertNotIn("quarter_line_settlement", recovered)

    def test_uses_persisted_same_stage_no_vig_probability_for_legacy_row(self) -> None:
        selected = {
            "code": "HIL", "side": "H", "line": 2.75, "odds": 1.81,
            "probability": 0.53142,
        }

        recovered = _with_quarter_line_profile(selected, {})

        profile = recovered["quarter_line_settlement"]
        self.assertEqual(profile["method"], "native_market_no_vig_probability")
        self.assertEqual(
            profile["source"]["selected_probability"], selected["probability"],
        )
        self.assertEqual(
            validate(profile, market="HIL", side="H", line=2.75), profile,
        )

    def test_integer_line_does_not_require_profile(self) -> None:
        selected = {"code": "HIL", "side": "H", "line": 3.0, "odds": 1.88}

        recovered = _with_quarter_line_profile(selected, {})

        self.assertEqual(recovered, selected)


if __name__ == "__main__":
    unittest.main()
