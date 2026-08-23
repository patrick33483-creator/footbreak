"""Focused tests for Stage 5: crown/native_stage_cutover.py and
crown/native_stage_deferred_projection.py.

Every test constructs its own temporary state dir -- no test here ever
touches a real environment variable's production value persistently (all
``os.environ`` mutations are wrapped in try/finally restore), a real
production path, or the network. All fixture identities and timestamps are
synthetic. No provider call, Telegram call, betting action, or workflow
dispatch happens anywhere in this file.
"""
from __future__ import annotations

import os
import tempfile
import unittest
from dataclasses import replace
from datetime import datetime, timedelta
from pathlib import Path
from unittest.mock import patch

from crown.common import HKT
from crown.config import settings
from crown import native_stage_store as store_mod
from crown import native_stage_cutover as cutover
from crown import native_stage_deferred_projection as dq


def _config(state_dir: Path):
    return replace(settings(), state_dir=state_dir)


def _kickoff(minutes_from_now: int, now: datetime) -> datetime:
    return now + timedelta(minutes=minutes_from_now)


def _prediction(match_id: str, stage: str, kickoff: datetime, status: str = "OK") -> dict:
    return {
        "match_id": match_id,
        "stage": stage,
        "kickoff_hkt": kickoff.isoformat(),
        "status": status,
        "league": "L", "home": "H", "away": "A",
        "forecast_candidates": [
            {"code": "HDC", "line": "-0.25", "side": "H", "odds": 1.91, "source": "s"},
        ],
        "collection_attempt": {"source": "titan007-crown-id-3", "reason": None},
        "generated_at": "2026-08-24T00:00:00+08:00",
    }


class FlagsTestCase(unittest.TestCase):
    """Base class that snapshots and restores every Stage 5 flag env var."""

    ENV_KEYS = (
        store_mod.ENV_ENABLED,
        cutover.ENV_CUTOVER_ENABLED,
        cutover.ENV_DEFERRED_PROJECTION_ENABLED,
    )

    def setUp(self):
        self._saved = {key: os.environ.get(key) for key in self.ENV_KEYS}
        for key in self.ENV_KEYS:
            os.environ.pop(key, None)

    def tearDown(self):
        for key, value in self._saved.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value

    def _set(self, store: bool = False, cutover_on: bool = False, deferred: bool = False):
        os.environ[store_mod.ENV_ENABLED] = "1" if store else "0"
        os.environ[cutover.ENV_CUTOVER_ENABLED] = "1" if cutover_on else "0"
        os.environ[cutover.ENV_DEFERRED_PROJECTION_ENABLED] = "1" if deferred else "0"


