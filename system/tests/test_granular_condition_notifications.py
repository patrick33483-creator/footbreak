from __future__ import annotations

import json
import sys
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[2]
SYSTEM = ROOT / "system"
if str(SYSTEM) not in sys.path:
    sys.path.insert(0, str(SYSTEM))

import notify

HKT = timezone(timedelta(hours=8))


def _history(stage: str, code="HDC"):
    rows = []
    for index in range(20):
        kickoff = datetime(2026, 8, 1, 20, tzinfo=HKT) + timedelta(days=index)
        rows.append({
            "match_id": f"history-{stage}-{index}", "stage": stage,
            "kickoff": kickoff.isoformat(),
            "predicted_at": (kickoff - timedelta(minutes=35)).isoformat(),
            "market_grades": [{
                "code": code, "side": "H", "line": -.25, "odds": 1.8,
                "grade_status": "GRADED", "hit": True,
            }],
        })
    return rows


def _ledger():
    kickoff_at = datetime(2099, 8, 2, 20, tzinfo=HKT)
    kickoff = kickoff_at.isoformat()
    stages = []
    for stage, minutes in (("首預", 90), ("T-30", 30), ("T-5", 5)):
        stages.append({
            "stage": stage, "ts": (kickoff_at - timedelta(minutes=minutes + 1)).isoformat(),
            "market_predictions": [{
                "code": "HDC", "side": "H", "line": -.25, "odds": 1.82,
                "observed_at": (kickoff_at - timedelta(minutes=minutes + 2)).isoformat(),
                "source": "hkjc_public_board",
            }],
        })
    return {"watch": {"future": {
        "match_id": "future", "kickoff": kickoff, "home": "主隊", "away": "客隊",
        "league": "測試", "stages": stages,
    }}}


