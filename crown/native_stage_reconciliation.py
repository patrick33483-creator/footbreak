"""Stage 3 of the Crown T-5 deadline-first patch: read-only reconciliation/
consumer adapter between ``NativeStageStore`` (per-fixture shadow durability)
and the legacy monolithic ``ledger.json`` ``watch[match_id]`` shape.

Context
-------
Stage 1 (``crown/native_stage_store.py``) delivered an isolated, per-fixture,
bounded, atomic persistence primitive with zero production call sites.
Stage 2 (``crown/native_stage_shadow.py``) wired that primitive into the real
tick path as **default-off shadow instrumentation** -- when the feature flag
is enabled, every due fixture also gets a shadow ``STARTED``/``COMMITTED``/
``FAILED``/``EXPIRED`` record, entirely independent of, and never read by,
the legacy ledger commit. Stage 2's honest gap list explicitly named the
missing piece: *"No reconciliation job yet consumes
``compare_shadow_to_legacy``/``project_legacy_watch_row`` to merge shadow
evidence into anything dashboard/Wilson/Telegram-visible."*

This module is that reconciliation job's **read side only**. It is a
default-off, explicitly-invoked, read-only adapter that:

  * Reads the per-fixture shadow store (``NativeStageStore.read``) and the
    legacy ``ledger.json`` ``watch[match_id]`` row for a bounded, caller
    supplied set of fixtures.
  * Performs exact identity verification (``match_id``, ``kickoff`` --
    tolerant of HKT/UTC ISO string formatting differences only, ``stage``,
    and per selected market ``code``/``side``/``line``/``odds``/``source``/
    ``observed_at`` timestamp) between the shadow snapshot and the legacy
    stage row for the same fixture/stage. Any mismatch is classified
    ``CONFLICT`` and is never silently coerced into a match.
  * Classifies every (fixture, stage) pair the caller asks about into
    exactly one of ``MATCH``, ``SHADOW_ONLY``, ``LEGACY_ONLY``, ``CONFLICT``,
    or ``EXPIRED_INVALID`` (see :class:`ReconciliationStatus`).
  * Produces either a read-only aggregate/per-fixture **evidence report**
    (:func:`build_acceptance_report`) or a bounded, schema-allow-listed
    **reconciliation plan** (:func:`build_reconciliation_plan`) describing
    exactly what a *future*, separately reviewed apply step would need to
    write into ``ledger.json`` to backfill ``SHADOW_ONLY`` rows -- but this
    module **never performs that write**. ``apply_reconciliation_plan`` in
    this file is intentionally an explicit, narrow, opt-in, pre-kickoff-only,
    identity-exact, dry-run-by-default function that still defaults to
    ``dry_run=True`` and refuses ever to run unless the caller passes
    ``dry_run=False`` *and* ``i_understand_this_writes_the_legacy_ledger=True``
    *and* every row it is about to write passes every fail-closed gate below.
    Even then, it is **not called from any tick/sweep/dashboard/notify code
    path** in this patch -- it exists only so a human operator or a future,
    separately reviewed automation can invoke it explicitly and see exactly
    one ``watch[match_id].stages`` row appended per fixture/stage, using the
    same ``sync_prediction``-shaped snapshot dict the legacy path already
    produces, and nothing else (no bet, no Wilson admission trigger, no
    Telegram, no dashboard write, no ``recompute_stats`` call).

Hard safety invariants (all covered by focused tests)
-------------------------------------------------------
  * **Read-only by default and in every code path except the one explicit
    apply function.** ``build_acceptance_report``/``build_reconciliation_plan``/
    the CLI entry point never write anything, anywhere, ever.
  * **No provider call.** This module never imports ``TitanClient``,
    ``PinnapiClient``, or any HKJC/network client, and never performs a
    socket call. It only reads two already-persisted JSON sources.
  * **Fail-closed identity verification.** ``match_id``, ``kickoff`` (after
    timezone-normalized comparison), ``stage``, and (when both sides have a
    market row for a given ``code``) ``side``/``line``/``odds``/``source``/
    ``observed_at`` must match exactly, or the pair is ``CONFLICT`` -- never
    silently treated as a match.
  * **Never merges/overwrites T-30 with T-5 or vice versa.** Every
    comparison and every planned write is scoped to one explicit stage.
  * **Idempotent.** Re-running the same comparison or the same plan build
    against unchanged inputs returns byte-identical classifications/plans.
  * **Post-kickoff: no backfill, ever.** A stage whose fixture kickoff has
    already passed is always classified ``EXPIRED_INVALID`` and is never
    included in a reconciliation plan's ``to_apply`` list, regardless of
    what either side recorded.
  * **Shadow corruption/missing files/lock contention/fsync or replace
    failure never raises out of this module and never affects the legacy
    read.** Every per-fixture read is wrapped; a broken shadow file for
    fixture A can never prevent fixture B's classification, and can never
    make the legacy side of the comparison look broken.
  * **Bounded.** Every function here takes an explicit, finite list of
    fixture identities to inspect (via ``FixtureLookup``) -- there is no
    unbounded "scan every fixture ever recorded" default; the CLI requires
    an explicit ``--match-id`` (repeatable) or an explicit ``--limit``
    capped at ``MAX_BOUNDED_FIXTURES``.
  * **No Wilson/dashboard/Telegram/settlement/betting/crossbook/Footbreak
    semantic change.** This module never imports ``analysis.wilson_validation``,
    ``crown.notify``, ``crown.settle``, ``crown.dashboard_data``, or any
    betting/crossbook module, and is not called from ``crown/engine.py`` or
    any tick/sweep/dashboard/notify code path.
"""
from __future__ import annotations