class DefaultOffEquivalenceTests(FlagsTestCase):
    """Requirement: any flag off => behaviour identical to unmodified base."""

    def test_all_flags_off_calls_project_fn_inline_and_writes_nothing(self):
        with tempfile.TemporaryDirectory() as tmp:
            self._set(store=False, cutover_on=False, deferred=False)
            config = _config(Path(tmp))
            now = datetime.now(HKT)
            calls = []
            result = cutover.commit_fixture_result(
                config, _prediction("m1", "T-5", _kickoff(10, now)), calls.append, now=now,
            )
            self.assertEqual(result.projection, "inline")
            self.assertIsNone(result.native_state)
            self.assertEqual(len(calls), 1)
            self.assertFalse((Path(tmp) / "native_stage").exists())
            self.assertFalse((Path(tmp) / "native_stage_projection_queue").exists())

    def test_store_on_but_cutover_off_still_projects_inline(self):
        """Stage 1/2 shadow-equivalent: native commit happens, legacy still inline."""
        with tempfile.TemporaryDirectory() as tmp:
            self._set(store=True, cutover_on=False, deferred=False)
            config = _config(Path(tmp))
            now = datetime.now(HKT)
            calls = []
            result = cutover.commit_fixture_result(
                config, _prediction("m1", "T-5", _kickoff(10, now)), calls.append, now=now,
            )
            self.assertEqual(result.projection, "inline")
            self.assertEqual(result.native_state, "COMMITTED")
            self.assertEqual(len(calls), 1, "legacy projection must still run inline when cutover flag is off")

    def test_cutover_on_but_deferred_off_still_projects_inline(self):
        with tempfile.TemporaryDirectory() as tmp:
            self._set(store=True, cutover_on=True, deferred=False)
            config = _config(Path(tmp))
            now = datetime.now(HKT)
            calls = []
            result = cutover.commit_fixture_result(
                config, _prediction("m1", "T-5", _kickoff(10, now)), calls.append, now=now,
            )
            self.assertEqual(result.native_state, "COMMITTED")
            self.assertEqual(result.projection, "inline")
            self.assertEqual(len(calls), 1)

    def test_drain_is_noop_when_deferred_projection_flag_off(self):
        with tempfile.TemporaryDirectory() as tmp:
            self._set(store=True, cutover_on=True, deferred=False)
            config = _config(Path(tmp))
            outcomes = cutover.drain_deferred_projections(config, lambda p: None)
            self.assertEqual(outcomes, [])

    def test_flags_matrix_never_raises(self):
        """Every one of the 8 flag combinations must be safely callable."""
        with tempfile.TemporaryDirectory() as tmp:
            now = datetime.now(HKT)
            for store_on in (False, True):
                for cutover_on in (False, True):
                    for deferred_on in (False, True):
                        with self.subTest(store=store_on, cutover=cutover_on, deferred=deferred_on):
                            self._set(store=store_on, cutover_on=cutover_on, deferred=deferred_on)
                            config = _config(Path(tmp) / f"{store_on}{cutover_on}{deferred_on}")
                            calls = []
                            mid = f"m-{store_on}-{cutover_on}-{deferred_on}"
                            result = cutover.commit_fixture_result(
                                config, _prediction(mid, "T-5", _kickoff(10, now)), calls.append, now=now,
                            )
                            self.assertIsNotNone(result)


