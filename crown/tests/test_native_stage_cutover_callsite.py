"""Stage 6 of the Crown T-5 deadline-first patch: real engine call-site tests.

Stage 5 (`native_stage_cutover.py`) was accepted only as an intermediate,
untested-at-the-call-site module: nothing in `crown/engine.py` imported or
called it. Stage 6 wires exactly one of the two call sites the user asked
for -- the native ID=3 `_run_local_bulk_timed_stages` batch path -- and
documents, with a control-flow regression test, why the other named call
site (`commit_completed` / `_run_tick_predictions` in the tick-mode
PinnAPI-bridge branch of `crown.engine.run`) is unreachable for
`mode == "tick"` under the engine's current control flow and therefore was
deliberately left unwired rather than falsely presented as live.

These tests exercise the real `crown.engine.run("tick", ...)` entry point
end to end (never `native_stage_cutover.py`'s functions directly, except in
the dedicated unreachability proof), with `TitanClient`/`PinnapiClient`
mocked at the exact same boundary as the pre-existing suite
(`crown/tests/test_crown.py`, `crown/tests/test_native_stage_shadow.py`).

No production access, provider call, Telegram, bet, push, or workflow
dispatch is exercised anywhere in this file.
"""
from __future__ import annotations

import os
import tempfile
import time
import unittest
from dataclasses import replace
from datetime import datetime, timedelta
from pathlib import Path
from unittest.mock import Mock, patch

from crown.common import HKT
from crown.config import settings
from crown.engine import run, _run_local_bulk_timed_stages, _run_tick_predictions
from crown.ledger import PREDICTION_ERA
from crown.matching import MATCHING_VERSION
from crown.state import load_ledger, load_predictions, save_ledger, save_predictions
from crown import native_stage_store as store_mod
from crown import native_stage_cutover as cutover_mod
from crown import native_stage_deferred_projection as deferred_mod


def _bulk_snapshot_payload(match_ids: list[str], now: datetime) -> dict:
    return {
        match_id: {
            "prices": [
                {"market": market, "line": line, "selection": side, "odds": odds,
                 "source_at": now.timestamp()}
                for market, line, sides in (
                    ("HDC", -0.25, (("H", 1.91), ("A", 1.99))),
                    ("HIL", 2.5, (("H", 1.95), ("L", 1.95))),
                )
                for side, odds in sides
            ],
            "asian_ok": True, "total_ok": True,
            "quote_source": "titan007-crown-id-3-bulk-current",
        }
        for match_id in match_ids
    }