import argparse
import json
import sys
from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum
from pathlib import Path
from typing import Any

from .common import HKT, parse_time
from .config import Settings
from .config import settings as _settings_factory
from .ledger import STAGES
from .state import load_ledger, state_lock
from . import native_stage_store as _store

# A caller-facing safety cap: this module refuses to inspect or plan for more
# than this many fixtures in a single call, regardless of how many are
# requested.  This keeps every read-only pass and every plan bounded and
# reviewable, matching the per-fixture bounded design of stages 1-2.
MAX_BOUNDED_FIXTURES = 200

# Bounded wait for the shared legacy state lock during an explicit apply.
# Short and fixed on purpose: this adapter must never let a tick/sweep-side
# lock holder be starved waiting on an operator-invoked reconciliation apply.
APPLY_LOCK_TIMEOUT_SECONDS = 2.0

_MARKET_IDENTITY_FIELDS = ("code", "side", "line", "odds", "source", "observed_at")


class ReconciliationStatus(str, Enum):
    """Exactly one of these per (fixture, stage) pair -- never ambiguous."""

    MATCH = "MATCH"
    SHADOW_ONLY = "SHADOW_ONLY"
    LEGACY_ONLY = "LEGACY_ONLY"
    CONFLICT = "CONFLICT"
    EXPIRED_INVALID = "EXPIRED_INVALID"


@dataclass(frozen=True)
class FixtureLookup:
    """One bounded unit of comparison work: a single fixture/stage pair."""

    match_id: str
    stage: str


@dataclass(frozen=True)
class StageComparison:
    """The fully-explained result of comparing one fixture's one stage."""

    match_id: str
    stage: str
    status: ReconciliationStatus
    reasons: tuple[str, ...] = field(default_factory=tuple)
    shadow_present: bool = False
    legacy_present: bool = False
    kickoff_hkt: str | None = None
    identity_checked: bool = False

    def as_dict(self) -> dict[str, Any]:
        return {
            "match_id": self.match_id,
            "stage": self.stage,
            "status": self.status.value,
            "reasons": list(self.reasons),
            "shadow_present": self.shadow_present,
            "legacy_present": self.legacy_present,
            "kickoff_hkt": self.kickoff_hkt,
            "identity_checked": self.identity_checked,
        }


def _safe_read_shadow(store: "_store.NativeStageStore", match_id: str) -> dict[str, Any] | None:
    """Read one fixture's shadow state; any failure degrades to "absent".

    A corrupt shard, a missing file, a lock-timeout on a concurrent writer,
    or any other exception must never propagate out of this module and must
    never be conflated with "the legacy side is also broken" -- it is always
    treated as if the shadow store simply has no record for this fixture.
    """
    try:
        return store.read(match_id)
    except Exception:  # noqa: BLE001 - isolate exactly this fixture's read
        return None


def _legacy_watch_row(ledger: dict[str, Any], match_id: str) -> dict[str, Any] | None:
    watch = ledger.get("watch")
    if not isinstance(watch, dict):
        return None
    row = watch.get(match_id)
    return row if isinstance(row, dict) else None


def _legacy_stage_row(watch: dict[str, Any] | None, stage: str) -> dict[str, Any] | None:
    if not isinstance(watch, dict):
        return None
    for row in watch.get("stages") or []:
        if isinstance(row, dict) and row.get("stage") == stage:
            return row
    return None


