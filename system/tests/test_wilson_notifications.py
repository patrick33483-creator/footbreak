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
    created = datetime.now(HKT)
    return {
        "bet_id": "fixture|HDC|T-5|wilson-test-strategy-v1", "portfolio": portfolio,
        "strategy": strategy, "status": "PENDING", "league": "England - Premier League", "home": "主隊", "away": "客隊",
        "kickoff": (datetime.now(HKT) + timedelta(hours=2)).isoformat(), "market_label": market,
        "match_id": "fixture", "code": "HDC", "side": "H", "selected_side": "H",
        "line": -.25, "stage": "T-5", "created_at": created.isoformat(),
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
    def _counterpart(self, row, *, odds=1.93, side=None, line=None, observed_seconds_ago=20):
        created = datetime.fromisoformat(row["created_at"])
        quote = {
            "code": row["code"], "side": side or row["side"], "line": row["line"] if line is None else line,
            "odds": odds, "source": "titan007-crown-id-3", "odds_status": "available",
            "observed_at": (created - timedelta(seconds=observed_seconds_ago)).isoformat(),
        }
        return [{
            "hkjc_match_id": row["match_id"], "kickoff_hkt": row["kickoff"],
            "current_selected_odds_journal": [quote],
        }]

    def _message_with_counterpart(self, row, **kwargs):
        with tempfile.TemporaryDirectory() as directory:
            evidence = Path(directory, "footbreak-execution-evidence.json")
            evidence.write_text(json.dumps(self._counterpart(row, **kwargs)), encoding="utf-8")
            with patch.dict("os.environ", {"FOOTBREAK_CROWN_EXECUTION_EVIDENCE_PATH": str(evidence)}):
                return notify._condition_bet_message(row)

    def test_concise_chinese_bet_message_uses_raw_minimum_and_stable_number(self):
        message = self._message_with_counterpart(bet(), odds=1.89)
        self.assertIsNotNone(message)
        self.assertLess(len(message), 500)
        for text in ("【足破 Wilson】", "英格蘭超級聯賽", "主隊 vs 客隊", "合符 足破 Wilson 條件 #7",
                     "馬會訊號：讓球 · 主讓 -0.25 @1.90",
                     "皇冠對照：讓球 · 主讓 -0.25 @1.89", "最低賠率要求：",
                     "決定：投注", "投注平台：馬會"):
            self.assertIn(text, message)
        self.assertNotIn("England - Premier League", message)
        self.assertNotIn("fixture|", message)
        self.assertNotIn("Wilson 95%", message)
        self.assertIsNone(notify._condition_bet_message(bet(
            strategy="independent-validation-v1", portfolio="footbreak_independent_validation")))

    def test_low_odds_match_alert_is_explicit_and_never_a_formal_bet(self):
        row = low_odds_observation()
        message = self._message_with_counterpart(row, odds=1.58)
        self.assertIsNotNone(message)
        for text in ("合符 足破 Wilson 條件 #7", "馬會訊號：讓球 · 主讓 -0.25 @1.50",
                     "皇冠對照：讓球 · 主讓 -0.25 @1.58", "最低賠率要求：",
                     "決定：不投注：賠率不足", "投注平台：皇冠"):
            self.assertIn(text, message)
        self.assertFalse(row["formal_bet"])

    def test_crown_comparison_is_explicit_when_exact_fresh_quote_is_unavailable(self):
        row = low_odds_observation(market="入球大細", number=8)
        row.update({"code": "HIL", "market": "HIL", "side": "H", "selected_side": "H",
                    "line": 2.5, "selected_line": 2.5, "selected_role": "大"})
        message = self._message_with_counterpart(row, side="L")
        self.assertIn("皇冠對照：未能確認（盤口不一致）", message)
        self.assertIn("決定：不投注：賠率不足", message)
        self.assertIn("投注平台：馬會", message)

    def test_crown_stale_t5_comparison_is_never_presented_as_current(self):
        row = low_odds_observation(market="入球大細")
        row.update({"code": "HIL", "market": "HIL", "side": "H", "selected_side": "H",
                    "line": 2.5, "selected_line": 2.5, "selected_role": "大"})
        message = self._message_with_counterpart(row, observed_seconds_ago=121)
        self.assertIn("皇冠對照：未能確認（非新鮮T-5）", message)

    def test_missing_native_crown_t5_is_not_relabelled_as_no_market(self):
        row = low_odds_observation(market="入球大細")
        row.update({"code": "HIL", "market": "HIL", "side": "H", "selected_side": "H",
                    "line": 2.5, "selected_line": 2.5, "selected_role": "大"})
        with tempfile.TemporaryDirectory() as directory:
            evidence = Path(directory, "empty-native-t5-sidecar.json")
            evidence.write_text("[]", encoding="utf-8")
            with patch.dict("os.environ", {"FOOTBREAK_CROWN_EXECUTION_EVIDENCE_PATH": str(evidence)}):
                message = notify._condition_bet_message(row)
        self.assertIn("皇冠對照：未能確認（系統未取得原生T-5）", message)
        self.assertNotIn("皇冠對照：未能確認（無同場盤）", message)

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
            self.assertTrue(any("合符 足破 Wilson 條件 #7" in text and "讓球" in text for text in messages))
            self.assertTrue(any("合符 足破 Wilson 條件 #8" in text and "入球大細" in text and "不投注：賠率不足" in text for text in messages))

    def test_shared_budget_prioritizes_wilson_over_cross_book_outbox(self):
        formal = bet()
        cross = {
            "bet_id": "cross|HIL|T-5", "portfolio": "footbreak_crown_execution_test",
            "strategy": "footbreak-crown-execution-test-v1", "status": "PENDING",
            "simulation_only": True, "real_betting_enabled": False, "league": "英超",
            "home": "主", "away": "客",
            "kickoff": (datetime.now(HKT) + timedelta(hours=2)).isoformat(),
            "market_label": "入球大細", "selected_role": "大", "selected_line": 2.5,
            "stake": 500, "condition_number": 9, "hkjc_signal_odds": 1.66,
            "hkjc_signal_source": "hkjc_public_board",
            "hkjc_signal_observed_at": datetime.now(HKT).isoformat(),
            "crown_execution_odds": 1.9, "crown_execution_source": "titan007-crown-id-3",
            "crown_execution_observed_at": datetime.now(HKT).isoformat(),
            "decision_at": datetime.now(HKT).isoformat(),
            "wilson_admission": admission_arithmetic(41, 59, 1.9),
        }
        with tempfile.TemporaryDirectory() as directory:
            state = str(Path(directory, "notify.json"))
            with patch.object(notify, "STATE", state), patch.object(notify, "send") as sender:
                ledger = {"bets": [formal], "footbreak_crown_execution_test": {"bets": [cross]}}
                self.assertEqual(
                    notify.notify_pending_committed_bets(ledger, max_attempts=1, max_seconds=5), 1,
                )
                self.assertEqual(sender.call_count, 1)
                self.assertIn("【足破 Wilson】", sender.call_args.args[0])
                self.assertEqual(
                    notify.notify_pending_committed_bets(ledger, max_attempts=1, max_seconds=5), 1,
                )
                self.assertEqual(sender.call_count, 2)
                self.assertIn("足破×皇冠執行測試倉", sender.call_args.args[0])

    def test_shared_wall_clock_budget_does_not_restart_for_cross_book_outbox(self):
        formal = bet()
        cross = {
            "bet_id": "clock|HIL|T-5", "portfolio": "footbreak_crown_execution_test",
            "strategy": "footbreak-crown-execution-test-v1", "status": "PENDING",
            "simulation_only": True, "real_betting_enabled": False, "league": "英超",
            "home": "主", "away": "客",
            "kickoff": (datetime.now(HKT) + timedelta(hours=2)).isoformat(),
            "market_label": "入球大細", "selected_role": "大", "selected_line": 2.5,
            "stake": 500, "condition_number": 9, "hkjc_signal_odds": 1.66,
            "hkjc_signal_source": "hkjc_public_board",
            "hkjc_signal_observed_at": datetime.now(HKT).isoformat(),
            "crown_execution_odds": 1.9, "crown_execution_source": "titan007-crown-id-3",
            "crown_execution_observed_at": datetime.now(HKT).isoformat(),
            "decision_at": datetime.now(HKT).isoformat(),
            "wilson_admission": admission_arithmetic(41, 59, 1.9),
        }
        # The first outbox consumes the one 0.5-second envelope.  The
        # Footbreak×Crown row stays durable; it must not get a new deadline.
        clock = iter([0.0, 0.0, 0.0, 1.0])
        with tempfile.TemporaryDirectory() as directory:
            state = str(Path(directory, "notify.json"))
            with patch.object(notify, "STATE", state), \
                 patch.object(notify, "send") as sender, \
                 patch("notify.time.monotonic", side_effect=lambda: next(clock)):
                delivered = notify.notify_pending_committed_bets(
                    {"bets": [formal], "footbreak_crown_execution_test": {"bets": [cross]}},
                    max_attempts=2, max_seconds=.5,
                )
        self.assertEqual(delivered, 1)
        self.assertEqual(sender.call_count, 1)

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
            self.assertIn("不投注：賠率不足", sender.call_args.args[0])
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
