from __future__ import annotations

import json
import tempfile
import unittest
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch

from analysis.granular_conditions import mine
from crown.config import settings
from crown.notify import _public_condition_text, notify_new

HKT = timezone(timedelta(hours=8))


def history(stage):
    values = []
    for i in range(12):
        kickoff = datetime(2026, 8, 1, 20, tzinfo=HKT) + timedelta(days=i)
        values.append({"match_id": f"{stage}-{i}", "stage": stage, "kickoff": kickoff.isoformat(),
                       "predicted_at": (kickoff - timedelta(minutes=40)).isoformat(),
                       "market_grades": [{"code": "HIL", "side": "H", "line": 2.5, "odds": 1.8,
                                          "grade_status": "GRADED", "hit": True}]})
    return values


def ledger():
    now = datetime.now(HKT).replace(microsecond=0)
    kickoff = now + timedelta(minutes=5)
    stages = [{"stage": name, "match_id": "future", "kickoff_hkt": kickoff.isoformat(),
               "ts": (kickoff - timedelta(minutes=minutes + 1)).isoformat(),
               "market_predictions": [{"code": "HIL", "side": "H", "line": 2.5, "odds": 1.83}]}
              for name, minutes in (("首預", 90), ("T-30", 30), ("T-5", 5))]
    return {"watch": {"future": {"match_id": "future", "kickoff": kickoff.isoformat(),
                                 "kickoff_hkt": kickoff.isoformat(), "home": "主", "away": "客",
                                 "league": "測試聯賽",
                                 "stages": stages}}}


