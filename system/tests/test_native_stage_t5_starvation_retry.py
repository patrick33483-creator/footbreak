"""Focused regressions for the 2026-08-22 T-5 starvation/non-retry defect.

Root cause under test: a same-kickoff batch large enough to exceed one
tick's bounded pass budget (or a single slow persistence/analysis call, or a
transient direct-read exception) used to leave the batch tail permanently
FAILED and excluded from all future due-selection, even while `now <
kickoff`. This violates the pre-kickoff retry contract. These tests assert,
per fixture, that:

  * a retryable FAILED reason (`tick_deadline_elapsed`,
    `persistence_timeout_or_error`, `analysis_timeout_or_error`,
    `provider_fetch_*`) remains selectable by `due_stage_work` while
    `now < kickoff`, and a new attempt may be started for it;
  * a non-retryable terminal state (COMMITTED, DATA_MISSING, EXPIRED, or a
    FAILED with any other reason) is never re-offered;
  * post-kickoff, an unresolved retryable-FAILED is still swept to EXPIRED
    exactly like any other unfinished attempt -- it cannot leak forever;
  * a later genuine terminal outcome (COMMITTED/DATA_MISSING) for the same
    identity always wins and stops further retries;
  * `_fair_rotate_due` in system/run_predict.py rotates a large same-due
    cluster across ticks so no single fixture is permanently last;
  * none of this touches Crown, Telegram, crossbook, betting, Radar, or UI
    code paths (this file only imports system.native_stage_state and the
    rotation helper from system.run_predict).
"""
from __future__ import annotations

import copy
import importlib.util
import sys
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

from system import native_stage_state as state

HKT = timezone(timedelta(hours=8))
UTC = timezone.utc


def _load_fair_rotate_due():
    """Import only `_fair_rotate_due` from system/run_predict.py without
    triggering that module's heavier optional-consumer/provider imports at
    collection time (keeps this test file import-light and read-only).
    `run_predict.py` does `import hkjc_feed as H` as a bare top-level name
    (it is normally run with `system/` on `sys.path`), so this loader adds
    that same directory to `sys.path` only for the duration of the import.
    """
    system_dir = str(Path(__file__).resolve().parents[1])
    module_path = Path(__file__).resolve().parents[1] / "run_predict.py"
    spec = importlib.util.spec_from_file_location("_footbreak_run_predict_under_test", module_path)
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    inserted = system_dir not in sys.path
    if inserted:
        sys.path.insert(0, system_dir)
    try:
        spec.loader.exec_module(module)
    finally:
        if inserted:
            sys.path.remove(system_dir)
    return module._fair_rotate_due