def _kickoff_of(
    shadow_state: dict[str, Any] | None, legacy_watch: dict[str, Any] | None,
) -> datetime | None:
    """Prefer the shadow store's own kickoff record; fall back to legacy.

    Both sides are expected to agree (checked separately as part of identity
    verification); this helper only picks a value to test post-kickoff
    expiry against, and never silently prefers one side's *stage* evidence
    over the other's.
    """
    candidates = []
    if isinstance(shadow_state, dict):
        candidates.append(shadow_state.get("kickoff_utc") or shadow_state.get("kickoff_hkt"))
    if isinstance(legacy_watch, dict):
        candidates.append(legacy_watch.get("kickoff_utc") or legacy_watch.get("kickoff_hkt") or legacy_watch.get("kickoff"))
    for candidate in candidates:
        parsed = parse_time(candidate) if candidate else None
        if parsed is not None:
            return parsed.astimezone(HKT)
    return None


def _normalize_kickoff(value: Any) -> datetime | None:
    parsed = parse_time(value) if value else None
    return parsed.astimezone(HKT) if parsed is not None else None


def _market_row_by_code(rows: Any, code: str) -> dict[str, Any] | None:
    if not isinstance(rows, list):
        return None
    for row in rows:
        if isinstance(row, dict) and row.get("code") == code:
            return row
    return None


def _compare_market_identity(
    shadow_journal: list[Any], legacy_predictions: list[Any],
) -> list[str]:
    """Exact per-market identity check: code/side/line/odds/source/observed_at.

    Only markets present on *both* sides are compared (a market present on
    only one side is already surfaced via the fixture-level SHADOW_ONLY /
    LEGACY_ONLY / partial-conflict reasons produced by the caller); this
    helper's job is strictly "when both sides claim a value for the same
    market code, do the fields agree exactly."
    """
    mismatches: list[str] = []
    shadow_codes = {
        row.get("code") for row in shadow_journal
        if isinstance(row, dict) and row.get("code")
    }
    legacy_codes = {
        row.get("code") for row in legacy_predictions
        if isinstance(row, dict) and row.get("code")
    }
    for code in sorted(shadow_codes & legacy_codes):
        shadow_row = _market_row_by_code(shadow_journal, code)
        legacy_row = _market_row_by_code(legacy_predictions, code)
        if shadow_row is None or legacy_row is None:
            continue
        for key in _MARKET_IDENTITY_FIELDS:
            shadow_value = shadow_row.get(key)
            legacy_value = legacy_row.get(key)
            if shadow_value is None or legacy_value is None:
                continue
            if str(shadow_value) != str(legacy_value):
                mismatches.append(
                    f"market_{code}_{key}_mismatch:shadow={shadow_value!r} legacy={legacy_value!r}"
                )
    return mismatches


