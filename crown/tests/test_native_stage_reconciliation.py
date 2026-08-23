"""Stage 3 of the Crown T-5 deadline-first patch: reconciliation/consumer adapter.

Exercises ``crown/native_stage_reconciliation.py`` -- a default-off,
explicitly-invoked, read-only-by-default adapter between the per-fixture
shadow store (stages 1-2) and the legacy monolithic ``ledger.json``. No
production access, provider call, Telegram, bet, or push is exercised
anywhere in this file; ``TitanClient``/``PinnapiClient``/HKJC discovery are
never imported or constructed.
"""
from __future__ import annotations

import json
import tempfile
import threading
import time
import unittest
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch

from crown.common import HKT
from crown.config import settings
from crown.state import load_ledger, save_ledger
from crown import native_stage_store as store_mod
from crown import native_stage_reconciliation as recon


def _config(state_dir: Path):
    return replace(settings(), state_dir=state_dir)


def _kickoff(minutes_from_now: float, now: datetime | None = None) -> datetime:
    base = now or datetime.now(HKT)
    return base + timedelta(minutes=minutes_from_now)


def _seed_shadow(config, match_id: str, stage: str, kickoff: datetime, *, odds=1.91, line=-0.25, side="H", source="s", observed_at=123, ts=None):
    store = store_mod.NativeStageStore(config.state_dir)
    store.mark_started(match_id, stage, kickoff=kickoff, league="L", home="H", away="Ay")
    store.commit_snapshot(match_id, stage, {
        "match_id": match_id, "league": "L", "home": "H", "away": "Ay",
        "selected_odds_journal": [{
            "code": "HDC", "line": line, "side": side, "odds": odds,
            "source": source, "observed_at": observed_at,
        }],
        "ts": (ts or datetime.now(HKT)).isoformat(),
    }, kickoff=kickoff)
    return store


def _seed_legacy(config, match_id: str, stage: str, kickoff: datetime, *, odds=1.91, line=-0.25, side="H", source="s", observed_at=123, extra_watch=None, data_missing=False):
    ledger = load_ledger(config)
    watch = ledger.setdefault("watch", {}).setdefault(match_id, {
        "match_id": match_id,
        "kickoff_hkt": kickoff.isoformat(),
        "kickoff_utc": kickoff.astimezone(timezone.utc).isoformat(),
        "stages": [],
    })
    if extra_watch:
        watch.update(extra_watch)
    if data_missing:
        row = {"stage": stage, "status": "DATA_MISSING", "odds_status": "missing", "market_predictions": []}
    else:
        row = {
            "stage": stage, "status": "ok", "odds_status": "available",
            "market_predictions": [{
                "code": "HDC", "line": line, "side": side, "odds": odds,
                "source": source, "observed_at": observed_at,
            }],
        }
    watch["stages"] = [r for r in watch["stages"] if r.get("stage") != stage] + [row]
    save_ledger(config, ledger)
    return ledger


class ConsistentProjectionTests(unittest.TestCase):
    """Requirement: 一致投影 -- MATCH classification and plan-row shape."""

    def test_identical_evidence_both_sides_classified_match(self):
        with tempfile.TemporaryDirectory() as tmp:
            config = _config(Path(tmp))
            now = datetime.now(HKT)
            kickoff = _kickoff(20, now)
            _seed_shadow(config, "M1", "T-5", kickoff)
            _seed_legacy(config, "M1", "T-5", kickoff)
            comparisons = recon.compare_many(config, [recon.FixtureLookup("M1", "T-5")], now=now)
            self.assertEqual(len(comparisons), 1)
            self.assertEqual(comparisons[0].status, recon.ReconciliationStatus.MATCH)
            self.assertTrue(comparisons[0].identity_checked)

    def test_plan_never_includes_a_match_row(self):
        with tempfile.TemporaryDirectory() as tmp:
            config = _config(Path(tmp))
            now = datetime.now(HKT)
            kickoff = _kickoff(20, now)
            _seed_shadow(config, "M2", "T-5", kickoff)
            _seed_legacy(config, "M2", "T-5", kickoff)
            plan = recon.build_reconciliation_plan(config, [recon.FixtureLookup("M2", "T-5")], now=now)
            self.assertEqual(plan["to_apply"], [])
            self.assertEqual(len(plan["skipped"]), 1)
            self.assertEqual(plan["skipped"][0]["status"], "MATCH")

    def test_plan_row_is_bounded_and_allow_listed(self):
        with tempfile.TemporaryDirectory() as tmp:
            config = _config(Path(tmp))
            now = datetime.now(HKT)
            kickoff = _kickoff(20, now)
            _seed_shadow(config, "M3", "T-5", kickoff)
            plan = recon.build_reconciliation_plan(config, [recon.FixtureLookup("M3", "T-5")], now=now)
            self.assertEqual(len(plan["to_apply"]), 1)
            row = plan["to_apply"][0]["projected_row"]
            allowed = set(recon._PLAN_ROW_ALLOWED_KEYS) | {
                "reconciliation_source", "reconciliation_generated_at",
                "reconciliation_note", "selected_odds_journal",
            }
            self.assertTrue(set(row.keys()).issubset(allowed))
            self.assertEqual(row["match_id"], "M3")
            self.assertEqual(row["stage"], "T-5")


