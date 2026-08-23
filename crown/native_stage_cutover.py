"""Stage 5: default-off per-fixture deadline-durability cutover.

Context — the actual 00:00 HKT bottleneck this stage targets
-------------------------------------------------------------
No genuine 00:00 HKT live-incident artifact exists anywhere in this
workspace at the time this module was written (searched for timestamped
incident/missing-stage reports; none found beyond the pre-existing 21:00
incident already documented in ``native_stage_store.py``'s own docstring).
Per explicit instruction, this module's design is therefore justified by
**code-level root-cause analysis only** -- it does not claim to reproduce or
have measured any specific live incident count, and the report accompanying
this patch says so explicitly.

The code-level bottleneck, confirmed by reading ``crown/engine.py`` and
``crown/state.py`` directly (not from memory):

  * ``crown.state.load_ledger``/``save_ledger`` always read/write the
    **entire** ``ledger.json`` document (every fixture's ``watch`` entry),
    and ``crown.state.merge_predictions`` always reads/writes the **entire**
    ``predictions.json`` document.  Neither cost is bounded by the size of
    one fixture's own record.
  * ``crown.engine._run_local_bulk_timed_stages`` (the native Crown ID=3
    T-30/T-5/首預 due-stage path, reached whenever ``titan_rows`` is
    non-empty for a tick -- see the early-return at the ``if titan_rows:``
    branch in ``crown.engine.run``) collects a whole batch of due fixtures'
    snapshots via ``_collect_locked_direct_snapshots``/
    ``_collect_same_id_bulk_fallback`` and only *then* calls
    ``_commit_stage_predictions`` **once for the whole batch** -- one
    ``state_lock`` + ``load_ledger`` + per-row ``sync_prediction`` +
    conditional ``recompute_stats`` + ``save_ledger`` + ``merge_predictions``
    cycle, holding the global lock for however long that entire loop takes.
  * The separate PinnAPI-bridge tick path (``mode == "tick"`` branch that
    builds ``pending_predictions`` and calls ``_run_tick_predictions`` with
    an ``on_complete`` callback, wired to a ``commit_completed`` closure
    that calls ``_commit_stage_predictions`` with a single-item list per
    completed fixture) was analyzed as part of this module's original
    design as a second, structurally-worse candidate for the same
    deadline-miss failure mode described above.

    **Stage 6 correction (2026-08-24), superseding the paragraph above and
    everything below in this section that assumed the bridge path runs in
    production:** static call-graph tracing plus live call-count
    instrumentation across the full existing test suite established that
    ``crown.engine.run()``'s ``mode == "tick"`` branch **never reaches**
    that ``commit_completed``/``_run_tick_predictions`` call site at all --
    every real tick control-flow path returns earlier (either the
    fast-no-op early return or the native ID=3
    ``_run_local_bulk_timed_stages`` path taken whenever ``titan_rows`` is
    non-empty). Zero calls were observed across the entire pre-existing
    181-test ``test_crown.py`` suite, and
    ``crown/tests/test_native_stage_cutover_callsite.py``
    (``BridgeCallbackUnreachableForTickTests``) now asserts this as a
    permanent control-flow regression test. This means the paragraph above
    is describing dead code: it cannot be "the strongest candidate for a
    same-minute multi-fixture deadline miss" in the running system because
    it does not run in the running system for tick mode. It is retained
    here, unedited apart from this correction notice, purely as a record of
    the (superseded) design reasoning that originally motivated
    ``wrap_completion_callback`` below; that function remains unused by any
    ``crown/engine.py`` call site and must not be treated as active
    durability. See ``CROWN_STAGE6_CRITICAL_FINDING_20260824.md`` for the
    full investigation.
  * ``crown.engine._run_tick_predictions`` also calls
    ``on_complete(value)`` (line ~599) with **no exception isolation** --
    if the callback raises, the whole bounded collection loop for every
    other still-in-flight fixture in that tick is destroyed. This module's
    ``wrap_completion_callback`` fixes that specific gap for a
    cutover-enabled callback supplied to that call site -- but per the
    Stage 6 correction immediately above, that call site is unreachable
    for ``mode == "tick"``, so this fix is dormant/unused code today, not
    an active production mitigation.

What this module changes and does not change
----------------------------------------------
**Stage 5 (original, historical):** this module added a parallel, opt-in
composition layer on top of Stages 1-4's already-tested primitives
(``crown.native_stage_store.NativeStageStore``,
``crown.native_stage_deferred_projection.DeferredProjectionQueue``) but was
not wired into any ``crown/engine.py`` call site -- shadow/parallel only,
exactly like Stages 1-2, with every flag default-off and unreachable from
the live tick.

**Stage 6 (current, 2026-08-24):** ``crown.engine._run_local_bulk_timed_stages``
(the live native ID=3 batch path -- the only tick-mode path confirmed
reachable, per the correction above) now calls this module's
``commit_native_only`` once per fixture, at collection time, via an
``on_result`` callback passed into ``_collect_locked_direct_snapshots``,
strictly before the existing single whole-batch
``_commit_stage_predictions`` legacy call. A bounded
``drain_deferred_projections`` call runs after that legacy call,
independently isolated. Both are no-ops (byte-identical to Stage 5
behaviour) whenever ``CROWN_NATIVE_STAGE_CUTOVER_ENABLED`` is off. The
tick-mode ``commit_completed``/``_run_tick_predictions`` call site was
deliberately **left untouched** in Stage 6, because it is dead code for
``mode == "tick"`` (see the correction above) -- wiring it would not fix
any reachable T-5 miss and was explicitly out of scope per the user's
Stage 6 direction. ``wrap_completion_callback`` below therefore remains
unused by any call site; it is kept only as tested-but-dormant code and
documentation of the (superseded) original two-call-site design. See
``CROWN_T5_NATIVE_STAGE_CUTOVER_RUNBOOK_20260824.md`` for the current,
accurate call-site wiring description.

New flags (all default OFF; composed, never replacing, the Stage 1 flag)
--------------------------------------------------------------------------
  * ``CROWN_NATIVE_STAGE_STORE_ENABLED`` (Stage 1, unchanged) -- must be
    on for anything in this module to construct a real
    ``NativeStageStore``; this module never constructs one when it is off.
  * ``CROWN_NATIVE_STAGE_CUTOVER_ENABLED`` (new) -- gates whether
    ``commit_fixture_result``/``wrap_pinnapi_bridge_commit_completed`` treat
    the native per-fixture commit as sufficient durability *on its own*
    (i.e. enqueue the legacy projection as deferred instead of calling it
    inline). When off, ``commit_fixture_result`` still writes to
    ``NativeStageStore`` (if Stage 1's flag is on) exactly as the Stage 2
    shadow path already does, but always also calls the legacy
    ``project_fn`` inline/synchronously -- i.e. base behaviour.
  * ``CROWN_NATIVE_STAGE_DEFERRED_PROJECTION_ENABLED`` (new) -- gates
    whether the legacy projection is queued into
    ``DeferredProjectionQueue`` for later ``drain()`` rather than called
    inline. This is independent of the cutover flag so a future rollout can
    enable "native commit is authoritative" before separately enabling
    "legacy projection is asynchronous", or vice versa, without one flag
    implying the other.

When either new flag is unset/false, every function in this module that a
caller might wire into the tick path degrades to calling the supplied
``project_fn`` inline, synchronously, in the same order and with the same
argument the existing code already uses today -- i.e. base-equivalent
behaviour, proven by the default-off-equivalence tests in this stage.
"""
from __future__ import annotations

