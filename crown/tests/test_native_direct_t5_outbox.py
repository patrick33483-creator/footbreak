from __future__ import annotations

import tempfile
import unittest
from dataclasses import replace
from pathlib import Path
from unittest.mock import patch

from crown.config import settings
from crown.ledger import sync_prediction
from crown.notify import notify_new
from crown.state import paths
from crown.common import read_json


KICKOFF = "2099-08-12T20:00:00+08:00"


def _prediction(stage: str, *, side: str = "H", line: float = -0.25, odds: float = 1.50) -> dict:
    observed = {
        "首預": "2099-08-12T18:00:00+08:00",
        "T-30": "2099-08-12T19:30:00+08:00",
        "T-5": "2099-08-12T19:55:00+08:00",
    }[stage]
    return {
        "match_id": "native-direct-fixture",
        "league": "皇冠測試聯賽",
        "home": "主隊",
        "away": "客隊",
        "kickoff_hkt": KICKOFF,
        "stage": stage,
        "forecast_candidates": [{
            "code": "HDC",
            "market": "皇冠讓球",
            "side": side,
            "line": line,
            "odds": odds,
            "observed_at": observed,
            "quote_source": "native-crown",
        }],
    }


class NativeDirectT5OutboxTests(unittest.TestCase):
    def _config(self, directory: str):
        return replace(settings(), state_dir=Path(directory), telegram_enabled=False)

    def _three_stages(self, config):
        ledger = {"bets": [], "watch": {}, "log": [], "stats": {}}
        sync_prediction(ledger, _prediction("首預"), config)
        sync_prediction(ledger, _prediction("T-30"), config)
        sync_prediction(ledger, _prediction("T-5"), config)
        return ledger

    def test_legacy_exact_hdc_consensus_creates_direct_outbox_low_odds_without_formal_stats(self):
        with tempfile.TemporaryDirectory() as directory:
            ledger = self._three_stages(self._config(directory))
        outbox = ledger["native_t5_direct_notifications"]["outbox"]
        self.assertEqual(len(outbox), 1)
        row = outbox[0]
        self.assertEqual(row["legacy_policy"], "legacy-hdc-exact-three-stage-v1")
        self.assertTrue(row["native_pre_kickoff_t5"])
        self.assertEqual(row["odds"], 1.50)
        self.assertFalse(row["formal"])
        self.assertEqual(row["execution"], "NO_BET_DIRECT_OBSERVATION")
        self.assertEqual(ledger["bets"], [])
        self.assertEqual(ledger["wilson_validation"].get("observations") or [], [])

    def test_formal_direct_event_uses_wilson_label_and_acknowledges_shared_formal_outbox(self):
        with tempfile.TemporaryDirectory() as directory:
            config = self._config(directory)
            ledger = self._three_stages(config)
            row = ledger["native_t5_direct_notifications"]["outbox"][0]
            row.update({
                "formal": True,
                "condition_numbers": [7],
                "formal_decision": "NO_BET_LOW_ODDS",
                "formal_notification_ids": ["native-direct-fixture|HDC|T-5|signature|low-odds"],
            })
            with patch("crown.notify._send", return_value=True) as sender:
                self.assertEqual(notify_new(ledger, config, max_attempts=1), 1)
            message = sender.call_args.args[1]
            self.assertIn("【皇冠 Wilson】", message)
            self.assertIn("條件 #7", message)
            self.assertIn("不投注：賠率不足", message)
            self.assertIn("不比較馬會", message)
            state = read_json(paths(config)["notify"], {})
            self.assertEqual(state["native_t5_direct_alerts"], [row["direct_signal_id"]])
            self.assertIn(
                "native-direct-fixture|HDC|T-5|signature|low-odds",
                state["wilson_match_alerts"],
            )
            with patch("crown.notify._send", return_value=True) as repeated:
                self.assertEqual(notify_new(ledger, config, max_attempts=1), 0)
            repeated.assert_not_called()

    def test_changed_line_or_repeated_stage_never_creates_event(self):
        with tempfile.TemporaryDirectory() as directory:
            config = self._config(directory)
            ledger = {"bets": [], "watch": {}, "log": [], "stats": {}}
            sync_prediction(ledger, _prediction("首預"), config)
            sync_prediction(ledger, _prediction("T-30", line=-0.5), config)
            sync_prediction(ledger, _prediction("T-5"), config)
            self.assertEqual(ledger["native_t5_direct_notifications"]["outbox"], [])

            # A repeated stage is not a new decision and cannot become a
            # retrospective notification after its stage has been persisted.
            self.assertEqual(sync_prediction(ledger, _prediction("T-5"), config), [])
            self.assertEqual(ledger["native_t5_direct_notifications"]["outbox"], [])

    def test_expired_direct_outbox_is_never_replayed(self):
        with tempfile.TemporaryDirectory() as directory:
            config = self._config(directory)
            ledger = self._three_stages(config)
            row = ledger["native_t5_direct_notifications"]["outbox"][0]
            row["kickoff"] = "2020-08-12T20:00:00+08:00"
            with patch("crown.notify._send", return_value=True) as sender:
                self.assertEqual(notify_new(ledger, config, max_attempts=1), 0)
            sender.assert_not_called()
            state = read_json(paths(config)["notify"], {})
            self.assertNotIn(row["direct_signal_id"], state["native_t5_direct_alerts"])

    def test_nonformal_message_is_explicit_research_and_never_calls_reciprocal_code(self):
        with tempfile.TemporaryDirectory() as directory:
            config = self._config(directory)
            ledger = self._three_stages(config)
            with patch("crown.notify._send", return_value=True) as sender, patch(
                "crown.notify.notify_bilateral_decisions"
            ) as reciprocal:
                self.assertEqual(notify_new(ledger, config, max_attempts=1), 1)
            text = sender.call_args.args[1]
            self.assertIn("【皇冠 T-5 觀察/研究訊號】", text)
            self.assertIn("非正式 Wilson 條件，不納入 x/20、不模擬投注", text)
            self.assertNotIn("馬會對照", text)
            reciprocal.assert_not_called()


if __name__ == "__main__":
    unittest.main()