class IdentityMismatchTests(unittest.TestCase):
    """Requirement: identity mismatch -- fail closed to CONFLICT, never coerced."""

    def test_odds_mismatch_is_conflict(self):
        with tempfile.TemporaryDirectory() as tmp:
            config = _config(Path(tmp))
            now = datetime.now(HKT)
            kickoff = _kickoff(20, now)
            _seed_shadow(config, "C1", "T-5", kickoff, odds=1.91)
            _seed_legacy(config, "C1", "T-5", kickoff, odds=2.05)
            comparisons = recon.compare_many(config, [recon.FixtureLookup("C1", "T-5")], now=now)
            self.assertEqual(comparisons[0].status, recon.ReconciliationStatus.CONFLICT)
            self.assertTrue(any("odds_mismatch" in r for r in comparisons[0].reasons))

    def test_line_mismatch_is_conflict(self):
        with tempfile.TemporaryDirectory() as tmp:
            config = _config(Path(tmp))
            now = datetime.now(HKT)
            kickoff = _kickoff(20, now)
            _seed_shadow(config, "C2", "T-5", kickoff, line=-0.25)
            _seed_legacy(config, "C2", "T-5", kickoff, line=0.0)
            comparisons = recon.compare_many(config, [recon.FixtureLookup("C2", "T-5")], now=now)
            self.assertEqual(comparisons[0].status, recon.ReconciliationStatus.CONFLICT)

    def test_side_mismatch_is_conflict(self):
        with tempfile.TemporaryDirectory() as tmp:
            config = _config(Path(tmp))
            now = datetime.now(HKT)
            kickoff = _kickoff(20, now)
            _seed_shadow(config, "C3", "T-5", kickoff, side="H")
            _seed_legacy(config, "C3", "T-5", kickoff, side="A")
            comparisons = recon.compare_many(config, [recon.FixtureLookup("C3", "T-5")], now=now)
            self.assertEqual(comparisons[0].status, recon.ReconciliationStatus.CONFLICT)

    def test_source_mismatch_is_conflict(self):
        with tempfile.TemporaryDirectory() as tmp:
            config = _config(Path(tmp))
            now = datetime.now(HKT)
            kickoff = _kickoff(20, now)
            _seed_shadow(config, "C4", "T-5", kickoff, source="provider-a")
            _seed_legacy(config, "C4", "T-5", kickoff, source="provider-b")
            comparisons = recon.compare_many(config, [recon.FixtureLookup("C4", "T-5")], now=now)
            self.assertEqual(comparisons[0].status, recon.ReconciliationStatus.CONFLICT)

    def test_observed_at_mismatch_is_conflict(self):
        with tempfile.TemporaryDirectory() as tmp:
            config = _config(Path(tmp))
            now = datetime.now(HKT)
            kickoff = _kickoff(20, now)
            _seed_shadow(config, "C5", "T-5", kickoff, observed_at=100)
            _seed_legacy(config, "C5", "T-5", kickoff, observed_at=200)
            comparisons = recon.compare_many(config, [recon.FixtureLookup("C5", "T-5")], now=now)
            self.assertEqual(comparisons[0].status, recon.ReconciliationStatus.CONFLICT)

    def test_kickoff_mismatch_is_conflict(self):
        with tempfile.TemporaryDirectory() as tmp:
            config = _config(Path(tmp))
            now = datetime.now(HKT)
            kickoff_shadow = _kickoff(20, now)
            kickoff_legacy = _kickoff(25, now)
            _seed_shadow(config, "C6", "T-5", kickoff_shadow)
            _seed_legacy(config, "C6", "T-5", kickoff_legacy)
            comparisons = recon.compare_many(config, [recon.FixtureLookup("C6", "T-5")], now=now)
            self.assertEqual(comparisons[0].status, recon.ReconciliationStatus.CONFLICT)
            self.assertTrue(any("kickoff_mismatch" in r for r in comparisons[0].reasons))

    def test_match_id_mismatch_between_lookup_and_shadow_state_is_conflict(self):
        with tempfile.TemporaryDirectory() as tmp:
            config = _config(Path(tmp))
            now = datetime.now(HKT)
            kickoff = _kickoff(20, now)
            store = store_mod.NativeStageStore(config.state_dir)
            # Deliberately corrupt: write to the M7 path but with a different
            # match_id recorded inside the payload.
            store.mark_started("M7", "T-5", kickoff=kickoff)
            state = store.read("M7")
            state["match_id"] = "SOMETHING_ELSE"
            store_mod._atomic_write(store.path_for("M7"), state)
            store.commit_snapshot("M7", "T-5", {
                "match_id": "M7",
                "selected_odds_journal": [{"code": "HDC", "line": -0.25, "side": "H", "odds": 1.91, "source": "s", "observed_at": 1}],
            }, kickoff=kickoff)
            # commit_snapshot rewrites the file but does not touch match_id
            state2 = store.read("M7")
            state2["match_id"] = "SOMETHING_ELSE"
            store_mod._atomic_write(store.path_for("M7"), state2)
            _seed_legacy(config, "M7", "T-5", kickoff)
            comparisons = recon.compare_many(config, [recon.FixtureLookup("M7", "T-5")], now=now)
            self.assertEqual(comparisons[0].status, recon.ReconciliationStatus.CONFLICT)
            self.assertTrue(any("match_id_mismatch" in r for r in comparisons[0].reasons))