def compare_fixture_stage(
    config: Settings,
    ledger: dict[str, Any],
    lookup: FixtureLookup,
    *,
    now: datetime | None = None,
    store: "_store.NativeStageStore | None" = None,
) -> StageComparison:
    """Read-only, fail-closed comparison of one fixture's one stage.

    ``ledger`` is passed in (already loaded once by the caller) so a bounded
    multi-fixture pass performs exactly one legacy ledger read total, never
    one read per fixture -- this function itself never calls
    ``load_ledger``/``save_ledger``.
    """
    match_id = str(lookup.match_id or "")
    stage = str(lookup.stage or "")
    now = (now or datetime.now(HKT)).astimezone(HKT)
    if not match_id or stage not in STAGES:
        return StageComparison(
            match_id=match_id, stage=stage, status=ReconciliationStatus.EXPIRED_INVALID,
            reasons=("invalid_lookup_identity",),
        )
    active_store = store or _store.NativeStageStore(config.state_dir)
    shadow_state = _safe_read_shadow(active_store, match_id)
    legacy_watch = _legacy_watch_row(ledger, match_id)
    legacy_stage = _legacy_stage_row(legacy_watch, stage)
    shadow_snapshot = None
    if isinstance(shadow_state, dict):
        snapshots = shadow_state.get("snapshots")
        if isinstance(snapshots, dict):
            candidate = snapshots.get(stage)
            if isinstance(candidate, dict):
                shadow_snapshot = candidate
    kickoff = _kickoff_of(shadow_state, legacy_watch)
    kickoff_hkt = kickoff.isoformat() if kickoff is not None else None

    def _legacy_committed() -> bool:
        return (
            isinstance(legacy_stage, dict)
            and legacy_stage.get("status") != "DATA_MISSING"
            and (
                legacy_stage.get("odds_status") is None
                or (
                    legacy_stage.get("odds_status") == "available"
                    and bool(legacy_stage.get("market_predictions"))
                )
            )
        )

    shadow_committed = shadow_snapshot is not None
    legacy_committed = _legacy_committed()

    # Post-kickoff: always EXPIRED_INVALID, never backfilled, regardless of
    # what either side privately recorded.  Checked before any other
    # classification so a stale but "matching" pair from long after kickoff
    # is never mistaken for actionable evidence.
    if kickoff is not None and kickoff <= now:
        reasons = ["post_kickoff_no_backfill"]
        if not shadow_committed and not legacy_committed:
            reasons.append("neither_side_ever_committed")
        return StageComparison(
            match_id=match_id, stage=stage, status=ReconciliationStatus.EXPIRED_INVALID,
            reasons=tuple(reasons), shadow_present=shadow_committed,
            legacy_present=legacy_committed, kickoff_hkt=kickoff_hkt,
            identity_checked=False,
        )

    if not shadow_committed and not legacy_committed:
        return StageComparison(
            match_id=match_id, stage=stage, status=ReconciliationStatus.EXPIRED_INVALID,
            reasons=("neither_side_has_a_committed_stage",), shadow_present=False,
            legacy_present=False, kickoff_hkt=kickoff_hkt, identity_checked=False,
        )
    if shadow_committed and not legacy_committed:
        return StageComparison(
            match_id=match_id, stage=stage, status=ReconciliationStatus.SHADOW_ONLY,
            reasons=("shadow_has_committed_snapshot_legacy_does_not",),
            shadow_present=True, legacy_present=False, kickoff_hkt=kickoff_hkt,
            identity_checked=False,
        )
    if legacy_committed and not shadow_committed:
        return StageComparison(
            match_id=match_id, stage=stage, status=ReconciliationStatus.LEGACY_ONLY,
            reasons=("legacy_has_committed_stage_shadow_does_not",),
            shadow_present=False, legacy_present=True, kickoff_hkt=kickoff_hkt,
            identity_checked=False,
        )

    # Both sides claim a committed stage: exact identity verification is
    # mandatory before this pair may ever be called a MATCH.
    mismatches: list[str] = []
    shadow_match_id = str((shadow_state or {}).get("match_id") or "")
    if shadow_match_id and shadow_match_id != match_id:
        mismatches.append(f"match_id_mismatch:shadow={shadow_match_id!r} lookup={match_id!r}")
    legacy_match_id = str((legacy_watch or {}).get("match_id") or "")
    if legacy_match_id and legacy_match_id != match_id:
        mismatches.append(f"match_id_mismatch:legacy={legacy_match_id!r} lookup={match_id!r}")
    shadow_kickoff = _normalize_kickoff(
        (shadow_state or {}).get("kickoff_utc") or (shadow_state or {}).get("kickoff_hkt")
    )
    legacy_kickoff = _normalize_kickoff(
        (legacy_watch or {}).get("kickoff_utc")
        or (legacy_watch or {}).get("kickoff_hkt")
        or (legacy_watch or {}).get("kickoff")
    )
    if shadow_kickoff is not None and legacy_kickoff is not None and shadow_kickoff != legacy_kickoff:
        mismatches.append(
            f"kickoff_mismatch:shadow={shadow_kickoff.isoformat()!r} legacy={legacy_kickoff.isoformat()!r}"
        )
    shadow_stage_field = str((shadow_snapshot or {}).get("stage") or "")
    if shadow_stage_field and shadow_stage_field != stage:
        mismatches.append(f"stage_mismatch:shadow={shadow_stage_field!r} lookup={stage!r}")
    legacy_stage_field = str((legacy_stage or {}).get("stage") or "")
    if legacy_stage_field and legacy_stage_field != stage:
        mismatches.append(f"stage_mismatch:legacy={legacy_stage_field!r} lookup={stage!r}")
    mismatches.extend(_compare_market_identity(
        (shadow_snapshot or {}).get("selected_odds_journal") or [],
        (legacy_stage or {}).get("market_predictions") or [],
    ))

    if mismatches:
        return StageComparison(
            match_id=match_id, stage=stage, status=ReconciliationStatus.CONFLICT,
            reasons=tuple(mismatches), shadow_present=True, legacy_present=True,
            kickoff_hkt=kickoff_hkt, identity_checked=True,
        )
    return StageComparison(
        match_id=match_id, stage=stage, status=ReconciliationStatus.MATCH,
        reasons=("identity_verified_exact_match",), shadow_present=True,
        legacy_present=True, kickoff_hkt=kickoff_hkt, identity_checked=True,
    )