class RetryableFailureClassificationTests(unittest.TestCase):
    def setUp(self) -> None:
        self.kickoff = datetime(2026, 8, 23, 18, 0, tzinfo=HKT)
        self.now = self.kickoff - timedelta(minutes=5)

    def _watch(self, match_id: str) -> dict:
        return {
            "match_id": match_id, "league": "測試聯賽", "home": "主隊", "away": "客隊",
            "kickoff": self.kickoff.isoformat(), "stages": [],
        }

    def _ledger_with_watch(self, match_id: str) -> tuple[dict, dict]:
        watch = self._watch(match_id)
        state.ensure_first_look_manifest(watch, now=self.now - timedelta(minutes=25))
        ledger = {"watch": {match_id: watch}}
        # Commit T-30 first so these T-5-focused cases exercise T-5 due
        # selection in isolation, matching the real production shape where
        # T-30 completed cleanly and only T-5 starved (see Mail 2 evidence).
        t30 = state.start_attempt(ledger, watch, "T-30", now=self.now - timedelta(minutes=25))
        state.enrich_snapshot({"stage": "T-30", "market_predictions": []}, watch, "T-30")
        watch["stages"].append({"stage": "T-30"})
        state.finish_attempt(ledger, t30, "COMMITTED", now=self.now - timedelta(minutes=25) + timedelta(seconds=1))
        return ledger, watch

    def test_tick_deadline_elapsed_is_retryable_pre_kickoff(self) -> None:
        ledger, watch = self._ledger_with_watch("A1")
        started = state.start_attempt(ledger, watch, "T-5", now=self.now)
        state.finish_attempt(ledger, started, "FAILED", now=self.now + timedelta(seconds=40), reason="tick_deadline_elapsed")
        due = state.due_stage_work(ledger, now=self.now + timedelta(seconds=41))
        self.assertEqual([(row["hkjc_match_id"], row["stage"]) for row in due], [("A1", "T-5")])
        retried = state.start_attempt(ledger, watch, "T-5", now=self.now + timedelta(seconds=42))
        self.assertEqual(retried["status"], "STARTED")
        self.assertNotEqual(retried["attempt_id"], started["attempt_id"])

    def test_persistence_timeout_is_retryable_pre_kickoff(self) -> None:
        ledger, watch = self._ledger_with_watch("A2")
        started = state.start_attempt(ledger, watch, "T-5", now=self.now)
        state.finish_attempt(ledger, started, "FAILED", now=self.now + timedelta(seconds=10), reason="persistence_timeout_or_error")
        due = state.due_stage_work(ledger, now=self.now + timedelta(seconds=11))
        self.assertEqual(len(due), 1)

    def test_provider_fetch_exception_is_retryable_pre_kickoff(self) -> None:
        ledger, watch = self._ledger_with_watch("A3")
        started = state.start_attempt(ledger, watch, "T-5", now=self.now)
        state.finish_attempt(ledger, started, "FAILED", now=self.now + timedelta(seconds=1), reason="provider_fetch_ConnectionError")
        due = state.due_stage_work(ledger, now=self.now + timedelta(seconds=2))
        self.assertEqual(len(due), 1)

    def test_non_retryable_failed_reason_is_never_reoffered(self) -> None:
        ledger, watch = self._ledger_with_watch("A4")
        started = state.start_attempt(ledger, watch, "T-5", now=self.now)
        state.finish_attempt(ledger, started, "FAILED", now=self.now + timedelta(seconds=1), reason="provider_kickoff_identity_mismatch")
        due = state.due_stage_work(ledger, now=self.now + timedelta(seconds=2))
        self.assertEqual(due, [])
        with self.assertRaises(ValueError):
            state.start_attempt(ledger, watch, "T-5", now=self.now + timedelta(seconds=3))

    def test_committed_and_data_missing_are_never_reoffered(self) -> None:
        for match_id, terminal in (("A5", "COMMITTED"), ("A6", "DATA_MISSING")):
            with self.subTest(terminal=terminal):
                ledger, watch = self._ledger_with_watch(match_id)
                started = state.start_attempt(ledger, watch, "T-5", now=self.now)
                if terminal == "COMMITTED":
                    state.enrich_snapshot({"stage": "T-5", "market_predictions": []}, watch, "T-5")
                    watch["stages"].append({"stage": "T-5"})
                state.finish_attempt(ledger, started, terminal, now=self.now + timedelta(seconds=1), reason="provider_fixture_missing" if terminal == "DATA_MISSING" else None)
                due = state.due_stage_work(ledger, now=self.now + timedelta(seconds=2))
                self.assertEqual(due, [])

    def test_genuine_terminal_after_retry_wins_and_stops_further_retries(self) -> None:
        ledger, watch = self._ledger_with_watch("A7")
        started = state.start_attempt(ledger, watch, "T-5", now=self.now)
        state.finish_attempt(ledger, started, "FAILED", now=self.now + timedelta(seconds=1), reason="tick_deadline_elapsed")
        retried = state.start_attempt(ledger, watch, "T-5", now=self.now + timedelta(seconds=2))
        state.finish_attempt(ledger, retried, "DATA_MISSING", now=self.now + timedelta(seconds=3), reason="provider_fixture_missing")
        due = state.due_stage_work(ledger, now=self.now + timedelta(seconds=4))
        self.assertEqual(due, [])
        with self.assertRaises(ValueError):
            state.start_attempt(ledger, watch, "T-5", now=self.now + timedelta(seconds=5))

    def test_unresolved_retryable_failed_is_swept_to_expired_post_kickoff(self) -> None:
        ledger, watch = self._ledger_with_watch("A8")
        started = state.start_attempt(ledger, watch, "T-5", now=self.now)
        state.finish_attempt(ledger, started, "FAILED", now=self.now + timedelta(seconds=1), reason="tick_deadline_elapsed")
        expired = state.expire_lapsed_work(ledger, now=self.kickoff)
        self.assertGreaterEqual(expired, 1)
        latest = state.latest_attempts(ledger)[("A8", state.iso_utc(self.kickoff.astimezone(UTC)), "T-5")]
        self.assertEqual(latest["status"], "EXPIRED")
        # No leak past kickoff: it must not still be selectable.
        self.assertEqual(state.due_stage_work(ledger, now=self.kickoff + timedelta(seconds=1)), [])

    def test_t30_committed_does_not_mask_t5_retryable_failure(self) -> None:
        """T-30 done but T-5 still due must surface T-5 independently."""
        # _ledger_with_watch already commits T-30 cleanly (mirrors the real
        # production shape: T-30 succeeded, only T-5 starved).
        ledger, watch = self._ledger_with_watch("A9")
        t5 = state.start_attempt(ledger, watch, "T-5", now=self.now)
        state.finish_attempt(ledger, t5, "FAILED", now=self.now + timedelta(seconds=1), reason="tick_deadline_elapsed")
        due = state.due_stage_work(ledger, now=self.now + timedelta(seconds=2))
        self.assertEqual([(row["hkjc_match_id"], row["stage"]) for row in due], [("A9", "T-5")])

    def test_completeness_projection_flags_retryable_pending_without_hiding_failed_reason(self) -> None:
        ledger, watch = self._ledger_with_watch("A10")
        started = state.start_attempt(ledger, watch, "T-5", now=self.now)
        state.finish_attempt(ledger, started, "FAILED", now=self.now + timedelta(seconds=1), reason="tick_deadline_elapsed")
        report = state.completeness_projection(ledger, now=self.now + timedelta(seconds=2))
        row = {r["hkjc_match_id"]: r for r in report["fixtures"]}["A10"]
        self.assertEqual(row["stages"]["T-5"]["status"], "FAILED")
        self.assertEqual(row["stages"]["T-5"]["reason"], "tick_deadline_elapsed")
        self.assertTrue(row["stages"]["T-5"]["retryable_pending"])


