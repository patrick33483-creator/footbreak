from __future__ import annotations

import time
import copy
import unittest
from contextlib import contextmanager
from dataclasses import replace
from datetime import datetime, timedelta
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

from crown import engine
from crown.common import HKT
from crown.config import settings


class StageAwarePendingDrainTests(unittest.TestCase):
    def test_deadline_bounds_optional_work_outside_state_lock(self):
        future = datetime.now(HKT) + timedelta(hours=2)
        ledger = {"bets": [], "watch": {}, "log": []}
        depth = {"value": 0}
        seen = {}

        @contextmanager
        def tracked_lock(*_args, **_kwargs):
            depth["value"] += 1
            try:
                yield True
            finally:
                depth["value"] -= 1

        def fake_sync(value, _prediction, _config, **_kwargs):
            value["watch"].setdefault("m1", {"stages": []})["stages"].append({
                "stage": "首預", "formal_admission_pending": True,
            })
            return []

        def slow_reconcile(value, _config):
            seen["lock_depth"] = depth["value"]
            time.sleep(0.12)
            value["watch"]["m1"]["stages"][0]["formal_admission_pending"] = False
            return []

        prediction = {
            "match_id": "m1", "kickoff_hkt": future.isoformat(),
            "stage": "首預", "status": "PREDICTION_READY",
        }
        with TemporaryDirectory() as directory:
            config = replace(settings(), state_dir=Path(directory))
            deadline = time.monotonic() + 0.02
            started = time.monotonic()
            with patch("crown.engine.state_lock", side_effect=tracked_lock), \
                 patch("crown.engine.load_ledger", return_value=ledger), \
                 patch("crown.engine.load_predictions", return_value=[]), \
                 patch("crown.engine.save_ledger"), \
                 patch("crown.engine.merge_predictions", return_value=[]), \
                 patch("crown.engine.sync_prediction", side_effect=fake_sync), \
                 patch(
                     "crown.engine.reconcile_pending_formal_admissions",
                     side_effect=slow_reconcile,
                 ):
                engine._commit_stage_predictions(
                    config, "tick", [prediction], deadline=deadline,
                )
            elapsed = time.monotonic() - started
        self.assertIn(seen.get("lock_depth"), (None, 0))
        self.assertLess(elapsed, 0.08)

    def test_empty_batch_still_attempts_bounded_pending_drain(self):
        with TemporaryDirectory() as directory:
            config = replace(settings(), state_dir=Path(directory))
            with patch("crown.engine.load_predictions", return_value=[]), \
                 patch(
                     "crown.engine.reconcile_pending_formal_admissions",
                     return_value=[],
                 ) as reconcile:
                result = engine._commit_stage_predictions(
                    config, "tick", [], deadline=time.monotonic() + 1,
                )
        self.assertEqual(result, ([], [], [], 0))
        reconcile.assert_called_once()

    def test_slow_recompute_is_inside_worker_deadline_and_never_saves(self):
        base = {
            "bets": [],
            "watch": {"m": {"stages": [{
                "stage": "T-5", "formal_admission_pending": True,
            }]}},
            "log": [],
        }
        def consume(staged, _config):
            staged["watch"]["m"]["stages"][0][
                "formal_admission_pending"
            ] = False
            return []

        def slow_recompute(staged, _config):
            time.sleep(0.12)
            staged["stats"] = {"done": True}
            return staged["stats"]

        with TemporaryDirectory() as directory:
            config = replace(settings(), state_dir=Path(directory))
            started = time.monotonic()
            with patch("crown.engine.state_lock", side_effect=lambda *_a, **_k: _lock()), \
                 patch("crown.engine.load_ledger", return_value=base), \
                 patch(
                     "crown.engine.reconcile_pending_formal_admissions",
                     side_effect=consume,
                 ), \
                 patch("crown.engine.recompute_stats", side_effect=slow_recompute), \
                 patch("crown.engine.save_ledger") as save:
                emitted = engine._drain_pending_formal_admissions(
                    config, deadline=started + 0.07,
                )
            elapsed = time.monotonic() - started
        self.assertLess(elapsed, 0.07)
        self.assertEqual(emitted, [])
        save.assert_not_called()

    def test_slow_final_atomic_save_cannot_publish_t5_across_kickoff(self):
        kickoff = datetime.now(HKT) + timedelta(seconds=0.25)
        snapshot_id = "a" * 64
        base = {
            "bets": [], "log": [],
            "watch": {"m": {
                "match_id": "m", "kickoff": kickoff.isoformat(),
                "kickoff_hkt": kickoff.isoformat(),
                "stages": [{
                    "stage": "T-5", "formal_admission_pending": True,
                    "formal_admission_status": "PENDING",
                    "formal_admission_snapshot_id": snapshot_id,
                }],
            }},
        }
        def consume(staged, _config):
            row = staged["watch"]["m"]["stages"][0]
            row["formal_admission_pending"] = False
            row["formal_admission_status"] = "COMPLETED"
            staged["bets"].append({"bet_id": "must-not-persist"})
            return ["must-not-persist"]

        saved = {}
        def slow_save(_config, value):
            time.sleep(0.35)
            saved["ledger"] = copy.deepcopy(value)
            saved["at"] = datetime.now(HKT)

        with TemporaryDirectory() as directory:
            config = replace(settings(), state_dir=Path(directory))
            with patch("crown.engine.state_lock", side_effect=lambda *_a, **_k: _lock()), \
                 patch("crown.engine.load_ledger", return_value=base), \
                 patch(
                     "crown.engine.reconcile_pending_formal_admissions",
                     side_effect=consume,
                 ), \
                 patch("crown.engine.recompute_stats", return_value={}), \
                 patch("crown.engine.save_ledger", side_effect=slow_save):
                started = time.monotonic()
                emitted = engine._drain_pending_formal_admissions(
                    config, deadline=time.monotonic() + 1,
                )
                elapsed = time.monotonic() - started
        self.assertEqual(emitted, [])
        self.assertLess(elapsed, 0.30)
        self.assertNotIn("ledger", saved)


@contextmanager
def _lock():
    yield True


if __name__ == "__main__":
    unittest.main()