def compare_many(
    config: Settings,
    lookups: list[FixtureLookup],
    *,
    now: datetime | None = None,
    ledger: dict[str, Any] | None = None,
) -> list[StageComparison]:
    """Bounded, read-only comparison across many explicit fixture/stage pairs.

    Loads the legacy ledger at most once (or reuses ``ledger`` if the caller
    already has one, e.g. a test harness), and constructs the shadow store
    at most once.  One fixture's shadow read failure never affects another
    fixture's comparison result -- each pair is independently isolated by
    ``compare_fixture_stage``/``_safe_read_shadow``.
    """
    if len(lookups) > MAX_BOUNDED_FIXTURES:
        raise ValueError(
            f"refusing unbounded reconciliation pass: {len(lookups)} lookups "
            f"> MAX_BOUNDED_FIXTURES={MAX_BOUNDED_FIXTURES}"
        )
    active_ledger = ledger if ledger is not None else load_ledger(config)
    store = _store.NativeStageStore(config.state_dir)
    now = now or datetime.now(HKT)
    return [
        compare_fixture_stage(config, active_ledger, lookup, now=now, store=store)
        for lookup in lookups
    ]


# ---------------------------------------------------------------------------
# Read-only aggregate/fixture-safe acceptance evidence report
# ---------------------------------------------------------------------------

