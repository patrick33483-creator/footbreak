from __future__ import annotations

import json
import os
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


def bet(strategy="wilson-test-strategy-v1", portfolio="crown_wilson_test", *, market="入球大細", number=3):
    created = now_hkt()
    return {
        "bet_id": "crown-fixture|HIL|T-5|wilson-test-strategy-v1", "portfolio": portfolio,
        "strategy": strategy, "status": "PENDING", "league": "USA - Major League Soccer", "home": "主隊", "away": "客隊",
        "kickoff": (now_hkt() + timedelta(hours=2)).isoformat(), "market_label": market,
        "match_id": "crown-fixture", "hkjc_match_id": "hkjc-fixture", "code": "HIL",
        "side": "H", "selected_side": "H", "line": 2.5, "stage": "T-5",
        "created_at": created.isoformat(), "admission_at": created.isoformat(),
        "selected_role": "大", "selected_line": 2.5, "odds": 1.90, "stake": 500,
        "frozen_condition_definition": {"path": "首預→T-30→T-5 all 主讓"},
        "frozen_historical_evidence": {"hits": 41, "decided": 59, "label": "凍結條件"},
        "wilson_admission": admission_arithmetic(41, 59, 1.90), "condition_number": number,
    }


def low_odds_observation(*, market="角球大細", number=4):
    row = bet(market=market, number=number)
    row.pop("bet_id")
    row["code"] = {"讓球": "HDC", "入球大細": "HIL", "角球大細": "CHL"}[market]
    row.update({
        "observation_id": f"crown-fixture|{market}|low-odds",
        "portfolio": "crown_wilson_observations", "formal_bet": False,
        "bet_status": "NO_BET_LOW_ODDS", "odds": 1.50,
        "wilson_admission": admission_arithmetic(41, 59, 1.50),
    })
    return row


