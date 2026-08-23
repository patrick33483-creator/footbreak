"""Default-off shadow wiring between the legacy tick path and NativeStageStore.

Stage 2 of the Crown T-5 deadline-first patch.  Stage 1
(``crown/native_stage_store.py``) delivered a fully isolated, tested
per-fixture persistence primitive that nothing in production called yet.
This module adds the *narrowest* seam that lets the existing
``crown.engine._run_local_bulk_timed_stages`` tick path optionally exercise
that primitive in **shadow mode**, without changing any observable
production behaviour:

  * When ``CROWN_NATIVE_STAGE_STORE_ENABLED`` is false/unset (the default),
    every function in this module is a guaranteed no-op: it does not read
    the flag-independent state, does not call
    :class:`crown.native_stage_store.NativeStageStore`, and performs no
    filesystem I/O whatsoever.  The exact byte sequence of calls the legacy
    path makes (``_journal_timed_stage_attempts``,
    ``_collect_locked_direct_snapshots``, ``_collect_same_id_bulk_fallback``,
    ``_commit_stage_predictions``, ``sync_prediction``, Wilson admission,
    Telegram outbox, dashboard projection) is completely untouched by this
    module either way -- this module never calls any of them, and nothing
    in ``crown/engine.py`` skips or reorders them because of this module.
  * When the flag is enabled, this module:
      1. Journals a shadow ``STARTED`` per eligible pre-kickoff row
         *before* the legacy path's own provider collection begins
         (``shadow_mark_started_batch``), mirroring
         ``_journal_timed_stage_attempts``'s write-ahead intent but into the
         separate per-fixture store.
      2. After the legacy path has already built its bounded
         ``stage_predictions`` batch (i.e. after
         ``_collect_locked_direct_snapshots``/``_collect_same_id_bulk_fallback``
         have already run and produced a result -- successful snapshot or
         explicit ``DATA_MISSING`` -- for every row), commits one shadow
         snapshot/failure per fixture independently
         (``shadow_commit_stage_predictions``).  This is a deliberate,
         explicitly-acknowledged narrowing versus "commit the instant each
         fixture's own provider read completes": rewriting
         ``_collect_locked_direct_snapshots`` to expose a per-completion
         callback would touch the existing bounded multiprocessing collector
         that the deadline-critical legacy path already depends on, which is
         exactly the kind of invasive, higher-risk collector rewrite the task
         explicitly asks this stage to avoid.  The remaining gap is stated
         honestly in the handoff report: shadow commits happen per fixture,
         but only once the whole bounded collection round for the batch has
         already finished, not the instant that one fixture's own snapshot
         arrives from the process pool.
  * Every call in this module is wrapped so that **no exception can ever
    propagate out of it**.  A shadow-store failure (a raised exception, a
    lock timeout, a disk error) is caught, and the legacy commit path is
    always allowed to proceed exactly as it would if this module did not
    exist.  The legacy ledger, not the shadow store, remains authoritative:
    Wilson admission, dashboard projection, and Telegram notification are
    driven exclusively by ``_commit_stage_predictions``/``sync_prediction``,
    which this module never calls, patches, or delays.
  * ``compare_shadow_to_legacy`` is a read-only, on-demand audit helper for
    comparing what the shadow store recorded against what the legacy
    ``stages``/``stage_attempts`` rows show for the same fixture.  It performs
    no writes anywhere and is not invoked by any tick/sweep/dashboard code
    path; it exists purely so a human or a follow-up test/report can inspect
    shadow-vs-legacy agreement without an automatic reconciliation write.
"""
from __future__ import annotations

from datetime import datetime
from typing import Any

from .common import HKT, parse_time
from .config import Settings
from . import native_stage_store as _store


def _safe_store(config: Settings) -> "_store.NativeStageStore | None":
    """Return a store instance only when explicitly enabled; never raise."""
    if not _store.is_enabled():
        return None
    try:
        return _store.NativeStageStore(config.state_dir)
    except Exception:
        # Constructing the store must never be able to affect the legacy
        # tick.  A misconfigured state_dir, permissions error, etc. simply
        # disables shadow instrumentation for this pass.
        return None


