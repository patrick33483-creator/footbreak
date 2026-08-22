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

import os
import subprocess
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

ROOT = Path(__file__).resolve().parents[2]


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


class CrownTickSystemdTimeoutBoundaryTests(unittest.TestCase):
    """Regression coverage for the second incident: systemd killed
    crown-tick.service via ``TimeoutStartSec=55`` before the child ever
    reached its native T-5/T-30 commit or write-ahead journal, even though
    the tick engine's own in-process deadline was a fixed 50s.  Root cause:
    ``ExecStartPre`` (disk_guard preflight + the T-5-priority preempt
    script, observed 3-23s in production journal logs) is *not* counted
    against the fixed 50s Python-side budget, so
    ExecStartPre-elapsed + 50s + parent teardown could exceed 55s and get
    the whole unit (and its in-flight children) SIGKILLed with no honest
    terminal state written at all.

    The fix makes ``deploy/crown-run.sh`` recompute the real remaining
    budget from a systemd-independent epoch sentinel written by the first
    ``ExecStartPre`` step, so the native-stage-owning Python process always
    gets ``TimeoutStartSec - stop_margin - ExecStartPre_elapsed`` seconds
    (capped at the configured fallback ceiling), never a naive fixed value
    that ignores how long ExecStartPre already ran.

    These tests only touch the shell wrapper and the systemd unit text; no
    Telegram, HKJC-comparison, betting, Radar, UI, or Footbreak code paths
    are exercised."""

    def _run_wrapper_budget_calc(self, execstartpre_elapsed: int, epoch_dir: str) -> dict:
        """Execute only the budget-recompute block from crown-run.sh against
        a real epoch sentinel file via a child shell, so the test exercises
        actual bash arithmetic/quoting rather than a Python reimplementation."""
        epoch_file = os.path.join(epoch_dir, "crown-tick-start-epoch")
        start_epoch = int(time.time()) - execstartpre_elapsed
        with open(epoch_file, "w", encoding="utf-8") as handle:
            handle.write(str(start_epoch))
        script = ROOT / "deploy" / "crown-run.sh"
        probe = (
            'CROWN_TICK_START_EPOCH_FILE="' + epoch_file + '" '
            'bash -c \'source <(sed -n "/^MODE=/,/^fi$/p" "' + str(script) + '") ; '
            'echo "$CROWN_TICK_PASS_DEADLINE_SECONDS"\' tick'
        )
        result = subprocess.run(
            ["bash", "-c", probe, "_"], capture_output=True, text=True, timeout=5,
        )
        return {
            "stdout": result.stdout.strip(),
            "stderr": result.stderr.strip(),
            "returncode": result.returncode,
        }

    def test_zero_execstartpre_elapsed_keeps_full_fallback_budget(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            out = self._run_wrapper_budget_calc(0, directory)
            self.assertEqual(out["returncode"], 0, out["stderr"])
            self.assertEqual(out["stdout"], "50")

    def test_typical_execstartpre_elapsed_shrinks_budget_below_fallback(self) -> None:
        # Median observed ExecStartPre duration in the production journal
        # (disk_guard preflight + crown-tick-preempt.sh) was ~6-8s.
        with tempfile.TemporaryDirectory() as directory:
            out = self._run_wrapper_budget_calc(8, directory)
            self.assertEqual(out["returncode"], 0, out["stderr"])
            self.assertEqual(out["stdout"], "43")

    def test_worst_observed_execstartpre_elapsed_still_leaves_native_commit_room(self) -> None:
        # Worst production journal sample: crown-tick-preempt.sh alone took
        # 23s (systemctl stop --no-block contention while stopping other
        # units). The recomputed budget must shrink, not stay fixed at 50,
        # so the combined wall clock cannot again exceed TimeoutStartSec.
        with tempfile.TemporaryDirectory() as directory:
            out = self._run_wrapper_budget_calc(23, directory)
            self.assertEqual(out["returncode"], 0, out["stderr"])
            self.assertEqual(out["stdout"], "28")

    def test_pathological_execstartpre_elapsed_floors_at_honest_minimum_not_zero(self) -> None:
        # Even if ExecStartPre somehow ran far longer than TimeoutStartSec
        # itself, the budget must floor at a small positive value -- enough
        # for one honest DATA_MISSING/FAILED write-ahead pass -- rather than
        # collapsing to zero or negative.
        with tempfile.TemporaryDirectory() as directory:
            out = self._run_wrapper_budget_calc(90, directory)
            self.assertEqual(out["returncode"], 0, out["stderr"])
            self.assertEqual(out["stdout"], "5")

    def test_missing_epoch_sentinel_falls_back_to_configured_default(self) -> None:
        # If the sentinel file is absent (e.g. an older systemd unit not yet
        # redeployed, or /run cleared), the wrapper must fall back to the
        # existing CROWN_TICK_PASS_DEADLINE_SECONDS environment value
        # unchanged rather than failing the tick outright.
        script = ROOT / "deploy" / "crown-run.sh"
        probe = (
            'CROWN_TICK_START_EPOCH_FILE="/nonexistent/crown-tick-start-epoch" '
            'CROWN_TICK_PASS_DEADLINE_SECONDS=50 bash -c '
            '\'source <(sed -n "/^MODE=/,/^fi$/p" "' + str(script) + '") ; '
            'echo "$CROWN_TICK_PASS_DEADLINE_SECONDS"\' _ tick'
        )
        result = subprocess.run(
            ["bash", "-c", probe], capture_output=True, text=True, timeout=5,
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout.strip(), "50")

    def test_non_tick_mode_is_never_given_a_recomputed_budget(self) -> None:
        # Sweep/settle/round-update runs are not the deadline-owning native
        # T-5/T-30 path; the recompute block must be a tick-only concern so
        # it can never change behaviour for any other Crown service.  The
        # real wrapper takes its mode from $1 (not from an env var), so this
        # probe must pass "sweep" positionally, exactly like the real
        # ExecStart=deploy/crown-run.sh <mode> invocation.
        with tempfile.TemporaryDirectory() as directory:
            epoch_file = os.path.join(directory, "crown-tick-start-epoch")
            with open(epoch_file, "w", encoding="utf-8") as handle:
                handle.write(str(int(time.time()) - 40))
            script = ROOT / "deploy" / "crown-run.sh"
            probe = (
                'CROWN_TICK_START_EPOCH_FILE="' + epoch_file + '" '
                'CROWN_TICK_PASS_DEADLINE_SECONDS=50 bash -c '
                '\'source <(sed -n "/^MODE=/,/^fi$/p" "' + str(script) + '") ; '
                'echo "$CROWN_TICK_PASS_DEADLINE_SECONDS"\' _ sweep'
            )
            result = subprocess.run(
                ["bash", "-c", probe], capture_output=True, text=True, timeout=5,
            )
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertEqual(result.stdout.strip(), "50")

    def test_service_unit_writes_epoch_sentinel_before_any_other_execstartpre(self) -> None:
        unit_text = (ROOT / "deploy/systemd/crown-tick.service").read_text(encoding="utf-8")
        lines = [line for line in unit_text.splitlines() if line.startswith("ExecStartPre=")]
        self.assertTrue(lines, "expected at least one ExecStartPre line")
        self.assertIn("crown-tick-start-epoch", lines[0])
        # The disk_guard preflight and the T-5 preempt script must still run
        # -- this fix must not remove either pre-check.
        self.assertTrue(any("disk_guard.py" in line for line in lines))
        self.assertTrue(any("crown-tick-preempt.sh" in line for line in lines))

    def test_service_unit_still_declares_the_fifty_five_second_boundary(self) -> None:
        unit_text = (ROOT / "deploy/systemd/crown-tick.service").read_text(encoding="utf-8")
        self.assertIn("TimeoutStartSec=55", unit_text)
        self.assertIn("CROWN_TICK_TIMEOUT_START_SECONDS=55", unit_text)
        self.assertIn("SendSIGKILL=yes", unit_text)

    def test_wrapper_recompute_block_is_shell_syntax_valid(self) -> None:
        script = ROOT / "deploy" / "crown-run.sh"
        result = subprocess.run(
            ["bash", "-n", str(script)], capture_output=True, text=True, timeout=5,
        )
        self.assertEqual(result.returncode, 0, result.stderr)


class CrownTwentySixSameKickoffRegressionTests(unittest.TestCase):
    """Regression for the exact second-incident batch shape: 26 fixtures
    sharing one due_at/kickoff, previously all EXPIRED with
    ``reason=deadline_exhausted_before_native_stage_commit`` because the
    systemd unit was killed before any of them reached native commit."""

    def _rows(self, count: int, stage: str = "T-5") -> list[dict]:
        kickoff = datetime.now(HKT) + timedelta(minutes=5)
        return [
            {
                "id": f"twenty-six-{i:03d}", "league": "L", "home": f"H{i}", "away": f"A{i}",
                "kickoff": kickoff, "_due_stage": stage,
            }
            for i in range(count)
        ]

    def test_twenty_six_fixture_batch_rotates_and_preserves_completeness(self) -> None:
        rows = self._rows(26)
        seen_first_positions = set()
        for minute_offset in range(26):
            with patch("crown.engine.time.time", return_value=1_700_000_000 + minute_offset * 60):
                ordered = crown_engine._prioritize_tick_rows(list(rows))
            self.assertEqual(len(ordered), 26)
            self.assertEqual(sorted(r["id"] for r in ordered), sorted(r["id"] for r in rows))
            seen_first_positions.add(ordered[0]["id"])
        # No fixture in the exact 26-fixture incident shape may be
        # permanently starved at the tail across the rotation window.
        self.assertEqual(len(seen_first_positions), 26)

    def test_all_twenty_six_due_unstarted_jobs_get_honest_retryable_failure(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            config = replace(settings(), state_dir=Path(directory), enabled=True)
            now = datetime.now(HKT)
            kickoff = now + timedelta(minutes=5)
            watch = {}
            for i in range(26):
                match_id = f"batch26-{i:03d}"
                row = _watch_row(match_id, kickoff)
                row["stage_jobs"]["T-5"]["due_at_utc"] = (
                    now - timedelta(seconds=1)
                ).astimezone(__import__("datetime").timezone.utc).isoformat()
                watch[match_id] = row
            save_ledger(config, {
                "bankroll": 50000, "bets": [], "log": [], "stats": {},
                "watch": watch,
            })
            changed = crown_engine.persist_timed_stage_timeout_failures(config, now=now)
            self.assertEqual(changed, 26)
            saved = load_ledger(config)["watch"]
            for i in range(26):
                match_id = f"batch26-{i:03d}"
                self.assertEqual(
                    saved[match_id]["stage_attempts"]["T-5"]["state"], "FAILED",
                    f"{match_id} did not get an honest FAILED terminal state",
                )
                self.assertEqual(saved[match_id]["stages"][0]["status"], "DATA_MISSING")


class CrownTickChildCleanupTests(unittest.TestCase):
    """The parent must not leak zombie/child processes across ticks: every
    forked provider/prediction child must be reaped (terminate+join, then
    kill+join on the same handle) exactly once per completed or abandoned
    slot, even under the systemd-timeout-forced-shutdown path."""

    def test_terminate_child_joins_after_terminate_and_after_kill(self) -> None:
        process = Mock()
        process.is_alive.side_effect = [True, True]
        receiver = Mock()
        crown_engine._terminate_child(process, receiver)
        receiver.close.assert_called_once()
        process.terminate.assert_called_once()
        process.kill.assert_called_once()
        self.assertEqual(process.join.call_count, 2)

    def test_terminate_child_does_not_call_kill_when_already_dead(self) -> None:
        # _terminate_child checks is_alive() once before terminate() and
        # once more before kill(); a child that is already dead at both
        # checks must short-circuit both actions.
        process = Mock()
        process.is_alive.side_effect = [False, False]
        receiver = Mock()
        crown_engine._terminate_child(process, receiver)
        process.terminate.assert_not_called()
        process.kill.assert_not_called()
        self.assertEqual(process.join.call_count, 1)

    def test_terminate_child_closes_receiver_even_if_process_is_alive_raises(self) -> None:
        process = Mock()
        process.is_alive.side_effect = RuntimeError("boom")
        receiver = Mock()
        with self.assertRaises(RuntimeError):
            crown_engine._terminate_child(process, receiver)
        receiver.close.assert_called_once()


class CrownPreKickoffRetryAndNoBackfillTests(unittest.TestCase):
    """Explicit coverage for two contract clauses that must hold regardless
    of which layer (Python deadline or systemd TimeoutStartSec) caused a
    tick to abandon a fixture: a still-pre-kickoff job must remain
    retryable (not permanently FAILED/terminal) until either it commits or
    kickoff passes, and a post-kickoff EXPIRED job must never be
    backfilled with a late prediction once genuinely past kickoff."""

    def test_pre_kickoff_timeout_failure_is_retried_on_a_later_tick(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            config = replace(settings(), state_dir=Path(directory), enabled=True)
            now = datetime.now(HKT)
            kickoff = now + timedelta(minutes=5)
            watch = _watch_row("retry-1", kickoff)
            watch["stage_jobs"]["T-5"]["due_at_utc"] = (
                now - timedelta(seconds=1)
            ).astimezone(__import__("datetime").timezone.utc).isoformat()
            save_ledger(config, {
                "bankroll": 50000, "bets": [], "log": [], "stats": {},
                "watch": {"retry-1": watch},
            })
            first_pass = crown_engine.persist_timed_stage_timeout_failures(config, now)
            self.assertEqual(first_pass, 1)
            saved = load_ledger(config)["watch"]["retry-1"]
            self.assertEqual(saved["stage_attempts"]["T-5"]["state"], "FAILED")
            # Still pre-kickoff and not COMMITTED: due_stage_jobs must keep
            # surfacing T-5 as due on the very next tick rather than treating
            # this DATA_MISSING attempt as a terminal, non-retryable state.
            self.assertIn("T-5", due_stage_jobs(saved, now + timedelta(seconds=5)))

    def test_no_backfill_after_kickoff_expiry_even_when_provider_data_becomes_available(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            config = replace(settings(), state_dir=Path(directory), enabled=True)
            now = datetime.now(HKT)
            kickoff = now - timedelta(minutes=39)
            watch = _watch_row("no-backfill-2", kickoff)
            save_ledger(config, {
                "bankroll": 50000, "bets": [], "log": [], "stats": {},
                "watch": {"no-backfill-2": watch},
            })
            expired = crown_engine._expire_lapsed_timed_stage_attempts(config, now)
            self.assertEqual(expired, 1)
            saved = load_ledger(config)
            watch_after = saved["watch"]["no-backfill-2"]
            self.assertEqual(watch_after["stage_attempts"]["T-5"]["state"], "EXPIRED")
            self.assertEqual(saved["bets"], [])
            # No T-5 market/prediction stage row was ever created -- the
            # post-kickoff EXPIRED path is an incident marker only.
            self.assertFalse(any(
                isinstance(row, dict) and row.get("stage") == "T-5"
                for row in watch_after.get("stages", [])
            ))
            # A later tick, even one that could reach a provider successfully,
            # must never surface this post-kickoff EXPIRED fixture as due
            # again -- no retry, no backfill once genuinely past kickoff.
            self.assertNotIn(
                "T-5", due_stage_jobs(watch_after, now + timedelta(minutes=1)),
            )


if __name__ == "__main__":
    unittest.main()