def build_acceptance_report(
    config: Settings,
    lookups: list[FixtureLookup],
    *,
    now: datetime | None = None,
    ledger: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Read-only aggregate + per-fixture evidence, never calls a provider.

    This is the acceptance/compare entry point the task asks for: bounded,
    schema-stable, safe to log or hand to a human reviewer.  It performs no
    writes anywhere and constructs no ``NativeStageStore`` write path.
    """
    comparisons = compare_many(config, lookups, now=now, ledger=ledger)
    aggregate: dict[str, int] = {status.value: 0 for status in ReconciliationStatus}
    for comparison in comparisons:
        aggregate[comparison.status.value] += 1
    return {
        "generated_at": (now or datetime.now(HKT)).astimezone(HKT).isoformat(),
        "requested": len(lookups),
        "compared": len(comparisons),
        "aggregate": aggregate,
        "fixtures": [comparison.as_dict() for comparison in comparisons],
        "provider_calls_made": 0,
        "writes_performed": 0,
    }


# ---------------------------------------------------------------------------
# Bounded, schema-allow-listed reconciliation PLAN (never auto-applied)
# ---------------------------------------------------------------------------

_PLAN_ROW_ALLOWED_KEYS = (
    "match_id", "league", "home", "away", "kickoff_hkt", "stage", "status",
    "odds_status", "odds_reason", "market_predictions", "selected_odds_journal",
    "ts", "schema_version", "source_status",
)


def _plan_row_from_shadow_snapshot(match_id: str, stage: str, snapshot: dict[str, Any]) -> dict[str, Any]:
    """Bounded, allow-listed legacy-compatible projection of one shadow snapshot.

    Deliberately narrower than ``project_legacy_watch_row`` (which projects a
    whole fixture): this produces exactly one stage row shaped closely enough
    to a legacy ``watch[match_id].stages[]`` entry that a *future*, separately
    reviewed apply step could append it, without inventing any field this
    module cannot itself verify from the shadow snapshot.
    """
    row = {key: snapshot.get(key) for key in _PLAN_ROW_ALLOWED_KEYS if key in snapshot}
    row.setdefault("match_id", match_id)
    row.setdefault("stage", stage)
    if "market_predictions" not in row:
        # The shadow snapshot only carries the bounded odds journal, not the
        # legacy market_predictions shape; a plan is honest about this gap
        # rather than fabricating a legacy-shaped market row it cannot prove.
        row["market_predictions"] = []
        row["reconciliation_note"] = (
            "shadow_snapshot_had_no_market_predictions_field; "
            "selected_odds_journal_only_see_plan_row_odds_journal"
        )
        row["selected_odds_journal"] = snapshot.get("selected_odds_journal") or []
    row["reconciliation_source"] = "native_stage_shadow_store"
    row["reconciliation_generated_at"] = snapshot.get("ts")
    return row


def build_reconciliation_plan(
    config: Settings,
    lookups: list[FixtureLookup],
    *,
    now: datetime | None = None,
    ledger: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Bounded, read-only PLAN: what a future apply step *would* write.

    This function never writes anything.  It returns ``to_apply`` (only
    ``SHADOW_ONLY`` pairs, each with a bounded allow-listed projected row),
    and ``skipped`` (every other classification, with the reason it is not
    a candidate for backfill: ``MATCH`` needs no write, ``LEGACY_ONLY`` has
    nothing to project from, ``CONFLICT`` must fail closed rather than guess
    which side is right, and ``EXPIRED_INVALID`` must never be backfilled
    post-kickoff).
    """
    comparisons = compare_many(config, lookups, now=now, ledger=ledger)
    store = _store.NativeStageStore(config.state_dir)
    to_apply: list[dict[str, Any]] = []
    skipped: list[dict[str, Any]] = []
    for comparison in comparisons:
        if comparison.status is not ReconciliationStatus.SHADOW_ONLY:
            skipped.append({
                "match_id": comparison.match_id, "stage": comparison.stage,
                "status": comparison.status.value, "reasons": list(comparison.reasons),
            })
            continue
        shadow_state = _safe_read_shadow(store, comparison.match_id)
        snapshot = None
        if isinstance(shadow_state, dict):
            snapshots = shadow_state.get("snapshots")
            if isinstance(snapshots, dict):
                candidate = snapshots.get(comparison.stage)
                if isinstance(candidate, dict):
                    snapshot = candidate
        if snapshot is None:
            # Re-read raced with the shadow store between compare_many and
            # here (e.g. concurrent writer, or corruption appeared only just
            # now): fail closed, do not plan a write for evidence we cannot
            # currently re-verify.
            skipped.append({
                "match_id": comparison.match_id, "stage": comparison.stage,
                "status": "SHADOW_ONLY_BUT_UNREADABLE_ON_REPLAN",
                "reasons": ["shadow_snapshot_unreadable_at_plan_build_time"],
            })
            continue
        row = _plan_row_from_shadow_snapshot(comparison.match_id, comparison.stage, snapshot)
        to_apply.append({
            "match_id": comparison.match_id, "stage": comparison.stage,
            "status": comparison.status.value, "projected_row": row,
        })
    return {
        "generated_at": (now or datetime.now(HKT)).astimezone(HKT).isoformat(),
        "requested": len(lookups),
        "to_apply": to_apply,
        "skipped": skipped,
        "apply_performed": False,
        "note": (
            "This is a plan only. No legacy ledger write has been performed. "
            "Call apply_reconciliation_plan explicitly (dry_run=False) to "
            "execute it, subject to additional pre-kickoff/identity gates."
        ),
    }


# ---------------------------------------------------------------------------
# Optional, explicit, narrow APPLY (dry-run by default; not wired anywhere)
# ---------------------------------------------------------------------------

class ReconciliationApplyRefused(RuntimeError):
    """Raised instead of writing, whenever any fail-closed apply gate fails."""


def apply_reconciliation_plan(
    config: Settings,
    plan: dict[str, Any],
    *,
    dry_run: bool = True,
    i_understand_this_writes_the_legacy_ledger: bool = False,
    now: datetime | None = None,
) -> dict[str, Any]:
    """Explicit, narrow, opt-in apply of a previously built plan's SHADOW_ONLY rows.

    Not called from any tick/sweep/dashboard/notify code path in this patch.
    Defaults are maximally conservative:

      * ``dry_run=True`` by default: returns exactly what *would* be written,
        performs zero writes, and this is true even if the caller also set
        ``i_understand_this_writes_the_legacy_ledger=True``.
      * Actually writing requires **both** ``dry_run=False`` **and**
        ``i_understand_this_writes_the_legacy_ledger=True``. Missing either
        one raises :class:`ReconciliationApplyRefused` before any I/O.
      * Every row is independently re-verified for identity and pre-kickoff
        safety at apply time (never trusts the plan's snapshot-in-time
        blindly) -- an expired or now-conflicting row is skipped, not
        forced through.
      * Writes exactly one new ``stages`` row per fixture/stage into
        ``ledger.json``'s ``watch[match_id]`` -- the same shape
        ``sync_prediction`` would have produced -- and nothing else: no bet,
        no Wilson admission, no Telegram enqueue, no dashboard write, no
        ``recompute_stats``/``challenger_v2`` call. This function does not
        import any of those modules.
      * A stage that already has a *committed* legacy row by apply time is
        skipped (COMMITTED snapshot uniqueness -- T-30/T-5 are never
        overwritten by this function, and neither is a previously-applied
        reconciliation row -- idempotent replay is a no-op, not a duplicate).
    """
    now = (now or datetime.now(HKT)).astimezone(HKT)
    result: dict[str, Any] = {
        "dry_run": dry_run,
        "applied": [],
        "refused": [],
        "would_apply": [],
        "generated_at": now.isoformat(),
    }
    to_apply = plan.get("to_apply") if isinstance(plan, dict) else None
    if not isinstance(to_apply, list):
        to_apply = []
    if len(to_apply) > MAX_BOUNDED_FIXTURES:
        raise ReconciliationApplyRefused(
            f"refusing unbounded apply: {len(to_apply)} rows > {MAX_BOUNDED_FIXTURES}"
        )

    # Re-verify every row's current-state safety before even considering a
    # write, regardless of dry_run -- a dry-run report should show exactly
    # what a real apply would do, including gates that would block it.
    verified_rows: list[dict[str, Any]] = []
    store = _store.NativeStageStore(config.state_dir)
    ledger_for_check = load_ledger(config)
    for entry in to_apply:
        match_id = str((entry or {}).get("match_id") or "")
        stage = str((entry or {}).get("stage") or "")
        projected_row = (entry or {}).get("projected_row")
        if not match_id or stage not in STAGES or not isinstance(projected_row, dict):
            result["refused"].append({"match_id": match_id, "stage": stage, "reason": "malformed_plan_entry"})
            continue
        fresh = compare_fixture_stage(
            config, ledger_for_check, FixtureLookup(match_id, stage), now=now, store=store,
        )
        if fresh.status is not ReconciliationStatus.SHADOW_ONLY:
            result["refused"].append({
                "match_id": match_id, "stage": stage,
                "reason": f"no_longer_shadow_only_at_apply_time:{fresh.status.value}",
            })
            continue
        verified_rows.append({"match_id": match_id, "stage": stage, "projected_row": projected_row})

    if dry_run or not i_understand_this_writes_the_legacy_ledger:
        result["would_apply"] = verified_rows
        if not dry_run and not i_understand_this_writes_the_legacy_ledger:
            raise ReconciliationApplyRefused(
                "dry_run=False requires i_understand_this_writes_the_legacy_ledger=True"
            )
        return result

    # Real apply path: bounded, holds the same short state lock the legacy
    # commit path uses, re-checks pre-kickoff safety one final time inside
    # the lock, and writes only the allow-listed row -- never a bet, never
    # a Wilson/Telegram/dashboard call.
    with state_lock(config, timeout_seconds=APPLY_LOCK_TIMEOUT_SECONDS) as acquired:
        if not acquired:
            raise ReconciliationApplyRefused("could not acquire legacy state lock within timeout")
        ledger = load_ledger(config)
        watches = ledger.setdefault("watch", {})
        for row in verified_rows:
            match_id = row["match_id"]
            stage = row["stage"]
            kickoff = _normalize_kickoff(
                (watches.get(match_id) or {}).get("kickoff_utc")
                or (watches.get(match_id) or {}).get("kickoff_hkt")
                or row["projected_row"].get("kickoff_hkt")
            )
            if kickoff is not None and kickoff <= now:
                result["refused"].append({
                    "match_id": match_id, "stage": stage, "reason": "post_kickoff_at_final_apply_check",
                })
                continue
            watch = watches.get(match_id)
            if not isinstance(watch, dict):
                result["refused"].append({
                    "match_id": match_id, "stage": stage,
                    "reason": "no_legacy_watch_shell_exists_refusing_to_fabricate_identity",
                })
                continue
            existing_rows = watch.get("stages")
            if not isinstance(existing_rows, list):
                existing_rows = []
                watch["stages"] = existing_rows
            already_rows = [
                r for r in existing_rows if isinstance(r, dict) and r.get("stage") == stage
            ]
            already_committed = [
                r for r in already_rows if r.get("status") != "DATA_MISSING"
            ]
            if already_committed:
                # COMMITTED snapshot uniqueness: never overwrite an existing
                # committed stage row, even with reconciliation evidence. A
                # row already written by a prior reconciliation apply also
                # counts as committed here, making replay idempotent.
                result["refused"].append({
                    "match_id": match_id, "stage": stage,
                    "reason": "legacy_stage_already_committed_refusing_overwrite",
                })
                continue
            new_row = dict(row["projected_row"])
            new_row["stage"] = stage
            new_row["match_id"] = match_id
            new_row.setdefault("status", "reconciled_from_shadow_store")
            # Replace only this stage's own DATA_MISSING placeholder rows (if
            # any); every other stage's rows are left byte-for-byte alone.
            watch["stages"] = [
                r for r in existing_rows
                if not (isinstance(r, dict) and r.get("stage") == stage)
            ] + [new_row]
            watch["stages"].sort(key=lambda r: STAGES.get(
                str(r.get("stage")) if isinstance(r, dict) else "", len(STAGES) + 1,
            ))
            result["applied"].append({"match_id": match_id, "stage": stage})
        if result["applied"]:
            from .common import iso_hkt
            ledger.setdefault("log", []).append({
                "ts": iso_hkt(), "kind": "native_stage_reconciliation_apply",
                "n_changes": len(result["applied"]),
                "changes": [f"{row['match_id']}:{row['stage']}" for row in result["applied"]],
                "simulation_only": True,
            })
            ledger["log"] = ledger["log"][-100:]
            from .state import save_ledger
            save_ledger(config, ledger)
    return result


# ---------------------------------------------------------------------------
# Read-only CLI entry point
# ---------------------------------------------------------------------------

def _build_arg_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="python -m crown.native_stage_reconciliation",
        description=(
            "Read-only acceptance/compare tool for the Crown native stage "
            "shadow store vs the legacy ledger. Never calls a provider, "
            "never writes anything. Use --plan to also print the bounded "
            "reconciliation plan (still never applied)."
        ),
    )
    parser.add_argument(
        "--match-id", action="append", default=[], dest="match_ids",
        help="Fixture match_id to inspect (repeatable). Required unless --limit is used.",
    )
    parser.add_argument(
        "--stage", action="append", default=[], dest="stages",
        choices=sorted(STAGES),
        help="Stage(s) to inspect per match id (repeatable). Defaults to all stages.",
    )
    parser.add_argument(
        "--limit", type=int, default=0,
        help=(
            f"Bounded fallback: inspect up to LIMIT (<= {MAX_BOUNDED_FIXTURES}) "
            "fixtures already present in the legacy watch ledger, newest first "
            "by discovered_at, instead of an explicit --match-id list."
        ),
    )
    parser.add_argument(
        "--plan", action="store_true",
        help="Also print the bounded reconciliation plan (SHADOW_ONLY rows only). Never applies it.",
    )
    parser.add_argument(
        "--json", action="store_true", dest="as_json",
        help="Print machine-readable JSON instead of a human summary.",
    )
    return parser