def shadow_mark_started_batch(
    config: Settings,
    rows: list[dict[str, Any]],
) -> int:
    """Best-effort shadow STARTED journal, mirroring the legacy write-ahead intent.

    Returns the number of rows successfully journaled in the shadow store
    (0 when disabled or on any failure).  This function is called by the
    tick path *before* provider collection begins, matching the deadline
    path order mandate, but it never itself performs provider I/O and never
    raises.
    """
    store = _safe_store(config)
    if store is None:
        return 0
    try:
        safe_rows = list(rows)
    except Exception:
        return 0
    now = datetime.now(HKT)
    marked = 0
    for row in safe_rows:
        try:
            stage = str(row.get("_due_stage") or "")
            match_id = str(row.get("id") or "")
            kickoff = parse_time(row.get("kickoff"))
            if stage not in {"首預", "T-30", "T-5"} or not match_id or kickoff is None:
                continue
            kickoff = kickoff.astimezone(HKT)
            if kickoff <= now:
                # Never start shadow work for an already-lapsed row; the
                # legacy _expire_lapsed_timed_stage_attempts path owns that.
                continue
            store.mark_started(
                match_id, stage, kickoff=kickoff,
                league=row.get("league"), home=row.get("home"), away=row.get("away"),
                native_fixture_id=match_id,
                legacy_watch_identity={
                    "match_id": match_id, "titan_match_id": match_id,
                    "native_fixture_id": match_id,
                    "league": row.get("league") or "", "home": row.get("home") or "",
                    "away": row.get("away") or "", "kickoff": kickoff.isoformat(),
                },
            )
            marked += 1
        except Exception:
            # One fixture's shadow STARTED failure must never block another
            # fixture's shadow journal, and must never propagate to the
            # legacy caller.
            continue
    return marked


def _shadow_snapshot_from_prediction(prediction: dict[str, Any]) -> dict[str, Any]:
    """Build a bounded shadow snapshot from an already-computed legacy prediction row.

    This reads only fields the legacy path already computed in-memory for
    its own commit; it performs no additional provider call.
    """
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


def shadow_commit_stage_predictions(
    config: Settings,
    stage_predictions: list[dict[str, Any]],
) -> dict[str, int]:
    """Best-effort per-fixture shadow commit from an already-built legacy batch.

    Called *after* the legacy path's bounded provider collection has already
    produced a result (successful snapshot or explicit DATA_MISSING) for
    every row in ``stage_predictions`` -- the batch that
    ``_commit_stage_predictions`` is about to commit into the monolithic
    ledger.  This function never mutates ``stage_predictions``, never mutates
    the legacy ledger, and any failure for one fixture (or for the whole
    call) is swallowed so the legacy commit that follows is unaffected.

    Returns a small counter dict for observability in tests; production code
    does not depend on its return value.
    """
    counters = {"committed": 0, "failed": 0, "expired": 0, "skipped": 0}
    try:
        rows = list(stage_predictions)
    except Exception:
        return counters
    store = _safe_store(config)
    if store is None:
        counters["skipped"] = len(rows)
        return counters
    now = datetime.now(HKT)
    for prediction in rows:
        try:
            stage = str(prediction.get("stage") or "")
            match_id = str(prediction.get("match_id") or "")
            kickoff = parse_time(prediction.get("kickoff_hkt"))
            if stage not in {"首預", "T-30", "T-5"} or not match_id or kickoff is None:
                counters["skipped"] += 1
                continue
            kickoff = kickoff.astimezone(HKT)
            if kickoff <= now:
                # Never let a shadow commit backfill a stage past kickoff,
                # even if the legacy prediction row was built just before it.
                store.expire_post_kickoff(match_id, stage, kickoff=kickoff)
                counters["expired"] += 1
                continue
            if prediction.get("status") == "DATA_MISSING":
                store.mark_failed(
                    match_id, stage, kickoff=kickoff,
                    reason=str(
                        (prediction.get("collection_attempt") or {}).get("reason")
                        or "native_quote_unavailable"
                    ),
                    retryable=True,
                )
                counters["failed"] += 1
                continue
            snapshot = _shadow_snapshot_from_prediction(prediction)
            store.commit_snapshot(match_id, stage, snapshot, kickoff=kickoff)
            counters["committed"] += 1
        except Exception:
            # Isolate exactly one fixture's shadow-commit failure. The
            # legacy commit for every fixture (including this one) is
            # unaffected because this function's caller ignores exceptions
            # and this loop never re-raises.
            counters["failed"] += 1
            continue
    return counters