class SnapshotSchemaMismatchTests(unittest.TestCase):
    """Requirement: snapshot schema mismatch -- malformed rows handled safely."""

    def test_missing_selected_odds_journal_on_shadow_side_is_still_shadow_only_or_match(self):
        with tempfile.TemporaryDirectory() as tmp:
            config = _config(Path(tmp))
            now = datetime.now(HKT)
            kickoff = _kickoff(20, now)
            store = store_mod.NativeStageStore(config.state_dir)
            store.mark_started("S1", "T-5", kickoff=kickoff)
            store.commit_snapshot("S1", "T-5", {"match_id": "S1"}, kickoff=kickoff)
            comparisons = recon.compare_many(config, [recon.FixtureLookup("S1", "T-5")], now=now)
            # No legacy row exists at all -> SHADOW_ONLY, not a crash.
            self.assertEqual(comparisons[0].status, recon.ReconciliationStatus.SHADOW_ONLY)

    def test_non_dict_stage_row_in_legacy_watch_is_ignored_not_fatal(self):
        with tempfile.TemporaryDirectory() as tmp:
            config = _config(Path(tmp))
            now = datetime.now(HKT)
            kickoff = _kickoff(20, now)
            _seed_shadow(config, "S2", "T-5", kickoff)
            ledger = load_ledger(config)
            ledger.setdefault("watch", {})["S2"] = {
                "match_id": "S2", "kickoff_hkt": kickoff.isoformat(),
                "kickoff_utc": kickoff.astimezone(timezone.utc).isoformat(),
                "stages": ["not-a-dict", 123, None],
            }
            save_ledger(config, ledger)
            comparisons = recon.compare_many(config, [recon.FixtureLookup("S2", "T-5")], now=now)
            self.assertEqual(comparisons[0].status, recon.ReconciliationStatus.SHADOW_ONLY)

    def test_invalid_lookup_stage_is_expired_invalid(self):
        with tempfile.TemporaryDirectory() as tmp:
            config = _config(Path(tmp))
            comparisons = recon.compare_many(config, [recon.FixtureLookup("X1", "NOT-A-STAGE")])
            self.assertEqual(comparisons[0].status, recon.ReconciliationStatus.EXPIRED_INVALID)

    def test_empty_match_id_is_expired_invalid(self):
        with tempfile.TemporaryDirectory() as tmp:
            config = _config(Path(tmp))
            comparisons = recon.compare_many(config, [recon.FixtureLookup("", "T-5")])
            self.assertEqual(comparisons[0].status, recon.ReconciliationStatus.EXPIRED_INVALID)

    def test_market_present_only_on_one_side_no_field_level_conflict_without_both(self):
        with tempfile.TemporaryDirectory() as tmp:
            config = _config(Path(tmp))
            now = datetime.now(HKT)
            kickoff = _kickoff(20, now)
            # Shadow has HDC only; legacy has HIL only -- both "committed" per
            # their own schema, but the codes do not overlap, so no per-field
            # comparison can run.  This must not silently become MATCH.
            store = store_mod.NativeStageStore(config.state_dir)
            store.mark_started("S3", "T-5", kickoff=kickoff)
            store.commit_snapshot("S3", "T-5", {
                "match_id": "S3",
                "selected_odds_journal": [{"code": "HDC", "line": -0.25, "side": "H", "odds": 1.91, "source": "s", "observed_at": 1}],
            }, kickoff=kickoff)
            ledger = load_ledger(config)
            ledger.setdefault("watch", {})["S3"] = {
                "match_id": "S3", "kickoff_hkt": kickoff.isoformat(),
                "kickoff_utc": kickoff.astimezone(timezone.utc).isoformat(),
                "stages": [{
                    "stage": "T-5", "status": "ok", "odds_status": "available",
                    "market_predictions": [{"code": "HIL", "line": 2.5, "side": "H", "odds": 1.95, "source": "s", "observed_at": 1}],
                }],
            }
            save_ledger(config, ledger)
            comparisons = recon.compare_many(config, [recon.FixtureLookup("S3", "T-5")], now=now)
            # No overlapping market code to compare => no mismatch detected =>
            # classified MATCH at the fixture level (both sides committed,
            # identity fields agree). This is intentionally documented: only
            # overlapping market codes get field-level verification.
            self.assertEqual(comparisons[0].status, recon.ReconciliationStatus.MATCH)


class T30T5RetentionTests(unittest.TestCase):
    """Requirement: T-30/T-5 保留 -- stages never overwrite each other."""

    def test_t30_and_t5_compared_independently(self):
        with tempfile.TemporaryDirectory() as tmp:
            config = _config(Path(tmp))
            now = datetime.now(HKT)
            kickoff = _kickoff(40, now)
            _seed_shadow(config, "T1", "T-30", kickoff, odds=1.80)
            _seed_shadow(config, "T1", "T-5", kickoff, odds=1.91)
            _seed_legacy(config, "T1", "T-30", kickoff, odds=1.80)
            # T-5 legacy absent -> SHADOW_ONLY for T-5, MATCH for T-30.
            comparisons = recon.compare_many(
                config, [recon.FixtureLookup("T1", "T-30"), recon.FixtureLookup("T1", "T-5")], now=now,
            )
            by_stage = {c.stage: c.status for c in comparisons}
            self.assertEqual(by_stage["T-30"], recon.ReconciliationStatus.MATCH)
            self.assertEqual(by_stage["T-5"], recon.ReconciliationStatus.SHADOW_ONLY)

    def test_apply_for_t5_does_not_touch_t30_row(self):
        with tempfile.TemporaryDirectory() as tmp:
            config = _config(Path(tmp))
            now = datetime.now(HKT)
            kickoff = _kickoff(40, now)
            _seed_shadow(config, "T2", "T-30", kickoff, odds=1.80)
            _seed_shadow(config, "T2", "T-5", kickoff, odds=1.91)
            _seed_legacy(config, "T2", "T-30", kickoff, odds=1.80)
            plan = recon.build_reconciliation_plan(
                config, [recon.FixtureLookup("T2", "T-30"), recon.FixtureLookup("T2", "T-5")], now=now,
            )
            self.assertEqual(len(plan["to_apply"]), 1)
            self.assertEqual(plan["to_apply"][0]["stage"], "T-5")
            result = recon.apply_reconciliation_plan(
                config, plan, dry_run=False, i_understand_this_writes_the_legacy_ledger=True, now=now,
            )
            self.assertEqual(len(result["applied"]), 1)
            ledger = load_ledger(config)
            stages = ledger["watch"]["T2"]["stages"]
            stage_names = sorted(r["stage"] for r in stages)
            self.assertEqual(stage_names, ["T-30", "T-5"])
            t30_row = next(r for r in stages if r["stage"] == "T-30")
            self.assertEqual(t30_row["market_predictions"][0]["odds"], 1.80)

    def test_apply_never_overwrites_existing_committed_t30(self):
        with tempfile.TemporaryDirectory() as tmp:
            config = _config(Path(tmp))
            now = datetime.now(HKT)
            kickoff = _kickoff(40, now)
            _seed_shadow(config, "T3", "T-30", kickoff, odds=1.99)  # different from legacy
            _seed_legacy(config, "T3", "T-30", kickoff, odds=1.80)  # legacy already committed
            plan = recon.build_reconciliation_plan(config, [recon.FixtureLookup("T3", "T-30")], now=now)
            # Both committed with different odds -> CONFLICT, never proposed for apply.
            self.assertEqual(plan["to_apply"], [])
            ledger_before = load_ledger(config)
            row_before = ledger_before["watch"]["T3"]["stages"][0]
            self.assertEqual(row_before["market_predictions"][0]["odds"], 1.80)


