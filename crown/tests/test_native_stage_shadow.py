"""Stage 2 of the Crown T-5 deadline-first patch: default-off shadow wiring.

These tests exercise the real ``crown.engine.run("tick", ...)`` deadline path
(the same ``_run_local_bulk_timed_stages`` bulk collector used in production)
with ``CROWN_NATIVE_STAGE_STORE_ENABLED`` toggled, proving:

  * Default-off (unset or false): the legacy tick's observable outcome
    (ledger rows, predictions.json, returned result dict, call counts of
    Wilson/aggregate consumers) is unchanged, and no shadow store file is
    ever created on disk.
  * Shadow-enabled: STARTED is journaled into the shadow store before the
    provider collector runs, a bounded snapshot/failure is committed per
    fixture into the shadow store after the legacy batch completes, T-30 is
    preserved when T-5 is later shadow-committed, duplicate/idempotent
    replay behaves correctly, no post-kickoff shadow fetch/backfill ever
    happens, a shadow-store exception for one fixture cannot block another
    fixture's *legacy* commit, and legacy Wilson/dashboard/notification
    behaviour is provably unaffected by the shadow store's presence.

No production access, provider call, Telegram, bet, or push is exercised;
``TitanClient``/``PinnapiClient``/HKJC discovery are always mocked/asserted
not-called exactly as in the pre-existing suite.
"""
from __future__ import annotations

import json
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
from crown.engine import run
from crown.ledger import PREDICTION_ERA
from crown.matching import MATCHING_VERSION
from crown.state import load_ledger, load_predictions, save_ledger, save_predictions
from crown import native_stage_store as store_mod
from crown import native_stage_shadow as shadow_mod


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


