from __future__ import annotations

import sys
import unittest
from pathlib import Path
from unittest.mock import patch

SYSTEM_DIR = Path(__file__).resolve().parents[1]
if str(SYSTEM_DIR) not in sys.path:
    sys.path.insert(0, str(SYSTEM_DIR))

import settle


class ResultSourceTests(unittest.TestCase):
    def test_hkjc_results_are_normalized_for_footbreak_settlement(self) -> None:
        official = {
            "50072040": {
                "home_score": 2,
                "away_score": 2,
                "corners_total": 9,
                "source": "hkjc_official",
            }
        }
        with patch("crown.hkjc.fetch_official_results", return_value=official) as fetch:
            rows = settle.fetch_hkjc_results({"50072040"}, {"2026-08-09"})
        fetch.assert_called_once_with({"50072040"}, {"2026-08-09"})
        self.assertEqual(rows["50072040"]["goals_home"], 2)
        self.assertEqual(rows["50072040"]["goals_away"], 2)
        self.assertEqual(rows["50072040"]["goals_total"], 4)
        self.assertEqual(rows["50072040"]["corners_total"], 9)
        self.assertEqual(rows["50072040"]["source"], "hkjc_official")

    def test_hkjc_non_result_statuses_are_exposed(self) -> None:
        official = {
            "50072899": {
                "status": "MATCHSUSPENDED",
                "refund_pools": ["HAD"],
                "payout_refund_pools": [],
                "source": "hkjc_official",
            }
        }
        with patch("crown.hkjc.fetch_official_match_statuses", return_value=official):
            rows = settle.fetch_hkjc_statuses({"50072899"}, {"2026-08-09"})
        self.assertEqual(rows["50072899"]["status"], "MATCHSUSPENDED")


if __name__ == "__main__":
    unittest.main()