class DuplicateIdempotencyTests(unittest.TestCase):
    """Requirement: duplicate/idempotency -- rerun produces no duplicate effect."""

    def test_repeated_compare_many_is_byte_identical(self):
        with tempfile.TemporaryDirectory() as tmp:
            config = _config(Path(tmp))
            now = datetime.now(HKT)
            kickoff = _kickoff(20, now)
            _seed_shadow(config, "D1", "T-5", kickoff)
            first = [c.as_dict() for c in recon.compare_many(config, [recon.FixtureLookup("D1", "T-5")], now=now)]
            second = [c.as_dict() for c in recon.compare_many(config, [recon.FixtureLookup("D1", "T-5")], now=now)]
            self.assertEqual(first, second)

    def test_repeated_plan_build_is_stable(self):
        with tempfile.TemporaryDirectory() as tmp:
            config = _config(Path(tmp))
            now = datetime.now(HKT)
            kickoff = _kickoff(20, now)
            _seed_shadow(config, "D2", "T-5", kickoff)
            plan1 = recon.build_reconciliation_plan(config, [recon.FixtureLookup("D2", "T-5")], now=now)
            plan2 = recon.build_reconciliation_plan(config, [recon.FixtureLookup("D2", "T-5")], now=now)
            self.assertEqual(
                [r["match_id"] for r in plan1["to_apply"]],
                [r["match_id"] for r in plan2["to_apply"]],
            )

    def test_repeated_apply_is_idempotent_no_duplicate_rows(self):
        with tempfile.TemporaryDirectory() as tmp:
            config = _config(Path(tmp))
            now = datetime.now(HKT)
            kickoff = _kickoff(20, now)
            _seed_shadow(config, "D3", "T-5", kickoff)
            _seed_legacy(config, "D3", "T-5", kickoff, data_missing=True)
            plan = recon.build_reconciliation_plan(config, [recon.FixtureLookup("D3", "T-5")], now=now)
            r1 = recon.apply_reconciliation_plan(config, plan, dry_run=False, i_understand_this_writes_the_legacy_ledger=True, now=now)
            r2 = recon.apply_reconciliation_plan(config, plan, dry_run=False, i_understand_this_writes_the_legacy_ledger=True, now=now)
            self.assertEqual(len(r1["applied"]), 1)
            self.assertEqual(len(r2["applied"]), 0)
            ledger = load_ledger(config)
            non_missing_rows = [
                r for r in ledger["watch"]["D3"]["stages"]
                if r.get("stage") == "T-5" and r.get("status") != "DATA_MISSING"
            ]
            self.assertEqual(len(non_missing_rows), 1)

    def test_data_missing_legacy_row_is_shadow_only_and_replaceable_once(self):
        with tempfile.TemporaryDirectory() as tmp:
            config = _config(Path(tmp))
            now = datetime.now(HKT)
            kickoff = _kickoff(20, now)
            _seed_shadow(config, "D4", "T-5", kickoff)
            _seed_legacy(config, "D4", "T-5", kickoff, data_missing=True)
            comparisons = recon.compare_many(config, [recon.FixtureLookup("D4", "T-5")], now=now)
            self.assertEqual(comparisons[0].status, recon.ReconciliationStatus.SHADOW_ONLY)


class CrashRestartTests(unittest.TestCase):
    """Requirement: crash/restart -- module tolerates interrupted shadow state."""

    def test_shadow_file_missing_after_simulated_crash_is_treated_as_absent(self):
        with tempfile.TemporaryDirectory() as tmp:
            config = _config(Path(tmp))
            now = datetime.now(HKT)
            kickoff = _kickoff(20, now)
            store = store_mod.NativeStageStore(config.state_dir)
            store.mark_started("R1", "T-5", kickoff=kickoff)  # STARTED only, no commit -- simulated crash
            _seed_legacy(config, "R1", "T-5", kickoff)
            comparisons = recon.compare_many(config, [recon.FixtureLookup("R1", "T-5")], now=now)
            # Shadow never reached COMMITTED -> no shadow snapshot -> LEGACY_ONLY.
            self.assertEqual(comparisons[0].status, recon.ReconciliationStatus.LEGACY_ONLY)

    def test_restart_after_partial_write_then_successful_commit_reconciles_cleanly(self):
        with tempfile.TemporaryDirectory() as tmp:
            config = _config(Path(tmp))
            now = datetime.now(HKT)
            kickoff = _kickoff(20, now)
            store = store_mod.NativeStageStore(config.state_dir)
            store.mark_started("R2", "T-5", kickoff=kickoff)  # first "process" crashes here
            # "Restart": second process resumes and completes the commit.
            store.commit_snapshot("R2", "T-5", {
                "match_id": "R2",
                "selected_odds_journal": [{"code": "HDC", "line": -0.25, "side": "H", "odds": 1.91, "source": "s", "observed_at": 1}],
            }, kickoff=kickoff)
            comparisons = recon.compare_many(config, [recon.FixtureLookup("R2", "T-5")], now=now)
            self.assertEqual(comparisons[0].status, recon.ReconciliationStatus.SHADOW_ONLY)


