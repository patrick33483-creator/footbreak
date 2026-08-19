from __future__ import annotations

import json
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


def bet(strategy="wilson-test-strategy-v1", portfolio="footbreak_wilson_test", *, market="讓球"):
    arithmetic = admission_arithmetic(41, 59, 1.90)
    return {
        "bet_id": "fixture|HDC|T-5|wilson-test-strategy-v1", "portfolio": portfolio,
        "strategy": strategy, "status": "PENDING", "league": "測試聯賽", "home": "主隊", "away": "客隊",
        "kickoff": (datetime.now(HKT) + timedelta(hours=2)).isoformat(), "market_label": market,
        "selected_role": "主讓", "selected_line": -.25, "odds": 1.90, "stake": 500,
        "frozen_condition_definition": {"path": "首預→T-30→T-5 all 主讓"},
        "frozen_historical_evidence": {"hits": 41, "decided": 59, "label": "凍結條件"},
        "wilson_admission": arithmetic, "condition_number": 7,
    }


def low_odds_observation(*, market="讓球", number=7):
    row = bet(market=market)
    row.pop("bet_id")
    row.update({
        "observation_id": f"fixture|{market}|low-odds",
        "portfolio": "footbreak_wilson_observations",
        "formal_bet": False,
        "bet_status": "NO_BET_LOW_ODDS",
        "condition_number": number,
        "odds": 1.50,
        "wilson_admission": admission_arithmetic(41, 59, 1.50),
    })
    return row


class FootbreakWilsonNotificationTest(unittest.TestCase):
    def test_concise_chinese_bet_message_uses_raw_minimum_and_stable_number(self):
        message = notify._condition_bet_message(bet())
        self.assertIsNotNone(message)
        self.assertEqual(message.count("\n"), 4)
        for text in ("測試聯賽", "主隊 vs 客隊", "合符條件 #7", "投注 讓球 · 主讓 -0.25（模擬）",
                     "現時賠率：1.90", "最低賠率要求："):
            self.assertIn(text, message)
        self.assertNotIn("fixture|", message)
        self.assertNotIn("Wilson 95%", message)
        self.assertIsNone(notify._condition_bet_message(bet(
            strategy="independent-validation-v1", portfolio="footbreak_independent_validation")))

    def test_low_odds_match_alert_is_explicit_and_never_a_formal_bet(self):
        row = low_odds_observation()
        message = notify._condition_observation_message(row)
        self.assertIsNotNone(message)
        for text in ("合符條件 #7", "不投注（賠率不足） 讓球 · 主讓 -0.25", "現時賠率：1.50", "最低賠率要求："):
            self.assertIn(text, message)
        self.assertNotIn("（模擬）", message)
        self.assertFalse(row["formal_bet"])

    def test_durable_dedupe_retry_and_bounded_multiple_market_outbox(self):
        row = bet()
        low = low_odds_observation(market="入球大細", number=8)
        with tempfile.TemporaryDirectory() as directory:
            state = str(Path(directory, "notify.json"))
            with patch.object(notify, "STATE", state), patch.object(notify, "send", side_effect=RuntimeError("temporary")):
                with self.assertRaisesRegex(RuntimeError, "temporary"):
                    notify.notify_pending_condition_bets({"bets": [row], "wilson_validation": {"observations": [low]}})
            with patch.object(notify, "STATE", state), patch.object(notify, "send") as send:
                ledger = {"bets": [row], "wilson_validation": {"observations": [low]}}
                self.assertEqual(notify.notify_pending_condition_bets(ledger, max_attempts=1), 1)
                self.assertEqual(notify.notify_pending_condition_bets(ledger), 1)
                self.assertEqual(notify.notify_pending_condition_bets(ledger), 0)
            self.assertEqual(send.call_count, 2)
            messages = [call.args[0] for call in send.call_args_list]
            self.assertTrue(any("合符條件 #7" in text and "讓球" in text for text in messages))
            self.assertTrue(any("合符條件 #8" in text and "入球大細" in text and "不投注（賠率不足）" in text for text in messages))

    def test_pre_upgrade_formal_ack_migrates_without_replaying_or_promoting_observation(self):
        formal = bet()
        low = low_odds_observation()
        with tempfile.TemporaryDirectory() as directory:
            state_path = Path(directory, "notify.json")
            # This is an old state file: it predates wilson_match_alerts.
            state_path.write_text(json.dumps({
                "condition_simulation_bets": [formal["bet_id"], formal["bet_id"]],
            }), encoding="utf-8")
            with patch.object(notify, "STATE", str(state_path)), \
                 patch.object(notify, "send") as sender:
                ledger = {"bets": [formal], "wilson_validation": {"observations": [low]}}
                self.assertEqual(notify.notify_pending_condition_bets(ledger), 1)
                self.assertEqual(notify.notify_pending_condition_bets(ledger), 0)
            self.assertEqual(sender.call_count, 1)
            self.assertIn("不投注（賠率不足）", sender.call_args.args[0])
            state = json.loads(state_path.read_text(encoding="utf-8"))
            self.assertEqual(state["condition_simulation_bets"], [formal["bet_id"]])
            self.assertEqual(
                state["wilson_match_alerts"],
                [formal["bet_id"], low["observation_id"]],
            )
            self.assertNotIn(low["observation_id"], state["condition_simulation_bets"])

    def test_transport_failures_never_acknowledge_formal_or_observation(self):
        formal = bet()
        low = low_odds_observation()
        for label, ledger in (
            ("formal", {"bets": [formal]}),
            ("observation", {"wilson_validation": {"observations": [low]}}),
        ):
            with self.subTest(label=label), tempfile.TemporaryDirectory() as directory:
                state_path = Path(directory, "notify.json")
                state_path.write_text(json.dumps({
                    "condition_simulation_bets": [],
                    "wilson_match_alerts": [],
                }), encoding="utf-8")
                with patch.object(notify, "STATE", str(state_path)), \
                     patch.object(notify, "send", side_effect=TimeoutError("timeout")):
                    with self.assertRaisesRegex(TimeoutError, "timeout"):
                        notify.notify_pending_condition_bets(ledger)
                state = json.loads(state_path.read_text(encoding="utf-8"))
                self.assertEqual(state["condition_simulation_bets"], [])
                self.assertEqual(state["wilson_match_alerts"], [])

    def test_legacy_granular_entry_point_is_retired(self):
        self.assertEqual(notify.notify_fresh_granular_conditions({"watch": {}}, [{"match_id": "x"}]), 0)


if __name__ == "__main__":
    unittest.main()