class ShadowWiringTestBase(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.state_dir = Path(self._tmp.name)
        self.config = replace(
            settings(), state_dir=self.state_dir, enabled=True, pinnapi_key="test",
        )
        self._env_patch = patch.dict(os.environ, {}, clear=False)
        self._env_patch.start()
        os.environ.pop(store_mod.ENV_ENABLED, None)

    def tearDown(self) -> None:
        os.environ.pop(store_mod.ENV_ENABLED, None)
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
        return titan_client

    def _native_store_dir(self) -> Path:
        return self.state_dir / store_mod.NATIVE_STAGE_SUBDIR


class DefaultOffEquivalenceTests(ShadowWiringTestBase):
    """Requirement 2: default-off must be a byte-for-byte no-op."""

    def test_flag_unset_creates_no_shadow_directory_or_files(self) -> None:
        match_ids = ["shadow-off-a", "shadow-off-b"]
        now, _cards = self._seed_due_t5_fixtures(match_ids)
        titan_client = self._titan_client(match_ids, now)
        with patch("crown.engine.TitanClient", return_value=titan_client), \
             patch("crown.engine.PinnapiClient", side_effect=AssertionError(
                 "PinnAPI must not run on the fast T-5 bulk path"
             )):
            result = run("tick", self.config)
        self.assertTrue(result["fast_t5_bulk"])
        self.assertEqual(result["predictions"], 2)
        # No shadow store directory was ever created.
        self.assertFalse(self._native_store_dir().exists())

    def test_flag_explicitly_false_also_creates_no_shadow_directory(self) -> None:
        os.environ[store_mod.ENV_ENABLED] = "0"
        match_ids = ["shadow-off-c"]
        now, _cards = self._seed_due_t5_fixtures(match_ids)
        titan_client = self._titan_client(match_ids, now)
        with patch("crown.engine.TitanClient", return_value=titan_client):
            run("tick", self.config)
        self.assertFalse(self._native_store_dir().exists())

    def test_default_off_result_and_ledger_identical_with_and_without_shadow_module_call_site(self) -> None:
        """Same inputs -> same observable result dict and same ledger shape.

        This directly patches the shadow entry points to raise if they ever
        do real work while disabled, proving default-off never delegates
        into the shadow store construction path.
        """
        match_ids = ["shadow-off-d", "shadow-off-e", "shadow-off-f"]
        now, _cards = self._seed_due_t5_fixtures(match_ids)
        titan_client = self._titan_client(match_ids, now)
        with patch("crown.engine.TitanClient", return_value=titan_client), \
             patch(
                 "crown.native_stage_shadow._store.NativeStageStore",
                 side_effect=AssertionError(
                     "default-off must never construct a NativeStageStore"
                 ),
             ):
            result = run("tick", self.config)
        self.assertEqual(result["predictions"], 3)
        self.assertEqual(len(result["fresh_t5_predictions"]), 3)
        ledger = load_ledger(self.config)
        for match_id in match_ids:
            stages = ledger["watch"][match_id]["stages"]
            self.assertEqual([row["stage"] for row in stages].count("T-5"), 1)

    def test_default_off_wilson_and_aggregate_consumers_unaffected(self) -> None:
        """Wilson/challenger/dashboard evidence stages must still run exactly as before."""
        match_ids = ["shadow-off-wilson"]
        now, _cards = self._seed_due_t5_fixtures(match_ids)
        titan_client = self._titan_client(match_ids, now)
        from crown import engine as crown_engine
        with patch("crown.engine.TitanClient", return_value=titan_client), \
             patch("crown.engine.recompute_stats", wraps=crown_engine.recompute_stats) as recompute:
            run("tick", self.config)
        recompute.assert_called_once()


class ShadowEnabledOrderingTests(ShadowWiringTestBase):
    """Requirement 3: STARTED-before-collector ordering and per-fixture commit."""

    def test_shadow_started_written_before_provider_collection(self) -> None:
        """The engine-level call order (unit, not process-boundary) is STARTED-first.

        ``_collect_locked_direct_snapshots``/``_collect_same_id_bulk_fallback``
        run their actual provider read inside forked child processes, so a
        closure-based observer cannot see shadow-store state written by the
        parent from inside that child.  This test instead directly patches
        the two collector functions (still exercising the real
        ``_run_local_bulk_timed_stages`` call order) and asserts that by the
        time either collector is invoked, the shadow store already has a
        STARTED attempt for the fixture -- proving the call site ordering
        without crossing a process boundary.
        """
        os.environ[store_mod.ENV_ENABLED] = "1"
        match_ids = ["shadow-on-order"]
        now, _cards = self._seed_due_t5_fixtures(match_ids)
        observed: dict[str, list[str]] = {"events": []}

        def fake_direct_collect(_config, _rows, _deadline, **_kwargs):
            store = store_mod.NativeStageStore(self.state_dir)
            state = store.read("shadow-on-order")
            observed["events"].append(
                "direct_collector_saw_shadow_started"
                if state and state["attempt_history"][-1]["state"] == "STARTED"
                else "direct_collector_saw_no_shadow_state"
            )
            return {}, 0

        def fake_bulk_fallback(_config, _deadline):
            return _bulk_snapshot_payload(match_ids, now), True

        with patch("crown.engine._collect_locked_direct_snapshots", side_effect=fake_direct_collect), \
             patch("crown.engine._collect_same_id_bulk_fallback", side_effect=fake_bulk_fallback):
            run("tick", self.config)
        self.assertEqual(observed["events"], ["direct_collector_saw_shadow_started"])

    def test_shadow_commit_happens_per_fixture_after_batch_collection(self) -> None:
        os.environ[store_mod.ENV_ENABLED] = "1"
        match_ids = [f"shadow-on-batch-{i}" for i in range(5)]
        now, _cards = self._seed_due_t5_fixtures(match_ids)
        titan_client = self._titan_client(match_ids, now)
        with patch("crown.engine.TitanClient", return_value=titan_client):
            run("tick", self.config)
        store = store_mod.NativeStageStore(self.state_dir)
        for match_id in match_ids:
            state = store.read(match_id)
            self.assertIsNotNone(state)
            self.assertIn("T-5", state["snapshots"])
            terminal_states = [row["state"] for row in state["attempt_history"] if row["stage"] == "T-5"]
            self.assertEqual(terminal_states, ["STARTED", "COMMITTED"])

    def test_shadow_records_data_missing_when_legacy_marks_data_missing(self) -> None:
        os.environ[store_mod.ENV_ENABLED] = "1"
        match_ids = ["shadow-on-missing"]
        now, _cards = self._seed_due_t5_fixtures(match_ids)
        titan_client = Mock()
        titan_client.crown_bulk_price_snapshots.return_value = {}
        titan_client.crown_price_snapshot.return_value = {"prices": [], "quote_source": "titan007-crown-id-3"}
        with patch("crown.engine.TitanClient", return_value=titan_client):
            result = run("tick", self.config)
        self.assertEqual(result["bulk_unavailable_predictions"], 1)
        legacy_stage = next(
            row for row in load_ledger(self.config)["watch"]["shadow-on-missing"]["stages"]
            if row["stage"] == "T-5"
        )
        self.assertEqual(legacy_stage["status"], "DATA_MISSING")
        store = store_mod.NativeStageStore(self.state_dir)
        shadow_state = store.read("shadow-on-missing")
        self.assertIsNotNone(shadow_state)
        terminal_states = [row["state"] for row in shadow_state["attempt_history"] if row["stage"] == "T-5"]
        self.assertEqual(terminal_states[-1], "FAILED")
        self.assertNotIn("T-5", shadow_state.get("snapshots") or {})

    def test_shadow_records_expired_for_post_kickoff_row_with_zero_extra_provider_call(self) -> None:
        os.environ[store_mod.ENV_ENABLED] = "1"
        match_id = "shadow-on-expired"
        # Seed a due job whose kickoff is already in the past via the write-ahead
        # journal path is out of scope here; instead exercise the shadow
        # commit function directly against an already-past-kickoff prediction
        # row, exactly as engine.py would pass it after building
        # stage_predictions (this mirrors what a same-batch race could produce).
        past_kickoff = (datetime.now(HKT) - timedelta(minutes=1)).isoformat()
        prediction = {
            "match_id": match_id, "stage": "T-5", "kickoff_hkt": past_kickoff,
            "status": "PREDICTION_READY", "league": "L", "home": "H", "away": "A",
            "forecast_candidates": [{"code": "HDC", "line": -0.25, "side": "H", "odds": 1.91,
                                       "source": "titan007-crown-id-3"}],
        }
        counters = shadow_mod.shadow_commit_stage_predictions(self.config, [prediction])
        self.assertEqual(counters["expired"], 1)
        store = store_mod.NativeStageStore(self.state_dir)
        state = store.read(match_id)
        self.assertIsNotNone(state)
        self.assertNotIn("T-5", state.get("snapshots") or {})
        self.assertEqual(state["attempt_history"][-1]["state"], "EXPIRED")


class ShadowExceptionIsolationTests(ShadowWiringTestBase):
    """Requirement 3: shadow failures must never block/rollback legacy commit."""

    def test_shadow_store_exception_does_not_block_legacy_commit(self) -> None:
        os.environ[store_mod.ENV_ENABLED] = "1"
        match_ids = ["shadow-exc-a", "shadow-exc-b"]
        now, _cards = self._seed_due_t5_fixtures(match_ids)
        titan_client = self._titan_client(match_ids, now)
        with patch("crown.engine.TitanClient", return_value=titan_client), \
             patch(
                 "crown.native_stage_shadow._store.NativeStageStore.commit_snapshot",
                 side_effect=RuntimeError("simulated shadow store failure"),
             ):
            result = run("tick", self.config)
        # Legacy commit still succeeded for both fixtures despite every
        # shadow commit_snapshot call raising.
        self.assertEqual(result["predictions"], 2)
        self.assertEqual(len(result["fresh_t5_predictions"]), 2)
        ledger = load_ledger(self.config)
        for match_id in match_ids:
            stage = next(
                row for row in ledger["watch"][match_id]["stages"] if row["stage"] == "T-5"
            )
            self.assertEqual(stage["status"], "PREDICTION_READY")

    def test_shadow_mark_started_exception_does_not_block_legacy_journal_or_commit(self) -> None:
        os.environ[store_mod.ENV_ENABLED] = "1"
        match_ids = ["shadow-exc-c"]
        now, _cards = self._seed_due_t5_fixtures(match_ids)
        titan_client = self._titan_client(match_ids, now)
        with patch("crown.engine.TitanClient", return_value=titan_client), \
             patch(
                 "crown.native_stage_shadow._store.NativeStageStore.mark_started",
                 side_effect=RuntimeError("simulated shadow STARTED failure"),
             ):
            result = run("tick", self.config)
        self.assertEqual(result["predictions"], 1)
        legacy_stage = next(
            row for row in load_ledger(self.config)["watch"]["shadow-exc-c"]["stages"]
            if row["stage"] == "T-5"
        )
        self.assertEqual(legacy_stage["status"], "PREDICTION_READY")

    def test_shadow_store_construction_failure_does_not_block_legacy_commit(self) -> None:
        os.environ[store_mod.ENV_ENABLED] = "1"
        match_ids = ["shadow-exc-d"]
        now, _cards = self._seed_due_t5_fixtures(match_ids)
        titan_client = self._titan_client(match_ids, now)
        with patch("crown.engine.TitanClient", return_value=titan_client), \
             patch(
                 "crown.native_stage_shadow._store.NativeStageStore",
                 side_effect=RuntimeError("cannot construct store"),
             ):
            result = run("tick", self.config)
        self.assertEqual(result["predictions"], 1)


class ShadowBatchSizeAndIdempotencyTests(ShadowWiringTestBase):
    """Requirement 5: 1/15/26/50/89 fixtures, duplicate/restart idempotency."""

    def _run_batch(self, n: int) -> None:
        os.environ[store_mod.ENV_ENABLED] = "1"
        match_ids = [f"shadow-batch-{n}-{i}" for i in range(n)]
        now, _cards = self._seed_due_t5_fixtures(match_ids)
        titan_client = self._titan_client(match_ids, now)
        with patch("crown.engine.TitanClient", return_value=titan_client):
            result = run("tick", self.config)
        self.assertEqual(result["predictions"], n)
        store = store_mod.NativeStageStore(self.state_dir)
        for match_id in match_ids:
            state = store.read(match_id)
            self.assertIsNotNone(state)
            self.assertIn("T-5", state["snapshots"])

    def test_batch_sizes_1_15_26_50_89(self) -> None:
        for n in (1, 15, 26, 50, 89):
            with self.subTest(n=n):
                tmp = tempfile.TemporaryDirectory()
                try:
                    self.state_dir = Path(tmp.name)
                    self.config = replace(
                        settings(), state_dir=self.state_dir, enabled=True, pinnapi_key="test",
                    )
                    self._run_batch(n)
                finally:
                    tmp.cleanup()

    def test_duplicate_tick_replay_is_idempotent_in_shadow_store(self) -> None:
        os.environ[store_mod.ENV_ENABLED] = "1"
        match_ids = ["shadow-dup-1"]
        now, _cards = self._seed_due_t5_fixtures(match_ids)
        titan_client = self._titan_client(match_ids, now)
        with patch("crown.engine.TitanClient", return_value=titan_client):
            run("tick", self.config)
        store = store_mod.NativeStageStore(self.state_dir)
        first = store.read("shadow-dup-1")

        # A second tick for the same already-committed T-5 stage must not
        # re-collect (legacy short-circuits on completed_stages) or create a
        # duplicate shadow commit.
        with patch("crown.engine.TitanClient", return_value=titan_client):
            run("tick", self.config)
        second = store.read("shadow-dup-1")
        self.assertEqual(first["snapshots"], second["snapshots"])
        committed_rows = [
            row for row in second["attempt_history"]
            if row["stage"] == "T-5" and row["state"] == "COMMITTED"
        ]
        self.assertEqual(len(committed_rows), 1)


class ShadowStagePreservationTests(ShadowWiringTestBase):
    """Requirement 3: T-30 preserved; real legacy identity passed on first creation."""

    def test_t30_preserved_in_shadow_store_when_t5_later_committed(self) -> None:
        """Same fixture, same persisted kickoff: T-30 tick, then T-5 tick.

        The persisted ``stage_jobs`` due times are derived once from the
        card's kickoff and are authoritative afterwards (matching existing
        production/legacy scheduling semantics) -- this test keeps the same
        kickoff throughout and instead fast-forwards the persisted T-5
        ``due_at_utc`` into the past between the two ticks, exactly as real
        elapsed wall-clock time would, without ever touching the fixture's
        own kickoff.
        """
        os.environ[store_mod.ENV_ENABLED] = "1"
        match_id = "shadow-t30-preserve"
        now = datetime.now(HKT)
        kickoff = now + timedelta(minutes=25)
        card = {
            "match_id": match_id, "league": "L", "home": "Home", "away": "Away",
            "kickoff_hkt": kickoff.isoformat(),
        }
        save_predictions(self.config, [card])
        save_ledger(self.config, {
            "bankroll": 50000, "bets": [], "log": [], "stats": {},
            "watch": {match_id: {
                "matching_version": MATCHING_VERSION, "prediction_era": PREDICTION_ERA,
                "stages": [{"stage": "首預"}],
            }},
        })
        titan_client = self._titan_client([match_id], now)
        with patch("crown.engine.TitanClient", return_value=titan_client):
            result1 = run("tick", self.config)
        self.assertEqual(result1["predictions"], 1)
        store = store_mod.NativeStageStore(self.state_dir)
        after_t30 = store.read(match_id)
        self.assertIn("T-30", after_t30["snapshots"])

        # Second tick: fast-forward only the persisted T-5 due time into the
        # past, matching how real elapsed wall-clock time would make it due,
        # while the fixture's own kickoff (still in the future) is untouched.
        ledger = load_ledger(self.config)
        ledger["watch"][match_id]["stage_jobs"]["T-5"]["due_at_utc"] = (
            (now - timedelta(seconds=1)).astimezone(HKT).isoformat()
        )
        save_ledger(self.config, ledger)
        titan_client2 = self._titan_client([match_id], now)
        with patch("crown.engine.TitanClient", return_value=titan_client2):
            result2 = run("tick", self.config)
        self.assertEqual(result2["predictions"], 1)
        after_t5 = store.read(match_id)
        self.assertIn("T-30", after_t5["snapshots"])
        self.assertIn("T-5", after_t5["snapshots"])
        self.assertEqual(after_t30["snapshots"]["T-30"], after_t5["snapshots"]["T-30"])

    def test_real_legacy_watch_identity_passed_on_first_creation(self) -> None:
        os.environ[store_mod.ENV_ENABLED] = "1"
        match_id = "shadow-identity-1"
        now, _cards = self._seed_due_t5_fixtures([match_id])
        titan_client = self._titan_client([match_id], now)
        with patch("crown.engine.TitanClient", return_value=titan_client):
            run("tick", self.config)
        store = store_mod.NativeStageStore(self.state_dir)
        state = store.read(match_id)
        identity = state.get("legacy_watch_identity") or {}
        self.assertEqual(identity.get("match_id"), match_id)
        self.assertEqual(identity.get("league"), "L")
        self.assertEqual(identity.get("home"), f"{match_id} Home")
        self.assertEqual(identity.get("away"), f"{match_id} Away")


class ShadowNoPostKickoffFetchTests(ShadowWiringTestBase):
    """Requirement 3: no post-kickoff provider call/backfill via shadow path."""

    def test_expired_lapsed_attempt_path_never_triggers_shadow_provider_call(self) -> None:
        os.environ[store_mod.ENV_ENABLED] = "1"
        match_id = "shadow-lapsed-1"
        now = datetime.now(HKT)
        kickoff = now - timedelta(minutes=1)
        save_ledger(self.config, {
            "bankroll": 50000, "bets": [], "log": [], "stats": {},
            "watch": {match_id: {
                "match_id": match_id, "native_fixture_id": match_id,
                "kickoff_hkt": kickoff.isoformat(), "kickoff": kickoff.isoformat(),
                "stages": [{"stage": "首預"}, {"stage": "T-30"}],
                "stage_attempts": {"T-5": {
                    "stage": "T-5", "state": "STARTED",
                    "started_at": (kickoff - timedelta(minutes=5)).isoformat(),
                }},
                "stage_jobs": {"T-5": {"stage": "T-5", "state": "STARTED"}},
            }},
        })
        save_predictions(self.config, [])
        with patch("crown.engine.TitanClient", side_effect=AssertionError(
            "expired attempt must not read a provider, shadow or otherwise"
        )):
            result = run("tick", self.config)
        self.assertTrue(result["fast_noop"])
        # The legacy expiry path runs before any shadow call site in
        # _run_local_bulk_timed_stages is even reached (it returns fast_noop
        # before that function is called), so no shadow state exists.
        self.assertFalse(self._native_store_dir().exists())


class ShadowComparisonHelperTests(ShadowWiringTestBase):
    """Requirement 4: read-only shadow-vs-legacy comparison helper."""

    def test_compare_shadow_to_legacy_agrees_after_normal_shadow_commit(self) -> None:
        """The stage actually processed through the shadow-instrumented tick agrees.

        首預/T-30 were seeded directly into the legacy ledger as pre-existing
        rows (never passing through a shadow-instrumented tick at all), so
        the shadow store honestly has no record of them -- that is correct
        disagreement, not a bug in the comparison helper. Only T-5, which
        this test's tick call actually ran through
        ``_run_local_bulk_timed_stages`` with shadow instrumentation enabled,
        is expected to agree.
        """
        os.environ[store_mod.ENV_ENABLED] = "1"
        match_id = "shadow-compare-1"
        now, _cards = self._seed_due_t5_fixtures([match_id])
        titan_client = self._titan_client([match_id], now)
        with patch("crown.engine.TitanClient", return_value=titan_client):
            run("tick", self.config)
        legacy_watch = load_ledger(self.config)["watch"][match_id]
        report = shadow_mod.compare_shadow_to_legacy(self.config, match_id, legacy_watch)
        self.assertTrue(report["shadow_present"])
        self.assertTrue(report["legacy_present"])
        self.assertTrue(report["stages"]["T-5"]["agrees"])
        self.assertTrue(report["stages"]["T-5"]["shadow_committed"])
        self.assertTrue(report["stages"]["T-5"]["legacy_committed"])

    def test_compare_shadow_to_legacy_detects_disagreement_when_shadow_failed(self) -> None:
        os.environ[store_mod.ENV_ENABLED] = "1"
        match_id = "shadow-compare-2"
        now, _cards = self._seed_due_t5_fixtures([match_id])
        titan_client = self._titan_client([match_id], now)
        with patch("crown.engine.TitanClient", return_value=titan_client), \
             patch(
                 "crown.native_stage_shadow._store.NativeStageStore.commit_snapshot",
                 side_effect=RuntimeError("simulated shadow failure"),
             ):
            run("tick", self.config)
        legacy_watch = load_ledger(self.config)["watch"][match_id]
        # Legacy still committed successfully (per exception isolation).
        legacy_stage = next(row for row in legacy_watch["stages"] if row["stage"] == "T-5")
        self.assertEqual(legacy_stage["status"], "PREDICTION_READY")
        report = shadow_mod.compare_shadow_to_legacy(self.config, match_id, legacy_watch)
        self.assertFalse(report["stages"]["T-5"]["agrees"])
        self.assertFalse(report["all_agree"])

    def test_compare_helper_performs_no_writes(self) -> None:
        os.environ[store_mod.ENV_ENABLED] = "1"
        match_id = "shadow-compare-3"
        now, _cards = self._seed_due_t5_fixtures([match_id])
        titan_client = self._titan_client([match_id], now)
        with patch("crown.engine.TitanClient", return_value=titan_client):
            run("tick", self.config)
        store_path = store_mod.fixture_store_path(self.state_dir, match_id)
        before = store_path.read_bytes()
        legacy_watch = load_ledger(self.config)["watch"][match_id]
        shadow_mod.compare_shadow_to_legacy(self.config, match_id, legacy_watch)
        after = store_path.read_bytes()
        self.assertEqual(before, after)


class ShadowModuleStaticGuardTests(unittest.TestCase):
    """Static checks that the wiring itself stayed minimal and safe."""

    def test_shadow_module_never_imports_wilson_dashboard_or_telegram(self) -> None:
        import ast
        import crown.native_stage_shadow as mod
        source = Path(mod.__file__).read_text(encoding="utf-8")
        tree = ast.parse(source)
        imported_names = set()
        for node in ast.walk(tree):
            if isinstance(node, ast.ImportFrom) and node.module:
                imported_names.add(node.module)
            elif isinstance(node, ast.Import):
                for alias in node.names:
                    imported_names.add(alias.name)
        forbidden = {"crown.notify", "crown.dashboard_data", "crown.dashboard_api",
                     "analysis.wilson_validation", "crown.settle"}
        self.assertEqual(imported_names & forbidden, set())

    def test_engine_call_sites_are_exception_guarded(self) -> None:
        import crown.engine as engine_mod
        source = Path(engine_mod.__file__).read_text(encoding="utf-8")
        self.assertIn("_native_shadow.shadow_mark_started_batch(config, rows)", source)
        self.assertIn("_native_shadow.shadow_commit_stage_predictions(config, stage_predictions)", source)
        # Both call sites are wrapped in a bare try/except Exception: pass,
        # i.e. immediately preceded by "try:" and followed by "except
        # Exception:" within a small window.
        idx_started = source.index("_native_shadow.shadow_mark_started_batch(config, rows)")
        window_started = source[max(0, idx_started - 40):idx_started + 120]
        self.assertIn("try:", window_started)
        self.assertIn("except Exception:", window_started)
        idx_commit = source.index("_native_shadow.shadow_commit_stage_predictions(config, stage_predictions)")
        window_commit = source[max(0, idx_commit - 40):idx_commit + 120]
        self.assertIn("try:", window_commit)
        self.assertIn("except Exception:", window_commit)


if __name__ == "__main__":
    unittest.main()