import os
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Callable

from .common import HKT, parse_time
from .config import Settings
from . import native_stage_store as _store
from .native_stage_deferred_projection import DeferredProjectionQueue

ENV_CUTOVER_ENABLED = "CROWN_NATIVE_STAGE_CUTOVER_ENABLED"
ENV_DEFERRED_PROJECTION_ENABLED = "CROWN_NATIVE_STAGE_DEFERRED_PROJECTION_ENABLED"


def cutover_enabled() -> bool:
    return os.getenv(ENV_CUTOVER_ENABLED, "0").strip().lower() in {"1", "true", "yes", "on"}


def deferred_projection_enabled() -> bool:
    return os.getenv(ENV_DEFERRED_PROJECTION_ENABLED, "0").strip().lower() in {"1", "true", "yes", "on"}


def _safe_store(config: Settings) -> "_store.NativeStageStore | None":
    if not _store.is_enabled():
        return None
    try:
        return _store.NativeStageStore(config.state_dir)
    except Exception:
        return None


def _safe_queue(config: Settings) -> DeferredProjectionQueue | None:
    try:
        return DeferredProjectionQueue(config.state_dir)
    except Exception:
        return None


@dataclass(frozen=True)
class FixtureCommitResult:
    match_id: str
    stage: str
    native_state: str | None  # COMMITTED | FAILED | DATA_MISSING | EXPIRED | None (native disabled)
    projection: str  # "inline" | "deferred" | "inline_fallback_after_deferred_error"


def _identity_of(prediction: dict[str, Any]) -> tuple[str, str, datetime | None]:
    match_id = str(prediction.get("match_id") or "")
    stage = str(prediction.get("stage") or "")
    kickoff = parse_time(prediction.get("kickoff_hkt"))
    if kickoff is not None:
        kickoff = kickoff.astimezone(HKT)
    return match_id, stage, kickoff