class CutoverCommitTests(FlagsTestCase):
    """Cutover-on behaviour: native commit is immediate, legacy is deferred."""

    def test_committed_snapshot_is_deferred_not_inline(self):
        with tempfile.TemporaryDirectory() as tmp:
            self._set(store=True, cutover_on=True, deferred=True)
            config = _config(Path(tmp))
            now = datetime.now(HKT)
            calls = []
            result = cutover.commit_fixture_result(
                config, _prediction("m1", "T-5", _kickoff(10, now)), calls.append, now=now,
            )
            self.assertEqual(result.native_state, "COMMITTED")
            self.assertEqual(result.projection, "deferred")
            self.assertEqual(len(calls), 0, "legacy projection must not run inline in cutover+deferred mode")
            store = store_mod.NativeStageStore(config.state_dir)
            native = store.read("m1")
            self.assertIn("T-5", native["snapshots"])
            queue = dq.DeferredProjectionQueue(config.state_dir)
            pending = queue.pending_items()
            self.assertEqual(len(pending), 1)
            self.assertEqual(pending[0]["match_id"], "m1")

    def test_data_missing_maps_to_native_data_missing_state(self):
        with tempfile.TemporaryDirectory() as tmp:
            self._set(store=True, cutover_on=True, deferred=True)
            config = _config(Path(tmp))
            now = datetime.now(HKT)
            result = cutover.commit_fixture_result(
                config, _prediction("m1", "T-5", _kickoff(10, now), status="DATA_MISSING"),
                lambda p: None, now=now,
            )
            self.assertEqual(result.native_state, "DATA_MISSING")

    def test_post_kickoff_expires_natively_and_never_backfills_via_drain(self):
        with tempfile.TemporaryDirectory() as tmp:
            self._set(store=True, cutover_on=True, deferred=True)
            config = _config(Path(tmp))
            now = datetime.now(HKT)
            past_kickoff = now - timedelta(seconds=5)
            calls = []
            result = cutover.commit_fixture_result(
                config, _prediction("m1", "T-5", past_kickoff), calls.append, now=now,
            )
            self.assertEqual(result.native_state, "EXPIRED")
            outcomes = cutover.drain_deferred_projections(config, calls.append, )
            self.assertTrue(any(o.state == "EXPIRED" for o in outcomes))
            self.assertEqual(len(calls), 0, "a post-kickoff fixture must never reach legacy project_fn")

    def test_t30_and_t5_never_overwrite_each_other_in_native_store(self):
        with tempfile.TemporaryDirectory() as tmp:
            self._set(store=True, cutover_on=True, deferred=True)
            config = _config(Path(tmp))
            now = datetime.now(HKT)
            kickoff = _kickoff(40, now)
            cutover.commit_fixture_result(config, _prediction("m1", "T-30", kickoff), lambda p: None, now=now)
            cutover.commit_fixture_result(config, _prediction("m1", "T-5", kickoff), lambda p: None, now=now)
            store = store_mod.NativeStageStore(config.state_dir)
            state = store.read("m1")
            self.assertIn("T-30", state["snapshots"])
            self.assertIn("T-5", state["snapshots"])

    def test_duplicate_commit_for_same_stage_is_idempotent(self):
        with tempfile.TemporaryDirectory() as tmp:
            self._set(store=True, cutover_on=True, deferred=True)
            config = _config(Path(tmp))
            now = datetime.now(HKT)
            kickoff = _kickoff(10, now)
            r1 = cutover.commit_fixture_result(config, _prediction("m1", "T-5", kickoff), lambda p: None, now=now)
            r2 = cutover.commit_fixture_result(config, _prediction("m1", "T-5", kickoff), lambda p: None, now=now)
            self.assertEqual(r1.native_state, "COMMITTED")
            self.assertEqual(r2.native_state, "COMMITTED")
            store = store_mod.NativeStageStore(config.state_dir)
            state = store.read("m1")
            committed_attempts = [
                a for a in state["attempt_history"]
                if a.get("stage") == "T-5" and a.get("state") == "COMMITTED"
            ]
            self.assertEqual(len(committed_attempts), 1, "must not create a second COMMITTED attempt")

    def test_native_store_exception_falls_back_to_inline_projection(self):
        """A native-store failure must never block or skip the legacy commit."""
        with tempfile.TemporaryDirectory() as tmp:
            self._set(store=True, cutover_on=True, deferred=True)
            config = _config(Path(tmp))
            now = datetime.now(HKT)
            calls = []
            with patch.object(cutover, "_commit_native", side_effect=RuntimeError("disk full")):
                result = cutover.commit_fixture_result(
                    config, _prediction("m1", "T-5", _kickoff(10, now)), calls.append, now=now,
                )
            self.assertIsNone(result.native_state)
            self.assertEqual(result.projection, "inline")
            self.assertEqual(len(calls), 1, "legacy projection must still happen when native commit raises")

    def test_deferred_queue_enqueue_exception_falls_back_to_inline(self):
        with tempfile.TemporaryDirectory() as tmp:
            self._set(store=True, cutover_on=True, deferred=True)
            config = _config(Path(tmp))
            now = datetime.now(HKT)
            calls = []
            with patch.object(dq.DeferredProjectionQueue, "enqueue", side_effect=RuntimeError("io error")):
                result = cutover.commit_fixture_result(
                    config, _prediction("m1", "T-5", _kickoff(10, now)), calls.append, now=now,
                )
            self.assertEqual(result.native_state, "COMMITTED")
            self.assertEqual(result.projection, "inline_fallback_after_deferred_error")
            self.assertEqual(len(calls), 1, "queue failure must not drop the legacy projection")