class CrownGranularNotificationTests(unittest.TestCase):
    def _config_with_history(self, directory):
        config = replace(settings(), state_dir=Path(directory), telegram_enabled=False)
        rows = history("T-30") + history("T-5")
        (config.state_dir / "prediction_history.json").write_text(
            json.dumps({
                "rows": rows,
                "stats": {"granular_conditions": mine(rows, system="crown")},
            }),
            encoding="utf-8",
        )
        return config

    def test_t30_t5_are_independent_and_recent_unacknowledged_rows_recover(self):
        with tempfile.TemporaryDirectory() as directory:
            config = self._config_with_history(directory)
            with patch("crown.notify._send") as sender:
                self.assertEqual(notify_new(ledger(), config, []), 2)
                self.assertEqual(notify_new(ledger(), config, [{"match_id": "future", "stage": "T-30"}]), 0)
                self.assertEqual(notify_new(ledger(), config, [{"match_id": "future", "stage": "T-30"}]), 0)
            self.assertEqual(sender.call_count, 2)
            self.assertIn("預備提示", sender.call_args_list[0].args[1])
            self.assertIn("數據提示", sender.call_args_list[1].args[1])
            for call in sender.call_args_list:
                message = call.args[1]
                self.assertIn("聯賽：測試聯賽", message)
                self.assertIn("投注：入球大細", message)
                self.assertIn("選擇：大", message)
                self.assertIn("盤口：2.5", message)
                self.assertIn("賠率：1.83", message)
                self.assertIn("命中率：", message)
                self.assertNotIn("HDC", message)
                self.assertNotIn("HIL", message)
                self.assertNotIn("CHL", message)
                self.assertNotRegex(message, r"\b[ABC](?:→[ABC])+\b")

    def test_timeout_retries_once_on_next_tick_without_a_fresh_handoff(self):
        with tempfile.TemporaryDirectory() as directory:
            config = self._config_with_history(directory)
            current = ledger()
            current["watch"]["future"]["stages"] = [
                stage for stage in current["watch"]["future"]["stages"]
                if stage["stage"] == "T-5"
            ]
            with patch("crown.notify._send", side_effect=TimeoutError("telegram timeout")):
                with self.assertRaises(TimeoutError):
                    notify_new(current, config, [{"match_id": "future", "stage": "T-5"}])
            with patch("crown.notify._send", return_value=True) as sender:
                self.assertEqual(notify_new(current, config, []), 1)
                self.assertEqual(notify_new(current, config, []), 0)
            self.assertEqual(sender.call_count, 1)

    def test_cached_ranking_notifies_without_remine(self):
        with tempfile.TemporaryDirectory() as directory:
            config = self._config_with_history(directory)
            current = ledger()
            current["watch"]["future"]["stages"] = [
                stage for stage in current["watch"]["future"]["stages"]
                if stage["stage"] == "T-5"
            ]
            with patch(
                "analysis.granular_conditions.mine",
                side_effect=AssertionError("tick must reuse cached ranking"),
            ), patch("crown.notify._send", return_value=True) as sender:
                self.assertEqual(
                    notify_new(current, config, [{"match_id": "future", "stage": "T-5"}]),
                    1,
                )
            self.assertEqual(sender.call_count, 1)

    def test_fresh_and_recovery_candidates_are_deduplicated(self):
        with tempfile.TemporaryDirectory() as directory:
            config = self._config_with_history(directory)
            current = ledger()
            current["watch"]["future"]["stages"] = [
                stage for stage in current["watch"]["future"]["stages"]
                if stage["stage"] == "T-5"
            ]
            with patch("crown.notify._send", return_value=True) as sender:
                self.assertEqual(
                    notify_new(current, config, [{"match_id": "future", "stage": "T-5"}]),
                    1,
                )
            self.assertEqual(sender.call_count, 1)

    def test_post_kickoff_and_stale_native_stage_never_replay(self):
        with tempfile.TemporaryDirectory() as directory:
            config = self._config_with_history(directory)
            post_kickoff = ledger()
            now = datetime.now(HKT).replace(microsecond=0)
            watch = post_kickoff["watch"]["future"]
            watch["kickoff"] = watch["kickoff_hkt"] = (now - timedelta(minutes=1)).isoformat()
            watch["stages"] = [stage for stage in watch["stages"] if stage["stage"] == "T-5"]
            watch["stages"][0]["kickoff_hkt"] = watch["kickoff_hkt"]
            watch["stages"][0]["ts"] = (now - timedelta(minutes=6)).isoformat()

            stale = ledger()
            stale_watch = stale["watch"]["future"]
            stale_watch["stages"] = [
                stage for stage in stale_watch["stages"] if stage["stage"] == "T-30"
            ]
            stale_watch["stages"][0]["ts"] = (now - timedelta(minutes=46)).isoformat()
            with patch("crown.notify._send") as sender:
                self.assertEqual(notify_new(post_kickoff, config, []), 0)
                self.assertEqual(notify_new(stale, config, []), 0)
            sender.assert_not_called()

    def test_post_hoc_recovered_stage_never_notifies(self):
        with tempfile.TemporaryDirectory() as directory:
            config = self._config_with_history(directory)
            recovered = ledger()
            recovered["watch"]["future"]["stages"] = [
                stage for stage in recovered["watch"]["future"]["stages"]
                if stage["stage"] == "T-5"
            ]
            recovered["watch"]["future"]["stages"][0]["post_hoc_backfill"] = True
            recovered["watch"]["future"]["stages"][0]["exclude_from_telegram"] = True
            with patch("crown.notify._send") as sender:
                self.assertEqual(
                    notify_new(recovered, config, [{"match_id": "future", "stage": "T-5"}]),
                    0,
                )
            sender.assert_not_called()

    def test_missing_league_fails_closed(self):
        with tempfile.TemporaryDirectory() as directory:
            config = self._config_with_history(directory)
            current = ledger()
            current["watch"]["future"]["league"] = ""
            with patch("crown.notify._send") as sender:
                self.assertEqual(
                    notify_new(current, config, [{"match_id": "future", "stage": "T-5"}]),
                    0,
                )
            sender.assert_not_called()



    def test_public_condition_text_uses_chinese_market_and_observed_roles(self):
        current = _public_condition_text("HDC｜方向 主讓→客受讓→主讓")
        self.assertEqual(current, "讓球｜方向 主讓→客受讓→主讓")
        legacy = _public_condition_text("HIL｜方向 A→B→A")
        self.assertIn("入球大細", legacy)
        self.assertNotRegex(legacy, r"\b[ABC](?:→[ABC])+\b")
        for code in ("HDC", "HIL", "CHL"):
            self.assertNotIn(code, _public_condition_text(f"{code}｜方向 A→B→A"))


if __name__ == "__main__":
    unittest.main()
