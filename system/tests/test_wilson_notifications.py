from __future__ import annotations

import json
import sys
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch

SYSTEM = Path(__file__).resolve().parents[1]
if str(SYSTEM) not in sys.path:
    sys.path.insert(0, str(SYSTEM))
ROOT = SYSTEM.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
from analysis import bilateral_decision as bilateral
from analysis.wilson_validation import admission_arithmetic
import notify
import record_picks

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
        "frozen_condition_signature": "condition-signature-7",
        "evidence_version": 3,
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


def bilateral_decision(*, kickoff=None, **overrides):
    record = {
        "system": "footbreak", "fixture": "fixture",
        "kickoff": kickoff or (datetime.now(HKT) + timedelta(hours=2)).isoformat(),
        "market": "HIL", "side": "H", "line": 2.5, "condition_number": 7,
        "condition_signature": "condition-signature-7",
        "evidence_version": 3,
        "league": "England - Premier League", "home": "主隊", "away": "客隊",
        "minimum_odds": 1.92, "signal_quote": 1.75,
        "counterpart_quote": None, "counterpart_reason": "系統未取得原生T-5",
        "decision": "COUNTERPART_UNAVAILABLE", "chosen_execution_book": None,
        "created_at": datetime.now(HKT).isoformat(),
    }
    record.update(overrides)
    record["decision_id"] = bilateral.decision_id(
        system=record["system"], fixture=record["fixture"],
        market=record["market"], side=record["side"], line=record["line"],
        condition_signature=record["condition_signature"],
        evidence_version=record["evidence_version"],
    )
    namespace = {}
    committed, _created = bilateral.persist_decision(namespace, record)
    return committed