def commit_fixture_result(
    config: Settings,
    prediction: dict[str, Any],
    project_fn: Callable[[dict[str, Any]], Any],
    *,
    now: datetime | None = None,
) -> FixtureCommitResult:
    """Commit exactly one already-collected stage-prediction result.

    Order of operations (per the design mandate):

      1. If ``CROWN_NATIVE_STAGE_STORE_ENABLED`` is on, commit this
         fixture's own snapshot/failure to ``NativeStageStore`` immediately
         -- this call touches only this fixture's own file and never reads
         or writes ``ledger.json``.  A native-store exception is isolated:
         it can never suppress or block the legacy projection that follows.
      2. If the native commit above did not happen (flag off) or
         ``CROWN_NATIVE_STAGE_CUTOVER_ENABLED`` is off, call ``project_fn``
         inline, synchronously, exactly as the unmodified legacy call site
         does today. This is the base-equivalent path.
      3. Only when *both* the native store and the cutover flag are on does
         this function treat the native commit as sufficient durability on
         its own and skip the inline legacy call -- instead, if
         ``CROWN_NATIVE_STAGE_DEFERRED_PROJECTION_ENABLED`` is also on, it
         enqueues the legacy projection into ``DeferredProjectionQueue``
         for a later bounded ``drain_deferred_projections`` call (never
         blocking this function's return). If the deferred-projection flag
         is off while the cutover flag is on, this function still calls
         ``project_fn`` inline -- the cutover flag alone only concerns
         *native* durability authority, not whether legacy projection is
         synchronous; only the deferred-projection flag controls that.

    A ``project_fn`` exception (inline path) propagates exactly as it does
    in the unmodified legacy call site today -- this function changes
    nothing about that behaviour when cutover/deferred flags are off.
    """
    match_id, stage, kickoff = _identity_of(prediction)
    native_state: str | None = None
    store = _safe_store(config)
    if store is not None and match_id and stage in {"首預", "T-30", "T-5"} and kickoff is not None:
        try:
            native_state = _commit_native(store, match_id, stage, prediction, kickoff, now=now)
        except Exception:
            # A native-store failure must never block or skip the legacy
            # projection that keeps Wilson/dashboard/Telegram/settlement
            # correct. Fall through to the inline/deferred decision below
            # exactly as if the native store were disabled for this call.
            native_state = None

    use_cutover = store is not None and native_state is not None and cutover_enabled()
    if not use_cutover:
        project_fn(prediction)
        return FixtureCommitResult(match_id, stage, native_state, "inline")

    if deferred_projection_enabled():
        queue = _safe_queue(config)
        if queue is not None:
            try:
                queue.enqueue(
                    match_id, stage,
                    kickoff=kickoff if kickoff is not None else datetime.now(HKT),
                    payload=prediction,
                )
                return FixtureCommitResult(match_id, stage, native_state, "deferred")
            except Exception:
                # Queue durability failure: never silently drop the legacy
                # projection. Fail safe by still projecting inline now
                # rather than losing the Wilson/dashboard/Telegram update.
                project_fn(prediction)
                return FixtureCommitResult(
                    match_id, stage, native_state, "inline_fallback_after_deferred_error",
                )
    # Cutover on but deferred-projection flag off: native store is
    # authoritative for durability, legacy projection still runs inline.
    project_fn(prediction)
    return FixtureCommitResult(match_id, stage, native_state, "inline")


def commit_native_only(
    config: Settings,
    prediction: dict[str, Any],
    *,
    now: datetime | None = None,
) -> str | None:
    """Commit exactly one fixture's native durability snapshot, nothing else.

    This performs *only* step 1 of ``commit_fixture_result`` -- the
    immediate per-fixture ``NativeStageStore`` commit -- and makes no
    decision at all about the legacy ledger/Wilson/dashboard/Telegram
    projection. It is intended for call sites (such as the native ID=3
    batch path in ``crown.engine._run_local_bulk_timed_stages``) that must
    keep their own unconditional, once-per-batch legacy commit fully
    intact and only want to move the *native* durability write earlier,
    without touching if/when/how the legacy projection itself runs.

    Returns the resulting native terminal state string (``COMMITTED`` /
    ``FAILED`` / ``DATA_MISSING`` / ``EXPIRED``), or ``None`` if the native
    store is disabled, the prediction's identity is incomplete, or the
    commit raised (isolated internally -- never propagates).
    """
    match_id, stage, kickoff = _identity_of(prediction)
    store = _safe_store(config)
    if store is None or not match_id or stage not in {"首預", "T-30", "T-5"} or kickoff is None:
        return None
    try:
        return _commit_native(store, match_id, stage, prediction, kickoff, now=now)
    except Exception:
        return None


