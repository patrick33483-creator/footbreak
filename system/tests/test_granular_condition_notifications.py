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
    for index in range(12):
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
    kickoff = datetime(2099, 8, 2, 20, tzinfo=HKT).isoformat()
    stages = []
    for stage, minutes in (("首預", 90), ("T-30", 30), ("T-5", 5)):
        stages.append({
            "stage": stage, "ts": (datetime(2099, 8, 2, 20, tzinfo=HKT) - timedelta(minutes=minutes + 1)).isoformat(),
            "market_predictions": [{"code": "HDC", "side": "H", "line": -.25, "odds": 1.82}],
        })
    return {"watch": {"future": {
        "match_id": "future", "kickoff": kickoff, "home": "主隊", "away": "客隊",
        "league": "測試", "stages": stages,
    }}}


class GranularConditionNotificationTests(unittest.TestCase):
    def test_fresh_only_stage_independent_idempotency_and_transport_failure(self):
        ledger = _ledger()
        payload = {"prediction_history": {"rows": _history("T-30") + _history("T-5")}}
        with tempfile.TemporaryDirectory() as directory:
            data, state = Path(directory, "data.json"), Path(directory, "state.json")
            data.write_text(json.dumps(payload), encoding="utf-8")
            with patch.object(notify, "DASHBOARD_DATA", str(data)), \
                 patch.object(notify, "STATE", str(state)), \
                 patch.object(notify, "send") as sender:
                self.assertEqual(notify.notify_fresh_granular_conditions(
                    ledger, [{"match_id": "future", "stage": "T-30"}]), 1)
                self.assertEqual(notify.notify_fresh_granular_conditions(
                    ledger, [{"match_id": "future", "stage": "T-30"}]), 0)
                self.assertEqual(notify.notify_fresh_granular_conditions(
                    ledger, [{"match_id": "future", "stage": "T-5"}]), 1)
            self.assertEqual(sender.call_count, 2)
            self.assertIn("預備提示", sender.call_args_list[0].args[0])
            self.assertIn("數據提示", sender.call_args_list[1].args[0])
            self.assertIn("只作數據提示，由你自行決定。", sender.call_args_list[1].args[0])

        with tempfile.TemporaryDirectory() as directory:
            data, state = Path(directory, "data.json"), Path(directory, "state.json")
            data.write_text(json.dumps(payload), encoding="utf-8")
            before = json.loads(json.dumps(ledger))
            with patch.object(notify, "DASHBOARD_DATA", str(data)), \
                 patch.object(notify, "STATE", str(state)), \
                 patch.object(notify, "send", side_effect=RuntimeError("down")):
                with self.assertRaisesRegex(RuntimeError, "down"):
                    notify.notify_fresh_granular_conditions(
                        ledger, [{"match_id": "future", "stage": "T-30"}])
            self.assertEqual(ledger, before)
            self.assertFalse(state.exists())

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

    def test_legacy_notification_dispatch_is_disabled(self):
        source = (SYSTEM / "record_picks.py").read_text(encoding="utf-8")
        notifier = (SYSTEM / "notify.py").read_text(encoding="utf-8")
        self.assertIn("notify_fresh_granular_conditions", source)
        self.assertNotIn("notify_fresh_t5_signals(led", source)
        self.assertIn("舊有 Telegram 通知已停用", notifier)


if __name__ == "__main__":
    unittest.main()
