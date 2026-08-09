import unittest
import sys
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import patch

SYSTEM = Path(__file__).resolve().parents[1]
if str(SYSTEM) not in sys.path:
    sys.path.insert(0, str(SYSTEM))

import run_predict


class FailClosedPredictionTest(unittest.TestCase):
    def test_failed_t5_prediction_is_a_terminal_no_bet_decision(self):
        match = {
            "id": "5001",
            "homeTeam": {"name_ch": "主隊", "name_en": "Home"},
            "awayTeam": {"name_ch": "客隊", "name_en": "Away"},
            "tournament": {"nameCH": "測試聯賽"},
        }
        kickoff = datetime(2026, 8, 9, 14, 0, tzinfo=timezone.utc)
        with patch.object(run_predict.H, "parse_kickoff", return_value=kickoff), \
             patch.object(run_predict, "hk_odds_fingerprint", return_value={"HDC": []}):
            result = run_predict.failed_prediction(
                match, "T-5", 4.5, "PinnAPI 賽事配對失敗，T-5 資料不足，最終決定不下注"
            )
        self.assertTrue(result["can_bet"])
        self.assertIsNone(result["pick"])
        self.assertEqual(result["conviction"], 0.0)
        self.assertIn("最終決定不下注", result["no_bet_reason"])
        self.assertEqual(result["stage"], "T-5")


if __name__ == "__main__":
    unittest.main()