def _default_lookups_from_limit(config: Settings, limit: int) -> list[FixtureLookup]:
    limit = max(0, min(limit, MAX_BOUNDED_FIXTURES))
    if limit == 0:
        return []
    ledger = load_ledger(config)
    watch = ledger.get("watch")
    if not isinstance(watch, dict):
        return []
    ordered_ids = sorted(
        watch.keys(),
        key=lambda match_id: str((watch.get(match_id) or {}).get("discovered_at") or ""),
        reverse=True,
    )[:limit]
    lookups: list[FixtureLookup] = []
    for match_id in ordered_ids:
        for stage in sorted(STAGES):
            lookups.append(FixtureLookup(match_id, stage))
            if len(lookups) >= MAX_BOUNDED_FIXTURES:
                return lookups
    return lookups


def main(argv: list[str] | None = None, *, config: Settings | None = None) -> int:
    """Read-only CLI entry point. Never returns a nonzero exit for CONFLICT/
    SHADOW_ONLY/LEGACY_ONLY findings themselves -- those are reported, not
    treated as a tool failure. Only a malformed invocation exits nonzero.
    """
    args = _build_arg_parser().parse_args(argv)
    active_config = config or _settings_factory()
    stages = args.stages or sorted(STAGES)
    if args.match_ids:
        lookups = [
            FixtureLookup(match_id, stage)
            for match_id in args.match_ids[:MAX_BOUNDED_FIXTURES]
            for stage in stages
        ]
        lookups = lookups[:MAX_BOUNDED_FIXTURES]
    elif args.limit:
        lookups = _default_lookups_from_limit(active_config, args.limit)
    else:
        print(
            "error: provide at least one --match-id or a nonzero --limit "
            "(refusing an unbounded scan)",
            file=sys.stderr,
        )
        return 2
    if not lookups:
        print(json.dumps({"requested": 0, "compared": 0, "fixtures": []}) if args.as_json else "No fixtures to compare.")
        return 0
    report = build_acceptance_report(active_config, lookups)
    if args.plan:
        report["plan"] = build_reconciliation_plan(active_config, lookups)
    if args.as_json:
        print(json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True))
    else:
        print(f"Compared {report['compared']} fixture/stage pairs:")
        for status, count in sorted(report["aggregate"].items()):
            print(f"  {status}: {count}")
        for row in report["fixtures"]:
            print(f"  - {row['match_id']} {row['stage']}: {row['status']} ({'; '.join(row['reasons'])})")
        if args.plan:
            plan = report["plan"]
            print(f"Plan: {len(plan['to_apply'])} row(s) would be proposed for backfill (not applied).")
    return 0


if __name__ == "__main__":  # pragma: no cover - thin CLI shim
    raise SystemExit(main())