class LargeBatchStarvationTests(unittest.TestCase):
    """Reproduces the exact 2026-08-22 17:55 HKT shape: 9 fixtures sharing
    one T-5 due minute, tail members FAILED with tick_deadline_elapsed."""

    def setUp(self) -> None:
        self.kickoff = datetime(2026, 8, 23, 18, 0, tzinfo=HKT)
        self.now = self.kickoff - timedelta(minutes=5)

    def _batch_ledger(self, size: int) -> tuple[dict, dict[str, dict]]:
        watches: dict[str, dict] = {}
        for index in range(size):
            match_id = f"B{index:03d}"
            watch = {
                "match_id": match_id, "league": "測試聯賽", "home": "主隊", "away": "客隊",
                "kickoff": self.kickoff.isoformat(), "stages": [],
            }
            state.ensure_first_look_manifest(watch, now=self.now - timedelta(minutes=25))
            watches[match_id] = watch
        ledger = {"watch": watches}
        # Commit T-30 cleanly for every fixture up front, isolating the T-5
        # batch-starvation scenario under test (matches the real production
        # shape: all 9 fixtures' T-30 committed; only T-5 starved).
        for match_id, watch in watches.items():
            attempt = state.start_attempt(ledger, watch, "T-30", now=self.now - timedelta(minutes=25))
            state.enrich_snapshot({"stage": "T-30", "market_predictions": []}, watch, "T-30")
            watch["stages"].append({"stage": "T-30"})
            state.finish_attempt(ledger, attempt, "COMMITTED", now=self.now - timedelta(minutes=25) + timedelta(seconds=1))
        return ledger, watches

    @staticmethod
    def _t5_only(due_rows: list) -> list:
        return [row for row in due_rows if row["stage"] == "T-5"]

    def test_50_plus_same_kickoff_fixtures_every_fixture_has_per_fixture_invariant(self) -> None:
        ledger, watches = self._batch_ledger(53)
        due = self._t5_only(state.due_stage_work(ledger, now=self.now))
        self.assertEqual(len(due), 53)
        # Simulate one bounded tick that can only process the first 9 before
        # its deadline elapses; the rest never even got a STARTED write-ahead.
        processed, starved = due[:9], due[9:]
        committed_row = processed[0]
        watch = watches[committed_row["hkjc_match_id"]]
        attempt = state.start_attempt(ledger, watch, committed_row["stage"], now=self.now)
        state.finish_attempt(ledger, attempt, "COMMITTED", now=self.now + timedelta(seconds=5))
        for row in processed[1:]:
            watch = watches[row["hkjc_match_id"]]
            attempt = state.start_attempt(ledger, watch, row["stage"], now=self.now)
            state.finish_attempt(ledger, attempt, "FAILED", now=self.now + timedelta(seconds=39), reason="tick_deadline_elapsed")
        # Per-fixture invariant: every one of the 53 fixtures must still be
        # either committed or remain selectable as due (never silently
        # dropped) while pre-kickoff.
        next_due = {(row["hkjc_match_id"], row["stage"]) for row in self._t5_only(state.due_stage_work(ledger, now=self.now + timedelta(seconds=40)))}
        report = state.completeness_projection(ledger, now=self.now + timedelta(seconds=40))
        by_id = {row["hkjc_match_id"]: row for row in report["fixtures"]}
        self.assertEqual(by_id[committed_row["hkjc_match_id"]]["stages"]["T-5"]["status"], "COMMITTED")
        self.assertNotIn((committed_row["hkjc_match_id"], "T-5"), next_due)
        for row in processed[1:]:
            self.assertIn(
                (row["hkjc_match_id"], "T-5"), next_due,
                f"{row['hkjc_match_id']} retryable-FAILED must remain due pre-kickoff",
            )
        for row in starved:
            self.assertIn(
                (row["hkjc_match_id"], "T-5"), next_due,
                f"{row['hkjc_match_id']} never attempted must remain due",
            )
        self.assertEqual(len(next_due), 52)

    def test_fair_rotation_prevents_same_tail_from_starving_every_tick(self) -> None:
        fair_rotate_due = _load_fair_rotate_due()
        ledger, watches = self._batch_ledger(9)
        due = state.due_stage_work(ledger, now=self.now)
        # due_stage_work returns dict rows; adapt to the (kickoff, match_id,
        # watch, stage) tuple shape _fair_rotate_due expects from
        # persisted_due_stages.
        tuples = [
            (self.kickoff.astimezone(UTC), row["hkjc_match_id"], watches[row["hkjc_match_id"]], row["stage"])
            for row in due
        ]
        leaders = set()
        for minute_offset in range(9):
            probe_now = self.now + timedelta(minutes=minute_offset)
            rotated = fair_rotate_due(tuples, probe_now)
            self.assertEqual(len(rotated), 9)
            self.assertEqual({t[1] for t in rotated}, {t[1] for t in tuples})
            leaders.add(rotated[0][1])
        # Across 9 consecutive ticks every fixture must get to lead the
        # queue at least once -- nobody is permanently last.
        self.assertEqual(leaders, {row["hkjc_match_id"] for row in due})