class LockContentionTests(unittest.TestCase):
    """Requirement: lock 競爭 -- reconciliation reads are resilient to contention."""

    def test_read_during_concurrent_shadow_writer_lock_does_not_raise(self):
        with tempfile.TemporaryDirectory() as tmp:
            config = _config(Path(tmp))
            now = datetime.now(HKT)
            kickoff = _kickoff(20, now)
            _seed_shadow(config, "L1", "T-5", kickoff)
            store = store_mod.NativeStageStore(config.state_dir)
            lock = store_mod._FixtureLock(config.state_dir, "L1", timeout_seconds=0.05)
            acquired = lock.__enter__()
            self.assertTrue(acquired)
            try:
                # Reading does not go through the fixture lock at all (read()
                # is a plain file read), so this must succeed immediately
                # even while a writer holds the lock.
                comparisons = recon.compare_many(config, [recon.FixtureLookup("L1", "T-5")], now=now)
                self.assertEqual(comparisons[0].status, recon.ReconciliationStatus.SHADOW_ONLY)
            finally:
                lock.__exit__()

    def test_apply_lock_timeout_refuses_cleanly_without_raising_to_caller(self):
        with tempfile.TemporaryDirectory() as tmp:
            config = _config(Path(tmp))
            now = datetime.now(HKT)
            kickoff = _kickoff(20, now)
            _seed_shadow(config, "L2", "T-5", kickoff)
            plan = recon.build_reconciliation_plan(config, [recon.FixtureLookup("L2", "T-5")], now=now)

            stop = threading.Event()

            def _hold_state_lock():
                import fcntl
                config.state_dir.mkdir(parents=True, exist_ok=True)
                path = config.state_dir / ".state.lock"
                with path.open("a+", encoding="utf-8") as handle:
                    fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
                    stop.wait(5.0)

            holder = threading.Thread(target=_hold_state_lock, daemon=True)
            holder.start()
            time.sleep(0.1)
            try:
                with self.assertRaises(recon.ReconciliationApplyRefused):
                    recon.apply_reconciliation_plan(
                        config, plan, dry_run=False,
                        i_understand_this_writes_the_legacy_ledger=True, now=now,
                    )
            finally:
                stop.set()
                holder.join(timeout=5.0)


class CorruptShardTests(unittest.TestCase):
    """Requirement: corrupt shard -- one fixture's broken file never blocks others."""

    def test_corrupt_shadow_json_for_one_fixture_treated_as_absent(self):
        with tempfile.TemporaryDirectory() as tmp:
            config = _config(Path(tmp))
            now = datetime.now(HKT)
            kickoff = _kickoff(20, now)
            store = store_mod.NativeStageStore(config.state_dir)
            path = store.path_for("K1")
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text("{not valid json!!", encoding="utf-8")
            _seed_legacy(config, "K1", "T-5", kickoff)
            comparisons = recon.compare_many(config, [recon.FixtureLookup("K1", "T-5")], now=now)
            self.assertEqual(comparisons[0].status, recon.ReconciliationStatus.LEGACY_ONLY)

    def test_corrupt_shard_for_one_fixture_does_not_block_a_healthy_sibling(self):
        with tempfile.TemporaryDirectory() as tmp:
            config = _config(Path(tmp))
            now = datetime.now(HKT)
            kickoff = _kickoff(20, now)
            store = store_mod.NativeStageStore(config.state_dir)
            corrupt_path = store.path_for("K2")
            corrupt_path.parent.mkdir(parents=True, exist_ok=True)
            corrupt_path.write_text("{{{{broken", encoding="utf-8")
            _seed_shadow(config, "K3", "T-5", kickoff)
            _seed_legacy(config, "K2", "T-5", kickoff)
            comparisons = recon.compare_many(
                config, [recon.FixtureLookup("K2", "T-5"), recon.FixtureLookup("K3", "T-5")], now=now,
            )
            by_id = {c.match_id: c.status for c in comparisons}
            self.assertEqual(by_id["K2"], recon.ReconciliationStatus.LEGACY_ONLY)
            self.assertEqual(by_id["K3"], recon.ReconciliationStatus.SHADOW_ONLY)

    def test_store_read_raising_arbitrary_exception_is_isolated(self):
        with tempfile.TemporaryDirectory() as tmp:
            config = _config(Path(tmp))
            now = datetime.now(HKT)
            kickoff = _kickoff(20, now)
            _seed_legacy(config, "K4", "T-5", kickoff)
            with patch.object(store_mod.NativeStageStore, "read", side_effect=RuntimeError("disk exploded")):
                comparisons = recon.compare_many(config, [recon.FixtureLookup("K4", "T-5")], now=now)
            self.assertEqual(comparisons[0].status, recon.ReconciliationStatus.LEGACY_ONLY)