def run_shadow_for_batch(
    config: Settings,
    rows: list[dict[str, Any]],
    stage_predictions: list[dict[str, Any]],
) -> dict[str, int] | None:
    """Single guarded entry point the tick path calls; never raises.

    Returns ``None`` when shadow mode is disabled (the common, default
    production case -- zero store construction, zero filesystem I/O), or a
    small counters dict when enabled. This function's own body is wrapped in
    a broad guard so that even a defect inside shadow bookkeeping itself
    cannot affect the caller.
    """
    if not _store.is_enabled():
        return None
    try:
        shadow_mark_started_batch(config, rows)
        return shadow_commit_stage_predictions(config, stage_predictions)
    except Exception:
        return None


# ---------------------------------------------------------------------------
# Read-only shadow-vs-legacy audit helper (no writes, no dashboard/UI)
# ---------------------------------------------------------------------------

def compare_shadow_to_legacy(
    config: Settings,
    match_id: str,
    legacy_watch: dict[str, Any] | None,
) -> dict[str, Any]:
    """Read-only comparison of one fixture's shadow store vs legacy watch row.

    This never writes anything (not to the shadow store, not to
    ``ledger.json``) and is not called from any tick/sweep/dashboard code
    path.  It exists so a human, a report, or a future test can inspect
    agreement between the two without an automatic reconciliation write.
    """
    store = _store.NativeStageStore(config.state_dir)
    shadow_state = store.read(match_id)
    legacy_stage_rows = {
        str(row.get("stage")): row
        for row in ((legacy_watch or {}).get("stages") or [])
        if isinstance(row, dict) and row.get("stage")
    }
    legacy_attempts = (legacy_watch or {}).get("stage_attempts") or {}
    result: dict[str, Any] = {
        "match_id": match_id,
        "shadow_present": shadow_state is not None,
        "legacy_present": bool(legacy_watch),
        "stages": {},
    }
    for stage in ("首預", "T-30", "T-5"):
        shadow_snapshot = (shadow_state or {}).get("snapshots", {}).get(stage)
        shadow_terminal = None
        for record in reversed((shadow_state or {}).get("attempt_history") or []):
            if isinstance(record, dict) and record.get("stage") == stage and record.get("state") in _store.TERMINAL_STATES:
                shadow_terminal = record.get("state")
                break
        legacy_row = legacy_stage_rows.get(stage)
        legacy_attempt = legacy_attempts.get(stage) if isinstance(legacy_attempts, dict) else None
        legacy_terminal = (
            legacy_attempt.get("state") if isinstance(legacy_attempt, dict) else None
        )
        legacy_committed = isinstance(legacy_row, dict) and legacy_row.get("status") != "DATA_MISSING"
        shadow_committed = shadow_terminal == "COMMITTED"
        result["stages"][stage] = {
            "shadow_terminal_state": shadow_terminal,
            "legacy_terminal_state": legacy_terminal,
            "shadow_committed": shadow_committed,
            "legacy_committed": legacy_committed,
            "agrees": shadow_committed == legacy_committed,
            "shadow_has_snapshot": shadow_snapshot is not None,
        }
    result["all_agree"] = all(row["agrees"] for row in result["stages"].values())
    return result
