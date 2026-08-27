from __future__ import annotations

import unittest

from analysis.audit_crown_condition2_three_stage import _qualify


class CrownCondition2ThreeStageAuditTest(unittest.TestCase):
    @staticmethod
    def rows(t30_side="H", t5_side="H"):
        return {
            ("1", "HIL", "首預"): {
                "side": "H", "selected_line": 2.75, "odds": 1.70,
            },
            ("1", "HIL", "T-30"): {
                "side": t30_side, "selected_line": 3.25, "odds": 1.55,
            },
            ("1", "HIL", "T-5"): {
                "side": t5_side, "selected_line": 2.50, "odds": 1.60,
            },
        }

    def test_later_line_and_odds_do_not_disqualify_all_over(self):
        qualified, reason, _stages = _qualify(self.rows(), "1")
        self.assertTrue(qualified)
        self.assertEqual(reason, "qualified")

    def test_t30_reversal_disqualifies(self):
        qualified, reason, _stages = _qualify(
            self.rows(t30_side="L"), "1",
        )
        self.assertFalse(qualified)
        self.assertEqual(reason, "T-30_not_over")

    def test_t5_reversal_disqualifies(self):
        qualified, reason, _stages = _qualify(
            self.rows(t5_side="L"), "1",
        )
        self.assertFalse(qualified)
        self.assertEqual(reason, "T-5_not_over")

    def test_missing_later_stage_disqualifies(self):
        rows = self.rows()
        del rows[("1", "HIL", "T-5")]
        qualified, reason, _stages = _qualify(rows, "1")
        self.assertFalse(qualified)
        self.assertEqual(reason, "missing_T-5_hil")


if __name__ == "__main__":
    unittest.main()