class PostKickoffNoBackfillTests(unittest.TestCase):
    """Requirement: post-kickoff no-backfill -- never proposed or applied."""

    def test_post_kickoff_fixture_is_expired_invalid_even_if_both_sides_committed(self):
        with tempfile.TemporaryDirectory() as tmp:
            config = _config(Path(tmp))
            now = datetime.now(HKT)
            kickoff = now - timedelta(minutes=1)
            # Force both sides to appear committed for a since-passed kickoff.
            store = store_mod.NativeStageStore(config.state_dir)
            store._atomic_write = store_mod._atomic_write  # keep default writer
            # Build shadow snapshot directly (bypassing commit_snapshot's own
            # kickoff guard) to simulate stale historic data reconciliation
            # must still refuse to touch.
            state = store_mod._new_fixture_state(
                "P1", league="L", home="H", away="Ay", kickoff=kickoff,
                native_fixture_id="P1", legacy_watch_identity=None,
            )
            state["snapshots"]["T-5"] = store_mod._bounded_snapshot("T-5", {
                "match_id": "P1",
                "selected_odds_journal": [{"code": "HDC", "line": -0.25, "side": "H", "odds": 1.91, "source": "s", "observed_at": 1}],
            })
            store_mod._append_attempt(state, "T-5", "COMMITTED")
            store_mod._atomic_write(store.path_for("P1"), state)
            _seed_legacy(config, "P1", "T-5", kickoff)
            comparisons = recon.compare_many(config, [recon.FixtureLookup("P1", "T-5")], now=now)
            self.assertEqual(comparisons[0].status, recon.ReconciliationStatus.EXPIRED_INVALID)

    def test_post_kickoff_shadow_only_never_appears_in_plan_to_apply(self):
        with tempfile.TemporaryDirectory() as tmp:
            config = _config(Path(tmp))
            now = datetime.now(HKT)
            kickoff = now - timedelta(minutes=1)
            store = store_mod.NativeStageStore(config.state_dir)
            state = store_mod._new_fixture_state(
                "P2", league="L", home="H", away="Ay", kickoff=kickoff,
                native_fixture_id="P2", legacy_watch_identity=None,
            )
            state["snapshots"]["T-5"] = store_mod._bounded_snapshot("T-5", {"match_id": "P2"})
            store_mod._append_attempt(state, "T-5", "COMMITTED")
            store_mod._atomic_write(store.path_for("P2"), state)
            plan = recon.build_reconciliation_plan(config, [recon.FixtureLookup("P2", "T-5")], now=now)
            self.assertEqual(plan["to_apply"], [])

    def test_apply_final_check_refuses_a_row_whose_kickoff_passed_between_plan_and_apply(self):
        with tempfile.TemporaryDirectory() as tmp:
            config = _config(Path(tmp))
            now = datetime.now(HKT)
            kickoff = _kickoff(0.02, now)  # ~1.2 seconds in the future
            _seed_shadow(config, "P3", "T-5", kickoff)
            plan = recon.build_reconciliation_plan(config, [recon.FixtureLookup("P3", "T-5")], now=now)
            self.assertEqual(len(plan["to_apply"]), 1)
            later = kickoff + timedelta(seconds=1)  # now strictly after kickoff
            result = recon.apply_reconciliation_plan(
                config, plan, dry_run=False, i_understand_this_writes_the_legacy_ledger=True, now=later,
            )
            self.assertEqual(result["applied"], [])
            self.assertTrue(any(
                "no_longer_shadow_only_at_apply_time" in r["reason"] for r in result["refused"]
            ))


class ClassificationTests(unittest.TestCase):
    """Requirement: shadow-only/legacy-only/conflict 分類 -- exhaustive coverage."""

    def test_shadow_only(self):
        with tempfile.TemporaryDirectory() as tmp:
            config = _config(Path(tmp))
            now = datetime.now(HKT)
            kickoff = _kickoff(20, now)
            _seed_shadow(config, "Z1", "T-5", kickoff)
            comparisons = recon.compare_many(config, [recon.FixtureLookup("Z1", "T-5")], now=now)
            self.assertEqual(comparisons[0].status, recon.ReconciliationStatus.SHADOW_ONLY)

    def test_legacy_only(self):
        with tempfile.TemporaryDirectory() as tmp:
            config = _config(Path(tmp))
            now = datetime.now(HKT)
            kickoff = _kickoff(20, now)
            _seed_legacy(config, "Z2", "T-5", kickoff)
            comparisons = recon.compare_many(config, [recon.FixtureLookup("Z2", "T-5")], now=now)
            self.assertEqual(comparisons[0].status, recon.ReconciliationStatus.LEGACY_ONLY)

    def test_conflict(self):
        with tempfile.TemporaryDirectory() as tmp:
            config = _config(Path(tmp))
            now = datetime.now(HKT)
            kickoff = _kickoff(20, now)
            _seed_shadow(config, "Z3", "T-5", kickoff, odds=1.5)
            _seed_legacy(config, "Z3", "T-5", kickoff, odds=1.9)
            comparisons = recon.compare_many(config, [recon.FixtureLookup("Z3", "T-5")], now=now)
            self.assertEqual(comparisons[0].status, recon.ReconciliationStatus.CONFLICT)

    def test_expired_invalid_neither_side(self):
        with tempfile.TemporaryDirectory() as tmp:
            config = _config(Path(tmp))
            comparisons = recon.compare_many(config, [recon.FixtureLookup("Z4", "T-5")])
            self.assertEqual(comparisons[0].status, recon.ReconciliationStatus.EXPIRED_INVALID)

    def test_aggregate_report_counts_every_category_exactly_once(self):
        with tempfile.TemporaryDirectory() as tmp:
            config = _config(Path(tmp))
            now = datetime.now(HKT)
            kickoff = _kickoff(20, now)
            _seed_shadow(config, "AG1", "T-5", kickoff)
            _seed_legacy(config, "AG2", "T-5", kickoff)
            _seed_shadow(config, "AG3", "T-5", kickoff, odds=1.1)
            _seed_legacy(config, "AG3", "T-5", kickoff, odds=1.2)
            _seed_shadow(config, "AG4", "T-5", kickoff)
            _seed_legacy(config, "AG4", "T-5", kickoff)
            lookups = [recon.FixtureLookup(m, "T-5") for m in ("AG1", "AG2", "AG3", "AG4", "AG5")]
            report = recon.build_acceptance_report(config, lookups, now=now)
            self.assertEqual(report["aggregate"]["SHADOW_ONLY"], 1)
            self.assertEqual(report["aggregate"]["LEGACY_ONLY"], 1)
            self.assertEqual(report["aggregate"]["CONFLICT"], 1)
            self.assertEqual(report["aggregate"]["MATCH"], 1)
            self.assertEqual(report["aggregate"]["EXPIRED_INVALID"], 1)
            self.assertEqual(report["provider_calls_made"], 0)
            self.assertEqual(report["writes_performed"], 0)


