"""Regression coverage for the Crown same-kickoff T-5 tick-starvation incident.

Root cause (2026-08-22 Crown T-5 outage, 18:00 HKT kickoff batch, 19
fixtures): when a same-kickoff batch is large enough that the deadline-owning
parent (`crown.run._run_tick_engine`) terminates the tick child before that
child ever reaches its own in-process write-ahead journal
(`crown.engine._journal_timed_stage_attempts`), no trace at all was left for
the affected fixtures -- not STARTED, not DATA_MISSING, not EXPIRED.  Once
kickoff passed, `due_stage_jobs()` permanently stopped selecting those jobs,
and the old `_expire_lapsed_timed_stage_attempts` safety net only recognized
attempts that had reached `STARTED`, so a job that was due but never even
attempted stayed silently missing forever.

These tests exercise the two honest-terminal-state safety nets patched in
`crown/engine.py`:

* `persist_timed_stage_timeout_failures` now also treats a job whose durable
  due time has elapsed, but whose attempt was never marked STARTED, as
  exactly as timed-out as one that reached STARTED and was abandoned.
* `_expire_lapsed_timed_stage_attempts` now also closes a post-kickoff job
  that was due but never attempted, in addition to one that was STARTED and
  abandoned.
* `_prioritize_tick_rows` now fairly rotates same-stage/same-kickoff clusters
  so a large same-kickoff batch (50+, or the real 19-fixture pattern) does
  not always starve the same tail fixtures tick after tick.

No test here touches Telegram, HKJC/cross-book comparison, betting, Radar, or
Footbreak/system code; every fixture in this file is a synthetic Crown-only
ledger row.
"""
from __future__ import annotations

import tempfile
import time
import unittest
from dataclasses import replace
from datetime import datetime, timedelta
from pathlib import Path
from unittest.mock import Mock, patch

from crown import engine as crown_engine
from crown.common import HKT
from crown.config import settings
from crown.ledger import PREDICTION_ERA, due_stage_jobs, ensure_stage_jobs
from crown.matching import MATCHING_VERSION
from crown.state import load_ledger, save_ledger


def _watch_row(match_id: str, kickoff: datetime, **overrides) -> dict:
    """Build a synthetic Crown watch row with only a T-5 durable job.

    ``ensure_stage_jobs`` also materializes a T-30 job (due 30 minutes before
    kickoff).  These tests are only exercising T-5 tick-starvation behaviour,
    so the T-30 job is dropped immediately after creation to keep each test's
    ``resolved``/``expired`` counts unambiguous about which stage moved.
    """
    watch = {
        "match_id": match_id,
        "native_fixture_id": match_id,
        "titan_match_id": match_id,
        "league": "L",
        "home": f"{match_id} H",
        "away": f"{match_id} A",
        "matching_version": MATCHING_VERSION,
        "prediction_era": PREDICTION_ERA,
        "stages": [],
        "stage_attempts": {},
    }
    ensure_stage_jobs(watch, kickoff)
    watch["stage_jobs"].pop("T-30", None)
    watch.update(overrides)
    return watch


