from __future__ import annotations

import unittest
from contextlib import nullcontext
from datetime import timedelta
from types import SimpleNamespace
from unittest.mock import patch
from pathlib import Path
import tempfile

from analysis.wilson_validation import admission_arithmetic
from crown.common import iso_hkt, now_hkt
from crown import notify


def bet(strategy="wilson-test-strategy-v1", portfolio="crown_wilson_test"):
    return {
        "bet_id": "crown-fixture|HIL|T-5|wilson-test-strategy-v1", "portfolio": portfolio,
        "strategy": strategy, "status": "PENDING", "home": "主隊", "away": "客隊",
        "kickoff": (now_hkt() + timedelta(hours=2)).isoformat(), "market_label": "入球大細",
        "selected_role": "大", "selected_line": 2.5, "odds": 1.90, "stake": 500,
        "frozen_condition_definition": {"path": "首預→T-30→T-5 all 主讓"},
        "frozen_historical_evidence": {"hits": 41, "decided": 59, "label": "凍結條件"},
        "wilson_admission": admission_arithmetic(41, 59, 1.90),
    }


class CrownWilsonNotificationTest(unittest.TestCase):
    def test_message_and_outbox_dedupe(self):
        row = bet()
        message = notify._wilson_message(row)
        self.assertIsNotNone(message)
        for text in ("Wilson 測試攻略｜模擬注", "系統：Crown", "HK$500", "命中 41/59",
                     "Wilson下限", "最低可接受賠率", "沒有任何保證"):
            self.assertIn(text, message)
        state = {"wilson_bets": []}
        with tempfile.TemporaryDirectory() as directory:
            config = SimpleNamespace(state_dir=Path(directory))
            with patch.object(notify, "notification_lock", return_value=nullcontext(True)), \
                 patch.object(notify, "_load", return_value=state), \
                 patch.object(notify, "_send", return_value=True), \
                 patch.object(notify, "write_json_atomic"):
                self.assertEqual(notify.notify_wilson_pending({"bets": [row]}, config), 1)
                self.assertEqual(notify.notify_wilson_pending({"bets": [row]}, config), 0)
        self.assertIsNone(notify._wilson_message(bet(
            strategy="independent-validation-v1", portfolio="crown_independent_validation")))


if __name__ == "__main__":
    unittest.main()