class MultiFixtureBatchTests(FlagsTestCase):
    """Requirement: 1/15/26/50/89 fixtures due simultaneously, out-of-order completion."""

    def _run_batch(self, tmp: Path, count: int, out_of_order: bool = False):
        self._set(store=True, cutover_on=True, deferred=True)
        config = _config(tmp)
        now = datetime.now(HKT)
        kickoff = _kickoff(10, now)
        ids = [f"F{i}" for i in range(count)]
        if out_of_order:
            ids = list(reversed(ids))
        legacy_calls = []
        native_states = {}
        for mid in ids:
            result = cutover.commit_fixture_result(
                config, _prediction(mid, "T-5", kickoff), legacy_calls.append, now=now,
            )
            native_states[mid] = result.native_state
        return config, native_states, legacy_calls

    def test_batch_sizes_1_15_26_50_89_all_commit_natively_without_inline_legacy(self):
        for count in (1, 15, 26, 50, 89):
            with self.subTest(count=count):
                with tempfile.TemporaryDirectory() as tmp:
                    config, native_states, legacy_calls = self._run_batch(Path(tmp), count)
                    self.assertEqual(len(native_states), count)
                    self.assertTrue(all(state == "COMMITTED" for state in native_states.values()))
                    self.assertEqual(len(legacy_calls), 0, "no fixture should hit inline legacy path in cutover+deferred mode")
                    queue = dq.DeferredProjectionQueue(config.state_dir)
                    self.assertEqual(len(queue.pending_items()), count)

    def test_out_of_order_completion_still_commits_every_fixture_independently(self):
        with tempfile.TemporaryDirectory() as tmp:
            config, native_states, _ = self._run_batch(Path(tmp), 26, out_of_order=True)
            self.assertEqual(len(native_states), 26)
            self.assertTrue(all(v == "COMMITTED" for v in native_states.values()))

    def test_full_batch_drains_every_queued_item_exactly_once(self):
        with tempfile.TemporaryDirectory() as tmp:
            config, _, _ = self._run_batch(Path(tmp), 50)
            drained = []
            outcomes = cutover.drain_deferred_projections(config, drained.append)
            self.assertEqual(len(outcomes), 50)
            self.assertTrue(all(o.state == "COMPLETED" for o in outcomes))
            self.assertEqual(len(drained), 50)
            self.assertEqual(len({d["match_id"] for d in drained}), 50)
            # Second drain must be a no-op (idempotent, all terminal now).
            outcomes2 = cutover.drain_deferred_projections(config, drained.append)
            self.assertEqual(outcomes2, [])
            self.assertEqual(len(drained), 50)