class CrownT5TickStarvationTests(unittest.TestCase):
    """Root-cause pattern: 19 same-kickoff fixtures, never STARTED, never terminal."""

    def test_due_unstarted_job_gets_honest_retryable_failure_before_kickoff(self) -> None:
        """A job whose due time elapsed but was never journaled STARTED must
        still receive an explicit, retryable DATA_MISSING -- the exact defect
        that left the 18:00 HKT batch of 19 fixtures with zero trace."""
        with tempfile.TemporaryDirectory() as directory:
            config = replace(settings(), state_dir=Path(directory), enabled=True)
            now = datetime.now(HKT)
            kickoff = now + timedelta(minutes=5)
            watch = _watch_row("gap-1", kickoff)
            # Simulate the durable T-5 job's due time having already elapsed
            # (due_at_utc = kickoff - 5m) without ever journaling STARTED.
            watch["stage_jobs"]["T-5"]["due_at_utc"] = (now - timedelta(seconds=1)).astimezone(
                __import__("datetime").timezone.utc
            ).isoformat()
            self.assertIsNone(watch["stage_attempts"].get("T-5"))
            save_ledger(config, {
                "bankroll": 50000, "bets": [], "log": [], "stats": {},
                "watch": {"gap-1": watch},
            })
            resolved = crown_engine.persist_timed_stage_timeout_failures(config, now)
            self.assertEqual(resolved, 1)
            saved = load_ledger(config)["watch"]["gap-1"]
            self.assertEqual(saved["stage_attempts"]["T-5"]["state"], "FAILED")
            self.assertEqual(saved["stage_jobs"]["T-5"]["state"], "FAILED")
            self.assertEqual(saved["stages"][0]["status"], "DATA_MISSING")
            # Still pre-kickoff and not COMMITTED: due_stage_jobs must keep
            # retrying it on the very next tick rather than treating this
            # DATA_MISSING attempt as a terminal success.
            self.assertIn("T-5", due_stage_jobs(saved, now))

    def test_never_started_job_is_not_resolved_before_its_own_due_time(self) -> None:
        """A future job that has not reached its due time yet must not be
        force-resolved early -- the fix only closes genuinely elapsed jobs."""
        with tempfile.TemporaryDirectory() as directory:
            config = replace(settings(), state_dir=Path(directory), enabled=True)
            now = datetime.now(HKT)
            kickoff = now + timedelta(minutes=20)
            watch = _watch_row("not-due-yet", kickoff)
            save_ledger(config, {
                "bankroll": 50000, "bets": [], "log": [], "stats": {},
                "watch": {"not-due-yet": watch},
            })
            resolved = crown_engine.persist_timed_stage_timeout_failures(config, now)
            self.assertEqual(resolved, 0)
            saved = load_ledger(config)["watch"]["not-due-yet"]
            self.assertNotIn("T-5", saved.get("stage_attempts", {}))

    def test_started_job_still_resolves_exactly_as_before(self) -> None:
        """Backward-compatible: a job that did reach STARTED before the child
        was killed keeps resolving to retryable DATA_MISSING (pre-existing
        behaviour, unchanged by this fix)."""
        with tempfile.TemporaryDirectory() as directory:
            config = replace(settings(), state_dir=Path(directory), enabled=True)
            now = datetime.now(HKT)
            kickoff = now + timedelta(minutes=5)
            watch = _watch_row(
                "started-1", kickoff,
                stage_attempts={"T-5": {"state": "STARTED"}},
            )
            watch["stage_jobs"]["T-5"]["state"] = "STARTED"
            save_ledger(config, {
                "bankroll": 50000, "bets": [], "log": [], "stats": {},
                "watch": {"started-1": watch},
            })
            resolved = crown_engine.persist_timed_stage_timeout_failures(config, now)
            self.assertEqual(resolved, 1)
            saved = load_ledger(config)["watch"]["started-1"]
            self.assertEqual(saved["stage_attempts"]["T-5"]["state"], "FAILED")

    def test_nineteen_same_kickoff_fixtures_all_unstarted_all_resolve(self) -> None:
        """Reproduces the exact 2026-08-22 18:00 HKT incident shape: 19
        fixtures sharing one kickoff/due minute, all past due, none STARTED.
        Every one must get an explicit retryable outcome, not silence."""
        with tempfile.TemporaryDirectory() as directory:
            config = replace(settings(), state_dir=Path(directory), enabled=True)
            now = datetime.now(HKT)
            kickoff = now + timedelta(minutes=4, seconds=59)
            watches = {}
            for i in range(19):
                match_id = f"batch18-{i:02d}"
                watch = _watch_row(match_id, kickoff)
                watch["stage_jobs"]["T-5"]["due_at_utc"] = (
                    now - timedelta(seconds=1)
                ).astimezone(__import__("datetime").timezone.utc).isoformat()
                watches[match_id] = watch
            save_ledger(config, {
                "bankroll": 50000, "bets": [], "log": [], "stats": {},
                "watch": watches,
            })
            resolved = crown_engine.persist_timed_stage_timeout_failures(config, now)
            self.assertEqual(resolved, 19)
            saved = load_ledger(config)["watch"]
            for match_id in watches:
                self.assertEqual(
                    saved[match_id]["stage_attempts"]["T-5"]["state"], "FAILED",
                    msg=f"{match_id} was not resolved",
                )
                self.assertEqual(saved[match_id]["stages"][0]["status"], "DATA_MISSING")