def bilateral_outbox(decision):
    return {
        "outbox_id": "outbox-" + decision["decision_id"],
        "decision_id": decision["decision_id"],
        "created_at": decision["created_at"],
        "notification_required": True,
        "delivery": "PENDING",
    }


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

    def test_persisted_counterpart_reason_distinguishes_no_fixture_from_exact_line(self):
        row = low_odds_observation(market="入球大細")
        row.update({"code": "HIL", "market": "HIL", "side": "H", "selected_side": "H",
                    "line": 2.5, "selected_line": 2.5, "selected_role": "大"})
        row["crown_counterpart"] = {
            "status": "UNAVAILABLE", "reason": "crown_fixture_not_listed",
            "market": "HIL", "side": "H", "line": 2.5,
        }
        _, text = notify._crown_counterpart(row)
        self.assertIn("皇冠未列出同場賽事", text)
        row["crown_counterpart"]["reason"] = "crown_t30_exact_line_unavailable"
        _, text = notify._crown_counterpart(row)
        self.assertIn("相同亞洲盤線", text)

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

    def test_notify_only_uses_the_unified_retry_dispatcher(self):
        with patch("notify.notify_pending_committed_bets", return_value=3) as dispatcher:
            self.assertEqual(record_picks.notify_only({"bets": []}), 3)
        dispatcher.assert_called_once_with({"bets": []})

    def test_bilateral_notice_supersedes_semantically_duplicate_native_observation(self):
        low = low_odds_observation(market="入球大細")
        low.update({
            "code": "HIL", "market": "HIL", "side": "H", "selected_side": "H",
            "line": 2.5, "selected_line": 2.5, "selected_role": "大",
        })
        decision = bilateral_decision(
            kickoff=low["kickoff"], signal_quote=low["odds"],
            minimum_odds=low["wilson_admission"]["minimum_acceptable_odds_raw"],
        )
        ledger = {
            "wilson_validation": {"observations": [low]},
            "footbreak_crown_execution_test": {
                "decisions": [decision],
                "decision_outbox": [bilateral_outbox(decision)],
            },
        }
        with tempfile.TemporaryDirectory() as directory:
            state = str(Path(directory, "notify.json"))
            with patch.object(notify, "STATE", state), patch.object(notify, "send") as sender:
                self.assertEqual(
                    notify.notify_pending_committed_bets(ledger, max_attempts=1, max_seconds=5), 1,
                )
                self.assertIn("對照收集失敗；保留原生訊號決定", sender.call_args.args[0])
                self.assertIn("皇冠對照：未能確認（系統未取得原生T-5）", sender.call_args.args[0])
                self.assertNotIn("counterpart_", sender.call_args.args[0])
                self.assertNotIn("crown_", sender.call_args.args[0])
                self.assertEqual(
                    notify.notify_pending_committed_bets(ledger, max_attempts=1, max_seconds=5), 0,
                )
            persisted = json.loads(Path(state).read_text(encoding="utf-8"))
        self.assertEqual(persisted.get("wilson_match_alerts", []), [])
        self.assertEqual(persisted["bilateral_decision_alerts"], [decision["decision_id"]])

    def test_opposite_side_or_line_never_suppresses_native_observation(self):
        low = low_odds_observation(market="入球大細")
        low.update({
            "code": "HIL", "market": "HIL", "side": "H", "selected_side": "H",
            "line": 2.5, "selected_line": 2.5, "selected_role": "大",
        })
        decision = bilateral_decision()
        decision["side"] = "L"
        decision["line"] = 2.75
        # Keep the original id/provenance deliberately: this simulates a
        # corrupted or semantically different row occupying the outbox.
        ledger = {
            "wilson_validation": {"observations": [low]},
            "footbreak_crown_execution_test": {
                "decisions": [decision],
                "decision_outbox": [bilateral_outbox(decision)],
            },
        }
        with tempfile.TemporaryDirectory() as directory:
            with patch.object(notify, "STATE", str(Path(directory, "notify.json"))), \
                 patch.object(notify, "send") as sender:
                self.assertEqual(notify.notify_pending_condition_bets(ledger), 1)
        self.assertIn("不投注：賠率不足", sender.call_args.args[0])

    def test_tampered_or_unrenderable_bilateral_row_fails_open_to_native(self):
        low = low_odds_observation(market="入球大細")
        low.update({
            "code": "HIL", "market": "HIL", "side": "H", "selected_side": "H",
            "line": 2.5, "selected_line": 2.5, "selected_role": "大",
        })
        for label, mutation in (
            ("bad provenance", lambda row: row.__setitem__("provenance_hash", "tampered")),
            ("bad formatter", lambda row: row.__setitem__("minimum_odds", "not-a-number")),
        ):
            with self.subTest(label=label):
                decision = bilateral_decision()
                mutation(decision)
                ledger = {
                    "wilson_validation": {"observations": [low]},
                    "footbreak_crown_execution_test": {
                        "decisions": [decision],
                        "decision_outbox": [bilateral_outbox(decision)],
                    },
                }
                with tempfile.TemporaryDirectory() as directory:
                    with patch.object(notify, "STATE", str(Path(directory, "notify.json"))), \
                         patch.object(notify, "send") as sender:
                        self.assertEqual(notify.notify_pending_condition_bets(ledger), 1)
                self.assertIn("不投注：賠率不足", sender.call_args.args[0])

    def test_machine_reason_is_redacted_even_when_mixed_with_chinese(self):
        for reason in ("資料 crown_internal_error", "資料crown_internal_error", "資料：crown_internal_error"):
            with self.subTest(reason=reason):
                decision = bilateral_decision(counterpart_reason=reason)
                message = notify._bilateral_decision_message(decision)
                self.assertIn("皇冠對照：未能確認（未能確認）", message)
                self.assertNotIn("crown_internal_error", message)

    def test_malformed_bilateral_row_does_not_block_valid_group_row(self):
        valid = bilateral_decision()
        malformed = dict(valid, decision_id="broken", minimum_odds="bad")
        ledger = {"footbreak_crown_execution_test": {
            "decisions": [valid, malformed],
            "decision_outbox": [bilateral_outbox(valid), bilateral_outbox(malformed)],
        }}
        with tempfile.TemporaryDirectory() as directory:
            with patch.object(notify, "STATE", str(Path(directory, "notify.json"))), \
                 patch.object(notify, "send") as sender:
                self.assertEqual(notify.notify_pending_bilateral_decisions(ledger), 1)
        self.assertIn("足破 Wilson 條件 #7", sender.call_args.args[0])

    def test_malformed_outbox_and_tampered_decision_never_suppress_or_dispatch(self):
        low = low_odds_observation(market="入球大細")
        low.update({
            "code": "HIL", "market": "HIL", "side": "H", "selected_side": "H",
            "line": 2.5, "selected_line": 2.5, "selected_role": "大",
        })
        decision = bilateral_decision(
            kickoff=low["kickoff"], signal_quote=low["odds"],
            minimum_odds=low["wilson_admission"]["minimum_acceptable_odds_raw"],
        )
        malformed_outbox = dict(bilateral_outbox(decision), delivery={"bad": "shape"})
        ledger = {
            "wilson_validation": {"observations": [low]},
            "footbreak_crown_execution_test": {
                "decisions": [decision], "decision_outbox": [malformed_outbox],
            },
        }
        with tempfile.TemporaryDirectory() as directory:
            with patch.object(notify, "STATE", str(Path(directory, "notify.json"))), \
                 patch.object(notify, "send") as sender:
                self.assertEqual(notify.notify_pending_condition_bets(ledger), 1)
                self.assertEqual(notify.notify_pending_bilateral_decisions(ledger), 0)
        self.assertEqual(sender.call_count, 1)

        tampered = dict(decision, provenance_hash="tampered")
        ledger = {"footbreak_crown_execution_test": {
            "decisions": [tampered],
            "decision_outbox": [bilateral_outbox(tampered)],
        }}
        with tempfile.TemporaryDirectory() as directory:
            with patch.object(notify, "STATE", str(Path(directory, "notify.json"))), \
                 patch.object(notify, "send") as sender:
                self.assertEqual(notify.notify_pending_bilateral_decisions(ledger), 0)
        sender.assert_not_called()

    def test_different_public_fixture_identity_never_suppresses_native(self):
        low = low_odds_observation(market="入球大細")
        low.update({
            "code": "HIL", "market": "HIL", "side": "H", "selected_side": "H",
            "line": 2.5, "selected_line": 2.5, "selected_role": "大",
        })
        decision = bilateral_decision(
            kickoff=low["kickoff"], signal_quote=low["odds"],
            minimum_odds=low["wilson_admission"]["minimum_acceptable_odds_raw"],
            league="Spain - La Liga", home="另一主隊", away="另一客隊",
        )
        ledger = {
            "wilson_validation": {"observations": [low]},
            "footbreak_crown_execution_test": {
                "decisions": [decision],
                "decision_outbox": [bilateral_outbox(decision)],
            },
        }
        with tempfile.TemporaryDirectory() as directory:
            with patch.object(notify, "STATE", str(Path(directory, "notify.json"))), \
                 patch.object(notify, "send") as sender:
                self.assertEqual(notify.notify_pending_condition_bets(ledger), 1)
        self.assertIn("英格蘭超級聯賽", sender.call_args.args[0])

    def test_native_observation_remains_available_if_bilateral_outbox_is_missing(self):
        low = low_odds_observation()
        decision = bilateral_decision()
        ledger = {
            "wilson_validation": {"observations": [low]},
            "footbreak_crown_execution_test": {
                "decisions": [decision],
                "decision_outbox": [],
            },
        }
        with tempfile.TemporaryDirectory() as directory:
            with patch.object(notify, "STATE", str(Path(directory, "notify.json"))), \
                 patch.object(notify, "send") as sender:
                self.assertEqual(notify.notify_pending_committed_bets(ledger), 1)
        self.assertIn("不投注：賠率不足", sender.call_args.args[0])

    def test_bilateral_retry_never_sends_after_kickoff(self):
        decision = bilateral_decision(kickoff=(datetime.now(HKT) - timedelta(seconds=1)).isoformat())
        ledger = {
            "footbreak_crown_execution_test": {
                "decisions": [decision],
                "decision_outbox": [{
                    "decision_id": decision["decision_id"], "notification_required": True,
                }],
            },
        }
        with tempfile.TemporaryDirectory() as directory:
            state = str(Path(directory, "notify.json"))
            with patch.object(notify, "STATE", state), patch.object(notify, "send") as sender:
                self.assertEqual(notify.notify_pending_bilateral_decisions(ledger), 0)
        sender.assert_not_called()

    def test_bilateral_fixture_group_has_complete_identity_and_acknowledges_all_conditions(self):
        first = bilateral_decision()
        second = bilateral_decision(
            kickoff=first["kickoff"], market="CHL", side="L", line=10.5,
            condition_number=8, signal_quote=1.81, minimum_odds=1.95,
            decision="NO_BET_LOW_ODDS",
        )
        ledger = {
            "footbreak_crown_execution_test": {
                "decisions": [first, second],
                "decision_outbox": [
                    bilateral_outbox(first),
                    bilateral_outbox(second),
                ],
            },
        }
        with tempfile.TemporaryDirectory() as directory:
            state = Path(directory, "notify.json")
            with patch.object(notify, "STATE", str(state)), patch.object(notify, "send") as sender:
                self.assertEqual(notify.notify_pending_bilateral_decisions(ledger), 1)
            saved = json.loads(state.read_text(encoding="utf-8"))
        self.assertEqual(sender.call_count, 1)
        message = sender.call_args.args[0]
        for expected in ("英格蘭超級聯賽", "主隊 vs 客隊", "HKT",
                         "足破 Wilson 條件 #7", "足破 Wilson 條件 #8",
                         "入球大細 · 大 2.5", "角球大細 · 細 10.5",
                         "最低 1.92", "最低 1.95", "決定："):
            self.assertIn(expected, message)
        self.assertNotIn("bilateral T-5", message)
        self.assertEqual(saved["bilateral_decision_alerts"], [first["decision_id"], second["decision_id"]])

    def test_bilateral_low_odds_formatter_shows_both_prices_minimum_and_no_bet(self):
        """A complete pair below the frozen minimum is a notified no-bet, not an outage."""
        decision = bilateral_decision()
        decision.update({
            "signal_quote": 1.75,
            "counterpart_quote": 1.84,
            "counterpart_reason": None,
            "minimum_odds": 1.92,
            "decision": "NO_BET_LOW_ODDS",
            "chosen_execution_book": None,
        })
        message = notify._bilateral_decision_message(decision)
        self.assertIsNotNone(message)
        for expected in (
            "馬會訊號 @1.75",
            "皇冠對照 @1.84",
            "最低 1.92",
            "賠率不足，所以不投注",
            "平台：—",
        ):
            self.assertIn(expected, message)
        self.assertNotIn("對照收集失敗", message)
        self.assertNotIn("模擬投注", message)

    def test_bilateral_groups_are_separate_for_different_fixture_or_platform(self):
        first = bilateral_decision()
        other_fixture = bilateral_decision(
            fixture="other",
            kickoff=(datetime.now(HKT) + timedelta(hours=3)).isoformat(),
            home="另一主隊", away="另一客隊", condition_number=8,
        )
        other_platform = bilateral_decision(system="crown", condition_number=9)
        ledger = {"footbreak_crown_execution_test": {
            "decisions": [first, other_fixture, other_platform],
            "decision_outbox": [
                bilateral_outbox(row)
                for row in (first, other_fixture, other_platform)
            ],
        }}
        with tempfile.TemporaryDirectory() as directory:
            with patch.object(notify, "STATE", str(Path(directory, "notify.json"))), \
                 patch.object(notify, "send") as sender:
                self.assertEqual(notify.notify_pending_bilateral_decisions(ledger), 2)
        self.assertEqual(sender.call_count, 2)
        self.assertTrue(all("條件 #9" not in call.args[0] for call in sender.call_args_list))

    def test_bilateral_group_transport_failure_keeps_every_id_for_one_retry(self):
        first = bilateral_decision()
        second = bilateral_decision(
            kickoff=first["kickoff"], market="CHL", side="L", line=10.5,
            condition_number=8,
        )
        ledger = {"footbreak_crown_execution_test": {
            "decisions": [first, second],
            "decision_outbox": [
                bilateral_outbox(row)
                for row in (first, second)
            ],
        }}
        with tempfile.TemporaryDirectory() as directory:
            state = Path(directory, "notify.json")
            with patch.object(notify, "STATE", str(state)), \
                 patch.object(notify, "send", side_effect=RuntimeError("temporary")):
                with self.assertRaisesRegex(RuntimeError, "temporary"):
                    notify.notify_pending_bilateral_decisions(ledger)
            self.assertFalse(state.exists())
            with patch.object(notify, "STATE", str(state)), patch.object(notify, "send") as sender:
                self.assertEqual(notify.notify_pending_bilateral_decisions(ledger), 1)
        self.assertEqual(sender.call_count, 1)

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