class DeferredProjectionQueueTests(unittest.TestCase):
    """Direct coverage of crown/native_stage_deferred_projection.py primitives."""

    def test_enqueue_is_idempotent_and_never_overwrites_pending_payload(self):
        with tempfile.TemporaryDirectory() as tmp:
            queue = dq.DeferredProjectionQueue(Path(tmp))
            now = datetime.now(HKT)
            kickoff = _kickoff(10, now)
            item1 = queue.enqueue("m1", "T-5", kickoff=kickoff, payload={"match_id": "m1", "stage": "T-5", "v": 1})
            item2 = queue.enqueue("m1", "T-5", kickoff=kickoff, payload={"match_id": "m1", "stage": "T-5", "v": 2})
            self.assertEqual(item1["payload"]["v"], 1)
            self.assertEqual(item2["payload"]["v"], 1)

    def test_drain_isolates_one_item_exception_from_others(self):
        with tempfile.TemporaryDirectory() as tmp:
            queue = dq.DeferredProjectionQueue(Path(tmp))
            now = datetime.now(HKT)
            kickoff = _kickoff(10, now)
            queue.enqueue("bad", "T-5", kickoff=kickoff, payload={"match_id": "bad", "stage": "T-5"})
            queue.enqueue("good", "T-5", kickoff=kickoff, payload={"match_id": "good", "stage": "T-5"})

            def project_fn(payload):
                if payload["match_id"] == "bad":
                    raise RuntimeError("boom")

            outcomes = queue.drain(project_fn)
            states = {o.match_id: o.state for o in outcomes}
            self.assertEqual(states["good"], dq.COMPLETED)
            self.assertEqual(states["bad"], dq.PENDING)  # retryable, not yet frozen

    def test_restart_drain_resumes_from_disk_state(self):
        """A fresh DeferredProjectionQueue instance sees prior pending items."""
        with tempfile.TemporaryDirectory() as tmp:
            now = datetime.now(HKT)
            kickoff = _kickoff(10, now)
            queue_a = dq.DeferredProjectionQueue(Path(tmp))
            queue_a.enqueue("m1", "T-5", kickoff=kickoff, payload={"match_id": "m1", "stage": "T-5"})
            # Simulate a process restart: brand-new instance, same directory.
            queue_b = dq.DeferredProjectionQueue(Path(tmp))
            pending = queue_b.pending_items()
            self.assertEqual(len(pending), 1)
            calls = []
            outcomes = queue_b.drain(calls.append)
            self.assertEqual(outcomes[0].state, dq.COMPLETED)
            self.assertEqual(len(calls), 1)

    def test_drain_idempotent_dedup_across_restarts(self):
        with tempfile.TemporaryDirectory() as tmp:
            now = datetime.now(HKT)
            kickoff = _kickoff(10, now)
            queue_a = dq.DeferredProjectionQueue(Path(tmp))
            queue_a.enqueue("m1", "T-5", kickoff=kickoff, payload={"match_id": "m1", "stage": "T-5"})
            calls = []
            queue_a.drain(calls.append)
            # New instance after "restart": must not re-project the same item.
            queue_b = dq.DeferredProjectionQueue(Path(tmp))
            queue_b.drain(calls.append)
            self.assertEqual(len(calls), 1)

    def test_expired_item_never_calls_project_fn(self):
        with tempfile.TemporaryDirectory() as tmp:
            queue = dq.DeferredProjectionQueue(Path(tmp))
            now = datetime.now(HKT)
            past = now - timedelta(minutes=1)
            queue.enqueue("m1", "T-5", kickoff=past, payload={"match_id": "m1", "stage": "T-5"})
            calls = []
            outcomes = queue.drain(calls.append, now=now)
            self.assertEqual(outcomes[0].state, dq.EXPIRED)
            self.assertEqual(len(calls), 0)

    def test_malformed_queue_item_fails_closed(self):
        with tempfile.TemporaryDirectory() as tmp:
            queue = dq.DeferredProjectionQueue(Path(tmp))
            now = datetime.now(HKT)
            kickoff = _kickoff(10, now)
            queue.enqueue("m1", "T-5", kickoff=kickoff, payload={"match_id": "WRONG", "stage": "T-5"})
            calls = []
            outcomes = queue.drain(calls.append)
            self.assertEqual(outcomes[0].state, dq.FAILED)
            self.assertEqual(outcomes[0].reason, "identity_mismatch_refused")
            self.assertEqual(len(calls), 0)

    def test_persistent_failure_freezes_after_max_attempts(self):
        with tempfile.TemporaryDirectory() as tmp:
            queue = dq.DeferredProjectionQueue(Path(tmp))
            now = datetime.now(HKT)
            kickoff = _kickoff(10, now)
            queue.enqueue("m1", "T-5", kickoff=kickoff, payload={"match_id": "m1", "stage": "T-5"})

            def boom(payload):
                raise RuntimeError("boom")

            last_outcomes = []
            for _ in range(dq._MAX_TERMINAL_FAILURES + 2):
                last_outcomes = queue.drain(boom)
            item = queue.read("m1", "T-5")
            self.assertEqual(item["state"], dq.FAILED)
            self.assertTrue(item.get("frozen"))
            # Further drains must not call boom again once frozen.
            calls_after_freeze = []

            def track(payload):
                calls_after_freeze.append(payload)
                raise RuntimeError("boom")

            queue.drain(track)
            self.assertEqual(calls_after_freeze, [])

    def test_attempt_history_is_append_only_and_bounded(self):
        with tempfile.TemporaryDirectory() as tmp:
            queue = dq.DeferredProjectionQueue(Path(tmp))
            now = datetime.now(HKT)
            kickoff = _kickoff(10, now)
            queue.enqueue("m1", "T-5", kickoff=kickoff, payload={"match_id": "m1", "stage": "T-5"})

            def boom(payload):
                raise RuntimeError("boom")

            for _ in range(dq._MAX_ATTEMPT_HISTORY + 5):
                queue.drain(boom)
                item = queue.read("m1", "T-5")
                if item["state"] == dq.FAILED and item.get("frozen"):
                    break
            item = queue.read("m1", "T-5")
            self.assertLessEqual(len(item["attempts"]), dq._MAX_ATTEMPT_HISTORY)

    def test_max_items_bounds_one_drain_call(self):
        with tempfile.TemporaryDirectory() as tmp:
            queue = dq.DeferredProjectionQueue(Path(tmp))
            now = datetime.now(HKT)
            kickoff = _kickoff(10, now)
            for i in range(10):
                queue.enqueue(f"m{i}", "T-5", kickoff=kickoff, payload={"match_id": f"m{i}", "stage": "T-5"})
            outcomes = queue.drain(lambda p: None, max_items=3)
            self.assertEqual(len(outcomes), 3)
            remaining = queue.pending_items()
            self.assertEqual(len(remaining), 7)