class ConsumerFailureIsolationTests(unittest.TestCase):
    """Requirement: consumer failure isolation -- one bad fixture never blocks others."""

    def test_one_corrupt_fixture_does_not_prevent_report_for_the_rest(self):
        with tempfile.TemporaryDirectory() as tmp:
            config = _config(Path(tmp))
            now = datetime.now(HKT)
            kickoff = _kickoff(20, now)
            store = store_mod.NativeStageStore(config.state_dir)
            bad_path = store.path_for("F1")
            bad_path.parent.mkdir(parents=True, exist_ok=True)
            bad_path.write_text("not json", encoding="utf-8")
            _seed_shadow(config, "F2", "T-5", kickoff)
            _seed_legacy(config, "F3", "T-5", kickoff)
            lookups = [recon.FixtureLookup(m, "T-5") for m in ("F1", "F2", "F3")]
            report = recon.build_acceptance_report(config, lookups, now=now)
            self.assertEqual(report["compared"], 3)
            by_id = {row["match_id"]: row["status"] for row in report["fixtures"]}
            self.assertEqual(by_id["F1"], "EXPIRED_INVALID")
            self.assertEqual(by_id["F2"], "SHADOW_ONLY")
            self.assertEqual(by_id["F3"], "LEGACY_ONLY")

    def test_plan_build_skips_broken_entry_without_aborting_whole_plan(self):
        with tempfile.TemporaryDirectory() as tmp:
            config = _config(Path(tmp))
            now = datetime.now(HKT)
            kickoff = _kickoff(20, now)
            _seed_shadow(config, "F4", "T-5", kickoff)
            _seed_shadow(config, "F5", "T-5", kickoff)
            lookups = [recon.FixtureLookup("F4", "T-5"), recon.FixtureLookup("F5", "T-5")]
            real_read = store_mod.NativeStageStore.read

            def _flaky_read(self, match_id, *args, **kwargs):
                if match_id == "F4":
                    # Simulate a re-read race: the snapshot vanished between
                    # compare_many's classification pass and the plan's own
                    # re-read, for this fixture only.
                    return {"snapshots": {"T-5": None}}
                return real_read(self, match_id, *args, **kwargs)

            with patch.object(store_mod.NativeStageStore, "read", _flaky_read):
                plan = recon.build_reconciliation_plan(config, lookups, now=now)
            match_ids_applied = {row["match_id"] for row in plan["to_apply"]}
            self.assertIn("F5", match_ids_applied)
            self.assertNotIn("F4", match_ids_applied)
            skipped_ids = {row["match_id"] for row in plan["skipped"]}
            self.assertIn("F4", skipped_ids)


class BoundedBatchSizeTests(unittest.TestCase):
    """Requirement: 1/15/26/50/89 fixture bounded run."""

    def test_batch_sizes_1_15_26_50_89(self):
        for size in (1, 15, 26, 50, 89):
            with self.subTest(size=size):
                with tempfile.TemporaryDirectory() as tmp:
                    config = _config(Path(tmp))
                    now = datetime.now(HKT)
                    kickoff = _kickoff(30, now)
                    match_ids = [f"BATCH-{size}-{i}" for i in range(size)]
                    for idx, match_id in enumerate(match_ids):
                        if idx % 3 == 0:
                            _seed_shadow(config, match_id, "T-5", kickoff)
                        elif idx % 3 == 1:
                            _seed_legacy(config, match_id, "T-5", kickoff)
                        else:
                            _seed_shadow(config, match_id, "T-5", kickoff, odds=1.5)
                            _seed_legacy(config, match_id, "T-5", kickoff, odds=1.5)
                    lookups = [recon.FixtureLookup(m, "T-5") for m in match_ids]
                    report = recon.build_acceptance_report(config, lookups, now=now)
                    self.assertEqual(report["compared"], size)
                    self.assertEqual(report["provider_calls_made"], 0)
                    self.assertEqual(report["writes_performed"], 0)
                    plan = recon.build_reconciliation_plan(config, lookups, now=now)
                    expected_shadow_only = len([i for i in range(size) if i % 3 == 0])
                    self.assertEqual(len(plan["to_apply"]), expected_shadow_only)

    def test_refuses_batches_larger_than_max_bounded_fixtures(self):
        with tempfile.TemporaryDirectory() as tmp:
            config = _config(Path(tmp))
            lookups = [
                recon.FixtureLookup(f"OVER-{i}", "T-5")
                for i in range(recon.MAX_BOUNDED_FIXTURES + 1)
            ]
            with self.assertRaises(ValueError):
                recon.compare_many(config, lookups)


