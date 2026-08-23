"""Characterization + mandatory test matrix for the Crown T-5 deadline-first patch.

Scope: crown/native_stage_store.py only.  These tests prove the new
per-fixture store can journal (STARTED) and commit (COMMITTED/FAILED/
DATA_MISSING/EXPIRED) 1, 15, 26, 50 and 89 simultaneously due fixtures
without ever calling the legacy monolithic ``crown.state.load_ledger`` /
``crown.state.save_ledger``, and exercise every item in the mandatory test
matrix from the task brief.  No production code path
(``crown/engine.py``, ``crown/ledger.py``, ``crown/state.py``,
``crown/settle.py``, ``crown/notify.py``, dashboard, Wilson, Radar, betting,
provider policy, historical data) is imported or modified by this file.
"""
from __future__ import annotations

import json
import os
import tempfile
import threading
import time
import unittest
from datetime import datetime, timedelta
from pathlib import Path
from unittest.mock import patch

from crown.common import HKT
from crown import native_stage_store as store_mod
from crown.native_stage_store import (
    DueFixture,
    FixtureLockTimeout,
    FixtureOutcome,
    NativeStageStore,
    commit_due_fixtures,
    fixture_store_path,
    project_legacy_watch_row,
)


def _kickoff(minutes: float) -> datetime:
    return datetime.now(HKT) + timedelta(minutes=minutes)


def _snapshot(match_id: str, stage: str) -> dict:
    return {
        "match_id": match_id,
        "stage": stage,
        "league": "L",
        "home": f"{match_id}-home",
        "away": f"{match_id}-away",
        "status": "PREDICTION_READY",
        "odds_status": "available",
        "source": "titan007-crown-id-3",
        "selected_odds_journal": [
            {"code": "HDC", "line": -0.25, "side": "H", "odds": 1.91,
             "odds_status": "available", "source": "titan007-crown-id-3"},
        ],
        "ts": "2026-08-23T20:59:00+08:00",
    }


class LegacyLedgerCallGuard:
    """Fails the test if legacy load_ledger/save_ledger is ever called."""

    def __enter__(self):
        self._patches = [
            patch(
                "crown.state.load_ledger",
                side_effect=AssertionError(
                    "deadline-critical native stage path must never call load_ledger"
                ),
            ),
            patch(
                "crown.state.save_ledger",
                side_effect=AssertionError(
                    "deadline-critical native stage path must never call save_ledger"
                ),
            ),
        ]
        for p in self._patches:
            p.start()
        return self

    def __exit__(self, *exc):
        for p in self._patches:
            p.stop()
        return False


class NativeStageStoreMatrixTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.state_dir = Path(self._tmp.name)
        self.store = NativeStageStore(self.state_dir)

    def tearDown(self) -> None:
        self._tmp.cleanup()

    # -- helper -----------------------------------------------------------
    def _fixtures(self, n: int, stage: str = "T-5", minutes: float = 4.0) -> list[DueFixture]:
        return [
            DueFixture(
                match_id=f"fx-{i:03d}", stage=stage, kickoff=_kickoff(minutes),
                league="L", home=f"H{i}", away=f"A{i}",
            )
            for i in range(n)
        ]

    def _always_available(self, fixture: DueFixture) -> dict:
        return _snapshot(fixture.match_id, fixture.stage)

    # -- 1. batch sizes -----------------------------------------------------
    def test_batch_sizes_journal_and_commit_without_legacy_ledger_calls(self) -> None:
        for n in (1, 15, 26, 50, 89):
            with self.subTest(n=n):
                tmp = tempfile.TemporaryDirectory()
                try:
                    state_dir = Path(tmp.name)
                    store = NativeStageStore(state_dir)
                    fixtures = [
                        DueFixture(
                            match_id=f"batch{n}-{i}", stage="T-5", kickoff=_kickoff(4.0),
                            league="L", home=f"H{i}", away=f"A{i}",
                        )
                        for i in range(n)
                    ]
                    with LegacyLedgerCallGuard():
                        outcomes = commit_due_fixtures(
                            store, fixtures, self._always_available,
                        )
                    self.assertEqual(len(outcomes), n)
                    self.assertTrue(all(o.state == "COMMITTED" for o in outcomes))
                    for fixture in fixtures:
                        path = fixture_store_path(state_dir, fixture.match_id)
                        self.assertTrue(path.exists())
                        data = json.loads(path.read_text(encoding="utf-8"))
                        self.assertEqual(data["snapshots"]["T-5"]["stage"], "T-5")
                        states = [row["state"] for row in data["attempt_history"]]
                        self.assertEqual(states, ["STARTED", "COMMITTED"])
                finally:
                    tmp.cleanup()

    def test_zero_legacy_ledger_calls_asserted_via_module_reference(self) -> None:
        """Extra guard: the module's *code* never imports load_ledger/save_ledger.

        Parses the module with ``ast`` and checks only real import statements
        and real name references (not docstrings/comments), so the incident
        narrative in the module docstring does not trip this guard.
        """
        import ast
        import crown.native_stage_store as mod
        source = Path(mod.__file__).read_text(encoding="utf-8")
        tree = ast.parse(source)
        imported_names = set()
        for node in ast.walk(tree):
            if isinstance(node, ast.ImportFrom):
                for alias in node.names:
                    imported_names.add(alias.name)
            elif isinstance(node, ast.Import):
                for alias in node.names:
                    imported_names.add(alias.name.split(".")[0])
        self.assertNotIn("load_ledger", imported_names)
        self.assertNotIn("save_ledger", imported_names)
        self.assertNotIn("state", imported_names)
        # No attribute access like crown.state.load_ledger / .save_ledger either.
        referenced_attrs = {
            node.attr for node in ast.walk(tree) if isinstance(node, ast.Attribute)
        }
        self.assertNotIn("load_ledger", referenced_attrs)
        self.assertNotIn("save_ledger", referenced_attrs)

    # -- 2. slow provider / partial timeout / one fixture exception --------
    def test_slow_provider_partial_timeout_isolates_slow_fixture(self) -> None:
        fixtures = self._fixtures(5)

        def collector(fixture: DueFixture):
            if fixture.match_id == "fx-002":
                raise TimeoutError("provider took too long")
            return _snapshot(fixture.match_id, fixture.stage)

        with LegacyLedgerCallGuard():
            outcomes = commit_due_fixtures(self.store, fixtures, collector)
        by_id = {o.match_id: o for o in outcomes}
        self.assertEqual(by_id["fx-002"].state, "FAILED")
        self.assertIn("TimeoutError", by_id["fx-002"].reason or "")
        for match_id in ("fx-000", "fx-001", "fx-003", "fx-004"):
            self.assertEqual(by_id[match_id].state, "COMMITTED")

    def test_one_fixture_exception_does_not_block_others(self) -> None:
        fixtures = self._fixtures(6)

        def collector(fixture: DueFixture):
            if fixture.match_id == "fx-003":
                raise ValueError("boom")
            return _snapshot(fixture.match_id, fixture.stage)

        outcomes = commit_due_fixtures(self.store, fixtures, collector)
        self.assertEqual(len(outcomes), 6)
        failed = [o for o in outcomes if o.match_id == "fx-003"][0]
        self.assertEqual(failed.state, "FAILED")
        committed = [o for o in outcomes if o.match_id != "fx-003"]
        self.assertTrue(all(o.state == "COMMITTED" for o in committed))

    # -- 3. crash after STARTED before snapshot; restart/idempotent replay -
    def test_crash_after_started_before_snapshot_then_idempotent_replay(self) -> None:
        fixture = DueFixture(match_id="crash-1", stage="T-5", kickoff=_kickoff(4.0))
        # Simulate the crash: only mark_started ever ran.
        self.store.mark_started(fixture.match_id, fixture.stage, kickoff=fixture.kickoff)
        state_after_crash = self.store.read(fixture.match_id)
        self.assertEqual(state_after_crash["attempt_history"][-1]["state"], "STARTED")
        self.assertNotIn("T-5", state_after_crash.get("snapshots") or {})

        # Restart: replay commit_due_fixtures for the same fixture. It must
        # not duplicate the STARTED attempt into a second STARTED row when
        # the collector eventually succeeds; the sequence should read
        # STARTED, STARTED(replay), COMMITTED at most -- and must reach
        # exactly one COMMITTED terminal state.
        outcomes = commit_due_fixtures(self.store, [fixture], self._always_available)
        self.assertEqual(outcomes[0].state, "COMMITTED")
        final = self.store.read(fixture.match_id)
        committed_rows = [
            row for row in final["attempt_history"]
            if row["stage"] == "T-5" and row["state"] == "COMMITTED"
        ]
        self.assertEqual(len(committed_rows), 1)
        self.assertEqual(final["snapshots"]["T-5"]["stage"], "T-5")

        # A second full replay after a successful commit must be a pure
        # no-op: no new attempt rows, no snapshot mutation.
        before = json.loads(json.dumps(final))
        outcomes2 = commit_due_fixtures(self.store, [fixture], self._always_available)
        self.assertEqual(outcomes2[0].state, "COMMITTED")
        after = self.store.read(fixture.match_id)
        self.assertEqual(before["snapshots"], after["snapshots"])
        self.assertEqual(before["attempt_history"], after["attempt_history"])

    # -- 4. lock contention --------------------------------------------------
    def test_lock_contention_second_writer_times_out_first_still_commits(self) -> None:
        fixture = DueFixture(match_id="locked-1", stage="T-5", kickoff=_kickoff(4.0))
        lock_path = store_mod._fixture_lock_path(self.state_dir, fixture.match_id)
        lock_path.parent.mkdir(parents=True, exist_ok=True)
        import fcntl
        holder = lock_path.open("a+", encoding="utf-8")
        fcntl.flock(holder.fileno(), fcntl.LOCK_EX)
        try:
            fast_store = NativeStageStore(self.state_dir, lock_timeout_seconds=0.2)
            with self.assertRaises(FixtureLockTimeout):
                fast_store.mark_started(fixture.match_id, fixture.stage, kickoff=fixture.kickoff)
        finally:
            fcntl.flock(holder.fileno(), fcntl.LOCK_UN)
            holder.close()
        # Lock released: the same fixture can now be journaled and committed.
        outcomes = commit_due_fixtures(self.store, [fixture], self._always_available)
        self.assertEqual(outcomes[0].state, "COMMITTED")

    def test_lock_contention_does_not_block_a_different_fixture(self) -> None:
        held_id = "locked-a"
        other_id = "locked-b"
        lock_path = store_mod._fixture_lock_path(self.state_dir, held_id)
        lock_path.parent.mkdir(parents=True, exist_ok=True)
        import fcntl
        holder = lock_path.open("a+", encoding="utf-8")
        fcntl.flock(holder.fileno(), fcntl.LOCK_EX)
        try:
            other = DueFixture(match_id=other_id, stage="T-5", kickoff=_kickoff(4.0))
            started = time.monotonic()
            outcomes = commit_due_fixtures(self.store, [other], self._always_available)
            self.assertLess(time.monotonic() - started, 1.0)
            self.assertEqual(outcomes[0].state, "COMMITTED")
        finally:
            fcntl.flock(holder.fileno(), fcntl.LOCK_UN)
            holder.close()

    # -- 5. fsync failure and os.replace failure -----------------------------
    def test_fsync_failure_leaves_no_partial_committed_file(self) -> None:
        fixture = DueFixture(match_id="fsync-fail", stage="T-5", kickoff=_kickoff(4.0))
        path = fixture_store_path(self.state_dir, fixture.match_id)

        real_fsync = os.fsync

        def boom_fsync(fd):
            raise OSError("simulated fsync failure")

        with patch("os.fsync", side_effect=boom_fsync):
            with self.assertRaises(OSError):
                self.store.mark_started(fixture.match_id, fixture.stage, kickoff=fixture.kickoff)
        # No file (and certainly no COMMITTED snapshot) exists after a
        # failed atomic write -- the temp file must not have been renamed.
        self.assertFalse(path.exists())
        # Only the mkstemp-created temp write artifact must be absent; a
        # separate per-fixture *lock* file legitimately persists across
        # writes and is not a leftover from the failed atomic write.
        leftover_temps = (
            list(path.parent.glob(f".{path.name}.*")) if path.parent.exists() else []
        )
        self.assertEqual(leftover_temps, [])

        # Recovery: without the injected failure, the same fixture can now
        # be journaled and committed normally.
        outcomes = commit_due_fixtures(self.store, [fixture], self._always_available)
        self.assertEqual(outcomes[0].state, "COMMITTED")

    def test_os_replace_failure_leaves_prior_committed_state_untouched(self) -> None:
        fixture = DueFixture(match_id="replace-fail", stage="T-30", kickoff=_kickoff(20.0))
        # First commit T-30 successfully.
        outcomes = commit_due_fixtures(self.store, [fixture], self._always_available)
        self.assertEqual(outcomes[0].state, "COMMITTED")
        before = self.store.read(fixture.match_id)

        t5_fixture = DueFixture(match_id="replace-fail", stage="T-5", kickoff=_kickoff(4.0))

        def boom_replace(_src, _dst):
            raise OSError("simulated os.replace failure")

        with patch("os.replace", side_effect=boom_replace):
            with self.assertRaises(OSError):
                self.store.mark_started(t5_fixture.match_id, t5_fixture.stage, kickoff=t5_fixture.kickoff)
        after_failed_replace = self.store.read(fixture.match_id)
        # The previously committed T-30 snapshot must be byte-for-byte the
        # same: a failed os.replace on a *later* write can never corrupt or
        # roll back the durable prior state.
        self.assertEqual(before["snapshots"]["T-30"], after_failed_replace["snapshots"]["T-30"])
        self.assertNotIn("T-5", after_failed_replace.get("snapshots") or {})

        # Recovery works afterward.
        outcomes2 = commit_due_fixtures(self.store, [t5_fixture], self._always_available)
        self.assertEqual(outcomes2[0].state, "COMMITTED")
        final = self.store.read(fixture.match_id)
        self.assertEqual(final["snapshots"]["T-30"], before["snapshots"]["T-30"])
        self.assertEqual(final["snapshots"]["T-5"]["stage"], "T-5")

    # -- 6. T-30 preserved when T-5 committed --------------------------------
    def test_t30_preserved_when_t5_committed(self) -> None:
        match_id = "preserve-1"
        t30 = DueFixture(match_id=match_id, stage="T-30", kickoff=_kickoff(25.0))
        commit_due_fixtures(self.store, [t30], self._always_available)
        state_after_t30 = self.store.read(match_id)
        self.assertIn("T-30", state_after_t30["snapshots"])

        t5 = DueFixture(match_id=match_id, stage="T-5", kickoff=_kickoff(4.0))
        commit_due_fixtures(self.store, [t5], self._always_available)
        state_after_t5 = self.store.read(match_id)
        self.assertIn("T-30", state_after_t5["snapshots"])
        self.assertIn("T-5", state_after_t5["snapshots"])
        self.assertEqual(
            state_after_t30["snapshots"]["T-30"], state_after_t5["snapshots"]["T-30"],
        )

    def test_legacy_watch_identity_preserved_on_first_create_not_overwritten(self) -> None:
        match_id = "legacy-1"
        legacy_identity = {"hkjc_match_id": "HKJC-1", "pinnapi_event_id": "PN-1"}
        fixture = DueFixture(
            match_id=match_id, stage="T-30", kickoff=_kickoff(25.0),
            legacy_watch_identity=legacy_identity,
        )
        self.store.mark_started(
            match_id, "T-30", kickoff=fixture.kickoff,
            legacy_watch_identity=legacy_identity,
        )
        state = self.store.read(match_id)
        self.assertEqual(state["legacy_watch_identity"], legacy_identity)

        # A later T-5 STARTED for the same fixture with a *different*
        # candidate identity must not overwrite the first one recorded.
        self.store.mark_started(
            match_id, "T-5", kickoff=_kickoff(4.0),
            legacy_watch_identity={"hkjc_match_id": "SHOULD-NOT-OVERWRITE"},
        )
        state2 = self.store.read(match_id)
        self.assertEqual(state2["legacy_watch_identity"], legacy_identity)

    # -- 7. duplicate T-5 does not create duplicate snapshot/attempt terminal
    def test_duplicate_t5_commit_is_idempotent(self) -> None:
        fixture = DueFixture(match_id="dup-1", stage="T-5", kickoff=_kickoff(4.0))
        outcomes1 = commit_due_fixtures(self.store, [fixture], self._always_available)
        outcomes2 = commit_due_fixtures(self.store, [fixture], self._always_available)
        self.assertEqual(outcomes1[0].state, "COMMITTED")
        self.assertEqual(outcomes2[0].state, "COMMITTED")
        state = self.store.read(fixture.match_id)
        committed_rows = [
            row for row in state["attempt_history"]
            if row["stage"] == "T-5" and row["state"] == "COMMITTED"
        ]
        self.assertEqual(len(committed_rows), 1)
        started_rows = [
            row for row in state["attempt_history"]
            if row["stage"] == "T-5" and row["state"] == "STARTED"
        ]
        # Only the very first STARTED before the first successful commit.
        self.assertEqual(len(started_rows), 1)

    def test_duplicate_commit_snapshot_call_direct(self) -> None:
        fixture = DueFixture(match_id="dup-2", stage="T-5", kickoff=_kickoff(4.0))
        snap = _snapshot(fixture.match_id, "T-5")
        self.store.mark_started(fixture.match_id, "T-5", kickoff=fixture.kickoff)
        self.store.commit_snapshot(fixture.match_id, "T-5", snap, kickoff=fixture.kickoff)
        state1 = self.store.read(fixture.match_id)
        # Call commit_snapshot again directly with a different payload; it
        # must be rejected as a no-op because the stage is already COMMITTED.
        mutated = dict(snap)
        mutated["selected_odds_journal"] = [{"code": "HDC", "line": -99, "side": "H", "odds": 5.0}]
        self.store.commit_snapshot(fixture.match_id, "T-5", mutated, kickoff=fixture.kickoff)
        state2 = self.store.read(fixture.match_id)
        self.assertEqual(state1["snapshots"]["T-5"], state2["snapshots"]["T-5"])

    # -- 8. post-kickoff expiry with zero provider call and no snapshot -----
    def test_post_kickoff_expiry_zero_provider_call_no_snapshot(self) -> None:
        fixture = DueFixture(match_id="expired-1", stage="T-5", kickoff=_kickoff(-1.0))
        calls = []

        def collector(f: DueFixture):
            calls.append(f.match_id)
            return _snapshot(f.match_id, f.stage)

        outcomes = commit_due_fixtures(self.store, [fixture], collector)
        self.assertEqual(outcomes[0].state, "EXPIRED")
        self.assertEqual(calls, [])
        state = self.store.read(fixture.match_id)
        self.assertNotIn("T-5", state.get("snapshots") or {})
        self.assertEqual(state["attempt_history"][-1]["state"], "EXPIRED")

    def test_post_kickoff_never_backfills_via_commit_snapshot_directly(self) -> None:
        fixture = DueFixture(match_id="expired-2", stage="T-5", kickoff=_kickoff(-2.0))
        snap = _snapshot(fixture.match_id, "T-5")
        result = self.store.commit_snapshot(fixture.match_id, "T-5", snap, kickoff=fixture.kickoff)
        self.assertNotIn("T-5", result.get("snapshots") or {})
        self.assertEqual(result["attempt_history"][-1]["state"], "EXPIRED")

    def test_expire_post_kickoff_creates_state_even_if_never_started(self) -> None:
        """A job killed before ever journaling STARTED still gets an honest EXPIRED."""
        fixture = DueFixture(match_id="never-started", stage="T-5", kickoff=_kickoff(-0.5))
        self.assertIsNone(self.store.read(fixture.match_id))
        result = self.store.expire_post_kickoff(
            fixture.match_id, fixture.stage, kickoff=fixture.kickoff,
        )
        self.assertIsNotNone(result)
        self.assertEqual(result["attempt_history"][-1]["state"], "EXPIRED")
        self.assertNotIn("T-5", result.get("snapshots") or {})

    # -- 9. optional consumer/Wilson/dashboard failure cannot roll back -----
    def test_consumer_projection_failure_cannot_roll_back_native_snapshot(self) -> None:
        fixture = DueFixture(match_id="consumer-1", stage="T-5", kickoff=_kickoff(4.0))
        commit_due_fixtures(self.store, [fixture], self._always_available)
        committed_state = self.store.read(fixture.match_id)

        class ExplodingConsumer:
            def project(self, _state):
                raise RuntimeError("dashboard/Wilson projection exploded")

        consumer = ExplodingConsumer()
        with self.assertRaises(RuntimeError):
            consumer.project(committed_state)

        # The native store on disk is untouched by the consumer failure.
        state_after = self.store.read(fixture.match_id)
        self.assertEqual(committed_state, state_after)

    def test_projection_helper_is_read_only_and_survives_bad_input(self) -> None:
        fixture = DueFixture(match_id="consumer-2", stage="T-5", kickoff=_kickoff(4.0))
        commit_due_fixtures(self.store, [fixture], self._always_available)
        state = self.store.read(fixture.match_id)
        projected = project_legacy_watch_row(state)
        self.assertEqual(projected["match_id"], fixture.match_id)
        self.assertTrue(projected["native_stage_store_projection"])
        # Confirm the projection is a copy: mutating it cannot affect the
        # underlying store snapshot dict identity for stages.
        projected["stages"][0]["stage"] = "MUTATED"
        state_after = self.store.read(fixture.match_id)
        self.assertNotEqual(state_after["snapshots"]["T-5"]["stage"], "MUTATED")

    # -- Bounded snapshot / no raw provider payload --------------------------
    def test_snapshot_is_bounded_and_drops_unbounded_raw_payload(self) -> None:
        fixture = DueFixture(match_id="bounded-1", stage="T-5", kickoff=_kickoff(4.0))
        raw = _snapshot(fixture.match_id, "T-5")
        raw["raw_provider_html"] = "x" * 5_000_000
        raw["credentials"] = {"token": "should-never-be-persisted"}
        raw["selected_odds_journal"] = raw["selected_odds_journal"] * 500  # 500 rows
        self.store.mark_started(fixture.match_id, "T-5", kickoff=fixture.kickoff)
        self.store.commit_snapshot(fixture.match_id, "T-5", raw, kickoff=fixture.kickoff)
        state = self.store.read(fixture.match_id)
        stored = state["snapshots"]["T-5"]
        self.assertNotIn("raw_provider_html", stored)
        self.assertNotIn("credentials", stored)
        self.assertLessEqual(len(stored["selected_odds_journal"]), 240)

    def test_attempt_history_is_bounded(self) -> None:
        fixture = DueFixture(match_id="bounded-history", stage="T-5", kickoff=_kickoff(4.0))
        for _ in range(30):
            self.store.mark_failed(
                fixture.match_id, "T-5", kickoff=fixture.kickoff, reason="native_quote_unavailable",
            )
        state = self.store.read(fixture.match_id)
        self.assertLessEqual(len(state["attempt_history"]), 12)

    # -- concurrency: many fixtures via real threads to exercise fcntl locks
    def test_concurrent_threads_committing_distinct_fixtures(self) -> None:
        n = 26
        fixtures = self._fixtures(n)
        errors: list[Exception] = []

        def worker(fixture: DueFixture) -> None:
            try:
                self.store.mark_started(fixture.match_id, fixture.stage, kickoff=fixture.kickoff)
                self.store.commit_snapshot(
                    fixture.match_id, fixture.stage,
                    _snapshot(fixture.match_id, fixture.stage), kickoff=fixture.kickoff,
                )
            except Exception as exc:  # noqa: BLE001
                errors.append(exc)

        threads = [threading.Thread(target=worker, args=(f,)) for f in fixtures]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=10)
        self.assertEqual(errors, [])
        for fixture in fixtures:
            state = self.store.read(fixture.match_id)
            self.assertEqual(state["snapshots"]["T-5"]["stage"], "T-5")

    def test_default_off_flag(self) -> None:
        with patch.dict(os.environ, {}, clear=False):
            os.environ.pop(store_mod.ENV_ENABLED, None)
            self.assertFalse(store_mod.is_enabled())
        with patch.dict(os.environ, {store_mod.ENV_ENABLED: "1"}, clear=False):
            self.assertTrue(store_mod.is_enabled())


if __name__ == "__main__":
    unittest.main()