class CompletionCallbackIsolationTests(unittest.TestCase):
    """Requirement: a completion-callback exception must not block siblings."""

    def test_wrap_completion_callback_isolates_exception(self):
        calls = []

        def flaky(value):
            calls.append(value)
            if value == "bad":
                raise RuntimeError("callback exploded")

        wrapped = cutover.wrap_completion_callback(flaky)
        wrapped("good")
        wrapped("bad")  # must not raise
        wrapped("good2")
        self.assertEqual(calls, ["good", "bad", "good2"])

    def test_wrap_completion_callback_preserves_successful_side_effects(self):
        results = []
        wrapped = cutover.wrap_completion_callback(results.append)
        for value in ("a", "b", "c"):
            wrapped(value)
        self.assertEqual(results, ["a", "b", "c"])


class WholeLedgerSlownessSimulationTests(FlagsTestCase):
    """Requirement: whole-ledger load/save simulated slow/raising must not
    block the native commit when cutover+deferred are enabled."""

    def test_slow_or_raising_legacy_project_fn_does_not_block_native_commit(self):
        with tempfile.TemporaryDirectory() as tmp:
            self._set(store=True, cutover_on=True, deferred=True)
            config = _config(Path(tmp))
            now = datetime.now(HKT)

            def slow_and_raising_legacy_commit(payload):
                raise RuntimeError("simulated whole-ledger save_ledger failure")

            result = cutover.commit_fixture_result(
                config, _prediction("m1", "T-5", _kickoff(10, now)),
                slow_and_raising_legacy_commit, now=now,
            )
            # In cutover+deferred mode, project_fn (the legacy path) is never
            # called inline at all -- so a slow/raising legacy path literally
            # cannot affect this call's return value or the native commit.
            self.assertEqual(result.native_state, "COMMITTED")
            self.assertEqual(result.projection, "deferred")
            store = store_mod.NativeStageStore(config.state_dir)
            self.assertIn("T-5", store.read("m1")["snapshots"])

    def test_drain_legacy_exception_does_not_roll_back_native_commit(self):
        with tempfile.TemporaryDirectory() as tmp:
            self._set(store=True, cutover_on=True, deferred=True)
            config = _config(Path(tmp))
            now = datetime.now(HKT)
            cutover.commit_fixture_result(
                config, _prediction("m1", "T-5", _kickoff(10, now)), lambda p: None, now=now,
            )
            store = store_mod.NativeStageStore(config.state_dir)
            before = store.read("m1")
            self.assertIn("T-5", before["snapshots"])

            def raising_legacy_commit(payload):
                raise RuntimeError("simulated legacy ledger corruption")

            cutover.drain_deferred_projections(config, raising_legacy_commit)
            after = store.read("m1")
            self.assertEqual(before["snapshots"], after["snapshots"])
            self.assertIn("T-5", after["snapshots"], "native commit must survive a legacy projection failure")


if __name__ == "__main__":
    unittest.main()
