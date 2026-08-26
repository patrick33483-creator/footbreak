"""Regression tests for native-first persistence and deferred consumers."""
from __future__ import annotations

import json
import os
import sys
import tempfile
import time
import unittest
from datetime import datetime, timedelta
from pathlib import Path
from unittest.mock import patch

SYSTEM = Path(__file__).resolve().parents[1]
if str(SYSTEM) not in sys.path:
    sys.path.insert(0, str(SYSTEM))

import native_stage_state as stage_state
import record_picks
import run_predict


class NativePostCommitJobTests(unittest.TestCase):
    def _result(self, kickoff, attempt_id, *, stage="T-5"):
        return {
            "match_id": "m1", "stage": stage, "kickoff_hkt": kickoff.isoformat(),
            "league": "測試聯賽", "home": "主隊", "away": "客隊",
            "conviction": 60.0, "model_source": "pinnapi",
            "sharp_reference_available": True, "can_bet": stage == "T-5",
            "candidates": [{
                "market": "入球大小", "code": "HIL", "condition": 2.5, "line": 2.5,
                "side": "L", "label": "細 2.5", "prob": .55, "push": 0.0,
                "odds": 1.91, "ev": -.01, "kelly_used": 0.0, "is_main": True,
                "source": "hkjc_public_board",
                "observed_at": (kickoff - timedelta(minutes=6)).isoformat(),
            }],
            "pick": None, "lead_view": None, "no_bet_reason": "測試",
            "weather": {}, "final": {}, "open": {}, "now": {}, "movement": {},
            "adjustments": [], "mults": {}, "outcome": {},
            "_native_stage_attempt_id": attempt_id,
        }

    def _ledger_with_started_attempt(self, kickoff, stage="T-5"):
        watch = {
            "match_id": "m1", "league": "測試聯賽", "home": "主隊", "away": "客隊",
            "kickoff": kickoff.isoformat(), "stages": [],
        }
        now = kickoff - timedelta(minutes=10)
        self.assertTrue(stage_state.ensure_manifest(
            watch, origin="first_look", now=now - timedelta(minutes=30),
        ))
        ledger = {"watch": {"m1": watch}}
        attempt = stage_state.start_attempt(ledger, watch, stage, now=now)
        return ledger, attempt

    def _run_sync(self, directory):
        return record_picks.sync("predictions.json", send_notifications=False)

    def test_timed_native_snapshot_and_commit_are_durable_before_sidecar(self):
        kickoff = datetime.now(record_picks.HKT) + timedelta(minutes=20)
        ledger, attempt = self._ledger_with_started_attempt(kickoff, stage="T-5")
        result = self._result(kickoff, attempt["attempt_id"])
        with tempfile.TemporaryDirectory() as directory:
            ledger_path = Path(directory, "sim_ledger.json")
            ledger_path.write_text(json.dumps(ledger), encoding="utf-8")
            Path(directory, "predictions.json").write_text(json.dumps([result]), encoding="utf-8")
            observed = {}

            def sidecar_after_native_commit(*_args, **_kwargs):
                disk = json.loads(ledger_path.read_text(encoding="utf-8"))
                observed["stage"] = disk["watch"]["m1"]["stages"][0]["stage"]
                observed["attempt"] = disk["native_stage_attempts"][-1]["status"]
                observed["job"] = disk["native_post_commit_jobs"][-1]["status"]
                raise RuntimeError("sidecar unavailable")

            with patch.object(record_picks, "HERE", directory), \
                 patch.object(record_picks, "LEDGER", str(ledger_path)), \
                 patch.object(record_picks, "capture_t5_counterparts", side_effect=sidecar_after_native_commit), \
                 patch.object(record_picks, "evaluate_new_t5", return_value=([], [])), \
                 patch.object(record_picks, "evaluate_probability_research", return_value=([], [])), \
                 patch.object(record_picks, "evaluate_crown_execution_t5", return_value=([], [])):
                self._run_sync(directory)

            self.assertEqual(observed, {"stage": "T-5", "attempt": "COMMITTED", "job": "PENDING"})
            saved = json.loads(ledger_path.read_text(encoding="utf-8"))
            self.assertEqual(saved["watch"]["m1"]["stages"][0]["stage"], "T-5")
            self.assertEqual(saved["native_stage_attempts"][-1]["status"], "COMMITTED")
            self.assertEqual(saved["native_post_commit_jobs"][-1]["status"], "COMPLETED")

    def test_t5_job_uses_fixed_twenty_second_cutoff_and_waits_without_sleeping(self):
        kickoff = datetime.now(record_picks.HKT) + timedelta(minutes=20)
        ledger, attempt = self._ledger_with_started_attempt(kickoff, stage="T-5")
        snapshot = record_picks._snap(
            self._result(kickoff, attempt["attempt_id"]), datetime.now(record_picks.HKT).isoformat(),
        )
        stage_state.enrich_snapshot(snapshot, ledger["watch"]["m1"], "T-5")
        ledger["watch"]["m1"]["stages"].append(snapshot)
        job = record_picks._enqueue_optional_job(
            ledger, "m1", snapshot, self._result(kickoff, attempt["attempt_id"]),
            now=snapshot["ts"], t5_safe_to_evaluate=True,
        )
        deadline = datetime.fromisoformat(job["cross_book_deadline_at"])
        self.assertEqual(
            deadline, datetime.fromisoformat(snapshot["ts"]) + timedelta(seconds=20),
        )
        record_picks.ensure_namespace(ledger, "footbreak")
        pending = {
            "HIL": {
                "status": "PENDING", "reason": "crown_counterpart_grace_pending",
            },
        }
        with patch.object(record_picks, "_record_learning_snapshot", return_value=None), \
             patch.object(record_picks, "_load_frozen_ranking", return_value=[]), \
             patch.object(record_picks, "evaluate_new_t5", return_value=([], [])) as native, \
             patch.object(record_picks, "evaluate_probability_research", return_value=([], [])), \
             patch.object(record_picks, "capture_t5_counterparts", return_value=pending) as capture, \
             patch.object(record_picks, "evaluate_crown_execution_t5") as cross:
            outcome = record_picks._process_optional_job(
                ledger, job, now=snapshot["ts"], changes=[],
            )
        self.assertEqual(outcome, "DEFERRED")
        native.assert_called_once()
        capture.assert_called_once()
        self.assertEqual(
            capture.call_args.kwargs["grace_deadline_at"],
            job["cross_book_deadline_at"],
        )
        cross.assert_not_called()

    def test_tampered_native_snapshot_is_rejected_before_optional_consumers(self):
        kickoff = datetime.now(record_picks.HKT) + timedelta(minutes=20)
        ledger, attempt = self._ledger_with_started_attempt(kickoff, stage="T-5")
        result = self._result(kickoff, attempt["attempt_id"])
        snapshot = record_picks._snap(
            result, datetime.now(record_picks.HKT).isoformat(),
        )
        stage_state.enrich_snapshot(snapshot, ledger["watch"]["m1"], "T-5")
        ledger["watch"]["m1"]["stages"].append(snapshot)
        job = record_picks._enqueue_optional_job(
            ledger, "m1", snapshot, result, now=snapshot["ts"],
            t5_safe_to_evaluate=True,
        )
        snapshot["market_predictions"][0]["odds"] = 9.99
        with patch.object(record_picks, "evaluate_new_t5") as native:
            with self.assertRaisesRegex(ValueError, "native_snapshot_hash_mismatch"):
                record_picks._process_optional_job(
                    ledger, job, now=snapshot["ts"], changes=[],
                )
        native.assert_not_called()

    def test_legacy_nonterminal_t5_job_is_pinned_to_snapshot_cutoff(self):
        stage_at = datetime.now(record_picks.HKT)
        kickoff = stage_at + timedelta(minutes=20)
        snapshot = {
            "stage": "T-5", "ts": stage_at.isoformat(),
            "kickoff": kickoff.isoformat(), "native_snapshot_id": "legacy:t5",
            "market_predictions": [],
        }
        ledger = {
            "watch": {"m1": {
                "match_id": "m1", "kickoff": kickoff.isoformat(),
                "league": "測試聯賽", "home": "主隊", "away": "客隊",
                "stages": [snapshot],
            }},
            "native_post_commit_jobs": [{
                "schema_version": 1, "job_id": "m1:legacy:t5",
                "status": "PENDING", "match_id": "m1", "stage": "T-5",
                "snapshot_id": "legacy:t5", "t5_safe_to_evaluate": True,
            }],
        }
        record_picks.ensure_namespace(ledger, "footbreak")
        with patch.object(record_picks, "_process_optional_job", return_value="DEFERRED"):
            record_picks._drain_optional_jobs(
                ledger, now=(stage_at + timedelta(seconds=5)).isoformat(),
                changes=[], notes=[],
            )
        latest = ledger["native_post_commit_jobs"][-1]
        self.assertEqual(latest["status"], "WAITING")
        self.assertEqual(
            datetime.fromisoformat(latest["cross_book_deadline_at"]),
            stage_at + timedelta(seconds=20),
        )

    def test_first_look_wilson_observation_runs_after_commit_without_changes(self):
        kickoff = datetime.now(record_picks.HKT) + timedelta(hours=2)
        ledger = {
            "watch": {
                "m1": {
                    "match_id": "m1", "league": "測試聯賽",
                    "home": "主隊", "away": "客隊",
                    "kickoff": kickoff.isoformat(), "stages": [],
                },
            },
        }
        result = self._result(kickoff, "", stage="首預")
        with tempfile.TemporaryDirectory() as directory:
            ledger_path = Path(directory, "sim_ledger.json")
            ledger_path.write_text(json.dumps(ledger), encoding="utf-8")
            Path(directory, "predictions.json").write_text(
                json.dumps([result]), encoding="utf-8",
            )
            durable = []

            def evaluate_after_save(_ledger, watch, stage, **_kwargs):
                saved = json.loads(ledger_path.read_text(encoding="utf-8"))
                durable.append((
                    saved["watch"]["m1"]["stages"][0]["stage"],
                    saved["native_post_commit_jobs"][-1]["status"],
                    stage,
                ))
                _ledger["wilson_validation"].setdefault("observations", []).append({
                    "observation_id": "early-formal-once",
                    "match_id": "m1", "stage": "首預", "formal_bet": False,
                })
                return [], []

            with patch.object(record_picks, "HERE", directory), \
                 patch.object(record_picks, "LEDGER", str(ledger_path)), \
                 patch.object(record_picks, "prefetch_crown_bridge", return_value={}), \
                 patch.object(
                     record_picks, "evaluate_wilson_stage",
                     side_effect=evaluate_after_save,
                 ) as evaluate:
                changes, _notes, _ = self._run_sync(directory)
                self._run_sync(directory)
            self.assertEqual(durable, [("首預", "PENDING", "首預")])
            self.assertEqual(evaluate.call_count, 1)
            self.assertEqual(changes, [])
            saved = json.loads(ledger_path.read_text(encoding="utf-8"))
            self.assertEqual(saved["bets"], [])
            self.assertEqual(
                saved["wilson_validation"]["observations"][0]["observation_id"],
                "early-formal-once",
            )

    def test_evaluator_crash_retries_from_committed_snapshot_once_without_missing_observation(self):
        kickoff = datetime.now(record_picks.HKT) + timedelta(minutes=20)
        ledger, attempt = self._ledger_with_started_attempt(kickoff, stage="T-5")
        result = self._result(kickoff, attempt["attempt_id"])
        with tempfile.TemporaryDirectory() as directory:
            ledger_path = Path(directory, "sim_ledger.json")
            ledger_path.write_text(json.dumps(ledger), encoding="utf-8")
            Path(directory, "predictions.json").write_text(json.dumps([result]), encoding="utf-8")
            with patch.object(record_picks, "HERE", directory), \
                 patch.object(record_picks, "LEDGER", str(ledger_path)), \
                 patch.object(record_picks, "capture_t5_counterparts", return_value={}), \
                 patch.object(record_picks, "evaluate_new_t5", side_effect=RuntimeError("matcher crash")):
                _, notes, _ = self._run_sync(directory)
            self.assertTrue(any("native snapshot remains committed" in note for note in notes))
            crashed = json.loads(ledger_path.read_text(encoding="utf-8"))
            self.assertEqual(crashed["native_stage_attempts"][-1]["status"], "COMMITTED")
            self.assertEqual(crashed["native_post_commit_jobs"][-1]["status"], "RETRYABLE_FAILURE")
            self.assertEqual(crashed["watch"]["m1"]["stages"][0]["stage"], "T-5")

            calls = []

            def formal_observation(led, watch, **_kwargs):
                calls.append(watch["stages"][-1]["native_snapshot_id"])
                led["wilson_validation"].setdefault("observations", []).append({
                    "match_id": "m1", "stage": "T-5",
                    "created_at": watch["stages"][-1]["ts"], "code": "HIL",
                    "observation_id": "formal-once",
                })
                return [], []

            with patch.object(record_picks, "HERE", directory), \
                 patch.object(record_picks, "LEDGER", str(ledger_path)), \
                 patch.object(record_picks, "capture_t5_counterparts", return_value={}), \
                 patch.object(record_picks, "evaluate_new_t5", side_effect=formal_observation), \
                 patch.object(record_picks, "evaluate_probability_research", return_value=([], [])), \
                 patch.object(record_picks, "evaluate_crown_execution_t5", return_value=([], [])):
                self._run_sync(directory)
                self._run_sync(directory)

            saved = json.loads(ledger_path.read_text(encoding="utf-8"))
            self.assertEqual(len(calls), 1, saved["native_post_commit_jobs"])
            self.assertEqual(saved["native_post_commit_jobs"][-1]["status"], "COMPLETED")
            observations = saved["wilson_validation"]["observations"]
            self.assertEqual([row["observation_id"] for row in observations], ["formal-once"])

    @unittest.skipUnless(os.name == "posix", "bounded urgent worker requires fork")
    def test_optional_timeout_cannot_rollback_native_commit_and_is_restart_retryable(self):
        kickoff = datetime.now(record_picks.HKT) + timedelta(minutes=20)
        ledger, attempt = self._ledger_with_started_attempt(kickoff, stage="T-5")
        result = self._result(kickoff, attempt["attempt_id"])
        with tempfile.TemporaryDirectory() as directory:
            ledger_path = Path(directory, "sim_ledger.json")
            ledger_path.write_text(json.dumps(ledger), encoding="utf-8")

            def slow_evaluator(*_args, **_kwargs):
                time.sleep(2)
                return [], []

            with patch.object(run_predict, "HERE", directory), \
                 patch.object(record_picks, "HERE", directory), \
                 patch.object(record_picks, "LEDGER", str(ledger_path)), \
                 patch.object(record_picks, "capture_t5_counterparts", return_value={}), \
                 patch.object(record_picks, "evaluate_new_t5", side_effect=slow_evaluator), \
                 patch.dict(os.environ, {"FOOTBREAK_URGENT_CALL_TIMEOUT_SECONDS": "0.15"}, clear=False):
                completed = run_predict._bounded_due_call(
                    "persist", result, time.monotonic() + 0.3,
                )
            self.assertIsNone(completed)
            timed_out = json.loads(ledger_path.read_text(encoding="utf-8"))
            self.assertEqual(timed_out["watch"]["m1"]["stages"][0]["stage"], "T-5")
            self.assertEqual(timed_out["native_stage_attempts"][-1]["status"], "COMMITTED")
            self.assertEqual(timed_out["native_post_commit_jobs"][-1]["status"], "PENDING")

            with patch.object(record_picks, "HERE", directory), \
                 patch.object(record_picks, "LEDGER", str(ledger_path)), \
                 patch.object(record_picks, "capture_t5_counterparts", return_value={}), \
                 patch.object(record_picks, "evaluate_new_t5", return_value=([], [])), \
                 patch.object(record_picks, "evaluate_probability_research", return_value=([], [])), \
                 patch.object(record_picks, "evaluate_crown_execution_t5", return_value=([], [])):
                Path(directory, "predictions.json").write_text(json.dumps([result]), encoding="utf-8")
                self._run_sync(directory)
            retried = json.loads(ledger_path.read_text(encoding="utf-8"))
            self.assertEqual(len(retried["watch"]["m1"]["stages"]), 1)
            self.assertEqual(retried["native_post_commit_jobs"][-1]["status"], "COMPLETED")

    def test_post_kickoff_optional_job_expires_without_evaluator_or_stage_backfill(self):
        kickoff = datetime.now(record_picks.HKT) - timedelta(minutes=1)
        snapshot = {
            "stage": "T-5", "native_snapshot_id": "snapshot:m1:T-5:old",
            "kickoff": kickoff.isoformat(), "ts": (kickoff - timedelta(minutes=5)).isoformat(),
        }
        ledger = {
            "watch": {"m1": {
                "match_id": "m1", "kickoff": kickoff.isoformat(), "league": "測試聯賽",
                "home": "主隊", "away": "客隊", "stages": [snapshot],
            }},
            "native_post_commit_jobs": [{
                "schema_version": 1, "job_id": "m1:snapshot:m1:T-5:old",
                "status": "PENDING", "match_id": "m1", "stage": "T-5",
                "snapshot_id": snapshot["native_snapshot_id"], "t5_safe_to_evaluate": True,
            }],
        }
        record_picks.ensure_namespace(ledger, "footbreak")
        with patch.object(record_picks, "evaluate_new_t5") as evaluate:
            record_picks._drain_optional_jobs(
                ledger, now=datetime.now(record_picks.HKT).isoformat(),
                changes=[], notes=[],
            )
        evaluate.assert_not_called()
        self.assertEqual(len(ledger["watch"]["m1"]["stages"]), 1)
        self.assertEqual(ledger["native_post_commit_jobs"][-1]["status"], "EXPIRED")
        self.assertEqual(
            ledger["wilson_validation"]["audit"][-1]["reason"],
            "optional_job_expired_before_native_evaluation",
        )

    def test_post_kickoff_result_expires_started_attempt_without_creating_snapshot_or_job(self):
        kickoff = datetime.now(record_picks.HKT) - timedelta(minutes=1)
        ledger, attempt = self._ledger_with_started_attempt(kickoff, stage="T-5")
        result = self._result(kickoff, attempt["attempt_id"])
        with tempfile.TemporaryDirectory() as directory:
            ledger_path = Path(directory, "sim_ledger.json")
            ledger_path.write_text(json.dumps(ledger), encoding="utf-8")
            Path(directory, "predictions.json").write_text(json.dumps([result]), encoding="utf-8")
            with patch.object(record_picks, "HERE", directory), \
                 patch.object(record_picks, "LEDGER", str(ledger_path)), \
                 patch.object(record_picks, "evaluate_new_t5") as evaluate:
                self._run_sync(directory)
            saved = json.loads(ledger_path.read_text(encoding="utf-8"))
        evaluate.assert_not_called()
        self.assertEqual(saved["watch"]["m1"]["stages"], [])
        self.assertEqual(saved["native_stage_attempts"][-1]["status"], "EXPIRED")
        self.assertNotIn("native_post_commit_jobs", saved)


if __name__ == "__main__":
    unittest.main()
