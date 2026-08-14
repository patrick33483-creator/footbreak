from __future__ import annotations

import json
import tempfile
import unittest
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch

from crown.config import settings
from crown.notify import notify_new

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
    kickoff = datetime(2099, 8, 1, 20, tzinfo=HKT)
    stages = [{"stage": name, "ts": (kickoff - timedelta(minutes=minutes + 1)).isoformat(),
               "market_predictions": [{"code": "HIL", "side": "H", "line": 2.5, "odds": 1.83}]}
              for name, minutes in (("首預", 90), ("T-30", 30), ("T-5", 5))]
    return {"watch": {"future": {"match_id": "future", "kickoff": kickoff.isoformat(),
                                 "kickoff_hkt": kickoff.isoformat(), "home": "主", "away": "客",
                                 "stages": stages}}}


class CrownGranularNotificationTests(unittest.TestCase):
    def test_t30_t5_are_independent_and_no_backfill(self):
        with tempfile.TemporaryDirectory() as directory:
            config = replace(settings(), state_dir=Path(directory), telegram_enabled=False)
            (config.state_dir / "prediction_history.json").write_text(
                json.dumps({"rows": history("T-30") + history("T-5")}), encoding="utf-8")
            with patch("crown.notify._send") as sender:
                self.assertEqual(notify_new(ledger(), config, []), 0)
                self.assertEqual(notify_new(ledger(), config, [{"match_id": "future", "stage": "T-30"}]), 1)
                self.assertEqual(notify_new(ledger(), config, [{"match_id": "future", "stage": "T-30"}]), 0)
                self.assertEqual(notify_new(ledger(), config, [{"match_id": "future", "stage": "T-5"}]), 1)
            self.assertEqual(sender.call_count, 2)
            self.assertIn("預備提示", sender.call_args_list[0].args[1])
            self.assertIn("數據提示", sender.call_args_list[1].args[1])


if __name__ == "__main__":
    unittest.main()
