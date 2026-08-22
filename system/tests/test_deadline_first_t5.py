import datetime as dt
import json
import os
import sys
import time
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

SYSTEM = Path(__file__).resolve().parents[1]
if str(SYSTEM) not in sys.path:
    sys.path.insert(0, str(SYSTEM))
import run_predict
import record_picks


class DeadlineFirstTickTests(unittest.TestCase):
    def _ledger(self, now, rows):
        return {"watch": {
            key: {"kickoff": (now + dt.timedelta(minutes=minutes)).isoformat(), "stages": stages,
                  "fixture_id": f"fixture-{key}"}
            for key, minutes, stages in rows
        }}

    def _match(self, key, now, minutes):
        return {"id": key, "status": "PREEVENT", "kickOffTime": (now + dt.timedelta(minutes=minutes)).isoformat(),
                "homeTeam": {}, "awayTeam": {}, "tournament": {}}

    def test_empty_due_tick_is_a_true_local_fast_noop(self):
        with tempfile.TemporaryDirectory() as directory, patch.object(run_predict, "HERE", directory):
            Path(directory, "sim_ledger.json").write_text(json.dumps(self._ledger(dt.datetime.now(run_predict.HKT), [])))
            with patch.object(run_predict.H, "fetch_matches") as fetch:
                self.assertEqual(run_predict.main(horizon_min=90), [])
            fetch.assert_not_called()

    def test_unavailable_fixture_does_not_block_later_due_card_or_discovery(self):
        now = dt.datetime.now(run_predict.HKT)
        with tempfile.TemporaryDirectory() as directory, patch.object(run_predict, "HERE", directory):
            Path(directory, "sim_ledger.json").write_text(json.dumps(self._ledger(now, [
                ("unavailable", 5, [{"stage": "T-30"}]), ("ready", 4, [{"stage": "T-30"}]),
            ])))
            ready = self._match("ready", now, 4)
            result = {"match_id": "ready", "kickoff_hkt": ready["kickOffTime"], "stage": "T-5", "candidates": []}
            def synchronous_call(kind, _payload, _deadline):
                return result if kind == "analyse" else True
            with patch.object(run_predict.H, "fetch_matches", return_value=[ready]) as fetch, \
                 patch.object(run_predict, "_bounded_due_call", side_effect=synchronous_call), \
                 patch.object(run_predict, "pick_one", return_value=(None, "觀望")), \
                 patch.object(run_predict.S, "list_fixtures") as discovery:
                rows = run_predict.main(horizon_min=90)
            self.assertEqual([row["match_id"] for row in rows], ["ready"])
            self.assertEqual(len(rows), 1)
            fetch.assert_called_once()
            discovery.assert_not_called()

    def _durable_result(self, match_id, kickoff):
        return {
            "match_id": match_id, "stage": "T-5", "kickoff_hkt": kickoff.isoformat(),
            "league": "L", "home": "H", "away": "A", "conviction": 0.0,
            "model_source": "pinnapi", "sharp_reference_available": True,
            "candidates": [], "weather": {}, "final": {}, "open": {}, "now": {},
            "movement": {}, "adjustments": [], "mults": {}, "outcome": {},
        }

    def test_hung_due_fixture_does_not_stop_two_later_durable_commits(self):
        now = dt.datetime.now(run_predict.HKT)
        with tempfile.TemporaryDirectory() as directory, \
             patch.object(run_predict, "HERE", directory), \
             patch.object(record_picks, "HERE", directory), \
             patch.object(record_picks, "LEDGER", str(Path(directory, "sim_ledger.json"))), \
             patch.dict(os.environ, {"FOOTBREAK_URGENT_CALL_TIMEOUT_SECONDS": "0.4"}, clear=False):
            ledger_path = Path(directory, "sim_ledger.json")
            ledger_path.write_text(json.dumps(self._ledger(now, [
                ("hung", 5, [{"stage": "T-30"}]),
                ("ready-one", 4, [{"stage": "T-30"}]),
                ("ready-two", 3, [{"stage": "T-30"}]),
            ])), encoding="utf-8")
            matches = [self._match(key, now, minutes) for key, minutes in (
                ("hung", 5), ("ready-one", 4), ("ready-two", 3)
            )]
            def analyse(row, *_args, stage_override=None, **_kwargs):
                if row["id"] == "hung":
                    time.sleep(2)
                kickoff = dt.datetime.fromisoformat(row["kickOffTime"])
                return self._durable_result(row["id"], kickoff) | {"stage": stage_override}
            started = time.monotonic()
            with patch.object(run_predict.H, "fetch_matches", return_value=matches), \
                 patch.object(run_predict, "analyse_match", side_effect=analyse), \
                 patch.object(run_predict, "pick_one", return_value=(None, "觀望")):
                rows = run_predict.main(horizon_min=90)
            self.assertLess(time.monotonic() - started, 2.0)
            self.assertEqual([row["match_id"] for row in rows], ["ready-two", "ready-one"])
            saved = json.loads(ledger_path.read_text(encoding="utf-8"))
            self.assertEqual(
                [row["stage"] for row in saved["watch"]["ready-one"]["stages"]], ["T-30", "T-5"]
            )
            self.assertEqual(
                [row["stage"] for row in saved["watch"]["ready-two"]["stages"]], ["T-30", "T-5"]
            )

    def test_successful_persistence_precedes_next_fixture_and_second_run_is_idempotent(self):
        now = dt.datetime.now(run_predict.HKT)
        with tempfile.TemporaryDirectory() as directory, \
             patch.object(run_predict, "HERE", directory), \
             patch.object(record_picks, "HERE", directory), \
             patch.object(record_picks, "LEDGER", str(Path(directory, "sim_ledger.json"))):
            ledger_path = Path(directory, "sim_ledger.json")
            ledger_path.write_text(json.dumps(self._ledger(now, [
                ("first", 4, [{"stage": "T-30"}]), ("second", 5, [{"stage": "T-30"}]),
            ])), encoding="utf-8")
            matches = [self._match("first", now, 4), self._match("second", now, 5)]
            proof = Path(directory, "second-saw-first")
            def analyse(row, *_args, stage_override=None, **_kwargs):
                if row["id"] == "second":
                    saved = json.loads(ledger_path.read_text(encoding="utf-8"))
                    if any(stage.get("stage") == "T-5" for stage in saved["watch"]["first"]["stages"]):
                        proof.write_text("yes", encoding="utf-8")
                return self._durable_result(row["id"], dt.datetime.fromisoformat(row["kickOffTime"])) | {"stage": stage_override}
            with patch.object(run_predict.H, "fetch_matches", return_value=matches), \
                 patch.object(run_predict, "analyse_match", side_effect=analyse), \
                 patch.object(run_predict, "pick_one", return_value=(None, "觀望")):
                first = run_predict.main(horizon_min=90)
                second = run_predict.main(horizon_min=90)
            self.assertEqual([row["match_id"] for row in first], ["first", "second"])
            self.assertEqual(second, [])
            self.assertEqual(proof.read_text(encoding="utf-8"), "yes")
            saved = json.loads(ledger_path.read_text(encoding="utf-8"))
            for match_id in ("first", "second"):
                self.assertEqual(
                    [stage["stage"] for stage in saved["watch"][match_id]["stages"]].count("T-5"), 1
                )
                stage = saved["watch"][match_id]["stages"][-1]
                self.assertEqual(stage["match_id"], match_id)
                self.assertTrue(stage["kickoff_at_utc"].endswith("+00:00"))
                self.assertTrue(stage["due_at_utc"].endswith("+00:00"))
            terminal = {
                (row["hkjc_match_id"], row["stage"]): row["status"]
                for row in saved["native_stage_attempts"]
                if row["status"] != "STARTED"
            }
            self.assertEqual(terminal[("first", "T-5")], "COMMITTED")
            self.assertEqual(terminal[("second", "T-5")], "COMMITTED")
            # No pick means no active bet and no duplicate notification-eligible native row.
            self.assertEqual(saved["bets"], [])

    def test_timeout_or_provider_error_writes_terminal_t5_evidence(self):
        now = dt.datetime.now(run_predict.HKT)
        with tempfile.TemporaryDirectory() as directory, patch.object(run_predict, "HERE", directory):
            path = Path(directory, "sim_ledger.json")
            path.write_text(json.dumps(self._ledger(now, [("retry", 5, [{"stage": "T-30"}])])) )
            with patch.object(run_predict.H, "fetch_matches", side_effect=TimeoutError):
                self.assertEqual(run_predict.main(horizon_min=90), [])
            saved = json.loads(path.read_text(encoding="utf-8"))
            self.assertEqual(
                saved["native_stage_attempts"][-1]["status"], "FAILED",
            )
            self.assertEqual(run_predict.pending_watch_match_ids(), [])

    def test_post_kickoff_result_is_never_persisted_as_native_t5(self):
        now = dt.datetime.now(run_predict.HKT)
        with tempfile.TemporaryDirectory() as directory, patch.object(run_predict, "HERE", directory):
            Path(directory, "sim_ledger.json").write_text(json.dumps(self._ledger(now, [("late", 5, [{"stage": "T-30"}])])) )
            match = self._match("late", now, 5)
            result = {"match_id": "late", "kickoff_hkt": match["kickOffTime"], "stage": "T-5", "candidates": []}
            with patch.object(run_predict.H, "fetch_matches", return_value=[match]), \
                 patch.object(run_predict, "analyse_match", return_value=result), \
                 patch.object(run_predict.H, "parse_kickoff", return_value=dt.datetime.now(run_predict.HKT) - dt.timedelta(seconds=1)), \
                 patch.object(run_predict, "_persist_urgent_result") as persist:
                self.assertEqual(run_predict.main(horizon_min=90), [])
            persist.assert_not_called()

    def test_hung_fixture_worker_is_terminated_before_pass_deadline(self):
        def hang(*_args, **_kwargs):
            import time
            time.sleep(2)
        deadline = __import__("time").monotonic() + 0.08
        with patch.object(run_predict, "analyse_match", side_effect=hang):
            started = __import__("time").monotonic()
            value = run_predict._bounded_due_call("analyse", ({}, None, "T-5", None), deadline)
        self.assertIsNone(value)
        self.assertLess(__import__("time").monotonic() - started, 0.7)

    def test_malformed_legacy_row_does_not_hide_valid_due_stage(self):
        now = dt.datetime.now(run_predict.HKT)
        with tempfile.TemporaryDirectory() as directory, patch.object(run_predict, "HERE", directory):
            Path(directory, "sim_ledger.json").write_text(json.dumps({"watch": {
                "legacy": {"kickoff": "not-a-date", "stages": [None, "T-5"]},
                "valid": {"kickoff": (now + dt.timedelta(minutes=5)).isoformat(), "stages": [{"stage": "T-30"}]},
            }}))
            self.assertEqual([row[1] for row in run_predict.persisted_due_stages()], ["valid"])


if __name__ == "__main__":
    unittest.main()
