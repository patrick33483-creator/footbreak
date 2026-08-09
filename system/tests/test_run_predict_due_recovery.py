import datetime as dt
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from system import run_predict


class DueRecoveryTests(unittest.TestCase):
    def test_pending_watch_ids_include_missing_t5_fixture(self):
        with tempfile.TemporaryDirectory() as tmp:
            kickoff = dt.datetime.now(run_predict.HKT) + dt.timedelta(minutes=6)
            ledger = {
                "watch": {
                    "missing": {
                        "kickoff": kickoff.isoformat(),
                        "stages": [{"stage": "首預"}],
                    },
                    "done": {
                        "kickoff": kickoff.isoformat(),
                        "stages": [{"stage": "T-5"}],
                    },
                }
            }
            Path(tmp, "sim_ledger.json").write_text(json.dumps(ledger), encoding="utf-8")
            with patch.object(run_predict, "HERE", tmp):
                self.assertEqual(run_predict.pending_watch_match_ids(90), ["missing"])

    def test_due_fetch_recovers_fixture_omitted_from_full_board(self):
        kickoff = dt.datetime.now(run_predict.HKT) + dt.timedelta(minutes=6)
        recovered = {"id": "missing", "status": "PREEVENT", "kickOffTime": kickoff.isoformat()}
        with patch.object(run_predict, "pending_watch_match_ids", return_value=["missing"]), \
             patch.object(
                 run_predict.H,
                 "fetch_matches",
                 side_effect=lambda match_ids=None: [recovered] if match_ids else [],
             ) as fetch:
            rows = run_predict.fetch_matches_with_due_recovery("due", 90)
        self.assertEqual(rows, [recovered])
        fetch.assert_any_call(match_ids=["missing"])

    def test_t5_window_keeps_one_to_two_minutes(self):
        self.assertEqual(run_predict.due_now(1.2, set()), "T-5")

    def test_t5_window_keeps_last_prematch_seconds(self):
        self.assertEqual(run_predict.due_now(0.1, set()), "T-5")
        self.assertIsNone(run_predict.due_now(0.0, set()))
        self.assertIsNone(run_predict.due_now(-0.1, set()))


if __name__ == "__main__":
    unittest.main()