class GranularConditionNotificationTests(unittest.TestCase):
    def test_retired_granular_candidate_notifications_are_silent(self):
        ledger = _ledger()
        payload = {"prediction_history": {"rows": _history("T-30") + _history("T-5")}}
        with tempfile.TemporaryDirectory() as directory:
            data, state = Path(directory, "data.json"), Path(directory, "state.json")
            data.write_text(json.dumps(payload), encoding="utf-8")
            with patch.object(notify, "DASHBOARD_DATA", str(data)), \
                 patch.object(notify, "STATE", str(state)), \
                 patch.object(notify, "send") as sender:
                self.assertEqual(notify.notify_fresh_granular_conditions(
                    ledger, [{"match_id": "future", "stage": "T-30"}]), 0)
                self.assertEqual(notify.notify_fresh_granular_conditions(
                    ledger, [{"match_id": "future", "stage": "T-5"}]), 0)
            sender.assert_not_called()

        with tempfile.TemporaryDirectory() as directory:
            data, state = Path(directory, "data.json"), Path(directory, "state.json")
            data.write_text(json.dumps(payload), encoding="utf-8")
            before = json.loads(json.dumps(ledger))
            with patch.object(notify, "DASHBOARD_DATA", str(data)), \
                 patch.object(notify, "STATE", str(state)), \
                 patch.object(notify, "send", side_effect=RuntimeError("down")):
                self.assertEqual(notify.notify_fresh_granular_conditions(
                    ledger, [{"match_id": "future", "stage": "T-30"}]), 0)
            self.assertEqual(ledger, before)
            self.assertFalse(state.exists())

    def test_missing_league_fails_closed(self):
        ledger = _ledger()
        ledger["watch"]["future"]["league"] = ""
        payload = {"prediction_history": {"rows": _history("T-5")}}
        with tempfile.TemporaryDirectory() as directory:
            data, state = Path(directory, "data.json"), Path(directory, "state.json")
            data.write_text(json.dumps(payload), encoding="utf-8")
            with patch.object(notify, "DASHBOARD_DATA", str(data)), \
                 patch.object(notify, "STATE", str(state)), \
                 patch.object(notify, "send") as sender:
                self.assertEqual(
                    notify.notify_fresh_granular_conditions(
                        ledger, [{"match_id": "future", "stage": "T-5"}]
                    ),
                    0,
                )
            sender.assert_not_called()

    def test_no_fresh_event_or_invalid_odds_never_sends(self):
        ledger = _ledger()
        ledger["watch"]["future"]["stages"][1]["market_predictions"][0]["odds"] = None
        payload = {"prediction_history": {"rows": _history("T-30")}}
        with tempfile.TemporaryDirectory() as directory:
            data = Path(directory, "data.json")
            data.write_text(json.dumps(payload), encoding="utf-8")
            with patch.object(notify, "DASHBOARD_DATA", str(data)), patch.object(notify, "send") as sender:
                self.assertEqual(notify.notify_fresh_granular_conditions(ledger, []), 0)
                self.assertEqual(notify.notify_fresh_granular_conditions(
                    ledger, [{"match_id": "future", "stage": "T-30"}]), 0)
            sender.assert_not_called()


    def test_public_condition_text_uses_chinese_market_and_observed_roles(self):
        current = notify._public_condition_text("CHL｜方向 角球大→角球細→角球大")
        self.assertEqual(current, "角球大細｜方向 角球大→角球細→角球大")
        legacy = notify._public_condition_text("HDC｜方向 A→B→A")
        self.assertIn("讓球", legacy)
        self.assertNotRegex(legacy, r"\b[ABC](?:→[ABC])+\b")
        for code in ("HDC", "HIL", "CHL"):
            self.assertNotIn(code, notify._public_condition_text(f"{code}｜方向 A→B→A"))


    def test_t30_preparation_and_true_new_t5_dispatch_are_separate(self):
        source = (SYSTEM / "record_picks.py").read_text(encoding="utf-8")
        notifier = (SYSTEM / "notify.py").read_text(encoding="utf-8")
        self.assertIn("notify_pending_condition_bets(ledger)", source)
        self.assertNotIn("notify_fresh_granular_conditions(ledger, fresh_t30_events)", source)
        self.assertIn('if stage == "T-30":', source)
        self.assertNotIn(
            'fresh_t30_events.append({"match_id": match_id, "stage": "T-5"})',
            source,
        )
        self.assertNotIn("notify_fresh_t5_signals(led", source)
        self.assertIn("舊有 Telegram 通知已停用", notifier)

    def test_old_v1_entry_bet_never_alerts_after_cutover(self):
        kickoff = (datetime.now(HKT) + timedelta(hours=2)).isoformat()
        bet = {
            "bet_id": "fixture|HIL|T-5|independent-validation-v1",
            "portfolio": "footbreak_independent_validation",
            "strategy": "independent-validation-v1",
            "league": "測試聯賽",
            "home": "主隊",
            "away": "客隊",
            "kickoff": kickoff,
            "market_label": "入球大細",
            "selected_role": "大",
            "selected_line": 2.5,
            "odds": 1.82,
            "condition_accuracy": .7,
            "condition_hits": 14,
            "condition_decided": 20,
        }
        with tempfile.TemporaryDirectory() as directory:
            state = Path(directory, "state.json")
            with patch.object(notify, "STATE", str(state)), patch.object(notify, "send") as sender:
                self.assertEqual(notify.notify_new_condition_bets({"bets": [bet]}, [bet["bet_id"]]), 0)
                self.assertEqual(notify.notify_new_condition_bets({"bets": [bet]}, [bet["bet_id"]]), 0)
            sender.assert_not_called()

        bet["league"] = ""
        with tempfile.TemporaryDirectory() as directory:
            with patch.object(notify, "STATE", str(Path(directory, "state.json"))), patch.object(notify, "send") as sender:
                self.assertEqual(notify.notify_new_condition_bets({"bets": [bet]}, [bet["bet_id"]]), 0)
            sender.assert_not_called()

    def test_old_v1_pending_bet_is_silent_even_with_historical_metrics(self):
        kickoff = (datetime.now(HKT) + timedelta(hours=2)).isoformat()
        bet = {
            "bet_id": "frozen|HDC|T-5|independent-validation-v1",
            "portfolio": "footbreak_independent_validation",
            "strategy": "independent-validation-v1",
            "league": "測試聯賽", "home": "主隊", "away": "客隊", "kickoff": kickoff,
            "market_label": "讓球", "selected_role": "主讓", "selected_line": -0.25,
            "odds": 2.0, "condition_accuracy": .7, "condition_hits": 14,
            "condition_decided": 20, "frozen_condition_signature": "condition-1",
        }
        ledger = {
            "bets": [bet],
            "independent_validation": {"conditions": {"condition-1": {"prospective": {
                "status": "已驗證", "hits": 30, "decided": 30, "accuracy": .75,
                "pnl": 1250, "roi": .166667,
            }}}},
        }
        with tempfile.TemporaryDirectory() as directory:
            state = Path(directory, "state.json")
            with patch.object(notify, "STATE", str(state)), patch.object(notify, "send") as sender:
                self.assertEqual(notify.notify_pending_condition_bets(ledger), 0)
                self.assertEqual(notify.notify_pending_condition_bets(ledger), 0)
        sender.assert_not_called()

    def test_old_v1_pending_bet_never_enters_retry_outbox(self):
        kickoff = (datetime.now(HKT) + timedelta(minutes=8)).isoformat()
        bet = {
            "bet_id": "retry|HDC|T-5|independent-validation-v1",
            "portfolio": "footbreak_independent_validation",
            "strategy": "independent-validation-v1",
            "league": "瑞典超級聯賽",
            "home": "米贊比",
            "away": "天狼星",
            "kickoff": kickoff,
            "market_label": "讓球",
            "selected_role": "主讓",
            "selected_line": -0.25,
            "odds": 1.82,
            "condition_accuracy": .7,
            "condition_hits": 14,
            "condition_decided": 20,
        }
        ledger = {"bets": [bet]}
        with tempfile.TemporaryDirectory() as directory:
            state = Path(directory, "state.json")
            with patch.object(notify, "STATE", str(state)), \
                 patch.object(notify, "send", side_effect=RuntimeError("temporary outage")):
                self.assertEqual(notify.notify_pending_condition_bets(ledger), 0)
            self.assertFalse(state.exists())

            with patch.object(notify, "STATE", str(state)), patch.object(notify, "send") as sender:
                self.assertEqual(notify.notify_pending_condition_bets(ledger), 0)
                self.assertEqual(notify.notify_pending_condition_bets(ledger), 0)
            sender.assert_not_called()
            self.assertFalse(state.exists())


if __name__ == "__main__":
    unittest.main()
