"""Regression tests for the authenticated Footbreak dashboard update API."""
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

import dashboard_api


class DashboardApiTests(unittest.TestCase):
    def test_dashboard_manual_refresh_triggers_settlement_and_busts_assets(self) -> None:
        root = Path(__file__).resolve().parents[2] / "hkjc-dashboard"
        app = (root / "app.js").read_text(encoding="utf-8")
        index = (root / "index.html").read_text(encoding="utf-8")

        self.assertIn("X-Footbreak-Action", app)
        self.assertIn("settle-simulation", app)
        self.assertIn("賽果核對完成，已更新到最新資料", app)
        self.assertIn("styles.css?v=20260813-data-health-shadow-condition-consensus-ranking-odds-tier-v2", index)
        self.assertIn("app.js?v=20260813-data-health-shadow-condition-consensus-ranking-odds-tier-v2", index)

    def test_perform_settlement_returns_new_prediction_history(self) -> None:
        payload = {
            "generated_at": "2026-08-10T20:00:00+08:00",
            "prediction_history": {"stats": {"graded": 4, "pending": 2}},
        }
        with tempfile.TemporaryDirectory() as directory:
            data_path = Path(directory) / "data.json"
            data_path.write_text(json.dumps(payload), encoding="utf-8")
            completed = type("Completed", (), {"returncode": 0})()
            with patch.object(dashboard_api, "DATA_PATH", str(data_path)), \
                 patch.object(dashboard_api.subprocess, "run", return_value=completed):
                result = dashboard_api.perform_settlement()
        self.assertTrue(result["ok"])
        self.assertEqual(result["prediction_history_stats"]["graded"], 4)
        self.assertEqual(result["data"]["prediction_history"]["stats"]["pending"], 2)


if __name__ == "__main__":
    unittest.main()