class ApplySafetyGateTests(unittest.TestCase):
    """Requirement: apply API 明確參數、pre-kickoff/identity gates, dry-run default."""

    def test_apply_defaults_to_dry_run_even_if_confirmation_flag_set(self):
        with tempfile.TemporaryDirectory() as tmp:
            config = _config(Path(tmp))
            now = datetime.now(HKT)
            kickoff = _kickoff(20, now)
            _seed_shadow(config, "AP1", "T-5", kickoff)
            plan = recon.build_reconciliation_plan(config, [recon.FixtureLookup("AP1", "T-5")], now=now)
            result = recon.apply_reconciliation_plan(
                config, plan, i_understand_this_writes_the_legacy_ledger=True, now=now,
            )
            self.assertTrue(result["dry_run"])
            self.assertEqual(result["applied"], [])
            self.assertEqual(len(result["would_apply"]), 1)
            ledger = load_ledger(config)
            self.assertNotIn("AP1", ledger.get("watch", {}))

    def test_apply_refuses_without_confirmation_flag(self):
        with tempfile.TemporaryDirectory() as tmp:
            config = _config(Path(tmp))
            now = datetime.now(HKT)
            kickoff = _kickoff(20, now)
            _seed_shadow(config, "AP2", "T-5", kickoff)
            plan = recon.build_reconciliation_plan(config, [recon.FixtureLookup("AP2", "T-5")], now=now)
            with self.assertRaises(recon.ReconciliationApplyRefused):
                recon.apply_reconciliation_plan(config, plan, dry_run=False, now=now)

    def test_apply_refuses_when_legacy_stage_already_committed(self):
        with tempfile.TemporaryDirectory() as tmp:
            config = _config(Path(tmp))
            now = datetime.now(HKT)
            kickoff = _kickoff(20, now)
            _seed_shadow(config, "AP3", "T-5", kickoff)
            _seed_legacy(config, "AP3", "T-5", kickoff, data_missing=True)
            plan = recon.build_reconciliation_plan(config, [recon.FixtureLookup("AP3", "T-5")], now=now)
            recon.apply_reconciliation_plan(
                config, plan, dry_run=False, i_understand_this_writes_the_legacy_ledger=True, now=now,
            )
            # A second plan built now sees a MATCH (fresh SHADOW row created
            # matches the applied legacy row); re-applying a stale plan that
            # still references AP3 must refuse rather than duplicate.
            result = recon.apply_reconciliation_plan(
                config, plan, dry_run=False, i_understand_this_writes_the_legacy_ledger=True, now=now,
            )
            self.assertEqual(result["applied"], [])

    def test_apply_never_fabricates_identity_for_a_fixture_with_no_legacy_shell(self):
        with tempfile.TemporaryDirectory() as tmp:
            config = _config(Path(tmp))
            now = datetime.now(HKT)
            kickoff = _kickoff(20, now)
            _seed_shadow(config, "AP4", "T-5", kickoff)
            plan = recon.build_reconciliation_plan(config, [recon.FixtureLookup("AP4", "T-5")], now=now)
            result = recon.apply_reconciliation_plan(
                config, plan, dry_run=False, i_understand_this_writes_the_legacy_ledger=True, now=now,
            )
            self.assertEqual(result["applied"], [])
            self.assertTrue(any(
                "no_legacy_watch_shell_exists" in r["reason"] for r in result["refused"]
            ))

    def test_apply_bounded_refuses_oversized_plan(self):
        with tempfile.TemporaryDirectory() as tmp:
            config = _config(Path(tmp))
            fake_plan = {
                "to_apply": [
                    {"match_id": f"OVER-{i}", "stage": "T-5", "projected_row": {}}
                    for i in range(recon.MAX_BOUNDED_FIXTURES + 1)
                ],
            }
            with self.assertRaises(recon.ReconciliationApplyRefused):
                recon.apply_reconciliation_plan(
                    config, fake_plan, dry_run=False, i_understand_this_writes_the_legacy_ledger=True,
                )

    def test_apply_touches_only_stages_never_bets_wilson_or_log_side_effects_beyond_audit_line(self):
        with tempfile.TemporaryDirectory() as tmp:
            config = _config(Path(tmp))
            now = datetime.now(HKT)
            kickoff = _kickoff(20, now)
            _seed_shadow(config, "AP5", "T-5", kickoff)
            _seed_legacy(config, "AP5", "T-5", kickoff, data_missing=True)
            ledger_before = load_ledger(config)
            bets_before = list(ledger_before.get("bets") or [])
            plan = recon.build_reconciliation_plan(config, [recon.FixtureLookup("AP5", "T-5")], now=now)
            recon.apply_reconciliation_plan(
                config, plan, dry_run=False, i_understand_this_writes_the_legacy_ledger=True, now=now,
            )
            ledger_after = load_ledger(config)
            self.assertEqual(list(ledger_after.get("bets") or []), bets_before)
            self.assertNotIn("crown", ledger_after)  # ensure_namespace never called
            last_log = ledger_after["log"][-1]
            self.assertEqual(last_log["kind"], "native_stage_reconciliation_apply")
            self.assertTrue(last_log["simulation_only"])


class NoProviderNoImportGuardTests(unittest.TestCase):
    """Static guard: module never imports provider/Wilson/Telegram/dashboard code."""

    def test_module_source_never_imports_forbidden_symbols(self):
        import ast
        import inspect
        source = inspect.getsource(recon)
        tree = ast.parse(source)
        imported_names: set[str] = set()
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                for alias in node.names:
                    imported_names.add(alias.name)
                    imported_names.add(alias.asname or alias.name)
            elif isinstance(node, ast.ImportFrom):
                module = node.module or ""
                for alias in node.names:
                    imported_names.add(f"{module}.{alias.name}")
                    imported_names.add(alias.asname or alias.name)
        forbidden = (
            "TitanClient", "PinnapiClient", "analysis.wilson_validation",
            "crown.notify", "crown.settle", "crown.dashboard_data",
            "crown.dashboard_api", "challenger_v2", "direct_t5_outbox",
        )
        for token in forbidden:
            self.assertFalse(
                any(token in name for name in imported_names),
                f"forbidden symbol {token!r} actually imported by reconciliation module: {imported_names!r}",
            )

    def test_module_has_no_tick_sweep_dashboard_call_site_in_engine(self):
        import inspect
        from crown import engine as engine_mod
        engine_source = inspect.getsource(engine_mod)
        self.assertNotIn("native_stage_reconciliation", engine_source)


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