class CrownWilsonNotificationTest(unittest.TestCase):
    def _message_with_hkjc(self, row, *, odds=1.89, side="H", line=2.5, observed_seconds_ago=20):
        created = now_hkt().fromisoformat(row["created_at"])
        source = {"watch": {row["hkjc_match_id"]: {
            "match_id": row["hkjc_match_id"], "kickoff": row["kickoff"],
            "stages": [{"stage": "T-5", "ts": row["created_at"], "market_predictions": [{
                "code": row["code"], "side": side, "line": line, "odds": odds,
                "source": "hkjc_public_board",
                "observed_at": (created - timedelta(seconds=observed_seconds_ago)).isoformat(),
            }]}],
        }}}
        with tempfile.TemporaryDirectory() as directory:
            evidence = Path(directory, "footbreak.json")
            evidence.write_text(json.dumps(source), encoding="utf-8")
            with patch.dict(os.environ, {"CROWN_HKJC_EXECUTION_EVIDENCE_PATH": str(evidence)}), \
                 patch("crown.hkjc_execution_test._native_t5", return_value=True):
                return (
                    notify._wilson_observation_message(row)
                    if row.get("portfolio") == "crown_wilson_observations"
                    else notify._wilson_message(row)
                )

    def test_concise_bet_message_and_outbox_dedupe(self):
        row = bet()
        message = self._message_with_hkjc(row, odds=1.89)
        self.assertIsNotNone(message)
        self.assertLess(len(message), 500)
        for text in ("【皇冠 Wilson】", "美國職業足球大聯盟", "合符條件 #3",
                     "皇冠訊號：入球大細 · 大 2.5 @1.90",
                     "馬會對照：1.89", "最低賠率要求：",
                     "決定：投注", "投注平台：皇冠"):
            self.assertIn(text, message)
        self.assertNotIn("USA - Major League Soccer", message)
        self.assertNotIn("crown-fixture", message)
        state = {"wilson_bets": [], "wilson_match_alerts": []}
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

    def test_low_odds_retry_and_multiple_markets_remain_distinct(self):
        formal = bet()
        low = low_odds_observation()
        message = self._message_with_hkjc(low, odds=1.58)
        self.assertIsNotNone(message)
        self.assertIn("合符條件 #4", message)
        self.assertIn("皇冠訊號：角球大細 · 大 2.5 @1.50", message)
        self.assertIn("馬會對照：1.58", message)
        self.assertIn("決定：不投注：賠率不足", message)
        self.assertIn("投注平台：馬會", message)
        state = {"wilson_bets": [], "wilson_match_alerts": []}
        with tempfile.TemporaryDirectory() as directory:
            config = SimpleNamespace(state_dir=Path(directory))
            with patch.object(notify, "notification_lock", return_value=nullcontext(True)), \
                 patch.object(notify, "_load", return_value=state), \
                 patch.object(notify, "_send", side_effect=[False, True, True]) as sender, \
                 patch.object(notify, "write_json_atomic"):
                ledger = {"bets": [formal], "wilson_validation": {"observations": [low]}}
                self.assertEqual(notify.notify_wilson_pending(ledger, config, max_attempts=1), 0)
                self.assertEqual(notify.notify_wilson_pending(ledger, config), 2)
                self.assertEqual(notify.notify_wilson_pending(ledger, config), 0)
        self.assertEqual(sender.call_count, 3)

    def test_higher_exact_hkjc_quote_selects_hkjc_without_changing_crown_minimum(self):
        row = bet()
        message = self._message_with_hkjc(row, odds=1.95)
        self.assertIn("皇冠訊號：入球大細 · 大 2.5 @1.90", message)
        self.assertIn("馬會對照：1.95", message)
        self.assertIn("決定：投注", message)
        self.assertIn("投注平台：馬會", message)
        self.assertIn(f"最低賠率要求：{row['wilson_admission']['minimum_acceptable_odds_raw']:.2f}", message)

    def test_unavailable_or_stale_hkjc_counterpart_is_explicit_and_fails_closed(self):
        row = low_odds_observation()
        message = self._message_with_hkjc(row, side="L")
        self.assertIn("馬會對照：未能確認（盤口不一致）", message)
        self.assertIn("決定：不投注：賠率不足", message)
        message = self._message_with_hkjc(row, observed_seconds_ago=121)
        self.assertIn("馬會對照：未能確認（非新鮮T-5）", message)

    def test_shared_budget_prioritizes_wilson_over_reciprocal_outbox(self):
        formal = bet()
        reciprocal = {
            "bet_id": "reciprocal|HIL|T-5", "portfolio": "crown_hkjc_execution_test",
            "strategy": "crown-hkjc-execution-test-v1", "status": "PENDING",
            "simulation_only": True, "real_betting_enabled": False, "league": "英超",
            "home": "主", "away": "客", "kickoff": (now_hkt() + timedelta(hours=2)).isoformat(),
            "market_label": "入球大細", "selected_role": "大", "selected_line": 2.5,
            "stake": 500, "condition_number": 9, "crown_signal_odds": 1.66,
            "crown_signal_source": "titan007-crown-id-3",
            "crown_signal_observed_at": iso_hkt(),
            "hkjc_execution_odds": 1.9, "hkjc_execution_source": "hkjc_public_board",
            "hkjc_execution_observed_at": iso_hkt(), "decision_at": iso_hkt(),
            "wilson_admission": admission_arithmetic(41, 59, 1.9),
        }
        state = {"wilson_bets": [], "wilson_match_alerts": [], "hkjc_execution_test_alerts": []}
        with tempfile.TemporaryDirectory() as directory:
            config = SimpleNamespace(state_dir=Path(directory))
            with patch.object(notify, "notification_lock", return_value=nullcontext(True)), \
                 patch.object(notify, "_load", return_value=state), \
                 patch.object(notify, "_send", return_value=True) as sender, \
                 patch.object(notify, "write_json_atomic"):
                ledger = {"bets": [formal], "crown_hkjc_execution_test": {"bets": [reciprocal]}}
                self.assertEqual(notify.notify_new(ledger, config, max_attempts=1, max_seconds=5), 1)
                self.assertEqual(sender.call_count, 1)
                self.assertIn("【皇冠 Wilson】", sender.call_args.args[1])
                # The reciprocal alert stays unacknowledged and is retried on
                # the next pass; it did not receive a second 5-second budget.
                self.assertEqual(notify.notify_new(ledger, config, max_attempts=1, max_seconds=5), 1)
                self.assertEqual(sender.call_count, 2)
                self.assertIn("皇冠×馬會執行測試倉", sender.call_args.args[1])

    def test_shared_wall_clock_budget_does_not_restart_for_reciprocal_outbox(self):
        formal = bet()
        reciprocal = {
            "bet_id": "clock|HIL|T-5", "portfolio": "crown_hkjc_execution_test",
            "strategy": "crown-hkjc-execution-test-v1", "status": "PENDING",
            "simulation_only": True, "real_betting_enabled": False, "league": "英超",
            "home": "主", "away": "客", "kickoff": (now_hkt() + timedelta(hours=2)).isoformat(),
            "market_label": "入球大細", "selected_role": "大", "selected_line": 2.5,
            "stake": 500, "condition_number": 9, "crown_signal_odds": 1.66,
            "crown_signal_source": "titan007-crown-id-3",
            "crown_signal_observed_at": iso_hkt(),
            "hkjc_execution_odds": 1.9, "hkjc_execution_source": "hkjc_public_board",
            "hkjc_execution_observed_at": iso_hkt(), "decision_at": iso_hkt(),
            "wilson_admission": admission_arithmetic(41, 59, 1.9),
        }
        # First outbox consumes the whole 0.5s envelope.  The reciprocal
        # outbox must not receive a fresh 0.5s deadline.
        clock = iter([0.0, 0.0, 0.0, 1.0, 1.0])
        state = {"wilson_bets": [], "wilson_match_alerts": [], "hkjc_execution_test_alerts": []}
        with tempfile.TemporaryDirectory() as directory:
            config = SimpleNamespace(state_dir=Path(directory))
            with patch.object(notify, "notification_lock", return_value=nullcontext(True)), \
                 patch.object(notify, "_load", return_value=state), \
                 patch.object(notify, "_send", return_value=True) as sender, \
                 patch.object(notify, "write_json_atomic"), \
                 patch("crown.notify.time.monotonic", side_effect=lambda: next(clock)):
                delivered = notify.notify_new(
                    {"bets": [formal], "crown_hkjc_execution_test": {"bets": [reciprocal]}},
                    config, max_attempts=2, max_seconds=.5,
                )
        self.assertEqual(delivered, 1)
        self.assertEqual(sender.call_count, 1)

    def test_pre_upgrade_formal_ack_migrates_without_replay_and_low_odds_sends_once(self):
        formal = bet()
        low = low_odds_observation()
        with tempfile.TemporaryDirectory() as directory:
            config = SimpleNamespace(state_dir=Path(directory))
            notify_path = config.state_dir / "notify_state.json"
            # An old on-disk state has formal acknowledgements only.
            notify_path.write_text(json.dumps({"wilson_bets": [formal["bet_id"], formal["bet_id"]]}))
            with patch.object(notify, "_send", return_value=True) as sender:
                ledger = {"bets": [formal], "wilson_validation": {"observations": [low]}}
                self.assertEqual(notify.notify_wilson_pending(ledger, config), 1)
                self.assertEqual(notify.notify_wilson_pending(ledger, config), 0)
            self.assertEqual(sender.call_count, 1)
            self.assertIn("不投注：賠率不足", sender.call_args.args[1])
            state = json.loads(notify_path.read_text())
            self.assertEqual(state["wilson_bets"], [formal["bet_id"]])
            self.assertEqual(
                state["wilson_match_alerts"],
                [formal["bet_id"], low["observation_id"]],
            )
            self.assertNotIn(low["observation_id"], state["wilson_bets"])

    def test_transport_failure_and_timeout_never_acknowledge_formal_or_observation(self):
        formal = bet()
        low = low_odds_observation()
        cases = (
            ("formal transport failure", {"bets": [formal]}, False),
            ("observation timeout", {"wilson_validation": {"observations": [low]}}, TimeoutError("timeout")),
        )
        for label, ledger, outcome in cases:
            with self.subTest(label=label), tempfile.TemporaryDirectory() as directory:
                config = SimpleNamespace(state_dir=Path(directory))
                notify_path = config.state_dir / "notify_state.json"
                notify_path.write_text(json.dumps({
                    "wilson_bets": [], "wilson_match_alerts": [],
                }))
                sender_kwargs = (
                    {"side_effect": outcome}
                    if isinstance(outcome, BaseException)
                    else {"return_value": outcome}
                )
                with patch.object(notify, "_send", **sender_kwargs):
                    if isinstance(outcome, BaseException):
                        with self.assertRaisesRegex(TimeoutError, "timeout"):
                            notify.notify_wilson_pending(ledger, config)
                    else:
                        self.assertEqual(notify.notify_wilson_pending(ledger, config), 0)
                state = json.loads(notify_path.read_text())
                self.assertEqual(state["wilson_bets"], [])
                self.assertEqual(state["wilson_match_alerts"], [])


if __name__ == "__main__":
    unittest.main()
