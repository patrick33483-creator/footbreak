"""Stage 7 of the Crown T-5 deadline-first patch: end-to-end deferred-
projection durability call-site tests.

Stage 6 wired live per-fixture native durability commits
(``native_stage_cutover.commit_native_only``) into the real
``_run_local_bulk_timed_stages`` batch path, but left a gap: nothing
enqueued a deferred legacy-projection job at native-commit time, and the
only drain call ran strictly AFTER the existing unconditional whole-batch
``_commit_stage_predictions`` legacy commit. If that legacy commit is
killed or times out, a fixture whose native snapshot is already durably
COMMITTED had no recorded backlog item and no guaranteed recovery path.

Stage 7 closes this gap with:

  1. ``enqueue_committed_snapshot`` -- called at native-commit time, before
     the legacy whole-batch commit.
  2. ``project_committed_native_snapshot`` -- a narrow, post-kickoff-safe
     projection writer that reuses ``crown.ledger.sync_prediction``'s
     ``deadline_critical_snapshot=True`` branch (proven, by direct reading,
     to never reach any Wilson/notification/research consumer) and adds an
     independent kickoff re-check plus idempotent-already-projected ACK.
  3. ``drain_deferred_projections_batch`` -- ONE ``state_lock`` +
     ``load_ledger`` + ``save_ledger`` transaction for up to N queued
     items, wired into two reachable recovery points: strictly before the
     unbounded legacy whole-batch commit in ``_run_local_bulk_timed_stages``,
     and in the ``fast_noop`` branch of ``crown.engine.run``.

These tests exercise the real ``crown.engine.run("tick", ...)`` entry point
end to end wherever possible, falling back to direct module-level calls
only for the narrow queue/writer primitives and for simulating a process
kill mid-legacy-commit (which cannot be expressed by letting ``run()``
complete normally).

No production access, provider call, Telegram, bet, push, or workflow
dispatch is exercised anywhere in this file.
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
from crown.engine import run, _NATIVE_STAGE7_RECOVERY_DRAIN_RESERVE_SECONDS
from crown.ledger import PREDICTION_ERA
from crown.matching import MATCHING_VERSION
from crown.state import load_ledger, save_ledger, save_predictions
from crown import native_stage_store as store_mod
from crown import native_stage_cutover as cutover_mod
from crown import native_stage_deferred_projection as deferred_mod

from crown.tests.test_native_stage_cutover_callsite import CutoverCallsiteTestBase


def _prediction(match_id: str, stage: str, kickoff: datetime, **overrides) -> dict:
    observed_at = (kickoff - timedelta(minutes=1)).isoformat()
    base = {
        "match_id": match_id, "stage": stage, "league": "L",
        "home": f"{match_id} Home", "away": f"{match_id} Away",
        "kickoff_hkt": kickoff.isoformat(),
        "status": "OK",
        "forecast_candidates": [
            {"code": "HDC", "line": -0.25, "side": "H", "odds": 1.91, "prob": 0.55,
             "observed_at": observed_at, "source": "test"},
        ],
        "collection_attempt": {"source": "test"},
        "matching_version": MATCHING_VERSION,
    }
    base.update(overrides)
    return base


# ---------------------------------------------------------------------------
# 1. enqueue_committed_snapshot: enqueue at native-commit time.
# ---------------------------------------------------------------------------

class EnqueueAtNativeCommitTests(CutoverCallsiteTestBase):
    def test_engine_enqueues_before_legacy_whole_batch_commit(self) -> None:
        """The live `_on_direct_result` closure must enqueue this fixture's
        deferred-projection job strictly before `_commit_stage_predictions`
        (the legacy whole-batch call) is invoked for the same tick."""
        self._enable_cutover(deferred=True)
        match_ids = ["enqueue-order-a"]
        now, _cards = self._seed_due_t5_fixtures(match_ids)
        titan_client = self._titan_client(match_ids, now)
        import crown.engine as eng
        orig_commit = eng._commit_stage_predictions
        pending_at_legacy_commit_time = {}

        def spy_commit(config, mode, stage_predictions, **kwargs):
            queue = deferred_mod.DeferredProjectionQueue(self.state_dir)
            for prediction in stage_predictions:
                match_id = str(prediction.get("match_id") or "")
                item = queue.read(match_id, "T-5")
                pending_at_legacy_commit_time[match_id] = item is not None
            return orig_commit(config, mode, stage_predictions, **kwargs)

        with patch("crown.engine.TitanClient", return_value=titan_client), \
             patch("crown.engine._commit_stage_predictions", side_effect=spy_commit):
            run("tick", self.config)
        self.assertTrue(
            pending_at_legacy_commit_time.get("enqueue-order-a"),
            "fixture must already be enqueued by the time the legacy whole-batch commit runs",
        )

    def test_no_enqueue_when_deferred_projection_flag_off(self) -> None:
        """Cutover on, but the deferred-projection flag off: native commit
        still happens, but nothing is enqueued (matches
        `enqueue_committed_snapshot`'s own default-off contract)."""
        self._enable_cutover(deferred=False)
        match_ids = ["no-enqueue-flag-off"]
        now, _cards = self._seed_due_t5_fixtures(match_ids)
        titan_client = self._titan_client(match_ids, now)
        with patch("crown.engine.TitanClient", return_value=titan_client):
            run("tick", self.config)
        queue = deferred_mod.DeferredProjectionQueue(self.state_dir)
        self.assertIsNone(queue.read("no-enqueue-flag-off", "T-5"))

    def test_no_enqueue_when_native_commit_is_not_committed(self) -> None:
        """`enqueue_committed_snapshot` must not be reachable for a fixture
        whose native commit did not return COMMITTED (e.g. DATA_MISSING) --
        `_on_direct_result` only fires for a usable snapshot, but this test
        pins the explicit ``native_state == "COMMITTED"`` gate directly."""
        self.assertFalse(
            cutover_mod.enqueue_committed_snapshot(
                self.config,
                {"match_id": "x", "stage": "T-5", "kickoff_hkt": datetime.now(HKT).isoformat()},
            )
        )
        os.environ[cutover_mod.ENV_DEFERRED_PROJECTION_ENABLED] = "1"
        # Even with the flag on, calling enqueue directly with an incomplete
        # identity must fail closed rather than guess.
        self.assertFalse(cutover_mod.enqueue_committed_snapshot(self.config, {}))


# ---------------------------------------------------------------------------
# 2. project_committed_native_snapshot: the narrow projection writer.
# ---------------------------------------------------------------------------

class NarrowProjectionWriterTests(CutoverCallsiteTestBase):
    def _base_ledger(self, match_id: str) -> dict:
        return {
            "bankroll": 50000, "bets": [], "log": [], "stats": {},
            "watch": {},
        }

    def test_writes_only_display_evidence_stage_row(self) -> None:
        now = datetime.now(HKT)
        kickoff = now + timedelta(minutes=10)
        ledger = self._base_ledger("proj-a")
        prediction = _prediction("proj-a", "T-5", kickoff)
        ok = cutover_mod.project_committed_native_snapshot(ledger, prediction, self.config, now=now)
        self.assertTrue(ok)
        stages = ledger["watch"]["proj-a"]["stages"]
        self.assertEqual([row["stage"] for row in stages], ["T-5"])
        self.assertTrue(stages[0].get("formal_admission_pending"))

    def test_creates_zero_wilson_bets_or_observations(self) -> None:
        now = datetime.now(HKT)
        kickoff = now + timedelta(minutes=10)
        ledger = self._base_ledger("proj-b")
        prediction = _prediction("proj-b", "T-5", kickoff)
        cutover_mod.project_committed_native_snapshot(ledger, prediction, self.config, now=now)
        self.assertEqual(ledger["bets"], [])
        self.assertNotIn("crown", ledger)
        self.assertNotIn("wilson_validation", ledger)

    def test_never_calls_evaluate_new_t5_or_record_new_native_t5_or_challenger(self) -> None:
        now = datetime.now(HKT)
        kickoff = now + timedelta(minutes=10)
        ledger = self._base_ledger("proj-c")
        prediction = _prediction("proj-c", "T-5", kickoff)
        with patch("crown.ledger.evaluate_new_t5", side_effect=AssertionError(
                 "must never call evaluate_new_t5")), \
             patch("crown.ledger.record_new_native_t5", side_effect=AssertionError(
                 "must never call record_new_native_t5")), \
             patch("crown.challenger_v2.evaluate_new_t5", side_effect=AssertionError(
                 "must never call challenger_v2.evaluate_new_t5")), \
             patch("crown.challenger_v2.recompute", side_effect=AssertionError(
                 "must never call challenger_v2.recompute")):
            ok = cutover_mod.project_committed_native_snapshot(ledger, prediction, self.config, now=now)
        self.assertTrue(ok)

    def test_never_ensures_crown_or_outbox_or_challenger_namespaces(self) -> None:
        now = datetime.now(HKT)
        kickoff = now + timedelta(minutes=10)
        ledger = self._base_ledger("proj-d")
        prediction = _prediction("proj-d", "T-5", kickoff)
        with patch("crown.ledger.ensure_namespace", side_effect=AssertionError(
                 "must never ensure the crown namespace")), \
             patch("crown.ledger.ensure_direct_t5_outbox", side_effect=AssertionError(
                 "must never ensure the direct outbox namespace")), \
             patch("crown.challenger_v2.ensure_namespace", side_effect=AssertionError(
                 "must never ensure the challenger_v2 namespace")):
            ok = cutover_mod.project_committed_native_snapshot(ledger, prediction, self.config, now=now)
        self.assertTrue(ok)

    def test_refuses_post_kickoff_projection(self) -> None:
        """An independent, local no-backfill refusal even if the caller
        never checked kickoff itself."""
        now = datetime.now(HKT)
        past_kickoff = now - timedelta(minutes=1)
        ledger = self._base_ledger("proj-e")
        prediction = _prediction("proj-e", "T-5", past_kickoff)
        ok = cutover_mod.project_committed_native_snapshot(ledger, prediction, self.config, now=now)
        self.assertFalse(ok)
        self.assertNotIn("proj-e", ledger["watch"])

    def test_idempotent_ack_when_already_projected(self) -> None:
        """A fixture whose legacy stage row already exists and is complete
        (e.g. the existing whole-batch commit already projected it) must be
        ACKed (return True) without a second mutation/consumer call."""
        now = datetime.now(HKT)
        kickoff = now + timedelta(minutes=10)
        ledger = self._base_ledger("proj-f")
        prediction = _prediction("proj-f", "T-5", kickoff)
        first = cutover_mod.project_committed_native_snapshot(ledger, prediction, self.config, now=now)
        self.assertTrue(first)
        stage_row_after_first = dict(ledger["watch"]["proj-f"]["stages"][0])
        second = cutover_mod.project_committed_native_snapshot(ledger, prediction, self.config, now=now)
        self.assertTrue(second)
        self.assertEqual(ledger["watch"]["proj-f"]["stages"][0], stage_row_after_first)

    def test_conflicting_identity_fails_closed(self) -> None:
        """A malformed/incomplete prediction (missing match_id or an
        unrecognized stage) must fail closed, never guess an identity."""
        now = datetime.now(HKT)
        ledger = self._base_ledger("proj-g")
        self.assertFalse(
            cutover_mod.project_committed_native_snapshot(ledger, {"stage": "T-5"}, self.config, now=now)
        )
        self.assertFalse(
            cutover_mod.project_committed_native_snapshot(
                ledger, {"match_id": "proj-g", "stage": "NOT-A-STAGE"}, self.config, now=now,
            )
        )
        self.assertFalse(
            cutover_mod.project_committed_native_snapshot(
                ledger, {"match_id": "proj-g", "stage": "T-5"}, self.config, now=now,
            )
        )

    def test_preserves_t30_when_projecting_t5(self) -> None:
        now = datetime.now(HKT)
        kickoff = now + timedelta(minutes=10)
        ledger = self._base_ledger("proj-h")
        ledger["watch"]["proj-h"] = {
            "match_id": "proj-h", "matching_version": MATCHING_VERSION,
            "prediction_era": PREDICTION_ERA,
            "stages": [{"stage": "T-30", "match_id": "proj-h", "kickoff_hkt": kickoff.isoformat(),
                        "ts": now.isoformat()}],
        }
        prediction = _prediction("proj-h", "T-5", kickoff)
        ok = cutover_mod.project_committed_native_snapshot(ledger, prediction, self.config, now=now)
        self.assertTrue(ok)
        stages = [row["stage"] for row in ledger["watch"]["proj-h"]["stages"]]
        self.assertIn("T-30", stages)
        self.assertIn("T-5", stages)


# ---------------------------------------------------------------------------
# 3. drain_deferred_projections_batch: one shared transaction for N items.
# ---------------------------------------------------------------------------

class BatchDrainSingleTransactionTests(CutoverCallsiteTestBase):
    def _enqueue_n(self, n: int, *, prefix: str) -> list[str]:
        os.environ[cutover_mod.ENV_DEFERRED_PROJECTION_ENABLED] = "1"
        queue = deferred_mod.DeferredProjectionQueue(self.state_dir)
        now = datetime.now(HKT)
        kickoff = now + timedelta(minutes=15)
        match_ids = [f"{prefix}-{i}" for i in range(n)]
        for match_id in match_ids:
            queue.enqueue(
                match_id, "T-5", kickoff=kickoff,
                payload=_prediction(match_id, "T-5", kickoff),
            )
        return match_ids

    def _run_batch_drain_counting_ledger_io(self, n: int, prefix: str):
        match_ids = self._enqueue_n(n, prefix=prefix)
        save_ledger(self.config, {"bankroll": 50000, "bets": [], "log": [], "stats": {}, "watch": {}})
        import crown.state as state_mod
        load_calls = []
        save_calls = []
        orig_load = state_mod.load_ledger
        orig_save = state_mod.save_ledger

        def counted_load(config):
            load_calls.append(1)
            return orig_load(config)

        def counted_save(config, data):
            save_calls.append(1)
            return orig_save(config, data)

        with patch("crown.native_stage_cutover.load_ledger", side_effect=counted_load), \
             patch("crown.native_stage_cutover.save_ledger", side_effect=counted_save):
            outcomes = cutover_mod.drain_deferred_projections_batch(
                self.config, max_items=max(n, 1),
            )
        return match_ids, outcomes, load_calls, save_calls

    def test_fifteen_items_one_ledger_read_and_write(self) -> None:
        match_ids, outcomes, load_calls, save_calls = self._run_batch_drain_counting_ledger_io(15, "batch15")
        self.assertEqual(len(load_calls), 1)
        self.assertEqual(len(save_calls), 1)
        self.assertEqual(len(outcomes), 15)
        ledger = load_ledger(self.config)
        for match_id in match_ids:
            self.assertEqual(
                [row["stage"] for row in ledger["watch"][match_id]["stages"]].count("T-5"), 1,
            )

    def test_twenty_six_items_one_ledger_read_and_write(self) -> None:
        match_ids, outcomes, load_calls, save_calls = self._run_batch_drain_counting_ledger_io(26, "batch26")
        self.assertEqual(len(load_calls), 1)
        self.assertEqual(len(save_calls), 1)
        self.assertEqual(len(outcomes), 26)

    def test_fifty_items_one_ledger_read_and_write(self) -> None:
        match_ids, outcomes, load_calls, save_calls = self._run_batch_drain_counting_ledger_io(50, "batch50")
        self.assertEqual(len(load_calls), 1)
        self.assertEqual(len(save_calls), 1)
        self.assertEqual(len(outcomes), 50)

    def test_eighty_nine_items_one_ledger_read_and_write(self) -> None:
        match_ids, outcomes, load_calls, save_calls = self._run_batch_drain_counting_ledger_io(89, "batch89")
        self.assertEqual(len(load_calls), 1)
        self.assertEqual(len(save_calls), 1)
        self.assertEqual(len(outcomes), 89)

    def test_max_items_bounds_batch_size(self) -> None:
        self._enqueue_n(10, prefix="bound")
        save_ledger(self.config, {"bankroll": 50000, "bets": [], "log": [], "stats": {}, "watch": {}})
        outcomes = cutover_mod.drain_deferred_projections_batch(self.config, max_items=3)
        self.assertEqual(len(outcomes), 3)

    def test_noop_when_flag_off(self) -> None:
        os.environ.pop(cutover_mod.ENV_DEFERRED_PROJECTION_ENABLED, None)
        queue = deferred_mod.DeferredProjectionQueue(self.state_dir)
        now = datetime.now(HKT)
        queue.enqueue(
            "flagoff-a", "T-5", kickoff=now + timedelta(minutes=10),
            payload=_prediction("flagoff-a", "T-5", now + timedelta(minutes=10)),
        )
        with patch("crown.native_stage_cutover.load_ledger", side_effect=AssertionError(
                "must not touch the ledger when the flag is off")):
            outcomes = cutover_mod.drain_deferred_projections_batch(self.config, max_items=10)
        self.assertEqual(outcomes, [])

    def test_noop_when_queue_empty(self) -> None:
        os.environ[cutover_mod.ENV_DEFERRED_PROJECTION_ENABLED] = "1"
        with patch("crown.native_stage_cutover.load_ledger", side_effect=AssertionError(
                "must not touch the ledger when nothing is queued")):
            outcomes = cutover_mod.drain_deferred_projections_batch(self.config, max_items=10)
        self.assertEqual(outcomes, [])

    def test_lock_contention_fails_closed_without_partial_progress(self) -> None:
        """If the state lock cannot be acquired within the strict budget,
        the drain must skip entirely (no I/O, nothing marked COMPLETED) --
        never take a partial/half batch."""
        self._enqueue_n(5, prefix="locked")
        save_ledger(self.config, {"bankroll": 50000, "bets": [], "log": [], "stats": {}, "watch": {}})
        from crown.state import state_lock as real_state_lock

        class _NeverAcquires:
            def __enter__(self):
                return False

            def __exit__(self, *exc):
                return False

        with patch("crown.native_stage_cutover.state_lock", return_value=_NeverAcquires()):
            outcomes = cutover_mod.drain_deferred_projections_batch(
                self.config, max_items=5, max_seconds=0.1,
            )
        self.assertEqual(outcomes, [])
        queue = deferred_mod.DeferredProjectionQueue(self.state_dir)
        for i in range(5):
            item = queue.read(f"locked-{i}", "T-5")
            self.assertEqual(item.get("state"), deferred_mod.PENDING)


# ---------------------------------------------------------------------------
# 4. Recovery placement: killed-during-legacy-commit, restart/next-tick, and
#    fast-noop backlog drain.
# ---------------------------------------------------------------------------

class RecoveryPlacementTests(CutoverCallsiteTestBase):
    def test_killed_during_legacy_commit_recovered_by_next_tick_without_provider_calls(self) -> None:
        """Simulates a process killed during the legacy whole-batch commit,
        strictly after native commit + enqueue already happened for this
        fixture. The next tick (for a *different*, newly-due fixture) must
        still recover the stranded fixture's legacy T-5 row via the bounded
        pre-legacy-commit recovery drain -- without any provider call for
        the stranded fixture."""
        self._enable_cutover(deferred=True)
        stranded_id = "killed-mid-commit"
        now = datetime.now(HKT)
        kickoff = now + timedelta(minutes=20)
        # Simulate: native commit + enqueue already happened for this
        # fixture in a prior (killed) tick, but the legacy whole-batch
        # commit never ran (process died before writing watch/stages).
        store = store_mod.NativeStageStore(self.state_dir)
        prediction = _prediction(stranded_id, "T-5", kickoff)
        snapshot = cutover_mod._snapshot_from_prediction(prediction)
        store.commit_snapshot(stranded_id, "T-5", snapshot, kickoff=kickoff)
        cutover_mod.enqueue_committed_snapshot(self.config, prediction)
        save_ledger(self.config, {
            "bankroll": 50000, "bets": [], "log": [], "stats": {},
            "watch": {},
        })
        save_predictions(self.config, [])

        # A second, unrelated fixture is due this tick -- this is what
        # actually drives `run("tick", ...)` down the native ID=3 path so
        # the pre-legacy-commit recovery drain call site executes for real.
        fresh_id = "fresh-this-tick"
        fresh_now, _cards = self._seed_due_t5_fixtures([fresh_id])
        # _seed_due_t5_fixtures overwrote predictions/ledger; re-verify the
        # stranded fixture's native+queue state (file-backed, untouched by
        # that call) is still exactly as set up above.
        self.assertIsNotNone(
            deferred_mod.DeferredProjectionQueue(self.state_dir).read(stranded_id, "T-5"),
        )
        titan_client = self._titan_client([fresh_id], fresh_now)

        provider_calls_for_stranded = []
        orig_crown_price_snapshot = titan_client.crown_price_snapshot.side_effect

        def spying_crown_price_snapshot(match_id, **kwargs):
            if match_id == stranded_id:
                provider_calls_for_stranded.append(match_id)
            return orig_crown_price_snapshot(match_id, **kwargs)

        titan_client.crown_price_snapshot.side_effect = spying_crown_price_snapshot

        with patch("crown.engine.TitanClient", return_value=titan_client):
            run("tick", self.config)

        self.assertEqual(
            provider_calls_for_stranded, [],
            "the stranded fixture's recovery projection must never call the provider",
        )
        ledger = load_ledger(self.config)
        self.assertIn(stranded_id, ledger["watch"])
        self.assertEqual(
            [row["stage"] for row in ledger["watch"][stranded_id]["stages"]].count("T-5"), 1,
        )
        # Zero Wilson/observation/bet rows from the recovery projection.
        self.assertEqual(ledger.get("bets", []), [])

    def test_fast_noop_path_drains_backlog(self) -> None:
        """A tick with nothing newly due (`fast_noop`) must still drain any
        backlog left by a previous killed tick."""
        self._enable_cutover(deferred=True)
        stranded_id = "noop-backlog-a"
        now = datetime.now(HKT)
        kickoff = now + timedelta(minutes=20)
        store = store_mod.NativeStageStore(self.state_dir)
        prediction = _prediction(stranded_id, "T-5", kickoff)
        snapshot = cutover_mod._snapshot_from_prediction(prediction)
        store.commit_snapshot(stranded_id, "T-5", snapshot, kickoff=kickoff)
        cutover_mod.enqueue_committed_snapshot(self.config, prediction)
        save_ledger(self.config, {"bankroll": 50000, "bets": [], "log": [], "stats": {}, "watch": {}})
        save_predictions(self.config, [])

        with patch("crown.engine.TitanClient", side_effect=AssertionError(
                "fast_noop backlog drain must not call any provider client")), \
             patch("crown.engine.PinnapiClient", side_effect=AssertionError(
                "fast_noop backlog drain must not call any provider client")):
            result = run("tick", self.config)
        self.assertTrue(result.get("fast_noop"))
        ledger = load_ledger(self.config)
        self.assertIn(stranded_id, ledger["watch"])
        self.assertEqual(
            [row["stage"] for row in ledger["watch"][stranded_id]["stages"]].count("T-5"), 1,
        )

    def test_urgent_due_batch_not_delayed_beyond_strict_budget(self) -> None:
        """The pre-legacy-commit recovery drain must never consume more
        than its strict reserve of the remaining tick deadline, even when a
        large backlog is queued -- it must be skipped entirely rather than
        eating into the urgent due-fixture collection/commit budget."""
        self._enable_cutover(deferred=True)
        # Queue a large-ish backlog.
        os.environ[cutover_mod.ENV_DEFERRED_PROJECTION_ENABLED] = "1"
        queue = deferred_mod.DeferredProjectionQueue(self.state_dir)
        now = datetime.now(HKT)
        kickoff = now + timedelta(minutes=25)
        for i in range(10):
            match_id = f"budget-backlog-{i}"
            queue.enqueue(
                match_id, "T-5", kickoff=kickoff,
                payload=_prediction(match_id, "T-5", kickoff),
            )
        urgent_id = "budget-urgent"
        urgent_now, _cards = self._seed_due_t5_fixtures([urgent_id])
        titan_client = self._titan_client([urgent_id], urgent_now)

        # Force the deadline remaining to be at/under the reserve so the
        # recovery drain must be skipped (never partially run).
        import crown.engine as eng
        real_remaining = eng._deadline_remaining

        def tiny_remaining(deadline):
            return min(real_remaining(deadline), _NATIVE_STAGE7_RECOVERY_DRAIN_RESERVE_SECONDS)

        with patch("crown.engine.TitanClient", return_value=titan_client), \
             patch("crown.engine._deadline_remaining", side_effect=tiny_remaining), \
             patch(
                 "crown.native_stage_cutover.drain_deferred_projections_batch",
                 side_effect=AssertionError("recovery drain must be skipped, not merely bounded, under budget"),
             ):
            result = run("tick", self.config)
        self.assertEqual(result["predictions"], 1)

    def test_successful_legacy_whole_batch_commit_then_drain_acks_idempotently(self) -> None:
        """When the legacy whole-batch commit succeeds normally (the common
        case), a later drain call for the same fixture must detect the
        already-projected row and ACK (COMPLETED) it without re-invoking
        any consumer or creating a duplicate stage row."""
        self._enable_cutover(deferred=True)
        match_ids = ["ack-after-success"]
        now, _cards = self._seed_due_t5_fixtures(match_ids)
        titan_client = self._titan_client(match_ids, now)
        with patch("crown.engine.TitanClient", return_value=titan_client):
            run("tick", self.config)
        ledger_before = load_ledger(self.config)
        stage_row_before = dict(
            next(r for r in ledger_before["watch"]["ack-after-success"]["stages"] if r["stage"] == "T-5")
        )
        # Drain again explicitly -- must be idempotent.
        cutover_mod.drain_deferred_projections_batch(self.config, max_items=10)
        ledger_after = load_ledger(self.config)
        self.assertEqual(
            [row["stage"] for row in ledger_after["watch"]["ack-after-success"]["stages"]].count("T-5"),
            1,
        )
        stage_row_after = dict(
            next(r for r in ledger_after["watch"]["ack-after-success"]["stages"] if r["stage"] == "T-5")
        )
        self.assertEqual(stage_row_before, stage_row_after)


if __name__ == "__main__":
    unittest.main()
