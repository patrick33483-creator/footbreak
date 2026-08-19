from __future__ import annotations

import sys
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch

from analysis.wilson_validation import admission_arithmetic

SYSTEM = Path(__file__).resolve().parents[1]
if str(SYSTEM) not in sys.path:
    sys.path.insert(0, str(SYSTEM))
import notify

HKT = timezone(timedelta(hours=8))


def bet(strategy="wilson-test-strategy-v1", portfolio="footbreak_wilson_test"):
    arithmetic = admission_arithmetic(41, 59, 1.90)
    return {
        "bet_id": "fixture|HDC|T-5|wilson-test-strategy-v1", "portfolio": portfolio,
        "strategy": strategy, "status": "PENDING", "league": "測試聯賽", "home": "主隊", "away": "客隊",
        "kickoff": (datetime.now(HKT) + timedelta(hours=2)).isoformat(), "market_label": "讓球",
        "selected_role": "主讓", "selected_line": -.25, "odds": 1.90, "stake": 500,
        "frozen_condition_definition": {"path": "首預→T-30→T-5 all 主讓"},
        "frozen_historical_evidence": {"hits": 41, "decided": 59, "label": "凍結條件"},
        "wilson_admission": arithmetic,
    }


class FootbreakWilsonNotificationTest(unittest.TestCase):
    def test_exact_chinese_simulation_message_and_old_strategy_is_silent(self):
        message = notify._condition_bet_message(bet())
        self.assertIsNotNone(message)
        for text in ("Wilson 測試攻略｜模擬注", "系統：Footbreak", "模擬注碼：HK$500",
                     "命中 41/59", "Wilson 95% 下限", "損益平衡命中率",
                     "PASS：Wilson下限", "最低可接受賠率", "沒有任何保證"):
            self.assertIn(text, message)
        self.assertIsNone(notify._condition_bet_message(bet(
            strategy="independent-validation-v1", portfolio="footbreak_independent_validation")))

    def test_durable_dedupe_and_retry_only_for_wilson(self):
        row = bet()
        with tempfile.TemporaryDirectory() as directory:
            state = str(Path(directory, "notify.json"))
            with patch.object(notify, "STATE", state), patch.object(notify, "send", side_effect=RuntimeError("temporary")):
                with self.assertRaisesRegex(RuntimeError, "temporary"):
                    notify.notify_pending_condition_bets({"bets": [row]})
            with patch.object(notify, "STATE", state), patch.object(notify, "send") as send:
                self.assertEqual(notify.notify_pending_condition_bets({"bets": [row]}), 1)
                self.assertEqual(notify.notify_pending_condition_bets({"bets": [row]}), 0)
            send.assert_called_once()

    def test_legacy_granular_entry_point_is_retired(self):
        self.assertEqual(notify.notify_fresh_granular_conditions({"watch": {}}, [{"match_id": "x"}]), 0)


if __name__ == "__main__":
    unittest.main()
