from __future__ import annotations

import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

SYSTEM_DIR = Path(__file__).resolve().parents[1]
if str(SYSTEM_DIR) not in sys.path:
    sys.path.insert(0, str(SYSTEM_DIR))

import settle
import titan_results
from crown.common import HKT
from crown.titan import TitanClient
from datetime import datetime


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

    def test_titan_strict_reversed_identity_fills_only_missing_corners(self) -> None:
        official = {
            "goals_home": 1, "goals_away": 3, "goals_total": 4,
            "corners_home": None, "corners_away": None,
            "corners_total": None, "source": "hkjc_official",
        }
        record = {
            "match_id": "50072659", "league": "北美聯賽盃",
            "home": "波特蘭伐木者", "away": "CF 阿美利加",
            "kickoff": "2026-08-10 10:15",
        }
        rows = [{
            "id": "2961746", "league": "中北美杯",
            "home": "墨西哥美洲(中)", "away": "波特兰伐木者",
            "kickoff": datetime(2026, 8, 10, 10, 25, tzinfo=HKT),
            "home_score": 3, "away_score": 1,
        }]
        client = Mock(spec=TitanClient)
        client.result_detail.return_value = {
            "titan_id": "2961746", "corners_home": 5,
            "corners_away": 5, "corners_total": 10,
        }
        result = titan_results.merge_titan_corners(
            official, record, client=client, rows=rows
        )
        self.assertEqual(result["goals_home"], 1)
        self.assertEqual(result["goals_away"], 3)
        self.assertEqual(result["corners_total"], 10)
        self.assertEqual(result["titan_id"], "2961746")
        self.assertIn("strict_identity_score", result["source"])

    def test_titan_wrong_score_does_not_fill_corners(self) -> None:
        official = {
            "goals_home": 1, "goals_away": 0, "goals_total": 1,
            "corners_home": None, "corners_away": None,
            "corners_total": None, "source": "hkjc_official",
        }
        record = {
            "match_id": "50072681", "league": "北美聯賽盃",
            "home": "聖地亞哥FC", "away": "迪祖亞拿",
            "kickoff": "2026-08-10 10:00",
        }
        rows = [{
            "id": "wrong", "league": "中北美杯",
            "home": "圣地亚哥", "away": "蒂华纳",
            "kickoff": datetime(2026, 8, 10, 10, 0, tzinfo=HKT),
            "home_score": 0, "away_score": 1,
        }]
        client = Mock(spec=TitanClient)
        result = titan_results.merge_titan_corners(
            official, record, client=client, rows=rows
        )
        self.assertIsNone(result["corners_total"])
        client.result_detail.assert_not_called()

    def test_public_refresh_uses_static_data_instead_of_missing_api_route(self) -> None:
        app = Path(SYSTEM_DIR.parent, "hkjc-dashboard", "app.js").read_text(
            encoding="utf-8"
        )
        self.assertNotIn("api/settle", app)
        self.assertIn("fetch('data.json?v='", app)
        self.assertIn("setInterval(() => refresh(true), 60000)", app)


if __name__ == "__main__":
    unittest.main()
