from __future__ import annotations

import json
import sys
import tempfile
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
        fetch.assert_called_once_with(
            {"50072040"}, {"2026-08-08", "2026-08-09"}
        )
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

    def test_corner_required_refreshes_an_incomplete_exact_fixture_cache(self) -> None:
        incomplete = {
            "fixture_id": "fx1", "goals_home": 1, "goals_away": 0,
            "goals_total": 1, "corners_home": None, "corners_away": None,
            "corners_total": None, "source": "opticodds_exact_fixture_id",
        }
        completed = {
            "fixture": {"id": "fx1", "status": "completed"},
            "scores": {
                "home": {"total": 1}, "away": {"total": 0},
            },
            "market_stats": {
                "home": {"team_total_corners": 6},
                "away": {"team_total_corners": 4},
            },
        }
        with tempfile.TemporaryDirectory() as directory, \
             patch.object(settle, "RESCACHE", directory), \
             patch.object(settle, "_call", return_value={"data": [completed]}) as call:
            Path(directory, "fx1.json").write_text(
                json.dumps(incomplete), encoding="utf-8"
            )
            result = settle.fetch_result("fx1", require_corners=True)
        call.assert_called_once()
        self.assertEqual(result["corners_total"], 10)

    def test_official_score_kept_when_exact_fixture_adds_corners(self) -> None:
        official = {
            "goals_home": 2, "goals_away": 1, "goals_total": 3,
            "corners_home": None, "corners_away": None,
            "corners_total": None, "source": "hkjc_official",
        }
        fallback = {
            "goals_home": 9, "goals_away": 9, "goals_total": 18,
            "corners_home": 7, "corners_away": 3, "corners_total": 10,
            "source": "opticodds_exact_fixture_id",
        }
        with patch.object(settle, "fetch_result", return_value=fallback) as fetch:
            result = settle.merge_missing_corners(official, "fx-safe")
        fetch.assert_called_once_with("fx-safe", require_corners=True)
        self.assertEqual(result["goals_home"], 2)
        self.assertEqual(result["goals_away"], 1)
        self.assertEqual(result["corners_total"], 10)
        self.assertIn("hkjc_official", result["source"])


if __name__ == "__main__":
    unittest.main()