def _commit_native(
    store: "_store.NativeStageStore",
    match_id: str,
    stage: str,
    prediction: dict[str, Any],
    kickoff: datetime,
    *,
    now: datetime | None = None,
) -> str:
    """Perform the immediate per-fixture native commit for one prediction.

    Mirrors ``native_stage_shadow._shadow_snapshot_from_prediction`` /
    ``shadow_commit_stage_predictions`` field mapping exactly, so a fixture
    already exercised in shadow mode commits an identical snapshot shape
    once cutover is enabled -- this stage does not change what gets
    persisted, only when and under what durability authority.
    """
    now = (now or datetime.now(HKT)).astimezone(HKT)
    if kickoff <= now:
        state = store.expire_post_kickoff(match_id, stage, kickoff=kickoff)
        return _latest_state(state, stage, default="EXPIRED")
    if prediction.get("status") == "DATA_MISSING":
        # The legacy prediction already reflects a terminal DATA_MISSING
        # outcome for this tick (the bounded provider collection for this
        # fixture is over, not merely a transient per-attempt error), so
        # this maps to the native store's own non-retryable terminal
        # DATA_MISSING state rather than a retryable FAILED attempt.
        state = store.mark_failed(
            match_id, stage, kickoff=kickoff,
            reason=str(
                (prediction.get("collection_attempt") or {}).get("reason")
                or "native_quote_unavailable"
            ),
            retryable=False,
        )
        return _latest_state(state, stage, default="DATA_MISSING")
    snapshot = _snapshot_from_prediction(prediction)
    state = store.commit_snapshot(match_id, stage, snapshot, kickoff=kickoff)
    return _latest_state(state, stage, default="COMMITTED")


def _latest_state(state: dict[str, Any], stage: str, *, default: str) -> str:
    for record in reversed(state.get("attempt_history") or []):
        if isinstance(record, dict) and record.get("stage") == stage:
            value = record.get("state")
            return str(value) if value else default
    return default


def _snapshot_from_prediction(prediction: dict[str, Any]) -> dict[str, Any]:
    candidates = prediction.get("forecast_candidates") or prediction.get("candidates") or []
    journal = []
    for row in candidates if isinstance(candidates, list) else []:
        if not isinstance(row, dict):
            continue
        journal.append({
            "code": row.get("code"), "line": row.get("line"), "side": row.get("side"),
            "odds": row.get("odds"),
            "odds_status": "available" if row.get("odds") else "missing",
            "source": row.get("source"),
        })
    attempt = prediction.get("collection_attempt") if isinstance(prediction.get("collection_attempt"), dict) else {}
    return {
        "match_id": prediction.get("match_id"),
        "league": prediction.get("league"),
        "home": prediction.get("home"),
        "away": prediction.get("away"),
        "status": prediction.get("status"),
        "odds_status": "available" if journal else "missing",
        "source": attempt.get("source") or "titan007-crown-id-3",
        "quote_source": attempt.get("source") or "titan007-crown-id-3",
        "selected_odds_journal": journal,
        "ts": prediction.get("generated_at"),
    }


def drain_deferred_projections(
    config: Settings,
    project_fn: Callable[[dict[str, Any]], Any],
    *,
    max_items: int | None = None,
) -> list[Any]:
    """Bounded, restart-safe drain of any queued deferred legacy projections.

    Never blocks or is blocked by native commits; safe to call from an
    independent bounded worker/tick separate from the deadline-critical
    collection path. Returns an empty list (no-op, no I/O) when the
    deferred-projection flag is off or the queue cannot be constructed.
    """
    if not deferred_projection_enabled():
        return []
    queue = _safe_queue(config)
    if queue is None:
        return []
    return queue.drain(project_fn, max_items=max_items)


def wrap_completion_callback(
    on_complete: Callable[[Any], None],
) -> Callable[[Any], None]:
    """Isolate exceptions from a per-completion callback.

    ``crown.engine._run_tick_predictions`` calls ``on_complete(value)`` with
    no exception isolation today: a raising callback would destroy the
    bounded collection loop for every other still-in-flight fixture in the
    same tick, if that loop ever ran. This wrapper preserves the exact call
    signature and return contract (``None``) while guaranteeing one
    fixture's callback exception can never propagate to the loop that
    drives every other fixture's result. It changes nothing about
    ``on_complete``'s own logic or side effects when it does not raise.

    Stage 6 correction: ``crown.engine.run()`` never calls
    ``_run_tick_predictions``/``commit_completed`` for ``mode == "tick"``
    (confirmed dead call site -- see the module docstring above and
    ``CROWN_STAGE6_CRITICAL_FINDING_20260824.md``). This function is not
    wired into that call site or any other; it is retained, tested, and
    correct in isolation, but dormant/unused in the running system.
    """

    def _wrapped(value: Any) -> None:
        try:
            on_complete(value)
        except Exception:
            # Isolated: a single fixture's commit-callback failure must
            # never abort collection/commit for any other fixture in the
            # same bounded batch.
            return

    return _wrapped
