from __future__ import annotations

import copy
import json
import sys
import tempfile
import unittest
from datetime import datetime, timedelta
from pathlib import Path
from unittest.mock import patch

SYSTEM = Path(__file__).resolve().parents[1]
if str(SYSTEM) not in sys.path:
    sys.path.insert(0, str(SYSTEM))

import notify
import record_picks


KICKOFF = "2099-08-12T20:00:00+08:00"


def _ledger(side="L", odds=1.91, line="2.5"):
    return {
        "watch": {
            "footbreak-safe-1": {
                "match_id": "footbreak-safe-1", "kickoff": KICKOFF,
                "league": "測試聯賽", "home": "主隊", "away": "客隊",
                "stages": [{
                    "stage": "T-5", "home": "主隊", "away": "客隊",
                    "market_predictions": [{
                        "code": "HIL", "side": side, "line": line,
                        "condition": line, "odds": odds,
                    }],
                }],
            }
        },
        "bets": [], "shadow_bets": [],
    }


class FootbreakT5SignalNotificationTests(unittest.TestCase):
    def test_hil_under_signal_is_retired(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            state = str(Path(directory) / "notify_state.json")
            with patch.object(notify, "STATE", state), patch.object(notify, "send") as sender:
                self.assertEqual(
                    notify.notify_fresh_t5_signals(_ledger(), ["footbreak-safe-1"]), 0
                )
                self.assertEqual(
                    notify.notify_fresh_t5_signals(_ledger(), ["footbreak-safe-1"]), 0
                )
            sender.assert_not_called()

    def test_rejects_non_under_missing_or_wrong_side_odds_and_non_t5(self) -> None:
        cases = [
            _ledger(side="H", odds=1.91),
            _ledger(side="L", odds=None),
            _ledger(side="L", odds=1.699),
        ]
        wrong_side = _ledger(side="L", odds=None)
        wrong_side["watch"]["footbreak-safe-1"]["stages"][0]["market_predictions"].append({
            "code": "HIL", "side": "H", "line": "2.5", "odds": 1.77,
        })
        cases.append(wrong_side)
        non_t5 = _ledger()
        non_t5["watch"]["footbreak-safe-1"]["stages"][0]["stage"] = "T-30"
        cases.append(non_t5)
        for ledger in cases:
            with self.subTest(ledger=ledger), tempfile.TemporaryDirectory() as directory:
                with patch.object(notify, "STATE", str(Path(directory) / "notify_state.json")), \
                     patch.object(notify, "send") as sender:
                    self.assertEqual(
                        notify.notify_fresh_t5_signals(ledger, ["footbreak-safe-1"]), 0
                    )
                sender.assert_not_called()

    def test_exact_1_70_boundary_stays_silent(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            state = str(Path(directory) / "notify_state.json")
            with patch.object(notify, "STATE", state), patch.object(notify, "send") as sender:
                self.assertEqual(
                    notify.notify_fresh_t5_signals(
                        _ledger(side="L", odds=1.70), ["footbreak-safe-1"]
                    ),
                    0,
                )
            sender.assert_not_called()

    def test_recovers_upcoming_signal_and_transport_failure_keeps_ledger(self) -> None:
        ledger = _ledger()
        with tempfile.TemporaryDirectory() as directory:
            state_path = Path(directory) / "notify_state.json"
            with patch.object(notify, "STATE", str(state_path)), patch.object(notify, "send") as sender:
                self.assertEqual(notify.notify_fresh_t5_signals(ledger, []), 0)
            sender.assert_not_called()

            state_path.unlink(missing_ok=True)
            before = copy.deepcopy(ledger)
            with patch.object(notify, "STATE", str(state_path)), \
                 patch.object(notify, "send", side_effect=RuntimeError("transport down")):
                self.assertEqual(
                    notify.notify_fresh_t5_signals(ledger, ["footbreak-safe-1"]), 0
                )
            self.assertEqual(ledger, before)
            self.assertFalse(state_path.exists())
            with patch.object(notify, "STATE", str(state_path)), \
                 patch.object(notify, "send") as sender:
                self.assertEqual(notify.notify_fresh_t5_signals(ledger, []), 0)
            sender.assert_not_called()

    def test_record_picks_dispatches_only_after_new_t5_snapshot_is_saved(self) -> None:
        kickoff = (datetime.now(record_picks.HKT) + timedelta(minutes=20)).strftime(
            "%Y-%m-%d %H:%M"
        )
        result = {
            "match_id": "persisted-t5", "stage": "T-5", "kickoff_hkt": kickoff,
            "league": "測試聯賽", "home": "主隊", "away": "客隊",
            "conviction": 60.0, "model_source": "pinnapi",
            "sharp_reference_available": True, "candidates": [{
                "market": "入球大小", "code": "HIL", "condition": "2.5",
                "side": "L", "label": "細 2.5", "prob": .55, "push": 0.0,
                "odds": 1.91, "ev": -.01, "kelly_used": 0.0, "is_main": True,
            }],
            "pick": None, "lead_view": None, "no_bet_reason": "測試",
            "can_bet": True, "weather": {}, "final": {}, "open": {}, "now": {},
            "movement": {}, "adjustments": [], "mults": {}, "outcome": {},
        }
        with tempfile.TemporaryDirectory() as directory:
            predictions = Path(directory) / "predictions.json"
            ledger_path = Path(directory) / "sim_ledger.json"
            predictions.write_text(json.dumps([result], ensure_ascii=False), encoding="utf-8")
            observed = {}

            def after_persist(ledger, fresh_ids):
                observed["disk"] = json.loads(ledger_path.read_text(encoding="utf-8"))
                observed["ids"] = fresh_ids
                return 1

            with patch.object(record_picks, "HERE", directory), \
                 patch.object(record_picks, "LEDGER", str(ledger_path)), \
                 patch.object(notify, "notify_fresh_t5_signals", side_effect=after_persist):
                _, _, saved = record_picks.sync()

            self.assertEqual(observed["ids"], ["persisted-t5"])
            self.assertEqual(
                observed["disk"]["watch"]["persisted-t5"]["stages"][0]["stage"], "T-5"
            )
            self.assertEqual(saved, observed["disk"])

    def test_record_picks_transport_failure_preserves_saved_t5_snapshot(self) -> None:
        kickoff = (datetime.now(record_picks.HKT) + timedelta(minutes=20)).strftime(
            "%Y-%m-%d %H:%M"
        )
        result = {
            "match_id": "failure-t5", "stage": "T-5", "kickoff_hkt": kickoff,
            "league": "測試聯賽", "home": "主隊", "away": "客隊",
            "conviction": 60.0, "model_source": "pinnapi",
            "sharp_reference_available": True, "candidates": [{
                "market": "入球大小", "code": "HIL", "condition": "2.5",
                "side": "L", "label": "細 2.5", "prob": .55, "push": 0.0,
                "odds": 1.91, "ev": -.01, "kelly_used": 0.0, "is_main": True,
            }],
            "pick": None, "lead_view": None, "no_bet_reason": "測試",
            "can_bet": True, "weather": {}, "final": {}, "open": {}, "now": {},
            "movement": {}, "adjustments": [], "mults": {}, "outcome": {},
        }
        with tempfile.TemporaryDirectory() as directory:
            Path(directory, "predictions.json").write_text(
                json.dumps([result], ensure_ascii=False), encoding="utf-8"
            )
            ledger_path = Path(directory, "sim_ledger.json")
            with patch.object(record_picks, "HERE", directory), \
                 patch.object(record_picks, "LEDGER", str(ledger_path)), \
                 patch.object(notify, "notify_fresh_t5_signals",
                              side_effect=RuntimeError("transport down")):
                _, notes, _ = record_picks.sync()
            persisted = json.loads(ledger_path.read_text(encoding="utf-8"))
            self.assertEqual(
                persisted["watch"]["failure-t5"]["stages"][0]["stage"], "T-5"
            )
            self.assertTrue(any("Telegram T-5 訊號發送失敗" in note for note in notes))