class CrownT5PostKickoffExpiryTests(unittest.TestCase):
    """The second safety net: a job that is now past kickoff but was never
    even attempted must become an honest, visible EXPIRED incident -- not
    permanent silence."""

    def test_post_kickoff_never_started_job_expires_with_incident(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            config = replace(settings(), state_dir=Path(directory), enabled=True)
            now = datetime.now(HKT)
            kickoff = now - timedelta(minutes=39)
            watch = _watch_row(f"post-ko-1", kickoff)
            self.assertNotIn("T-5", watch["stage_attempts"])
            save_ledger(config, {
                "bankroll": 50000, "bets": [], "log": [], "stats": {},
                "watch": {"post-ko-1": watch},
            })
            expired = crown_engine._expire_lapsed_timed_stage_attempts(config, now)
            self.assertGreaterEqual(expired, 1)
            saved = load_ledger(config)["watch"]["post-ko-1"]
            self.assertEqual(saved["stage_attempts"]["T-5"]["state"], "EXPIRED")
            self.assertEqual(
                saved["stage_attempts"]["T-5"]["reason"],
                "deadline_exhausted_before_native_stage_commit",
            )
            self.assertEqual(saved["stage_jobs"]["T-5"]["state"], "EXPIRED")
            incidents = saved.get("stage_incidents") or []
            self.assertTrue(any(
                row.get("stage") == "T-5"
                and row.get("reason") == "deadline_exhausted_before_native_stage_commit"
                for row in incidents
            ))

    def test_post_kickoff_expiry_is_idempotent_and_does_not_duplicate_incidents(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            config = replace(settings(), state_dir=Path(directory), enabled=True)
            now = datetime.now(HKT)
            kickoff = now - timedelta(minutes=39)
            watch = _watch_row("post-ko-2", kickoff)
            save_ledger(config, {
                "bankroll": 50000, "bets": [], "log": [], "stats": {},
                "watch": {"post-ko-2": watch},
            })
            crown_engine._expire_lapsed_timed_stage_attempts(config, now)
            second = crown_engine._expire_lapsed_timed_stage_attempts(
                config, now + timedelta(seconds=30),
            )
            self.assertEqual(second, 0)
            saved = load_ledger(config)["watch"]["post-ko-2"]
            incidents = [
                row for row in (saved.get("stage_incidents") or [])
                if row.get("stage") == "T-5"
            ]
            self.assertEqual(len(incidents), 1)

    def test_post_kickoff_committed_job_never_gets_overwritten(self) -> None:
        """A genuinely COMMITTED T-5 must never be touched by the expiry
        safety net, even after kickoff."""
        with tempfile.TemporaryDirectory() as directory:
            config = replace(settings(), state_dir=Path(directory), enabled=True)
            now = datetime.now(HKT)
            kickoff = now - timedelta(minutes=39)
            watch = _watch_row(
                "post-ko-committed", kickoff,
                stages=[{
                    "stage": "T-5", "status": "PREDICTION_READY",
                    "odds_status": "available", "market_predictions": [{"code": "HDC"}],
                }],
                stage_attempts={"T-5": {"state": "COMMITTED"}},
            )
            watch["stage_jobs"]["T-5"]["state"] = "COMMITTED"
            save_ledger(config, {
                "bankroll": 50000, "bets": [], "log": [], "stats": {},
                "watch": {"post-ko-committed": watch},
            })
            expired = crown_engine._expire_lapsed_timed_stage_attempts(config, now)
            self.assertEqual(expired, 0)
            saved = load_ledger(config)["watch"]["post-ko-committed"]
            self.assertEqual(saved["stage_attempts"]["T-5"]["state"], "COMMITTED")
            self.assertEqual(saved["stage_jobs"]["T-5"]["state"], "COMMITTED")

    def test_post_kickoff_started_job_still_expires_exactly_as_before(self) -> None:
        """Backward-compatible: a STARTED-then-abandoned job still expires."""
        with tempfile.TemporaryDirectory() as directory:
            config = replace(settings(), state_dir=Path(directory), enabled=True)
            now = datetime.now(HKT)
            kickoff = now - timedelta(minutes=39)
            watch = _watch_row(
                "post-ko-started", kickoff,
                stage_attempts={"T-5": {"state": "STARTED", "started_at": "x"}},
            )
            watch["stage_jobs"]["T-5"]["state"] = "STARTED"
            save_ledger(config, {
                "bankroll": 50000, "bets": [], "log": [], "stats": {},
                "watch": {"post-ko-started": watch},
            })
            expired = crown_engine._expire_lapsed_timed_stage_attempts(config, now)
            self.assertEqual(expired, 1)
            saved = load_ledger(config)["watch"]["post-ko-started"]
            self.assertEqual(saved["stage_attempts"]["T-5"]["state"], "EXPIRED")

    def test_no_backfill_after_kickoff_expiry_creates_no_prediction(self) -> None:
        """Contract: post-kickoff EXPIRED is an incident marker only. It must
        never create a market prediction, odds snapshot, or simulated bet."""
        with tempfile.TemporaryDirectory() as directory:
            config = replace(settings(), state_dir=Path(directory), enabled=True)
            now = datetime.now(HKT)
            kickoff = now - timedelta(minutes=39)
            watch = _watch_row("no-backfill-1", kickoff)
            save_ledger(config, {
                "bankroll": 50000, "bets": [], "log": [], "stats": {},
                "watch": {"no-backfill-1": watch},
            })
            crown_engine._expire_lapsed_timed_stage_attempts(config, now)
            saved = load_ledger(config)
            self.assertEqual(saved["bets"], [])
            watch_after = saved["watch"]["no-backfill-1"]
            # No T-5 stage row was created; only the attempt/job/incident
            # bookkeeping records the honest miss.
            self.assertFalse(any(
                isinstance(row, dict) and row.get("stage") == "T-5"
                for row in watch_after.get("stages", [])
            ))


class CrownSameKickoffFairRotationTests(unittest.TestCase):
    """Same-kickoff batch fairness: a 50+ fixture batch (or the real
    19-fixture pattern) must not always starve the same tail fixtures."""

    def _rows(self, count: int, stage: str = "T-5") -> list[dict]:
        kickoff = datetime.now(HKT) + timedelta(minutes=5)
        return [
            {
                "id": f"fix-{i:03d}", "league": "L", "home": f"H{i}", "away": f"A{i}",
                "kickoff": kickoff, "_due_stage": stage,
            }
            for i in range(count)
        ]

    def test_fifty_plus_same_kickoff_batch_changes_leader_across_minutes(self) -> None:
        rows = self._rows(53)
        leaders = set()
        for minute_offset in range(6):
            with patch("crown.engine.time.time", return_value=1_700_000_000 + minute_offset * 60):
                ordered = crown_engine._prioritize_tick_rows(list(rows))
            self.assertEqual(len(ordered), 53)
            self.assertEqual({row["id"] for row in ordered}, {row["id"] for row in rows})
            leaders.add(ordered[0]["id"])
        # Across six consecutive tick minutes, more than one fixture must
        # have had a turn at the front of the oversized cluster.
        self.assertGreater(len(leaders), 1)

    def test_nineteen_fixture_batch_rotates_and_preserves_completeness(self) -> None:
        rows = self._rows(19)
        seen_first_positions = set()
        for minute_offset in range(19):
            with patch("crown.engine.time.time", return_value=1_700_000_000 + minute_offset * 60):
                ordered = crown_engine._prioritize_tick_rows(list(rows))
            self.assertEqual(len(ordered), 19)
            self.assertEqual(sorted(r["id"] for r in ordered), sorted(r["id"] for r in rows))
            seen_first_positions.add(ordered[0]["id"])
        # Over 19 distinct minutes, every fixture must have led at least once,
        # so no single fixture can be permanently starved at the tail.
        self.assertEqual(len(seen_first_positions), 19)

    def test_t5_still_ranked_ahead_of_t30_and_first_look_after_rotation(self) -> None:
        kickoff = datetime.now(HKT) + timedelta(minutes=5)
        rows = (
            [{"id": "fl-1", "kickoff": kickoff, "_due_stage": "首預"}]
            + [{"id": f"t30-{i}", "kickoff": kickoff, "_due_stage": "T-30"} for i in range(3)]
            + [{"id": f"t5-{i}", "kickoff": kickoff, "_due_stage": "T-5"} for i in range(3)]
        )
        ordered = crown_engine._prioritize_tick_rows(list(rows))
        stages = [row["_due_stage"] for row in ordered]
        self.assertEqual(stages[:3], ["T-5", "T-5", "T-5"])
        self.assertEqual(stages[3:6], ["T-30", "T-30", "T-30"])
        self.assertEqual(stages[6:], ["首預"])

    def test_single_fixture_batch_is_unaffected_by_rotation(self) -> None:
        rows = self._rows(1)
        ordered = crown_engine._prioritize_tick_rows(list(rows))
        self.assertEqual(ordered, rows)

    def test_distinct_kickoffs_are_not_merged_into_one_rotation_cluster(self) -> None:
        kickoff_a = datetime.now(HKT) + timedelta(minutes=5)
        kickoff_b = datetime.now(HKT) + timedelta(minutes=35)
        rows = (
            [{"id": f"a-{i}", "kickoff": kickoff_a, "_due_stage": "T-5"} for i in range(3)]
            + [{"id": f"b-{i}", "kickoff": kickoff_b, "_due_stage": "T-5"} for i in range(3)]
        )
        ordered = crown_engine._prioritize_tick_rows(list(rows))
        # The earlier kickoff's cluster must remain entirely ahead of the
        # later kickoff's cluster: rotation only reorders within a cluster.
        first_half_ids = {row["id"] for row in ordered[:3]}
        self.assertEqual(first_half_ids, {"a-0", "a-1", "a-2"})


class CrownTickChildKilledBeforeJournalTests(unittest.TestCase):
    """End-to-end shape of the actual failure: the tick child is killed by
    the parent deadline before it reaches `_journal_timed_stage_attempts`,
    and the parent-level safety net must still leave an honest trace."""

    def test_run_tick_engine_timeout_branch_invokes_parent_safety_net(self) -> None:
        from crown import run as crown_run

        with tempfile.TemporaryDirectory() as directory:
            config = replace(settings(), state_dir=Path(directory), enabled=True)
            now = datetime.now(HKT)
            kickoff = now + timedelta(minutes=5)
            watch = _watch_row("child-killed-1", kickoff)
            watch["stage_jobs"]["T-5"]["due_at_utc"] = (
                now - timedelta(seconds=1)
            ).astimezone(__import__("datetime").timezone.utc).isoformat()
            save_ledger(config, {
                "bankroll": 50000, "bets": [], "log": [], "stats": {},
                "watch": {"child-killed-1": watch},
            })

            class _NeverRespondingReceiver:
                def poll(self, _timeout):
                    return False

                def close(self):
                    return None

            class _NeverStartingProcess:
                def start(self):
                    return None

                def is_alive(self):
                    return False

                def terminate(self):
                    return None

                def join(self, timeout=None):
                    return None

                def kill(self):
                    return None

            class _FakeContext:
                def Pipe(self, duplex=False):
                    class _Sender:
                        def close(self):
                            return None
                    return _NeverRespondingReceiver(), _Sender()

                def Process(self, target, args):
                    return _NeverStartingProcess()

            with patch("crown.run.multiprocessing.get_context", return_value=_FakeContext()), \
                 patch("crown.run.settings", return_value=config):
                result = crown_run._run_tick_engine(config, time.monotonic() + 2.0)
            self.assertEqual(result.get("engine_warning"), "deferred_tick_deadline")
            self.assertEqual(result.get("timeout_failure_snapshots"), 1)
            saved = load_ledger(config)["watch"]["child-killed-1"]
            self.assertEqual(saved["stage_attempts"]["T-5"]["state"], "FAILED")
            self.assertEqual(saved["stages"][0]["status"], "DATA_MISSING")


if __name__ == "__main__":
    unittest.main()