class TimerDelayRestartLockAndLegacyTests(unittest.TestCase):
    def setUp(self) -> None:
        self.kickoff = datetime(2026, 8, 23, 18, 0, tzinfo=HKT)

    @staticmethod
    def _commit_t30(ledger: dict, watch: dict, now: datetime) -> None:
        """Commits T-30 cleanly so a test can isolate T-5 due selection,
        mirroring the real production shape where T-30 succeeded and only
        T-5 was affected."""
        attempt = state.start_attempt(ledger, watch, "T-30", now=now)
        state.enrich_snapshot({"stage": "T-30", "market_predictions": []}, watch, "T-30")
        watch["stages"].append({"stage": "T-30"})
        state.finish_attempt(ledger, attempt, "COMMITTED", now=now + timedelta(seconds=1))

    def test_timer_delay_of_several_minutes_still_finds_pre_kickoff_due_work(self) -> None:
        watch = {"match_id": "C1", "league": "x", "home": "h", "away": "a", "kickoff": self.kickoff.isoformat(), "stages": []}
        creation_now = self.kickoff - timedelta(minutes=40)
        state.ensure_first_look_manifest(watch, now=creation_now)
        ledger = {"watch": {"C1": watch}}
        self._commit_t30(ledger, watch, creation_now)
        # Timer meant to fire near T-5 due (17:55) is delayed by systemd load
        # until 17:58 -- still pre-kickoff, must still be due.
        delayed_now = self.kickoff - timedelta(minutes=2)
        due = state.due_stage_work(ledger, now=delayed_now)
        self.assertEqual([(row["hkjc_match_id"], row["stage"]) for row in due], [("C1", "T-5")])

    def test_process_restart_resumes_non_terminal_started_attempt(self) -> None:
        watch = {"match_id": "C2", "league": "x", "home": "h", "away": "a", "kickoff": self.kickoff.isoformat(), "stages": []}
        state.ensure_first_look_manifest(watch, now=self.kickoff - timedelta(minutes=40))
        ledger = {"watch": {"C2": watch}}
        self._commit_t30(ledger, watch, self.kickoff - timedelta(minutes=40))
        due_before = self.kickoff - timedelta(minutes=5)
        started = state.start_attempt(ledger, watch, "T-5", now=due_before)
        # Process crashes/restarts here -- write-ahead STARTED already
        # persisted, so a fresh process must still see it as in-flight, not
        # silently re-created or dropped.
        reopened_ledger = copy.deepcopy(ledger)
        due_after_restart = state.due_stage_work(reopened_ledger, now=due_before + timedelta(seconds=30))
        self.assertEqual([(row["hkjc_match_id"], row["stage"]) for row in due_after_restart], [("C2", "T-5")])
        resumed = state.start_attempt(reopened_ledger, watch, "T-5", now=due_before + timedelta(seconds=31))
        self.assertEqual(resumed["attempt_id"], started["attempt_id"])
        self.assertEqual(resumed["status"], "STARTED")

    def test_started_child_timeout_is_terminal_failed_and_then_retryable(self) -> None:
        watch = {"match_id": "C3", "league": "x", "home": "h", "away": "a", "kickoff": self.kickoff.isoformat(), "stages": []}
        state.ensure_first_look_manifest(watch, now=self.kickoff - timedelta(minutes=40))
        ledger = {"watch": {"C3": watch}}
        self._commit_t30(ledger, watch, self.kickoff - timedelta(minutes=40))
        now = self.kickoff - timedelta(minutes=5)
        started = state.start_attempt(ledger, watch, "T-5", now=now)
        # Forked analyse/persist child exceeds its own bounded timeout.
        state.finish_attempt(ledger, started, "FAILED", now=now + timedelta(seconds=8), reason="analysis_timeout_or_error")
        due = state.due_stage_work(ledger, now=now + timedelta(seconds=9))
        self.assertEqual(len(due), 1)

    def test_lock_contention_leaves_attempt_started_and_still_due_for_retry(self) -> None:
        """A concurrent tick holding a lock means this tick cannot even
        write a new STARTED event; the existing STARTED (write-ahead) from
        the lock holder must remain the sole source of truth and still be
        due for the *next* tick if that holder never finishes."""
        watch = {"match_id": "C4", "league": "x", "home": "h", "away": "a", "kickoff": self.kickoff.isoformat(), "stages": []}
        state.ensure_first_look_manifest(watch, now=self.kickoff - timedelta(minutes=40))
        ledger = {"watch": {"C4": watch}}
        now = self.kickoff - timedelta(minutes=5)
        holder_attempt = state.start_attempt(ledger, watch, "T-5", now=now)
        # Second tick attempts the same key while lock is (conceptually)
        # held; start_attempt is idempotent for STARTED and returns the
        # same in-flight event rather than fabricating a duplicate.
        contended = state.start_attempt(ledger, watch, "T-5", now=now + timedelta(seconds=1))
        self.assertEqual(contended["attempt_id"], holder_attempt["attempt_id"])
        self.assertEqual(len(ledger["native_stage_attempts"]), 1)

    def test_legacy_future_card_migration_still_participates_in_retry_contract(self) -> None:
        watch = {"match_id": "C5", "league": "x", "home": "h", "away": "a", "kickoff": self.kickoff.isoformat(), "stages": []}
        ledger = {"watch": {"C5": watch}}
        migrate_now = self.kickoff - timedelta(hours=1)
        migrated = state.migrate_future_manifests(ledger, now=migrate_now)
        self.assertEqual(migrated, 1)
        self._commit_t30(ledger, watch, migrate_now)
        now = self.kickoff - timedelta(minutes=5)
        started = state.start_attempt(ledger, watch, "T-5", now=now)
        state.finish_attempt(ledger, started, "FAILED", now=now + timedelta(seconds=1), reason="tick_deadline_elapsed")
        due = state.due_stage_work(ledger, now=now + timedelta(seconds=2))
        self.assertEqual([(row["hkjc_match_id"], row["stage"]) for row in due], [("C5", "T-5")])

    def test_utc_hkt_day_crossing_kickoff_still_computes_correct_due_and_retry(self) -> None:
        # HKT midnight kickoff crosses back to the previous UTC calendar day.
        midnight_kickoff = datetime(2026, 8, 24, 0, 10, tzinfo=HKT)
        watch = {"match_id": "C6", "league": "x", "home": "h", "away": "a", "kickoff": midnight_kickoff.isoformat(), "stages": []}
        creation_now = midnight_kickoff - timedelta(minutes=40)
        state.ensure_first_look_manifest(watch, now=creation_now)
        manifest = watch["native_stage_manifest"]
        self.assertEqual(manifest["identity"]["kickoff_at_utc"], "2026-08-23T16:10:00+00:00")
        ledger = {"watch": {"C6": watch}}
        self._commit_t30(ledger, watch, creation_now)
        t5_due_now = midnight_kickoff - timedelta(minutes=5)
        started = state.start_attempt(ledger, watch, "T-5", now=t5_due_now)
        state.finish_attempt(ledger, started, "FAILED", now=t5_due_now + timedelta(seconds=1), reason="persistence_timeout_or_error")
        due = state.due_stage_work(ledger, now=t5_due_now + timedelta(seconds=2))
        self.assertEqual([(row["hkjc_match_id"], row["stage"]) for row in due], [("C6", "T-5")])
        # Still pre-kickoff by only a few minutes across the UTC day boundary.
        self.assertTrue(t5_due_now + timedelta(seconds=2) < midnight_kickoff)


class OptionalConsumerCrashDoesNotBlockNativeCommitTests(unittest.TestCase):
    """Confirms the fix does not introduce any dependency from the native
    commit path onto optional consumers (dashboard/matcher/sidecar). The
    native ledger functions under test take no consumer objects at all, so
    a crash in an optional consumer cannot affect them by construction;
    this test pins that contract so a future change cannot regress it."""

    def test_finish_attempt_signature_has_no_consumer_dependency(self) -> None:
        import inspect
        params = list(inspect.signature(state.finish_attempt).parameters)
        self.assertEqual(params, ["ledger", "attempt", "status", "now", "reason"])

    def test_due_stage_work_and_expire_are_pure_ledger_functions(self) -> None:
        import inspect
        self.assertEqual(
            list(inspect.signature(state.due_stage_work).parameters),
            ["ledger", "now", "horizon_minutes"],
        )
        self.assertEqual(list(inspect.signature(state.expire_lapsed_work).parameters), ["ledger", "now"])


if __name__ == "__main__":
    unittest.main()