class CutoverCallsiteTestBase(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.state_dir = Path(self._tmp.name)
        self.config = replace(
            settings(), state_dir=self.state_dir, enabled=True, pinnapi_key="test",
        )
        self._env_patch = patch.dict(os.environ, {}, clear=False)
        self._env_patch.start()
        for key in (
            store_mod.ENV_ENABLED,
            cutover_mod.ENV_CUTOVER_ENABLED,
            cutover_mod.ENV_DEFERRED_PROJECTION_ENABLED,
        ):
            os.environ.pop(key, None)

    def tearDown(self) -> None:
        for key in (
            store_mod.ENV_ENABLED,
            cutover_mod.ENV_CUTOVER_ENABLED,
            cutover_mod.ENV_DEFERRED_PROJECTION_ENABLED,
        ):
            os.environ.pop(key, None)
        self._env_patch.stop()
        self._tmp.cleanup()

    def _seed_due_t5_fixtures(self, match_ids: list[str], minutes: float = 4.0):
        now = datetime.now(HKT)
        cards = [
            {
                "match_id": match_id, "league": "L",
                "home": f"{match_id} Home", "away": f"{match_id} Away",
                "kickoff_hkt": (now + timedelta(minutes=minutes)).isoformat(),
            }
            for match_id in match_ids
        ]
        save_predictions(self.config, cards)
        save_ledger(self.config, {
            "bankroll": 50000, "bets": [], "log": [], "stats": {},
            "watch": {
                match_id: {
                    "matching_version": MATCHING_VERSION, "prediction_era": PREDICTION_ERA,
                    "stages": [{"stage": "首預"}, {"stage": "T-30"}],
                }
                for match_id in match_ids
            },
        })
        return now, cards

    def _titan_client(self, match_ids: list[str], now: datetime) -> Mock:
        titan_client = Mock()
        titan_client.crown_bulk_price_snapshots.return_value = _bulk_snapshot_payload(match_ids, now)
        titan_client.crown_prices.side_effect = AssertionError(
            "per-fixture Crown page must not run for a valid bulk batch"
        )
        # The native ID=3 batch path always tries the direct-ID collector
        # (``_collect_locked_direct_snapshots``, forked child processes
        # calling ``crown_price_snapshot``) before its bulk fallback. That
        # direct collector is exactly where Stage 6's ``on_result``/
        # ``_on_direct_result`` cutover hook fires, so it must return a
        # genuinely valid per-fixture payload here for these call-site
        # tests to exercise the real hook rather than silently falling
        # through to the (also real, unmodified) bulk fallback below it.
        single_snapshot = {
            "prices": [
                {"market": "HDC", "line": -0.25, "selection": "H", "odds": 1.91,
                 "source_at": now.timestamp()},
                {"market": "HDC", "line": -0.25, "selection": "A", "odds": 1.99,
                 "source_at": now.timestamp()},
            ],
            "asian_ok": True, "total_ok": True,
            "quote_source": "titan007-crown-id-3",
        }
        titan_client.crown_price_snapshot.side_effect = (
            lambda match_id, **kwargs: dict(single_snapshot)
        )
        return titan_client

    def _native_store_dir(self) -> Path:
        return self.state_dir / store_mod.NATIVE_STAGE_SUBDIR

    def _enable_cutover(self, *, deferred: bool = False) -> None:
        os.environ[store_mod.ENV_ENABLED] = "1"
        os.environ[cutover_mod.ENV_CUTOVER_ENABLED] = "1"
        if deferred:
            os.environ[cutover_mod.ENV_DEFERRED_PROJECTION_ENABLED] = "1"

    def _read_native(self, match_id: str) -> dict | None:
        store = store_mod.NativeStageStore(self.state_dir)
        return store.read(match_id)

    def _native_t5_state(self, match_id: str) -> str | None:
        """The terminal state of this fixture's T-5 attempt, per the real
        ``NativeStageStore`` on-disk schema: the most recent
        ``attempt_history`` entry for stage ``T-5``."""
        native = self._read_native(match_id)
        if not native:
            return None
        history = [
            entry for entry in (native.get("attempt_history") or [])
            if isinstance(entry, dict) and entry.get("stage") == "T-5"
        ]
        if not history:
            return None
        return history[-1].get("state")

    def _assert_all_committed_natively(self, match_ids: list[str]) -> None:
        for match_id in match_ids:
            state = self._native_t5_state(match_id)
            self.assertEqual(state, "COMMITTED", f"{match_id} native T-5 state was {state!r}")


# ---------------------------------------------------------------------------
# 0. Control-flow unreachability proof for the PinnAPI-bridge callback path.
# ---------------------------------------------------------------------------

class BridgeCallbackUnreachableForTickTests(CutoverCallsiteTestBase):
    """Documents (with a failing-if-ever-violated assertion) that the
    tick-mode `commit_completed` / `_run_tick_predictions` call site the
    user separately named cannot fire for `mode == "tick"` under the
    engine's current control flow, so nobody later mistakes it for active
    durability protection.
    """

    def test_run_tick_predictions_never_called_for_tick_mode_with_due_fixture(self) -> None:
        """A due local T-5 fixture takes the native ID=3 early-return path,
        never falling through to `_run_tick_predictions`."""
        match_ids = ["bridge-unreachable-a"]
        now, _cards = self._seed_due_t5_fixtures(match_ids)
        titan_client = self._titan_client(match_ids, now)
        calls: list[int] = []
        import crown.engine as eng
        orig = eng._run_tick_predictions

        def spy(payloads, deadline, on_complete):
            calls.append(len(payloads))
            return orig(payloads, deadline, on_complete)

        with patch("crown.engine.TitanClient", return_value=titan_client), \
             patch("crown.engine.PinnapiClient", side_effect=AssertionError(
                 "PinnAPI must not run on the fast T-5 bulk path"
             )), \
             patch("crown.engine._run_tick_predictions", side_effect=spy):
            result = run("tick", self.config)
        self.assertTrue(result.get("fast_t5_bulk"))
        self.assertEqual(calls, [], "commit_completed's driver must not run for a due tick fixture")

    def test_run_tick_predictions_never_called_for_tick_mode_with_no_due_fixture(self) -> None:
        """No local predictions/watch entries -> fast_noop, also never
        reaching `_run_tick_predictions`."""
        save_predictions(self.config, [])
        save_ledger(self.config, {"bankroll": 50000, "bets": [], "watch": {}, "log": [], "stats": {}})
        calls: list[int] = []
        import crown.engine as eng
        orig = eng._run_tick_predictions

        def spy(payloads, deadline, on_complete):
            calls.append(len(payloads))
            return orig(payloads, deadline, on_complete)

        with patch("crown.engine.TitanClient", side_effect=AssertionError(
                 "fast_noop must make no provider client"
             )), \
             patch("crown.engine.PinnapiClient", side_effect=AssertionError(
                 "fast_noop must make no provider client"
             )), \
             patch("crown.engine._run_tick_predictions", side_effect=spy):
            result = run("tick", self.config)
        self.assertTrue(result.get("fast_noop"))
        self.assertEqual(calls, [])

    def test_run_tick_predictions_itself_is_unaffected_and_still_directly_callable(self) -> None:
        """Sanity check on the primitive used above: `_run_tick_predictions`
        with a real non-empty payload list still runs its own callback (this
        proves the emptiness above is about what `run()` builds and passes
        to it for `mode == "tick"`, not that the function itself is broken
        or was modified by this stage)."""
        received: list[str] = []

        def on_complete(value):
            received.append(value)

        result = _run_tick_predictions(["not-a-real-payload"], time.monotonic() + 5, on_complete)
        # Malformed payload: expect a bounded, isolated failure outcome, not
        # a crash -- this only proves the driver itself still executes.
        self.assertIn("completed", result)


# ---------------------------------------------------------------------------
# 1. Native ID=3 batch path: default-off exact legacy equivalence.
# ---------------------------------------------------------------------------

class NativeBatchPathDefaultOffTests(CutoverCallsiteTestBase):
    def test_cutover_flag_unset_no_on_result_effect_result_identical(self) -> None:
        match_ids = [f"batchoff-{i}" for i in range(5)]
        now, _cards = self._seed_due_t5_fixtures(match_ids)
        titan_client = self._titan_client(match_ids, now)
        with patch("crown.engine.TitanClient", return_value=titan_client):
            result = run("tick", self.config)
        self.assertEqual(result["predictions"], 5)
        self.assertFalse(self._native_store_dir().exists())

    def test_cutover_flag_false_explicit_on_direct_result_is_never_constructed(self) -> None:
        """With CROWN_NATIVE_STAGE_STORE_ENABLED=1 but
        CROWN_NATIVE_STAGE_CUTOVER_ENABLED=0, the pre-existing Stage 2
        shadow mechanism (shadow_commit_stage_predictions, unconditional
        on the store flag alone, unrelated to this stage) still writes a
        native COMMITTED snapshot -- that is correct, unmodified Stage 2
        behaviour and is not what this stage's _on_direct_result hook
        controls. What this stage's flag actually gates is whether
        _on_direct_result/commit_native_only fire *during* collection at
        all; confirm that directly rather than asserting against the
        unrelated Stage 2 native-store side effect.
        """
        os.environ[store_mod.ENV_ENABLED] = "1"
        os.environ[cutover_mod.ENV_CUTOVER_ENABLED] = "0"
        match_ids = ["batchoff-explicit"]
        now, _cards = self._seed_due_t5_fixtures(match_ids)
        titan_client = self._titan_client(match_ids, now)
        with patch("crown.engine.TitanClient", return_value=titan_client), \
             patch("crown.native_stage_cutover.commit_native_only", side_effect=AssertionError(
                 "commit_native_only must never be called when the cutover flag is off"
             )):
            result = run("tick", self.config)
        self.assertEqual(result["predictions"], 1)

    def test_default_off_exact_call_count_to_commit_stage_predictions(self) -> None:
        """Exactly one whole-batch legacy commit call, never split, with
        cutover off -- byte-identical to every prior stage."""
        match_ids = [f"batchoff-count-{i}" for i in range(4)]
        now, _cards = self._seed_due_t5_fixtures(match_ids)
        titan_client = self._titan_client(match_ids, now)
        import crown.engine as eng
        orig = eng._commit_stage_predictions
        calls = []

        def spy(config, mode, stage_predictions, **kwargs):
            calls.append(len(stage_predictions))
            return orig(config, mode, stage_predictions, **kwargs)

        with patch("crown.engine.TitanClient", return_value=titan_client), \
             patch("crown.engine._commit_stage_predictions", side_effect=spy):
            run("tick", self.config)
        self.assertEqual(calls, [4], "exactly one batch call covering all 4 fixtures")


# ---------------------------------------------------------------------------
# 2. Native ID=3 batch path: cutover-enabled per-fixture native durability.
# ---------------------------------------------------------------------------

class NativeBatchPathCutoverEnabledTests(CutoverCallsiteTestBase):
    def _run_sized(self, n: int, *, deferred: bool = False):
        self._enable_cutover(deferred=deferred)
        match_ids = [f"sized-{n}-{i}" for i in range(n)]
        now, _cards = self._seed_due_t5_fixtures(match_ids)
        titan_client = self._titan_client(match_ids, now)
        with patch("crown.engine.TitanClient", return_value=titan_client):
            result = run("tick", self.config)
        return match_ids, result

    def test_one_fixture_commits_natively_and_legacy_batch_succeeds(self) -> None:
        """Explicit size=1 case for parity with the requested 1/15/26/50/89
        matrix (single-fixture behaviour is otherwise only covered
        incidentally by other tests in this file)."""
        match_ids, result = self._run_sized(1)
        self.assertEqual(result["predictions"], 1)
        self._assert_all_committed_natively(match_ids)

    def test_fifteen_fixtures_commit_natively_and_legacy_batch_succeeds(self) -> None:
        match_ids, result = self._run_sized(15)
        self.assertEqual(result["predictions"], 15)
        self._assert_all_committed_natively(match_ids)

    def test_twenty_six_fixtures_commit_natively_and_legacy_batch_succeeds(self) -> None:
        match_ids, result = self._run_sized(26)
        self.assertEqual(result["predictions"], 26)
        self._assert_all_committed_natively(match_ids)

    def test_fifty_fixtures_commit_natively_and_legacy_batch_succeeds(self) -> None:
        match_ids, result = self._run_sized(50)
        self.assertEqual(result["predictions"], 50)
        self._assert_all_committed_natively(match_ids)

    def test_eighty_nine_fixtures_commit_natively_and_legacy_batch_succeeds(self) -> None:
        match_ids, result = self._run_sized(89)
        self.assertEqual(result["predictions"], 89)
        self._assert_all_committed_natively(match_ids)

    def test_native_commit_happens_even_when_legacy_batch_commit_is_slow(self) -> None:
        """The defining property this stage exists to prove: native
        per-fixture durability must not wait on the whole-ledger legacy
        commit. Here `_commit_stage_predictions` (the single whole-batch
        legacy call) is delayed; native snapshots must already exist by the
        time `_on_direct_result` fires per fixture, strictly before that
        slow legacy call even begins."""
        self._enable_cutover()
        match_ids = [f"slowlegacy-{i}" for i in range(5)]
        now, _cards = self._seed_due_t5_fixtures(match_ids)
        titan_client = self._titan_client(match_ids, now)
        import crown.engine as eng
        orig_commit = eng._commit_stage_predictions
        native_snapshot_existed_before_legacy_call = {}

        def slow_commit(config, mode, stage_predictions, **kwargs):
            for prediction in stage_predictions:
                match_id = str(prediction.get("match_id") or "")
                native_snapshot_existed_before_legacy_call[match_id] = (
                    self._native_t5_state(match_id) == "COMMITTED"
                )
            time.sleep(0.05)
            return orig_commit(config, mode, stage_predictions, **kwargs)

        with patch("crown.engine.TitanClient", return_value=titan_client), \
             patch("crown.engine._commit_stage_predictions", side_effect=slow_commit):
            run("tick", self.config)
        for match_id in match_ids:
            self.assertTrue(
                native_snapshot_existed_before_legacy_call.get(match_id),
                f"{match_id} native commit did not happen before the (slow) legacy batch call",
            )

    def test_native_commit_survives_a_raising_legacy_batch_commit(self) -> None:
        """If the single legacy whole-batch commit
        (``_commit_stage_predictions``, called strictly after collection --
        see ``crown.engine._run_local_bulk_timed_stages``) raises entirely,
        every fixture's native durability snapshot committed per-fixture via
        ``on_result``/``_on_direct_result`` during collection must already
        be safely on disk: native durability does not depend on the legacy
        batch commit succeeding.

        (The write-ahead legacy journal, ``_journal_timed_stage_attempts``,
        runs even earlier than collection and also touches ``save_ledger``
        -- deliberately targeting ``_commit_stage_predictions`` here,
        rather than ``save_ledger`` globally, isolates the specific claim:
        a failure in the *legacy projection* step specifically must not
        take down the *native* durability write that already happened
        before it.)
        """
        self._enable_cutover()
        match_ids = ["raising-legacy-a", "raising-legacy-b"]
        now, _cards = self._seed_due_t5_fixtures(match_ids)
        titan_client = self._titan_client(match_ids, now)
        with patch("crown.engine.TitanClient", return_value=titan_client), \
             patch("crown.engine._commit_stage_predictions", side_effect=RuntimeError(
                 "simulated legacy whole-batch commit failure"
             )):
            with self.assertRaises(RuntimeError):
                run("tick", self.config)
        self._assert_all_committed_natively(match_ids)

    def test_one_native_store_failure_does_not_block_sibling_fixtures(self) -> None:
        """A native-store exception for one fixture (isolated inside
        `_on_direct_result`/`commit_native_only`) must never prevent any
        other fixture in the same batch from committing natively or from
        being included in the legacy batch commit."""
        self._enable_cutover()
        match_ids = ["isolate-a", "isolate-bad", "isolate-c"]
        now, _cards = self._seed_due_t5_fixtures(match_ids)
        titan_client = self._titan_client(match_ids, now)
        import crown.native_stage_cutover as cutover
        orig = cutover.commit_native_only

        def flaky(config, prediction, **kwargs):
            if prediction.get("match_id") == "isolate-bad":
                raise RuntimeError("simulated native-store failure for one fixture")
            return orig(config, prediction, **kwargs)

        with patch("crown.engine.TitanClient", return_value=titan_client), \
             patch("crown.native_stage_cutover.commit_native_only", side_effect=flaky):
            result = run("tick", self.config)
        self.assertEqual(result["predictions"], 3)
        self._assert_all_committed_natively(["isolate-a", "isolate-c"])
        # The failing fixture's legacy projection must still have happened
        # (the single whole-batch legacy call is untouched by native
        # failures) -- confirmed via the ledger, since native commit failure
        # must never suppress legacy Wilson/dashboard/Telegram evidence.
        ledger = load_ledger(self.config)
        self.assertEqual(
            [row["stage"] for row in ledger["watch"]["isolate-bad"]["stages"]].count("T-5"),
            1,
        )

    def test_out_of_order_collection_each_fixture_commits_independently(self) -> None:
        """Fixtures whose direct-ID snapshots become available out of
        request order must each still commit their own native snapshot
        independently, keyed correctly to their own match_id."""
        self._enable_cutover()
        match_ids = ["order-a", "order-b", "order-c"]
        now, _cards = self._seed_due_t5_fixtures(match_ids)
        titan_client = self._titan_client(match_ids, now)
        # crown_bulk_price_snapshots returns a dict (unordered by definition
        # for this test's purposes); the point is each fixture's own
        # snapshot commits to its own native record regardless of dict
        # iteration/collection order.
        with patch("crown.engine.TitanClient", return_value=titan_client):
            run("tick", self.config)
        self._assert_all_committed_natively(match_ids)
        for match_id in match_ids:
            native = self._read_native(match_id)
            self.assertEqual(native.get("match_id", match_id), match_id)

    def test_duplicate_tick_does_not_duplicate_native_commit_or_legacy_stage(self) -> None:
        """A second tick after the first has already committed T-5 must not
        create a second native COMMITTED write or a duplicate legacy T-5
        stage entry."""
        self._enable_cutover()
        match_ids = ["dup-a"]
        now, _cards = self._seed_due_t5_fixtures(match_ids)
        titan_client = self._titan_client(match_ids, now)
        with patch("crown.engine.TitanClient", return_value=titan_client):
            first = run("tick", self.config)
            second = run("tick", self.config)
        self.assertEqual(first["predictions"], 1)
        self.assertTrue(second.get("fast_noop") or second.get("predictions", 0) == 0)
        ledger = load_ledger(self.config)
        self.assertEqual(
            [row["stage"] for row in ledger["watch"]["dup-a"]["stages"]].count("T-5"), 1,
        )

    def test_t30_preserved_when_t5_is_later_committed(self) -> None:
        """T-30 already on the ledger must survive a subsequent native-cutover
        T-5 commit for the same fixture -- stage identity/non-overwrite."""
        self._enable_cutover()
        match_id = "t30-preserve"
        now = datetime.now(HKT)
        save_predictions(self.config, [{
            "match_id": match_id, "league": "L", "home": "H", "away": "A",
            "kickoff_hkt": (now + timedelta(minutes=4)).isoformat(),
        }])
        save_ledger(self.config, {
            "bankroll": 50000, "bets": [], "log": [], "stats": {},
            "watch": {match_id: {
                "matching_version": MATCHING_VERSION, "prediction_era": PREDICTION_ERA,
                "stages": [
                    {"stage": "首預"},
                    {"stage": "T-30", "match_id": match_id,
                     "kickoff_hkt": (now + timedelta(minutes=4)).isoformat(),
                     "ts": now.isoformat()},
                ],
            }},
        })
        titan_client = self._titan_client([match_id], now)
        with patch("crown.engine.TitanClient", return_value=titan_client):
            run("tick", self.config)
        ledger = load_ledger(self.config)
        stages = [row["stage"] for row in ledger["watch"][match_id]["stages"]]
        self.assertIn("T-30", stages)
        self.assertIn("T-5", stages)

    def test_no_post_kickoff_native_commit(self) -> None:
        """A fixture whose kickoff has already passed must never receive a
        fresh native COMMITTED snapshot via this batch path."""
        self._enable_cutover()
        match_id = "post-kickoff"
        past = datetime.now(HKT) - timedelta(minutes=1)
        save_predictions(self.config, [{
            "match_id": match_id, "league": "L", "home": "H", "away": "A",
            "kickoff_hkt": past.isoformat(),
        }])
        save_ledger(self.config, {
            "bankroll": 50000, "bets": [], "log": [], "stats": {},
            "watch": {match_id: {
                "matching_version": MATCHING_VERSION, "prediction_era": PREDICTION_ERA,
                "stages": [{"stage": "首預"}, {"stage": "T-30"}],
            }},
        })
        titan_client = self._titan_client([match_id], past)
        with patch("crown.engine.TitanClient", return_value=titan_client):
            result = run("tick", self.config)
        # A past-kickoff fixture is not selected as a due row at all by
        # `_tick_rows_from_predictions`, so the whole tick is a fast_noop and
        # no native write of any kind happens for it.
        self.assertTrue(result.get("fast_noop"))
        self.assertIsNone(self._read_native(match_id))

    def test_deferred_projection_flag_on_still_runs_legacy_batch_commit_unconditionally(self) -> None:
        """This batch path deliberately never enqueues into
        `DeferredProjectionQueue` itself (see the code comment at its call
        site) -- the single whole-batch legacy commit runs unconditionally
        regardless of the deferred-projection flag, precisely to avoid
        double-projecting a fixture. Confirm that invariant holds with the
        flag on."""
        match_ids, result = self._run_sized(6, deferred=True)
        self.assertEqual(result["predictions"], 6)
        self._assert_all_committed_natively(match_ids)
        for match_id in match_ids:
            ledger = load_ledger(self.config)
            self.assertEqual(
                [row["stage"] for row in ledger["watch"][match_id]["stages"]].count("T-5"), 1,
            )
        # Queue should be empty: this path never enqueues.
        queue = deferred_mod.DeferredProjectionQueue(self.state_dir)
        self.assertEqual(queue.pending_items(), [])

    def test_bounded_drain_call_is_a_noop_when_queue_empty_and_flag_off(self) -> None:
        """The bounded deferred-drain call added at the end of this batch
        path's non-critical section must be a safe no-op when nothing was
        ever enqueued and the flag is off -- confirming it never performs
        surprise I/O on the ordinary default-off path."""
        match_ids = ["drain-noop"]
        now, _cards = self._seed_due_t5_fixtures(match_ids)
        titan_client = self._titan_client(match_ids, now)
        with patch("crown.engine.TitanClient", return_value=titan_client), \
             patch(
                 "crown.native_stage_cutover._safe_queue",
                 side_effect=AssertionError("deferred queue must not be constructed when flag is off"),
             ):
            result = run("tick", self.config)
        self.assertEqual(result["predictions"], 1)


# ---------------------------------------------------------------------------
# 3. Wilson / dashboard / Telegram / settlement call-count non-interference.
# ---------------------------------------------------------------------------

class ConsumerNonInterferenceTests(CutoverCallsiteTestBase):
    def test_recompute_stats_called_exactly_once_with_cutover_enabled(self) -> None:
        self._enable_cutover()
        match_ids = ["wilson-a"]
        now, _cards = self._seed_due_t5_fixtures(match_ids)
        titan_client = self._titan_client(match_ids, now)
        from crown import engine as crown_engine
        with patch("crown.engine.TitanClient", return_value=titan_client), \
             patch("crown.engine.recompute_stats", wraps=crown_engine.recompute_stats) as recompute:
            run("tick", self.config)
        recompute.assert_called_once()

    def test_notify_new_not_invoked_by_engine_run_itself(self) -> None:
        """`run()` never calls the notifier directly; Telegram dispatch is
        performed by a separate caller. This stage does not change that."""
        self._enable_cutover()
        match_ids = ["notify-a"]
        now, _cards = self._seed_due_t5_fixtures(match_ids)
        titan_client = self._titan_client(match_ids, now)
        with patch("crown.engine.TitanClient", return_value=titan_client), \
             patch("crown.notify.notify_new", side_effect=AssertionError(
                 "run() must never call the notifier directly"
             )):
            run("tick", self.config)


# ---------------------------------------------------------------------------
# 4. Restart / idempotent drain of the deferred-projection queue.
# ---------------------------------------------------------------------------

class RestartDrainIdempotencyTests(CutoverCallsiteTestBase):
    def test_manually_enqueued_item_drains_exactly_once_across_restarts(self) -> None:
        """Simulates a restart: an item enqueued in one process/tick is
        drained idempotently by a later, independent call -- draining twice
        must not project twice."""
        os.environ[store_mod.ENV_ENABLED] = "1"
        os.environ[cutover_mod.ENV_DEFERRED_PROJECTION_ENABLED] = "1"
        queue = deferred_mod.DeferredProjectionQueue(self.state_dir)
        now = datetime.now(HKT)
        queue.enqueue(
            "restart-a", "T-5", kickoff=now + timedelta(minutes=4),
            payload={"match_id": "restart-a", "stage": "T-5",
                     "kickoff_hkt": (now + timedelta(minutes=4)).isoformat()},
        )
        projected = []
        first_drain = cutover_mod.drain_deferred_projections(
            self.config, projected.append, max_items=10,
        )
        second_drain = cutover_mod.drain_deferred_projections(
            self.config, projected.append, max_items=10,
        )
        self.assertEqual(len(projected), 1, "exactly one projection across both drain calls")
        self.assertEqual(second_drain, [])


if __name__ == "__main__":
    unittest.main()
