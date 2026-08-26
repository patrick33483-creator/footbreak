"""Crown prediction pass with independent forecasting and strict PinnAPI betting gates."""
from __future__ import annotations

import copy
import json
import math
import multiprocessing
import os
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone
from multiprocessing.connection import wait as wait_for_connections
from typing import Any, Callable

from analysis.quarter_line import from_two_sided_market

from .common import HKT, iso_hkt, parse_time
from .config import Settings
from .hkjc import event_from_match, fetch_matches, flatten_odds
from .ledger import (
    PREDICTION_ERA,
    completed_stages,
    due_stage_jobs,
    ensure_stage_jobs,
    market_entry_thresholds,
    reconcile_pending_formal_admissions,
    recompute_stats,
    stage_for,
    stages_due,
    sync_prediction,
)
from .lines import parse_hkjc_total
from .matching import (
    MATCHING_VERSION,
    Event,
    Match,
    BridgeMatch,
    bridge_titan_to_pinnapi,
    same_event_for_hkjc,
)
from .pinnapi import PinnapiClient
from .period import in_current_period, in_future_round_update_window
from .state import (
    load_ledger, load_predictions, merge_predictions, paths, save_ledger, save_predictions, state_lock,
    schedule_footbreak_execution_evidence_projection,
)
from .titan import TitanClient
from . import tick_timing_probe as _timing
from . import native_stage_shadow as _native_shadow
from . import native_stage_cutover as _native_cutover
from .reverse_t5_bridge import enqueue_committed_t5


_FIRST_LOOK_RECONCILE_MAX_FIXTURES = 30
# Crown T-5 deadline-first patch, stage 6: bounds how many queued deferred
# legacy-projection items a single tick's non-critical drain call will
# process. No-op/unused when CROWN_NATIVE_STAGE_DEFERRED_PROJECTION_ENABLED
# is off. Keeps the drain itself bounded even if the queue has backed up.
_NATIVE_DEFERRED_DRAIN_MAX_ITEMS = 50
# Stage 7: hard ceiling on how much of the remaining tick deadline the
# pre-legacy-commit and fast-noop recovery drains may ever consume. Chosen
# to be small relative to `_TIMED_STAGE_COMMIT_RESERVE_SECONDS` (8.0s) so a
# recovery drain can never itself become a new cause of deadline starvation
# for this tick's own urgent due-stage work.
_NATIVE_STAGE7_RECOVERY_DRAIN_RESERVE_SECONDS = 3.0
_FIRST_LOOK_RECONCILE_FIXTURE_SECONDS = 30.0
_FIRST_LOOK_RECONCILE_QUOTE_SECONDS = 15.0


def _event_from_titan(row: dict[str, Any]) -> Event:
    return Event(str(row["id"]), str(row["league"]), str(row["home"]), str(row["away"]), row["kickoff"], {"raw": row})


def _event_from_pinnapi(row: dict[str, Any]) -> Event:
    return Event(str(row["id"]), str(row["league"]), str(row["home"]), str(row["away"]),
                 datetime.fromtimestamp(float(row["kickoff"]), HKT), {"raw": row})


def _native_only_bridge() -> BridgeMatch:
    """Represent a native-Crown pass without invoking counterpart matching."""
    absent = Match(None, False, 0.0, "native_only_reconciliation")
    return BridgeMatch(absent, absent, "native_only", "native_only_reconciliation")


def _tick_rows_from_predictions(
    predictions: list[dict[str, Any]],
    ledger: dict[str, Any],
    now: datetime,
) -> list[dict[str, Any]]:
    """Select only locally known Crown cards whose timed stage is due."""
    rows: list[dict[str, Any]] = []
    seen: set[tuple[str, str]] = set()

    def append_due(identity: dict[str, Any], watch: dict[str, Any]) -> None:
        match_id = str(
            watch.get("native_fixture_id") or watch.get("titan_match_id")
            or identity.get("id") or identity.get("match_id") or ""
        )
        kickoff = parse_time(identity.get("kickoff") or identity.get("kickoff_hkt"))
        if not match_id or kickoff is None:
            return
        done = completed_stages(watch, MATCHING_VERSION, PREDICTION_ERA)
        jobs = due_stage_jobs(watch, now)
        # Existing cards from before the durable-job migration become visible
        # through the old legal windows once, then the write-ahead path
        # reconstructs and persists their job records before collection.
        # A durable job is the deadline authority.  Do not fall back to the
        # legacy wide (20–40 minute) window merely because a future job is not
        # due yet: that would start a 13:00 T-30 at 12:27 and consume its
        # attempt before the persisted 12:30 deadline.
        if not jobs and not isinstance(watch.get("stage_jobs"), dict):
            minutes = (kickoff - now).total_seconds() / 60
            jobs = [stage for stage in stages_due(minutes, False, done) if stage in {"T-30", "T-5"}]
        first_due = stages_due(
            (kickoff - now).total_seconds() / 60, False, done,
        )
        if "首預" in first_due:
            jobs.append("首預")
        for stage in jobs:
            key = (match_id, stage)
            if key in seen:
                continue
            rows.append({
                "id": match_id,
                "league": identity.get("league") or "",
                "home": identity.get("home") or "",
                "away": identity.get("away") or "",
                "kickoff": kickoff,
                "_due_stage": stage,
            })
            seen.add(key)

    for card in predictions:
        match_id = str(card.get("match_id") or "")
        if not match_id:
            continue
        append_due(card, (ledger.get("watch") or {}).get(match_id, {}))
    # The durable watch is authoritative for a scheduled native stage.  A
    # projection/merge delay must not make a known T-30 disappear from the
    # time-critical local queue just because its dashboard card is absent.
    for key, watch in (ledger.get("watch") or {}).items():
        if not isinstance(watch, dict):
            continue
        match_id = str(watch.get("match_id") or key or "")
        kickoff = parse_time(watch.get("kickoff_hkt") or watch.get("kickoff"))
        if not match_id or kickoff is None:
            continue
        append_due({"id": match_id, "kickoff": kickoff,
                    "league": watch.get("league"), "home": watch.get("home"),
                    "away": watch.get("away")}, watch)
    rows = _prioritize_tick_rows(rows)
    return rows


def _prioritize_tick_rows(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Put native deadlines ahead of first-look work without starving T-30."""
    rank = {"T-5": 0, "T-30": 1, "首預": 2}
    ordered = sorted(
        rows,
        key=lambda row: (
            rank.get(str(row.get("_due_stage") or ""), 3),
            row.get("kickoff") or datetime.max.replace(tzinfo=HKT),
        ),
    )
    return _fair_rotate_same_kickoff_cluster(ordered)


# Below this cluster size, sorting alone already gives every fixture a
# worker slot within one or two ticks, so rotation is unnecessary churn.
# At or above it, an oversized same-stage/same-kickoff cluster can exceed a
# single tick's bounded collection budget and sorting alone would always
# strand the same tail fixtures -- exactly the 19-fixture 18:00 HKT pattern.
_FAIR_ROTATION_CLUSTER_THRESHOLD = 5


def _fair_rotate_same_kickoff_cluster(
    rows: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    """Rotate large same-stage/same-kickoff clusters so no tail starves.

    A large same-kickoff batch (for example 19+ fixtures all due at the same
    T-5 minute) can exceed one tick's bounded collection budget.  Sorting
    alone always keeps the same fixtures at the tail of an oversized cluster,
    so they never get a worker slot on any tick before their durable job
    window closes at kickoff.  This keeps every (stage, kickoff) group intact
    and in its prioritized position, but rotates the *order within* a
    same-stage/same-kickoff cluster using the current wall-clock minute, so
    consecutive ticks give a different subset of that cluster the earliest
    worker slots and every fixture in an oversized batch eventually reaches
    the front while kickoff is still in the future.  Small clusters (below
    ``_FAIR_ROTATION_CLUSTER_THRESHOLD``) are left in their sorted order
    unchanged, since they already drain within a tick or two.
    """
    if len(rows) < 2:
        return rows
    clusters: dict[tuple[str, str], list[dict[str, Any]]] = {}
    order: list[tuple[str, str]] = []
    for row in rows:
        stage = str(row.get("_due_stage") or "")
        kickoff = row.get("kickoff")
        due_key = kickoff.isoformat() if hasattr(kickoff, "isoformat") else str(kickoff)
        key = (stage, due_key)
        if key not in clusters:
            clusters[key] = []
            order.append(key)
        clusters[key].append(row)
    minute_index = int(time.time() // 60)
    rotated: list[dict[str, Any]] = []
    for key in order:
        bucket = clusters[key]
        if len(bucket) >= _FAIR_ROTATION_CLUSTER_THRESHOLD:
            offset = minute_index % len(bucket)
            bucket = bucket[offset:] + bucket[:offset]
        rotated.extend(bucket)
    return rotated


def _repair_durable_stage_jobs(config: Settings, now: datetime) -> int:
    """Recreate missing future job metadata without creating a prediction."""
    repaired = 0
    with state_lock(config, timeout_seconds=1.0) as acquired:
        if not acquired:
            return 0
        ledger = load_ledger(config)
        watches = ledger.get("watch")
        if not isinstance(watches, dict):
            return 0
        for watch in watches.values():
            if not isinstance(watch, dict):
                continue
            kickoff = parse_time(
                watch.get("kickoff_utc") or watch.get("kickoff_hkt") or watch.get("kickoff")
            )
            if kickoff is None or kickoff <= now:
                continue
            before = watch.get("stage_jobs")
            before_keys = set(before) if isinstance(before, dict) else set()
            ensure_stage_jobs(watch, kickoff)
            if set((watch.get("stage_jobs") or {})) != before_keys:
                repaired += 1
        if repaired:
            save_ledger(config, ledger)
    return repaired


def _tick_pass_deadline_seconds() -> float:
    """Return the bounded native-stage tick budget.

    The systemd service owns the final stop margin.  Within this budget the
    durable native T-30/T-5 write is authoritative: Telegram, dashboards and
    other consumers must use only whatever time remains after that write.
    """
    try:
        configured = float(os.getenv("CROWN_TICK_PASS_DEADLINE_SECONDS", "30"))
    except ValueError:
        configured = 30.0
    # The service timeout is 55 seconds.  Retain a small stop margin while
    # permitting a same-minute batch of exact-ID snapshots to complete and
    # commit before kickoff.
    return min(50.0, max(1.0, configured))


def _tick_workers() -> int:
    try:
        configured = int(os.getenv("CROWN_TICK_MAX_WORKERS", "8"))
    except ValueError:
        configured = 8
    return min(12, max(1, configured))


def _deadline_remaining(deadline: float) -> float:
    """Return the remaining monotonic pass budget without a negative timeout."""
    return max(0.0, deadline - time.monotonic())


_MIN_DEADLINE_CALL_SECONDS = 0.25
_TIMED_STAGE_COMMIT_RESERVE_SECONDS = 8.0
_TIMED_STAGE_DIRECT_MAX_SECONDS = 8.0
_TIMED_STAGE_LOCK_WAIT_SECONDS = 2.0


def _tick_provider_deadline_seconds() -> float:
    """Give the native provider/commit path the full bounded tick budget.

    A former Telegram reserve shortened a 30-second tick to 22.5 seconds.
    Under a same-kickoff batch this terminated the engine after writing
    STARTED journals but before atomically committing native snapshots.  The
    durable core now takes the whole bounded budget; callers may run transport
    only from genuinely leftover time.
    """
    return _tick_pass_deadline_seconds()


def _tick_hkjc_fetch_process(send: Any) -> None:
    """Keep the shared Footbreak HKJC reader out of Crown's deadline process."""
    try:
        send.send(("ok", fetch_matches()))
    except BaseException as exc:
        send.send(("error", type(exc).__name__))
    finally:
        send.close()


def _fetch_tick_hkjc_matches(deadline: float) -> list[dict[str, Any]]:
    """Fail closed if the legacy HKJC feed cannot finish inside this tick.

    The shared reader does not expose a per-request budget.  Isolating only
    this optional bridge feed preserves Footbreak's scheduling code while
    allowing Crown to terminate a stuck request rather than overrun T-30/T-5.
    """
    remaining = _deadline_remaining(deadline)
    if remaining < _MIN_DEADLINE_CALL_SECONDS or os.name != "posix":
        return []
    context = multiprocessing.get_context("fork")
    receiver, sender = context.Pipe(duplex=False)
    process = context.Process(target=_tick_hkjc_fetch_process, args=(sender,))
    process.start()
    sender.close()
    try:
        if not receiver.poll(remaining):
            return []
        status, value = receiver.recv()
        return value if status == "ok" and isinstance(value, list) else []
    except EOFError:
        return []
    finally:
        receiver.close()
        if process.is_alive():
            process.terminate()
        process.join(timeout=0.03)
        if process.is_alive():
            process.kill()
            process.join(timeout=0.03)


def _reconcile_hkjc_identities(
    config: Settings,
    hkjc_events: list[Event],
    *,
    now: datetime | None = None,
) -> int:
    """Enrich active Crown fixtures with one strict HKJC identity.

    This runs only from a non-deadline sweep after the HKJC board has already
    been fetched.  It never performs provider I/O, changes a native stage,
    evaluates a condition, creates a simulation, or sends a notification.
    Existing identities are immutable and ambiguous/name-weak matches remain
    unresolved.
    """
    if not hkjc_events:
        return 0
    at = (now or datetime.now(HKT)).astimezone(HKT)
    reconciled = 0
    with state_lock(config):
        predictions = load_predictions(config)
        ledger = load_ledger(config)
        watches = ledger.setdefault("watch", {})
        changed_predictions = False
        changed_ledger = False
        for card in predictions:
            if not isinstance(card, dict):
                continue
            match_id = str(card.get("match_id") or card.get("titan_match_id") or "").strip()
            watch = watches.get(match_id)
            if not match_id or not isinstance(watch, dict):
                continue
            native_fixture_id = str(
                watch.get("native_fixture_id") or watch.get("titan_match_id")
                or watch.get("match_id") or ""
            ).strip()
            if native_fixture_id != match_id:
                continue
            card_hkjc_id = str(card.get("hkjc_match_id") or "").strip()
            watch_hkjc_id = str(watch.get("hkjc_match_id") or "").strip()
            if card_hkjc_id and watch_hkjc_id and card_hkjc_id != watch_hkjc_id:
                # Conflicting durable identities are never auto-repaired.
                continue
            existing_hkjc_id = card_hkjc_id or watch_hkjc_id
            if existing_hkjc_id:
                synchronized = False
                if not card_hkjc_id:
                    card["hkjc_match_id"] = existing_hkjc_id
                    changed_predictions = True
                    synchronized = True
                if not watch_hkjc_id:
                    watch["hkjc_match_id"] = existing_hkjc_id
                    changed_ledger = True
                    synchronized = True
                if synchronized:
                    reconciled += 1
                continue
            kickoff = parse_time(watch.get("kickoff_hkt") or watch.get("kickoff"))
            if not match_id or kickoff is None or kickoff <= at:
                continue
            target = Event(
                match_id,
                str(watch.get("league") or ""),
                str(watch.get("home") or ""),
                str(watch.get("away") or ""),
                kickoff,
                None,
            )
            matched = same_event_for_hkjc(target, hkjc_events)
            if matched.event is None or matched.reversed:
                continue
            hkjc_match_id = str(matched.event.id or "").strip()
            if not hkjc_match_id:
                continue
            card["hkjc_match_id"] = hkjc_match_id
            mapping = card.setdefault("mapping", {})
            if isinstance(mapping, dict):
                mapping["titan_to_hkjc_score"] = round(matched.score, 3)
                mapping["titan_to_hkjc_reason"] = None
                mapping["hkjc_identity_source"] = "noncritical_sweep_unique_verified"
                mapping["hkjc_identity_resolved_at"] = iso_hkt(at)
                mapping["orientation"] = "direct_only"
            changed_predictions = True
            watch["hkjc_match_id"] = hkjc_match_id
            watch_mapping = watch.setdefault("mapping", {})
            if isinstance(watch_mapping, dict):
                watch_mapping["titan_to_hkjc_score"] = round(matched.score, 3)
                watch_mapping["titan_to_hkjc_reason"] = None
                watch_mapping["hkjc_identity_source"] = (
                    "noncritical_sweep_unique_verified"
                )
                watch_mapping["hkjc_identity_resolved_at"] = iso_hkt(at)
                watch_mapping["orientation"] = "direct_only"
            changed_ledger = True
            reconciled += 1
        if changed_ledger:
            save_ledger(config, ledger)
        if changed_predictions:
            save_predictions(config, predictions)
    return reconciled


def _prediction_process(send: Any, payload: tuple[Any, ...]) -> None:
    """Run one provider-heavy prediction outside the tick process.

    A socket/library call can ignore cancellation in a Python thread.  A
    separate process is deliberately used here so the parent can terminate an
    overdue page without waiting for an executor's worker shutdown.  This runs
    only on the Linux deployment target, where ``fork`` also avoids sharing a
    request connection between fixtures.
    """
    try:
        send.send(("ok", _prediction(*payload)))
    except BaseException as exc:
        send.send(("error", type(exc).__name__))
    finally:
        send.close()


def _crown_snapshot_process(
    send: Any,
    config: Settings,
    match_id: str,
    max_seconds: float,
) -> None:
    """Read one locked native fixture outside the deadline-owning process."""
    _timing.record(
        "direct_id_fetch_child_start", match_id=match_id,
        extra={"max_seconds": round(max_seconds, 3)},
    )
    try:
        snapshot = TitanClient(config).crown_price_snapshot(
            match_id, max_seconds=max_seconds,
        )
        send.send(("ok", snapshot))
        _timing.record("direct_id_fetch_child_ok", match_id=match_id)
    except BaseException as exc:
        _timing.record(
            "direct_id_fetch_child_error", match_id=match_id,
            extra={"exception_type": type(exc).__name__},
        )
        send.send(("error", type(exc).__name__))
    finally:
        send.close()


def _crown_bulk_snapshots_process(
    send: Any,
    config: Settings,
    max_seconds: float,
) -> None:
    """Read the optional same-ID board fallback in a killable child process."""
    try:
        snapshots = TitanClient(config).crown_bulk_price_snapshots(
            max_seconds=max_seconds,
        )
        send.send(("ok", snapshots))
    except BaseException as exc:
        send.send(("error", type(exc).__name__))
    finally:
        send.close()


def _terminate_child(process: Any, receiver: Any) -> None:
    """Terminate a provider child without waiting beyond the native deadline."""
    _terminate_start = time.monotonic() if _timing.enabled() else None
    try:
        receiver.close()
    finally:
        if process.is_alive():
            process.terminate()
        process.join(timeout=0.05)
        killed = False
        if process.is_alive():
            process.kill()
            process.join(timeout=0.05)
            killed = True
        _timing.record(
            "child_cleanup_complete",
            extra={
                "needed_sigkill": killed,
                "cleanup_seconds": (
                    round(time.monotonic() - _terminate_start, 3)
                    if _terminate_start is not None else None
                ),
            },
        )


def _timed_stage_commit_reserve(remaining: float) -> float:
    """Keep enough absolute time to atomically write an attempt or snapshot."""
    return min(
        _TIMED_STAGE_COMMIT_RESERVE_SECONDS,
        max(_MIN_DEADLINE_CALL_SECONDS, remaining * 0.25),
    )


def _collect_locked_direct_snapshots(
    config: Settings,
    rows: list[dict[str, Any]],
    deadline: float,
    *,
    on_result: Callable[[dict[str, Any], dict[str, Any]], None] | None = None,
) -> tuple[dict[str, dict[str, Any]], int]:
    """Collect exact-ID quotes with killable workers and a hard commit reserve.

    Threads cannot be safely cancelled once a provider socket stalls.  This
    deliberately mirrors the prediction worker process boundary so a stuck
    direct ID page cannot consume the pre-kickoff ledger-commit window.

    ``on_result`` (Crown T-5 deadline-first patch, stage 6; optional,
    default ``None``) -- when supplied, is invoked with ``(row, value)`` at
    the exact moment this collector accepts a usable snapshot for that row,
    strictly before the next ``wait_for_connections`` cycle begins, so a
    caller can commit each fixture natively the instant it is available
    instead of waiting for the whole batch's collection loop to finish.
    This never changes which snapshots are accepted, never changes the
    return value below, never blocks or slows the collection loop for any
    other in-flight fixture, and is fully isolated: any exception raised by
    ``on_result`` is swallowed here so a caller's commit failure can never
    interrupt collection for the rest of the batch. When ``on_result`` is
    ``None`` (the default), this function's behaviour is byte-identical to
    every prior stage.
    """
    if not rows or os.name != "posix":
        return {}, 0
    remaining = _deadline_remaining(deadline)
    reserve = _timed_stage_commit_reserve(remaining)
    direct_window = min(
        _TIMED_STAGE_DIRECT_MAX_SECONDS,
        max(0.0, remaining - reserve),
    )
    _timing.record(
        "direct_snapshots_collection_entered", deadline=deadline,
        extra={
            "row_count": len(rows),
            "direct_window_s": round(direct_window, 3),
            "reserve_s": round(reserve, 3),
            "workers": min(_tick_workers(), len(rows)),
        },
    )
    if direct_window < _MIN_DEADLINE_CALL_SECONDS:
        return {}, 0
    context = multiprocessing.get_context("fork")
    stop_at = time.monotonic() + direct_window
    queued = iter(rows)
    active: dict[Any, tuple[Any, dict[str, Any]]] = {}
    snapshots: dict[str, dict[str, Any]] = {}
    attempted = 0
    exhausted = False
    while active or not exhausted:
        while (
            not exhausted
            and len(active) < min(_tick_workers(), len(rows))
            and time.monotonic() < stop_at
        ):
            try:
                row = next(queued)
            except StopIteration:
                exhausted = True
                break
            match_id = str(row.get("id") or "")
            if not match_id:
                continue
            receiver, sender = context.Pipe(duplex=False)
            child = context.Process(
                target=_crown_snapshot_process,
                args=(sender, config, match_id, min(
                    _TIMED_STAGE_DIRECT_MAX_SECONDS,
                    max(_MIN_DEADLINE_CALL_SECONDS, stop_at - time.monotonic()),
                )),
            )
            child.start()
            sender.close()
            active[receiver] = (child, row)
            attempted += 1
        remaining = stop_at - time.monotonic()
        if remaining <= 0:
            break
        if not active:
            continue
        for receiver in wait_for_connections(list(active), timeout=min(0.10, remaining)):
            child, row = active.pop(receiver)
            try:
                status, value = receiver.recv()
            except EOFError:
                status, value = "error", "worker_exited"
            finally:
                _terminate_child(child, receiver)
            kickoff = parse_time(row.get("kickoff"))
            if (
                status == "ok"
                and isinstance(value, dict)
                and kickoff is not None
                and value.get("quote_source") == _CROWN_ID3_SOURCE
                and _valid_pre_kickoff_bulk_snapshot(value, kickoff)
                and value.get("prices")
            ):
                snapshots[str(row.get("id") or "")] = value
                if on_result is not None:
                    try:
                        on_result(row, value)
                    except Exception:
                        # Isolated: a caller's per-fixture commit failure
                        # must never interrupt collection for any other
                        # still-in-flight fixture in this same batch.
                        pass
        for receiver, (child, _row) in list(active.items()):
            if child.is_alive() or receiver.poll():
                continue
            active.pop(receiver)
            _terminate_child(child, receiver)
    for receiver, (child, _row) in active.items():
        _terminate_child(child, receiver)
    _timing.record(
        "direct_snapshots_collection_returning", deadline=deadline,
        extra={"attempted": attempted, "usable_snapshots": len(snapshots)},
    )
    return snapshots, attempted


def _collect_same_id_bulk_fallback(
    config: Settings,
    deadline: float,
) -> tuple[dict[str, dict[str, Any]], bool]:
    """Use the broad board only when its bounded child cannot steal commit time."""
    if os.name != "posix":
        return {}, False
    remaining = _deadline_remaining(deadline)
    reserve = _timed_stage_commit_reserve(remaining)
    budget = min(
        _TIMED_STAGE_DIRECT_MAX_SECONDS,
        max(0.0, remaining - reserve),
    )
    if budget < _MIN_DEADLINE_CALL_SECONDS:
        return {}, False
    context = multiprocessing.get_context("fork")
    receiver, sender = context.Pipe(duplex=False)
    child = context.Process(
        target=_crown_bulk_snapshots_process,
        args=(sender, config, budget),
    )
    child.start()
    sender.close()
    try:
        if not receiver.poll(budget):
            return {}, True
        status, value = receiver.recv()
        return (value if status == "ok" and isinstance(value, dict) else {}), True
    except EOFError:
        return {}, True
    finally:
        _terminate_child(child, receiver)


def _run_tick_predictions(
    payloads: list[tuple[Any, ...]],
    deadline: float,
    on_complete: Any,
) -> dict[str, int]:
    """Bound concurrent T-5 work and commit each completed result immediately.

    ``Process.terminate`` is essential: ``Future.cancel`` cannot cancel a
    running ThreadPoolExecutor task, and executor context-manager shutdown
    waits for exactly the stuck worker that caused this incident.
    """
    if not payloads:
        return {"completed": 0, "failed": 0, "deferred": 0}
    if os.name != "posix":
        # Crown production is Linux.  Failing closed elsewhere is safer than
        # silently reintroducing an unkillable thread-based deadline breach.
        return {"completed": 0, "failed": 0, "deferred": len(payloads)}

    context = multiprocessing.get_context("fork")
    queued = iter(payloads)
    active: dict[Any, tuple[Any, Any]] = {}
    completed = failed = submitted = 0
    exhausted = False
    while active or not exhausted:
        while (
            not exhausted
            and len(active) < _tick_workers()
            and time.monotonic() < deadline
        ):
            try:
                payload = next(queued)
            except StopIteration:
                exhausted = True
                break
            receiver, sender = context.Pipe(duplex=False)
            process = context.Process(target=_prediction_process, args=(sender, payload))
            process.start()
            sender.close()
            active[receiver] = (process, payload)
            submitted += 1

        remaining = deadline - time.monotonic()
        if remaining <= 0:
            break
        if not active:
            continue
        ready = wait_for_connections(
            list(active), timeout=min(0.10, remaining)
        )
        for receiver in ready:
            process, _payload = active.pop(receiver)
            try:
                status, value = receiver.recv()
            except EOFError:
                status, value = "error", "worker_exited"
            finally:
                receiver.close()
                process.join(timeout=0.03)
            if status == "ok":
                on_complete(value)
                completed += 1
            else:
                failed += 1

        # A worker can die before writing its pipe.  Do not wait for a timeout
        # before releasing its slot for the next kickoff group.
        for receiver, (process, _payload) in list(active.items()):
            if process.is_alive():
                continue
            if receiver.poll():
                continue
            active.pop(receiver)
            receiver.close()
            process.join(timeout=0.03)
            failed += 1

    for receiver, (process, _payload) in active.items():
        if process.is_alive():
            process.terminate()
        process.join(timeout=0.05)
        if process.is_alive():
            process.kill()
            process.join(timeout=0.05)
        receiver.close()
    # Anything never submitted, plus forcibly terminated workers, remains due
    # because no stage was written.  The next minute will collect fresh,
    # correctly-labelled pre-kickoff evidence.
    return {
        "completed": completed,
        "failed": failed,
        "deferred": len(payloads) - completed - failed,
    }


def _sweep_rows_with_due_existing(
    titan_rows: list[dict[str, Any]],
    predictions: list[dict[str, Any]],
    ledger: dict[str, Any],
    now: datetime,
    *,
    window_contains: Any = in_current_period,
) -> list[dict[str, Any]]:
    """Recover stale first-look cards omitted from Titan's current fixture list."""
    rows = list(titan_rows)
    seen = {str(row.get("id") or "") for row in rows}
    for card in predictions:
        match_id = str(card.get("match_id") or "")
        kickoff = parse_time(card.get("kickoff_hkt") or card.get("kickoff"))
        if (
            not match_id
            or match_id in seen
            or kickoff is None
            or kickoff <= now
            or not window_contains(kickoff, now)
        ):
            continue
        done = completed_stages(
            (ledger.get("watch") or {}).get(match_id, {}),
            MATCHING_VERSION,
            PREDICTION_ERA,
        )
        if not stage_for((kickoff - now).total_seconds() / 60, True, done):
            continue
        rows.append({
            "id": match_id,
            "league": card.get("league") or "",
            "home": card.get("home") or "",
            "away": card.get("away") or "",
            "kickoff": kickoff,
        })
        seen.add(match_id)
    rows.sort(key=lambda row: row["kickoff"])
    return rows


def _hourly_first_look_reconciliation_rows(
    titan_rows: list[dict[str, Any]], ledger: dict[str, Any], now: datetime,
) -> list[dict[str, Any]]:
    """Keep only exact native IDs missing a usable first-look in this board.

    This is deliberately fixture-ID-only: it never name-rematches a provider
    row and never creates a record once kickoff has passed.
    """
    rows: list[dict[str, Any]] = []
    watches = ledger.get("watch") if isinstance(ledger.get("watch"), dict) else {}
    for titan in titan_rows:
        event = _event_from_titan(titan)
        if event.kickoff <= now or not in_current_period(event.kickoff, now):
            continue
        watch = watches.get(event.id, {})
        done = completed_stages(watch, MATCHING_VERSION, PREDICTION_ERA)
        if "首預" not in done:
            rows.append(titan)
    return rows


def _record_hourly_first_look_reconciliation_incident(
    config: Settings, reason: str, now: datetime,
) -> None:
    """Append bounded provider-failure evidence without inventing any stage."""
    with state_lock(config, timeout_seconds=1.0) as acquired:
        if not acquired:
            return
        ledger = load_ledger(config)
        incidents = ledger.get("hourly_first_look_reconciliation_incidents")
        if not isinstance(incidents, list):
            incidents = []
        incidents.append({
            "at": now.astimezone(HKT).isoformat(),
            "origin": "hourly_first_look_reconciliation",
            "status": "PROVIDER_UNAVAILABLE",
            "reason": reason,
        })
        ledger["hourly_first_look_reconciliation_incidents"] = incidents[-100:]
        save_ledger(config, ledger)


def _line_key(market: str, line: float | None) -> tuple[str, int | None]:
    return market, None if line is None else round(line * 4)


_CROWN_ID3_SOURCE = "titan007-crown-id-3"
_CROWN_BULK_ID3_SOURCE = "titan007-crown-id-3-bulk-current"
_CACHED_T5_FALLBACK_SOURCE = "cached_t5_exact_pre_kickoff_crown_id_3"
_CACHED_T5_FALLBACK_MAX_AGE_SECONDS = 10 * 60


def _epoch_observed_at(value: Any) -> float | None:
    """Return a finite epoch timestamp without inventing an observation time."""
    try:
        observed = float(value)
    except (TypeError, ValueError):
        parsed = parse_time(str(value or ""))
        return parsed.timestamp() if parsed is not None else None
    if not math.isfinite(observed) or observed <= 0:
        return None
    # The price adapter writes seconds, but old immutable journals can contain
    # milliseconds.  Normalize only an otherwise real timestamp.
    return observed / 1000 if observed >= 10_000_000_000 else observed


def _same_cached_fixture_identity(
    titan: dict[str, Any],
    cached_card: dict[str, Any],
) -> bool:
    """Require the complete, direct fixture identity; never name-match a cache."""
    kickoff = parse_time(titan.get("kickoff"))
    cached_kickoff = parse_time(
        cached_card.get("kickoff_hkt") or cached_card.get("kickoff")
    )
    return bool(
        str(cached_card.get("match_id") or "") == str(titan.get("id") or "")
        and str(cached_card.get("home") or "") == str(titan.get("home") or "")
        and str(cached_card.get("away") or "") == str(titan.get("away") or "")
        and kickoff is not None
        and cached_kickoff is not None
        and cached_kickoff == kickoff
    )


def _cached_t5_crown_snapshot(
    titan: dict[str, Any],
    cached_card: dict[str, Any] | None,
    now: datetime | None = None,
) -> dict[str, Any] | None:
    """Build a narrow T-5 fallback only from saved exact Crown-ID-3 evidence.

    ``book_odds.crown`` by itself is deliberately insufficient: older cards
    have no fixture-bound selected-quote provenance.  A cache is usable only
    when its current journal proves one exact HDC/HIL selected quote, with the
    same real source timestamp and decimal odds, for this exact fixture.  The
    returned prices remain a complete saved board so the ordinary forecast
    code derives its payload rather than fabricating a selected prediction.
    """
    if not isinstance(cached_card, dict) or not _same_cached_fixture_identity(
        titan, cached_card
    ):
        return None
    now = (now or datetime.now(HKT)).astimezone(HKT)
    kickoff = parse_time(titan.get("kickoff"))
    if kickoff is None or now >= kickoff:
        return None
    prices = list(((cached_card.get("book_odds") or {}).get("crown") or []))
    journal = list(cached_card.get("current_selected_odds_journal") or [])
    if not prices or not journal:
        return None
    accepted: list[dict[str, Any]] = []
    for selected in journal:
        if not isinstance(selected, dict):
            continue
        market = str(selected.get("code") or "")
        side = str(selected.get("side") or "")
        allowed_sides = {"HDC": {"H", "A"}, "HIL": {"H", "L"}}
        if (
            market not in allowed_sides
            or side not in allowed_sides[market]
            or selected.get("provider") != "Crown"
            or selected.get("source") not in {
                _CROWN_ID3_SOURCE, _CROWN_BULK_ID3_SOURCE,
            }
        ):
            continue
        try:
            line = float(selected.get("line"))
            odds = float(selected.get("odds"))
        except (TypeError, ValueError):
            continue
        if not (math.isfinite(line) and math.isfinite(odds) and odds > 1):
            continue
        def exact_saved_quote(row: Any) -> bool:
            try:
                return bool(
                    isinstance(row, dict)
                    and row.get("market") == market
                    and row.get("selection") == side
                    and float(row.get("line")) == line
                    and float(row.get("odds")) == odds
                )
            except (TypeError, ValueError):
                return False
        quote = next((row for row in prices if exact_saved_quote(row)), None)
        if quote is None:
            continue
        observed = _epoch_observed_at(quote.get("source_at"))
        journal_observed = _epoch_observed_at(selected.get("observed_at"))
        if (
            observed is None
            or journal_observed is None
            or observed != journal_observed
            or observed >= kickoff.timestamp()
            or observed > now.timestamp()
            or now.timestamp() - observed > _CACHED_T5_FALLBACK_MAX_AGE_SECONDS
            or kickoff.timestamp() - observed > _CACHED_T5_FALLBACK_MAX_AGE_SECONDS
        ):
            continue
        accepted.append({
            "market": market, "line": line, "selection": side, "odds": odds,
            "observed_at": observed,
        })
    if not accepted:
        return None
    # Keep only supported Crown markets.  A selected exact quote is mandatory;
    # unselected opposite sides are retained solely to calculate the normal
    # complete-market forecast and are never used as a substitute selection.
    fallback_prices = [
        dict(row) for row in prices
        if isinstance(row, dict) and row.get("market") in {"HDC", "HIL"}
    ]
    if not fallback_prices:
        return None
    return {
        "prices": fallback_prices,
        "asian_ok": False,
        "total_ok": False,
        "cached_t5_fallback": True,
        "cached_t5_fallback_source": _CACHED_T5_FALLBACK_SOURCE,
        "cached_t5_selected_quotes": accepted,
    }


def _valid_pre_kickoff_bulk_snapshot(
    snapshot: dict[str, Any] | None,
    kickoff: datetime,
) -> bool:
    """Bulk current odds are unusable if any retained observation is in-play."""
    if not snapshot or snapshot.get("quote_source") != _CROWN_BULK_ID3_SOURCE:
        return True
    prices = list(snapshot.get("prices") or [])
    return bool(prices) and all(
        isinstance(row, dict)
        and (observed := _epoch_observed_at(row.get("source_at"))) is not None
        and observed < kickoff.timestamp()
        for row in prices
    )


def _fresh(line: dict[str, Any], config: Settings, now: float) -> tuple[bool, str | None]:
    age = now - float(line.get("source_at") or 0)
    if age < -30 or age > config.source_max_age_seconds:
        return False, f"source_stale_{age:.0f}s"
    return True, None


def _pairs(prices: list[dict[str, Any]], market: str, line: float) -> dict[str, float] | None:
    wanted = [price for price in prices if _line_key(price["market"], price.get("line")) == _line_key(market, line)]
    keys = ("H", "A") if market == "HDC" else ("H", "L")
    result = {key: next((float(price["odds"]) for price in wanted if price["selection"] == key), None) for key in keys}
    return result if all(value and value > 1 for value in result.values()) else None


def _display_quarter_line(line: float, *, signed: bool = True) -> str:
    quarters = round(float(line) * 4)
    if quarters % 2 == 0:
        values = [quarters / 4]
    elif quarters > 0:
        values = [(quarters - 1) / 4, (quarters + 1) / 4]
    else:
        values = [(quarters + 1) / 4, (quarters - 1) / 4]

    def part(value: float) -> str:
        text = f"{value:g}"
        return f"+{text}" if signed and value > 0 else text

    return "/".join(part(value) for value in values)


def _crown_market_forecasts(
    crown_prices: list[dict[str, Any]],
    config: Settings,
    now: float,
    require_fresh: bool = True,
) -> tuple[list[dict[str, Any]], list[str]]:
    """Build forecast-only HDC/HIL views from Crown's complete current market.

    This is deliberately independent of PinnAPI. It supplies a direction for
    the prediction-learning ledger and may support a confidence-only T-5
    simulation when the current Crown quote is complete and fresh.
    """
    output: list[dict[str, Any]] = []
    reasons: list[str] = []
    for market, sides in (("HDC", ("H", "A")), ("HIL", ("H", "L"))):
        lines = sorted({
            float(row["line"])
            for row in crown_prices
            if row.get("market") == market and row.get("line") is not None
        })
        complete: list[tuple[float, dict[str, dict[str, Any]], dict[str, float]]] = []
        for line in lines:
            rows = {
                side: next((
                    row for row in crown_prices
                    if _line_key(str(row.get("market")), row.get("line")) == _line_key(market, line)
                    and row.get("selection") == side
                ), None)
                for side in sides
            }
            if not all(rows.values()):
                reasons.append(f"crown_incomplete_{market}_{line:g}")
                continue
            if require_fresh:
                stale = [
                    reason
                    for row in rows.values()
                    if not _fresh(row, config, now)[0]
                    for reason in [_fresh(row, config, now)[1]]
                ]
                if stale:
                    reasons.extend(f"crown_{reason}" for reason in stale if reason)
                    continue
            try:
                implied = {side: 1 / float(rows[side]["odds"]) for side in sides}
            except (TypeError, ValueError, ZeroDivisionError):
                reasons.append(f"crown_invalid_odds_{market}_{line:g}")
                continue
            if any(float(rows[side]["odds"]) <= 1 for side in sides):
                reasons.append(f"crown_invalid_odds_{market}_{line:g}")
                continue
            denominator = sum(implied.values())
            probabilities = {side: implied[side] / denominator for side in sides}
            complete.append((line, rows, probabilities))
        if not complete:
            reasons.append(f"no_complete_current_crown_{market}")
            continue
        # The most balanced complete line is the market's central/main line.
        line, rows, probabilities = min(
            complete,
            key=lambda item: (abs(item[2][sides[0]] - 0.5), abs(item[0]), item[0]),
        )
        side = max(sides, key=lambda item: probabilities[item])
        side_label = (
            ("主" if side == "H" else "客")
            if market == "HDC"
            else ("大" if side == "H" else "細")
        )
        market_label = "讓球" if market == "HDC" else "入球大細"
        probability = probabilities[side]
        selected_line = -line if market == "HDC" and side == "A" else line
        display_line = _display_quarter_line(selected_line, signed=market == "HDC")
        settlement_profile = (
            from_two_sided_market(
                line=line,
                side=side,
                over_odds=rows["H"]["odds"],
                under_odds=rows["L"]["odds"],
            )
            if market == "HIL" else None
        )
        output.append({
            "market": market_label,
            "code": market,
            "condition": f"{line:g}",
            "line": line,
            "side": side,
            "label": f"皇冠{market_label} {side_label} {display_line}",
            "odds": round(float(rows[side]["odds"]), 3),
            # Keep the exact observed source time with the selected quote.
            # It is carried into the immutable stage journal by ledger.py.
            "observed_at": rows[side].get("source_at"),
            "prob": round(probability, 5),
            "conviction": round(probability * 100, 1),
            "provider": "Crown",
            "source": "titan007-crown-id-3",
            "bookmaker": "Crown",
            "reference": "crown_full_market_no_vig",
            "forecast_only": True,
            **(
                {"quarter_line_settlement": settlement_profile}
                if settlement_profile is not None else {}
            ),
        })
    return output, sorted(set(reasons))


def _candidates(crown_prices: list[dict[str, Any]], pinnapi_prices: list[dict[str, Any]], config: Settings,
                now: float, inferred_timestamp: bool) -> tuple[list[dict[str, Any]], list[str]]:
    reasons: list[str] = []
    if inferred_timestamp and not config.allow_inferred_pinnapi_timestamp:
        return [], ["pinnapi_source_timestamp_missing"]
    candidates = []
    for crown in crown_prices:
        market, line, side = crown["market"], float(crown["line"]), crown["selection"]
        good, reason = _fresh(crown, config, now)
        if not good:
            reasons.append(f"crown_{reason}")
            continue
        reference = _pairs(pinnapi_prices, market, line)
        if not reference:
            reasons.append(f"no_exact_pinnapi_{market}_{line:g}")
            continue
        match_keys = ("H", "A") if market == "HDC" else ("H", "L")
        # The independent probability is made from both exact reference
        # selections.  Retain the newer source timestamp: this is the point
        # at which the complete probability was knowable, not the Crown
        # quote timestamp used as the bet price.
        reference_rows = [
            next((
                price for price in pinnapi_prices
                if _line_key(price["market"], price.get("line")) == _line_key(market, line)
                and price["selection"] == key
            ), None)
            for key in match_keys
        ]
        try:
            reference_observed_at = max(float(row["source_at"]) for row in reference_rows if row is not None)
            if len(reference_rows) != len(match_keys) or not math.isfinite(reference_observed_at):
                reference_observed_at = None
        except (KeyError, TypeError, ValueError):
            reference_observed_at = None
        implied = {key: 1 / float(reference[key]) for key in match_keys}
        den = sum(implied.values())
        probability = implied[side] / den
        odds = float(crown["odds"])
        ev = probability * odds - 1
        kelly = max(0.0, (probability * odds - 1) / (odds - 1))
        # Keep 50 as genuinely neutral instead of flattening every negative
        # edge to the same score.  Positive-edge thresholds are unchanged.
        conviction = max(0.0, min(100.0, 50.0 + ev * 500.0))
        candidates.append({
            "market": market, "code": market, "condition": f"{line:g}", "line": line, "side": side,
            "label": f"{market} {side} {line:g}", "odds": round(odds, 3), "prob": round(probability, 5),
            "ev": round(ev, 5), "kelly_raw": round(kelly, 5), "kelly_used": round(kelly / 3, 5),
            "conviction": round(conviction, 1), "reference": "pinnapi_exact_full_match",
            "provider": "Crown", "source": "titan007-crown-id-3", "bookmaker": "Crown",
            "observed_at": crown.get("source_at"),
            "probability_observed_at": reference_observed_at,
        })
    return sorted(candidates, key=lambda row: (row["ev"], row["conviction"]), reverse=True), sorted(set(reasons))


def _hkjc_chl(match: dict[str, Any] | None) -> list[dict[str, Any]]:
    """Return HKJC CHL rows after the strict bridge, never as Crown prices."""
    if not match:
        return []
    return [{
        "market": "CHL",
        "provider": "HKJC",
        "source": "hkjc_chl",
        "bookmaker": "HKJC",
        **row,
    } for row in flatten_odds(match).get("CHL", [])]


def _hkjc_chl_forecasts(
    hkjc_lines: list[dict[str, Any]],
) -> tuple[list[dict[str, Any]], list[str]]:
    """Build a forecast-only CHL view from a complete current HKJC market.

    This is the corner equivalent of the Crown HDC/HIL no-vig forecast.  It is
    valid for prediction history and learning only: without an independent
    exact-line PinnAPI reference it must never carry EV/Kelly or create a bet.
    """
    complete: list[
        tuple[float, dict[str, Any], dict[str, float]]
    ] = []
    reasons: list[str] = []
    for row in hkjc_lines:
        line = parse_hkjc_total(row.get("condition"))
        if line is None:
            reasons.append(f"invalid_hkjc_chl_line_{row.get('condition')}")
            continue
        odds = row.get("odds") or {}
        try:
            prices = {side: float(odds.get(side)) for side in ("H", "L")}
        except (TypeError, ValueError):
            reasons.append(f"hkjc_incomplete_CHL_{line:g}")
            continue
        if any(price <= 1 for price in prices.values()):
            reasons.append(f"hkjc_invalid_odds_CHL_{line:g}")
            continue
        implied = {side: 1 / prices[side] for side in ("H", "L")}
        denominator = sum(implied.values())
        probabilities = {
            side: implied[side] / denominator for side in ("H", "L")
        }
        complete.append((line, row, probabilities))
    if not complete:
        return [], sorted(set(reasons + ["no_complete_current_hkjc_CHL"]))

    line, row, probabilities = min(
        complete,
        key=lambda item: (
            not bool(item[1].get("main")),
            abs(item[2]["H"] - 0.5),
            abs(item[0]),
            item[0],
        ),
    )
    side = max(("H", "L"), key=lambda item: probabilities[item])
    probability = probabilities[side]
    odds = float((row.get("odds") or {})[side])
    return [{
        "market": "HKJC角球大細",
        "code": "CHL",
        "condition": f"{line:g}",
        "line": line,
        "side": side,
        "label": f"角球大細 {'大' if side == 'H' else '細'} {row.get('condition')}",
        "odds": round(odds, 3),
        "prob": round(probability, 5),
        "conviction": round(probability * 100, 1),
        "provider": "HKJC",
        "source": "hkjc_chl",
        "bookmaker": "HKJC",
        "reference": "hkjc_full_market_no_vig",
        "forecast_only": True,
    }], sorted(set(reasons))


def _hkjc_chl_candidates(
    hkjc_lines: list[dict[str, Any]],
    pinnapi_corner_prices: list[dict[str, Any]],
    config: Settings,
    now: float,
    inferred_timestamp: bool,
) -> tuple[list[dict[str, Any]], list[str]]:
    """Compare HKJC full-match CHL with PinnAPI's exact same corner line.

    Titan's Crown feed has no verified corner quote.  This intentionally builds
    an independent HKJC-priced candidate rather than placing CHL in the Crown
    quote list or assigning it Crown bookmaker provenance.
    """
    if inferred_timestamp and not config.allow_inferred_pinnapi_timestamp:
        return [], ["pinnapi_corner_source_timestamp_missing"]
    candidates: list[dict[str, Any]] = []
    reasons: list[str] = []
    for hkjc in hkjc_lines:
        line = parse_hkjc_total(hkjc.get("condition"))
        if line is None:
            reasons.append(f"invalid_hkjc_chl_line_{hkjc.get('condition')}")
            continue
        # The HKJC query is a current response snapshot; it exposes no quote
        # timestamp, so the response-observed time is retained explicitly.
        quote = dict(hkjc, source_at=float(hkjc.get("source_at") or now))
        good, reason = _fresh(quote, config, now)
        if not good:
            reasons.append(f"hkjc_{reason}")
            continue
        reference_rows = [
            row for row in pinnapi_corner_prices
            if _line_key(str(row.get("market")), row.get("line")) == _line_key("CHL", line)
        ]
        if not reference_rows:
            reasons.append(f"no_exact_pinnapi_CHL_{line:g}")
            continue
        if any(not _fresh(row, config, now)[0] for row in reference_rows):
            reasons.append(f"pinnapi_corner_source_stale_CHL_{line:g}")
            continue
        reference = _pairs(pinnapi_corner_prices, "CHL", line)
        if not reference:
            reasons.append(f"no_complete_pinnapi_CHL_{line:g}")
            continue
        implied = {key: 1 / float(reference[key]) for key in ("H", "L")}
        denominator = sum(implied.values())
        for side in ("H", "L"):
            try:
                odds = float((hkjc.get("odds") or {}).get(side))
            except (TypeError, ValueError):
                continue
            if odds <= 1:
                continue
            probability = implied[side] / denominator
            ev = probability * odds - 1
            kelly = max(0.0, (probability * odds - 1) / (odds - 1))
            conviction = max(0.0, min(100.0, 50.0 + ev * 500.0))
            candidates.append({
                "market": "HKJC角球大細",
                "code": "CHL",
                # Persist the canonical quarter line for Asian settlement;
                # ``label`` retains HKJC's original split-line presentation.
                "condition": f"{line:g}",
                "line": line,
                "side": side,
                "label": f"角球大細 {'大' if side == 'H' else '細'} {hkjc.get('condition')}",
                "odds": round(odds, 3),
                "prob": round(probability, 5),
                "ev": round(ev, 5),
                "kelly_raw": round(kelly, 5),
                "kelly_used": round(kelly / 3, 5),
                "conviction": round(conviction, 1),
                "provider": "HKJC",
                "source": "hkjc_chl",
                "bookmaker": "HKJC",
                "reference": "pinnapi_corner_exact_full_match",
                "reference_provider": "PinnAPI",
            })
    return sorted(candidates, key=lambda row: (row["ev"], row["conviction"]), reverse=True), sorted(set(reasons))


def _wdl_prediction(prices: list[dict[str, Any]]) -> dict[str, Any]:
    """Return a complete no-vig 1X2 view, or no prediction at all."""
    odds = {
        selection: next((
            float(price["odds"]) for price in prices
            if price.get("market") == "1X2" and price.get("selection") == selection
        ), None)
        for selection in ("H", "D", "A")
    }
    if not all(value and value > 1 for value in odds.values()):
        return {
            "outcome": None, "forecast": None, "probability": None,
            "likely_score": None, "prediction_source": None,
        }
    implied = {selection: 1 / float(value) for selection, value in odds.items()}
    total = sum(implied.values())
    probabilities = {selection: implied[selection] / total for selection in ("H", "D", "A")}
    pick = max(probabilities, key=probabilities.get)
    labels = {"H": "主勝", "D": "和局", "A": "客勝"}
    return {
        "outcome": {
            "home": round(probabilities["H"], 6),
            "draw": round(probabilities["D"], 6),
            "away": round(probabilities["A"], 6),
        },
        "forecast": labels[pick],
        "probability": round(probabilities[pick], 6),
        "likely_score": None,
        "prediction_source": "pinnapi_1x2_no_vig",
    }


def _fixture_baseline_prediction(
    forecasts: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    """Return a low-confidence, always-available WDL learning prediction.

    This fallback exists so every Crown fixture produces a scoreable prediction
    record even when one or more quote/reference providers are incomplete.  It
    is deliberately excluded from EV, Kelly and simulated-bet candidates.
    Where Crown has an HDC direction, that direction nudges the league-neutral
    prior; otherwise a conservative home-advantage prior is used.
    """
    handicap = next(
        (row for row in (forecasts or []) if row.get("code") == "HDC"),
        None,
    )
    side = str((handicap or {}).get("side") or "")
    if side == "H":
        probabilities = {"home": 0.44, "draw": 0.29, "away": 0.27}
        forecast, likely_score = "主勝", "1-0"
        source = "crown_hdc_direction_low_confidence_v1"
    elif side == "A":
        probabilities = {"home": 0.28, "draw": 0.29, "away": 0.43}
        forecast, likely_score = "客勝", "0-1"
        source = "crown_hdc_direction_low_confidence_v1"
    else:
        probabilities = {"home": 0.40, "draw": 0.30, "away": 0.30}
        forecast, likely_score = "主勝", "1-0"
        source = "fixture_prior_low_confidence_v1"
    return {
        "probabilities": probabilities,
        "forecast": forecast,
        "probability": max(probabilities.values()),
        "likely_score": likely_score,
        "prediction_source": source,
        "baseline_low_confidence": True,
    }


def _prediction(titan: dict[str, Any], bridge: BridgeMatch, h_match: dict[str, Any] | None,
                stage: str, config: Settings, titan_client: TitanClient, pinnapi_client: PinnapiClient,
                crown_snapshot: dict[str, Any] | None = None,
                previous_crown_prices: list[dict[str, Any]] | None = None,
                entry_policies: dict[str, dict[str, Any]] | None = None,
                cached_t5_card: dict[str, Any] | None = None) -> dict[str, Any]:
    event = _event_from_titan(titan)
    minutes = round((event.kickoff - datetime.now(HKT)).total_seconds() / 60, 1)
    base = {
        "schema_version": "crown-prediction-v2", "matching_version": MATCHING_VERSION,
        "generated_at": iso_hkt(), "match_id": event.id,
        "league": event.league, "home": event.home, "away": event.away, "kickoff_hkt": iso_hkt(event.kickoff),
        # Local observation time for the first persisted card.  It lets a
        # read-only diagnostic distinguish a missing first look from a fixture
        # that was not yet discovered; it is never sourced from a provider.
        "discovered_at": iso_hkt(),
        "mins_to_ko": minutes, "stage": stage, "titan_match_id": event.id,
        "pinnapi_event_id": bridge.event.id if bridge.event else None,
        "hkjc_match_id": str((h_match or {}).get("id") or (h_match or {}).get("frontEndId") or "") or None,
        "market_sources": {
            "HDC": "titan007-crown-id-3",
            "HIL": "titan007-crown-id-3",
            "CHL": "HKJC CHL odds vs PinnAPI CHL exact full-match reference; not Crown odds",
        },
        "mapping": {
            "path": bridge.path, "reason": bridge.reason,
            "titan_to_hkjc_score": round(bridge.hkjc.score, 3),
            "titan_to_hkjc_reason": bridge.hkjc.reason,
            "hkjc_to_pinnapi_score": round(bridge.pinnapi.score, 3),
            "hkjc_to_pinnapi_reason": bridge.pinnapi.reason,
            "orientation": "reversed_identity_only" if bridge.reversed else "direct_only",
        },
        "execution": {"enabled": True, "mode": "simulation", "real_betting_enabled": False,
                      "reason": "Only T-5 can create an idempotent simulated bet; no order client exists."},
        "candidates": [], "forecast_candidates": [], "pick": None, "lead_view": None, "status": "DATA_MISSING",
        "verdict": "無法完整預測", "no_bet_reason": None, "book_odds": {"crown": [], "hkjc_chl": _hkjc_chl(h_match)},
        "outcome": None, "forecast": None, "probability": None, "likely_score": None,
        "prediction_source": None, "sharp_reference_available": False,
        "edge_reference_status": "not_checked", "edge_reference_note": None,
    }
    corner_forecasts, corner_forecast_reasons = _hkjc_chl_forecasts(
        base["book_odds"]["hkjc_chl"]
    )
    base["forecast_candidates"] = corner_forecasts
    if corner_forecast_reasons:
        base["corner_forecast_notes"] = corner_forecast_reasons
    # Crown is the board master.  A direct bulk/page snapshot wins.  Only when
    # it is absent may a verified, exact saved T-5 card prevent a per-fixture
    # page timeout; an unscoped old price list is never enough to skip a call.
    if not _valid_pre_kickoff_bulk_snapshot(crown_snapshot, event.kickoff):
        crown_snapshot = None
    cached_t5_snapshot = (
        _cached_t5_crown_snapshot(titan, cached_t5_card)
        if stage == "T-5" and crown_snapshot is None else None
    )
    quote_snapshot = crown_snapshot or cached_t5_snapshot
    quote_source = str(
        (quote_snapshot or {}).get("quote_source")
        or (quote_snapshot or {}).get("cached_t5_fallback_source")
        or _CROWN_ID3_SOURCE
    )
    cached_t5_fallback = bool(
        (quote_snapshot or {}).get("cached_t5_fallback")
    )
    base["market_sources"]["HDC"] = quote_source
    base["market_sources"]["HIL"] = quote_source
    base["crown_quote_source"] = quote_source
    base["crown_quote_status"] = (
        "cached_t5_fallback"
        if cached_t5_fallback
        else "bulk_current"
        if quote_source == "titan007-crown-id-3-bulk-current"
        else "direct_current"
    )
    # Fetch and preserve its quote before any
    # HKJC/PinnAPI bridge decision so Crown-only fixtures can still be shown,
    # while edge calculation remains fail-closed without PinnAPI.
    crown = (
        list((quote_snapshot or {}).get("prices") or [])
        if quote_snapshot is not None
        else titan_client.crown_prices(event.id)
    )
    used_cached_crown = False
    if not crown and previous_crown_prices:
        # A current empty/error response must not erase an earlier valid
        # pre-match market view.  Reuse it for forecasting and learning only;
        # stale/current-source uncertainty can never unlock edge or a bet.
        crown = list(previous_crown_prices)
        used_cached_crown = True
    base["book_odds"]["crown"] = crown
    base["source_snapshot_at"] = iso_hkt()
    if not crown:
        base.update(_fixture_baseline_prediction())
        base["status"] = "PREDICTION_READY"
        base["verdict"] = "已預測"
        base["conviction"] = max(
            [round(float(base["probability"]) * 100, 1)]
            + [float(row["conviction"]) for row in corner_forecasts]
        )
        base["edge_reference_status"] = "unavailable"
        base["edge_reference_note"] = "皇冠即時盤及 PinnAPI EV 參考暫不可用；只保留低信念純預測。"
        base["no_bet_reason"] = None
        return base
    now = time.time()
    forecasts, forecast_reasons = _crown_market_forecasts(
        crown,
        config,
        now,
        # A sweep snapshot was fetched during this same bounded board pass.
        # Large fixture batches can take more than the normal live-freshness
        # window to reach _prediction(), but the retained source_at remains
        # the real pre-kickoff observation time.  Do not discard that valid
        # 首預 evidence merely because it waited in the local work queue.
        # Tick-mode direct reads still enforce the normal freshness gate.
        require_fresh=not (used_cached_crown or quote_snapshot is not None),
    )
    if cached_t5_fallback:
        # The cache proves only the saved selected quote(s), not every other
        # line retained on the board.  Refuse a recomputed direction unless it
        # is exactly the market/line/side/odds evidence that passed validation.
        accepted = {
            (
                row["market"], float(row["line"]), row["selection"],
                float(row["odds"]), float(row["observed_at"]),
            )
            for row in (quote_snapshot or {}).get("cached_t5_selected_quotes", [])
            if isinstance(row, dict)
        }
        forecasts = [
            forecast for forecast in forecasts
            if (
                forecast.get("code"),
                float(forecast.get("line")),
                forecast.get("side"),
                float(forecast.get("odds")),
                _epoch_observed_at(forecast.get("observed_at")),
            ) in accepted
        ]
    for forecast in forecasts:
        forecast["source"] = quote_source
        if cached_t5_fallback:
            forecast["quote_status"] = "cached_t5_fallback"
            forecast["quote_fallback_source"] = _CACHED_T5_FALLBACK_SOURCE
    all_forecasts = forecasts + corner_forecasts
    base["forecast_candidates"] = all_forecasts
    base["crown_quote_cached_forecast_only"] = used_cached_crown
    base["crown_cached_t5_fallback"] = cached_t5_fallback
    if used_cached_crown:
        source_times = [
            float(row.get("source_at") or 0)
            for row in crown
            if float(row.get("source_at") or 0) > 0
        ]
        base["crown_cached_source_at"] = min(source_times) if source_times else None
    if all_forecasts:
        base["status"] = "PREDICTION_READY"
        base["verdict"] = "已預測"
        base["conviction"] = max(
            float(row["conviction"]) for row in all_forecasts
        )
        base["prediction_source"] = (
            "crown_and_hkjc_full_market_no_vig"
            if forecasts and corner_forecasts
            else (
                "crown_full_market_no_vig"
                if forecasts
                else "hkjc_full_market_no_vig"
            )
        )
    base.update(_fixture_baseline_prediction(forecasts))
    base["status"] = "PREDICTION_READY"
    base["verdict"] = "已預測"
    base["conviction"] = max(
        float(base.get("conviction") or 0),
        round(float(base["probability"]) * 100, 1),
    )
    if used_cached_crown:
        base["no_bet_reason"] = (
            "皇冠即時盤目前不可用；已沿用最後一次有效皇冠盤作純預測及學習，"
            "禁止計算 edge 及投注。"
        )
        return base
    if stage == "T-5" and (
        quote_source == _CROWN_BULK_ID3_SOURCE or cached_t5_fallback
    ):
        # T-5 stage persistence is time-critical.  A valid exact current bulk
        # Crown board is enough for the granular-condition portfolio; optional
        # per-fixture PinnAPI EV/corner calls can otherwise consume the entire
        # same-kickoff batch deadline.  Do not invent EV, Kelly, or a sharp
        # probability: defer those fields and retain only the real Crown
        # complete-market forecast/selected quote evidence.
        base["edge_reference_status"] = "deferred_t5_stage_priority"
        base["edge_reference_note"] = (
            "T-5 已以皇冠精確盤優先落盤；PinnAPI EV 參考延後，未計算 EV 或 Kelly。"
        )
        base["sharp_reference_available"] = False
        base["no_bet_reason"] = None
        return base
    if not bridge.event:
        # PinnAPI is an optional EV reference, never a prerequisite for Crown
        # forecasting. Keep mapping diagnostics out of the main prediction /
        # no-bet verdict so the UI cannot mislabel a valid Crown forecast as
        # "unable to predict".
        base["edge_reference_status"] = "unavailable"
        base["edge_reference_note"] = (
            "PinnAPI 暫無安全唯一同場參考；不計算 EV。"
            f" 映射診斷：{bridge.reason or 'unknown'}。"
        )
        if forecasts:
            base["no_bet_reason"] = None
        else:
            base["edge_reference_note"] = (
                "皇冠盤未形成完整雙邊市場，已保留低信念賽果 baseline；"
                "PinnAPI EV 參考暫不可用。"
            )
            base["no_bet_reason"] = None
        return base
    try:
        pinnapi = pinnapi_client.lines(bridge.event.id)
    except Exception as exc:
        # Deliberately do not include provider responses, URLs, or credentials.
        base["edge_reference_status"] = "unavailable"
        base["edge_reference_note"] = (
            f"PinnAPI 參考暫時不可用 ({type(exc).__name__})；不計算 EV。"
        )
        if forecasts:
            base["no_bet_reason"] = None
        else:
            base["edge_reference_note"] = (
                "皇冠盤未形成完整雙邊市場，已保留低信念賽果 baseline；"
                f"PinnAPI 參考暫時不可用 ({type(exc).__name__})。"
            )
            base["no_bet_reason"] = None
        return base
    prices = pinnapi["prices"]
    base.update(_wdl_prediction(prices))
    base["sharp_reference_available"] = True
    base["edge_reference_status"] = "available"
    base["edge_reference_note"] = None
    candidates, reasons = _candidates(crown, prices, config, now, bool(pinnapi["timestamp_inferred"]))
    for candidate in candidates:
        candidate["source"] = quote_source
        if cached_t5_fallback:
            candidate["quote_status"] = "cached_t5_fallback"
            candidate["quote_fallback_source"] = _CACHED_T5_FALLBACK_SOURCE
    corner_candidates: list[dict[str, Any]] = []
    corner_reasons: list[str] = []
    if base["book_odds"]["hkjc_chl"]:
        try:
            corners = pinnapi_client.corner_lines(bridge.event.id)
            base["pinnapi_corner_event_id"] = corners.get("corner_event_id")
            base["pinnapi_corner_source_at"] = corners.get("source_at")
            base["pinnapi_corner_timestamp_inferred"] = corners.get("timestamp_inferred")
            corner_candidates, corner_reasons = _hkjc_chl_candidates(
                base["book_odds"]["hkjc_chl"],
                list(corners.get("prices") or []),
                config,
                now,
                bool(corners.get("timestamp_inferred")),
            )
        except Exception as exc:
            # CHL is a separate HKJC candidate.  A special-market outage must
            # fail it closed without changing Crown HDC/HIL decisions.
            corner_reasons = [f"pinnapi_corner_lines_unavailable_{type(exc).__name__}"]
    if corner_reasons:
        # Keep CHL's independently fail-closed state visible even where a
        # normal Crown HDC/HIL candidate remains available.
        base["corner_no_bet_reason"] = "；".join(corner_reasons)
    base["pinnapi_source_at"] = pinnapi["source_at"]
    base["pinnapi_timestamp_inferred"] = pinnapi["timestamp_inferred"]
    base["pinnapi_timestamp_basis"] = pinnapi.get("timestamp_basis")
    candidates = sorted(candidates + corner_candidates,
                        key=lambda row: (row["ev"], row["conviction"]), reverse=True)
    policies = entry_policies or {
        code: {
            "code": code,
            "n_settled": 0,
            "min_samples": 30,
            "min_edge": config.min_edge,
            "confidence_floor": config.confidence_floor,
            "reason": "insufficient_market_sample",
        }
        for code in ("HDC", "HIL", "CHL")
    }
    for candidate in candidates:
        candidate["entry_policy"] = policies.get(
            str(candidate.get("code") or ""),
            {
                "min_edge": config.min_edge,
                "confidence_floor": config.confidence_floor,
                "reason": "configured_default",
            },
        )
    base["entry_policies"] = policies
    base["candidates"] = candidates
    if candidates:
        # Exact PinnAPI same-line views supersede same-market no-vig forecasts
        # for learning quality.  Keep forecast-only markets that have no exact
        # reference instead of dropping them from prediction history.
        exact_codes = {str(row.get("code") or "") for row in candidates}
        base["forecast_candidates"] = candidates + [
            row for row in all_forecasts
            if str(row.get("code") or "") not in exact_codes
        ]
    base["lead_view"] = candidates[0] if candidates else None
    if not candidates:
        prefix = "已保留皇冠全盤預測；" if forecasts else ""
        base["no_bet_reason"] = prefix + "；".join(
            reasons + corner_reasons or ["Crown/PinnAPI 無可比較完整雙邊盤"]
        )
        return base
    eligible = [
        candidate for candidate in candidates
        if float(candidate["conviction"]) >= float(candidate["entry_policy"]["confidence_floor"])
        and float(candidate["ev"]) >= float(candidate["entry_policy"]["min_edge"])
    ]
    lead = eligible[0] if eligible else candidates[0]
    base["conviction"] = lead["conviction"]
    base["lead_view"] = lead
    base["status"] = "REFERENCE_READY"
    base["verdict"] = "傾向" if stage != "T-5" else "觀望"
    base["pick"] = None
    if stage == "T-5":
        base["no_bet_reason"] = "此預測只供資訊；條件模擬倉只會以歷史已結算細緻條件判定。"
    else:
        base["no_bet_reason"] = f"{stage} 僅記錄資訊；條件模擬倉只在新 T-5 判定。"
    return base


def _refresh_crown_quote(
    previous: dict[str, Any],
    titan: dict[str, Any],
    titan_client: TitanClient,
    crown_snapshot: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Refresh board identity and Crown prices without replaying a prediction stage.

    The 30-minute board pass is intentionally separate from 首預/T-30/T-5.
    Existing stage decisions, candidates and simulated picks remain historical
    snapshots.  A successfully fetched market replaces its old quote, including
    a confirmed empty market.  A failed market fetch retains its prior quote and
    is explicitly marked stale, preventing transient source failures from
    deleting otherwise valid Crown fixtures from the board.
    """
    event = _event_from_titan(titan)
    refreshed = dict(previous)
    # Internal merge instruction: a concurrent sweep may finish after a newer
    # T-30/T-5 tick.  It may refresh quote fields, never roll the card's stage
    # or decision backwards.
    refreshed["_quote_refresh_only"] = True
    refreshed.update({
        "league": event.league,
        "home": event.home,
        "away": event.away,
        "kickoff_hkt": iso_hkt(event.kickoff),
        "mins_to_ko": round((event.kickoff - datetime.now(HKT)).total_seconds() / 60, 1),
        "generated_at": iso_hkt(),
        "source_snapshot_at": iso_hkt(),
        "crown_quote_attempted_at": iso_hkt(),
    })
    book_odds = dict(refreshed.get("book_odds") or {})
    snapshot = crown_snapshot or titan_client.crown_price_snapshot(event.id)
    incoming = list(snapshot.get("prices") or [])
    prior = list(book_odds.get("crown") or [])
    merged_prices: list[dict[str, Any]] = []
    stale_markets: list[str] = []
    for market, status_key in (("HDC", "asian_ok"), ("HIL", "total_ok")):
        if snapshot.get(status_key):
            merged_prices.extend(row for row in incoming if row.get("market") == market)
        else:
            merged_prices.extend(row for row in prior if row.get("market") == market)
            stale_markets.append(market)
    book_odds["crown"] = merged_prices
    refreshed["book_odds"] = book_odds
    # A refresh is not a stage replay.  Keep a separate, visibly current
    # selected-quote journal for the dashboard rather than mutating the
    # immutable stage's market_predictions/learning payload.
    current_selected = []
    observed_board_at = refreshed["crown_quote_attempted_at"]
    selected_views = (
        refreshed.get("forecast_candidates")
        or refreshed.get("market_predictions")
        or []
    )
    for selected in selected_views:
        if not isinstance(selected, dict):
            continue
        code, side = selected.get("code"), selected.get("side")
        try:
            line = float(selected.get("line", selected.get("condition")))
        except (TypeError, ValueError):
            continue
        quote = next((
            row for row in merged_prices
            if row.get("market") == code
            and row.get("selection") == side
            and _line_key(str(code), row.get("line")) == _line_key(str(code), line)
        ), None)
        odds = quote.get("odds") if quote else None
        try:
            valid = float(odds) > 1
        except (TypeError, ValueError):
            valid = False
        current_selected.append({
            "code": code, "line": selected.get("line", selected.get("condition")),
            "side": side, "odds": odds if valid else None,
            "odds_status": "available" if valid else "missing",
            "reason": None if valid else "current_exact_quote_unavailable",
            "source": "titan007-crown-id-3", "provider": "Crown",
            # Titan may expose a provider price timestamp.  Separately retain
            # the exact board observation time of this refresh; neither value
            # is inferred from an older prediction-stage generation time.
            "observed_at": quote.get("source_at") if quote else observed_board_at,
            "observed_board_at": observed_board_at,
        })
    refreshed["current_selected_odds_journal"] = current_selected
    refreshed["current_odds_status"] = "available" if current_selected and all(
        item["odds_status"] == "available" for item in current_selected
    ) else "missing"
    refreshed["current_odds_reason"] = (
        None if refreshed["current_odds_status"] == "available"
        else "no_current_selected_quote"
    )
    refreshed["current_odds_refreshed_at"] = observed_board_at
    refreshed["current_odds_refresh_source"] = "titan007-crown-id-3"
    refreshed["crown_quote_stale_markets"] = stale_markets
    if not stale_markets:
        refreshed["crown_quote_refreshed_at"] = iso_hkt()
    if not book_odds["crown"]:
        refreshed["no_bet_reason"] = "皇冠公司盤口目前不可用；不顯示為皇冠有效賽事。"
    return refreshed


def _skip_new_confirmed_empty_crown(
    crown_snapshot: dict[str, Any] | None,
    previous: dict[str, Any] | None,
) -> bool:
    """Skip only a brand-new fixture whose two Crown markets are confirmed empty."""
    return bool(
        previous is None
        and crown_snapshot is not None
        and crown_snapshot.get("asian_ok")
        and crown_snapshot.get("total_ok")
        and not crown_snapshot.get("prices")
    )


def refresh_current_quotes(config: Settings) -> dict[str, Any]:
    """Refresh dashboard-only quote fields for known, not-yet-started cards.

    This command deliberately does not enter the matching, forecasting,
    ledger, learning, settlement, bet, or notification paths.  In particular,
    it cannot retrofit a current price into a completed historical stage.
    """
    now = datetime.now(HKT)
    current = load_predictions(config)
    titan = TitanClient(config)
    updates: list[dict[str, Any]] = []
    for card in current:
        kickoff = parse_time(card.get("kickoff_hkt") or card.get("kickoff"))
        match_id = str(card.get("titan_match_id") or card.get("match_id") or "")
        if not match_id or kickoff is None or kickoff <= now:
            continue
        row = {
            "id": match_id, "league": card.get("league") or "",
            "home": card.get("home") or "", "away": card.get("away") or "",
            "kickoff": kickoff,
        }
        try:
            snapshot = titan.crown_price_snapshot(match_id)
        except Exception:
            snapshot = {"prices": [], "asian_ok": False, "total_ok": False}
        updates.append(_refresh_crown_quote(card, row, titan, snapshot))
    with state_lock(config):
        retained = merge_predictions(config, updates, now=now)
    return {
        "ok": True, "mode": "refresh", "predictions": len(updates),
        "retained_predictions": len(retained), "simulations_created": 0,
        "fresh_t5_predictions": [], "safe_quote_refresh_only": True,
    }


def _has_pending_formal_admission(ledger: dict[str, Any], stage: str | None = None) -> bool:
    return any(
        isinstance(row, dict)
        and row.get("formal_admission_pending") is True
        and (stage is None or row.get("stage") == stage)
        for watch in (ledger.get("watch") or {}).values()
        if isinstance(watch, dict)
        for row in watch.get("stages") or []
    )


def _optional_save_worker(
    config: Settings, ledger: dict[str, Any], sender: Any,
) -> None:
    try:
        save_ledger(config, ledger)
        sender.send(True)
    except Exception:
        try:
            sender.send(False)
        except Exception:
            pass
    finally:
        sender.close()


def _canonical_ledger_bytes(ledger: dict[str, Any]) -> bytes:
    """Byte-identical payload emitted by common.write_json_atomic."""
    return (
        json.dumps(
            ledger, ensure_ascii=False, indent=2, sort_keys=True,
        ) + "\n"
    ).encode("utf-8")


def _durable_commit_matches(
    config: Settings, intended_bytes: bytes,
) -> bool:
    try:
        return paths(config)["ledger"].read_bytes() == intended_bytes
    except OSError:
        return False


def _bounded_optional_save(
    config: Settings, ledger: dict[str, Any], *, budget: float,
) -> bool:
    """Hard-bound an atomic optional save in a killable forked process."""
    if budget <= 0:
        return False
    intended_bytes = _canonical_ledger_bytes(ledger)
    started = time.monotonic()
    deadline = started + budget
    # Reserve part of the caller's budget for TERM/KILL/reap rather than
    # adding a fixed cleanup delay after the advertised timeout.
    cleanup_reserve = min(0.02, max(0.005, budget * 0.25))
    work_deadline = max(started, deadline - cleanup_reserve)
    context = multiprocessing.get_context("fork")
    receiver, sender = context.Pipe(duplex=False)
    process = context.Process(
        target=_optional_save_worker, args=(config, ledger, sender),
    )
    process.start()
    sender.close()
    process.join(max(0.0, work_deadline - time.monotonic()))
    if process.is_alive():
        process.terminate()
        term_remaining = max(0.0, deadline - time.monotonic())
        process.join(min(term_remaining, cleanup_reserve / 2))
        if process.is_alive():
            process.kill()
            process.join(max(0.0, deadline - time.monotonic()))
        # SIGKILL cannot be ignored. Always reap before returning/releasing the
        # parent's state lock, even if scheduler latency exhausts the budget.
        if process.is_alive():
            process.join()
        receiver.close()
        return _durable_commit_matches(config, intended_bytes)
    acknowledged = False
    try:
        acknowledged = bool(receiver.recv()) if receiver.poll() else False
    except (EOFError, BrokenPipeError, OSError):
        acknowledged = False
    receiver.close()
    # Resolve every outcome—including save-before-ack exit—against the exact
    # canonical bytes while the caller still owns the CAS lock. Parsed Python
    # equality is deliberately forbidden (1 and 1.0 are different commits).
    durable_matches = _durable_commit_matches(config, intended_bytes)
    return durable_matches and (
        acknowledged or process.exitcode is not None
    )


def _drain_pending_formal_admissions(
    config: Settings, *, deadline: float | None = None,
) -> list[str]:
    """Run bounded optional matching outside the state lock, then CAS-save.

    The worker receives an isolated ledger copy. If it exceeds the remaining
    budget, it can only mutate that abandoned copy. The final short lock writes
    results only when the durable ledger is byte-for-byte unchanged since the
    copy was loaded, preserving native-writer preemption.
    """
    remaining = _deadline_remaining(deadline) if deadline is not None else 0.5
    if remaining <= 0:
        return []
    lock_wait = min(0.05, remaining)
    with state_lock(config, timeout_seconds=lock_wait) as acquired:
        if not acquired:
            return []
        base = load_ledger(config)
        staged = copy.deepcopy(base)
    pending_t5 = _has_pending_formal_admission(staged, "T-5")
    result: dict[str, Any] = {}
    remaining = _deadline_remaining(deadline) if deadline is not None else 0.5
    final_commit_reserve = 0.05
    kickoff_safety_margin = 0.05
    worker_budget = remaining - final_commit_reserve
    pending_kickoffs = [
        parse_time(
            row.get("kickoff_hkt") or row.get("kickoff")
            or watch.get("kickoff_hkt") or watch.get("kickoff")
        )
        for watch in (staged.get("watch") or {}).values()
        if isinstance(watch, dict)
        for row in watch.get("stages") or []
        if isinstance(row, dict)
        and row.get("formal_admission_pending") is True
    ]
    pending_kickoffs = [value for value in pending_kickoffs if value is not None]
    if pending_kickoffs:
        worker_budget = min(
            worker_budget,
            (min(pending_kickoffs) - datetime.now(HKT)).total_seconds()
            - kickoff_safety_margin,
        )
    if worker_budget < 0.01:
        return []

    def consume() -> None:
        try:
            result["emitted"] = reconcile_pending_formal_admissions(staged, config)
            if pending_t5:
                recompute_stats(staged, config)
        except Exception as exc:
            result["error"] = type(exc).__name__

    worker = threading.Thread(target=consume, daemon=True)
    worker.start()
    worker.join(max(0.0, worker_budget))
    if worker.is_alive() or "error" in result:
        return []
    emitted = result.get("emitted")
    emitted = list(emitted) if isinstance(emitted, (list, tuple)) else []
    if staged == base:
        return emitted
    remaining = _deadline_remaining(deadline) if deadline is not None else 0.5
    if remaining <= 0:
        return []
    with state_lock(config, timeout_seconds=min(0.05, remaining)) as acquired:
        if not acquired:
            return []
        current = load_ledger(config)
        if current != base:
            return []
        # Final execution-time proof: if a T-5 completed on the staged copy
        # but its immutable kickoff has now arrived, discard every optional
        # staged mutation and persist only a terminal expiry from the base.
        crossed_ids: set[str] = set()
        final_now = datetime.now(HKT)
        for watch in (base.get("watch") or {}).values():
            if not isinstance(watch, dict):
                continue
            for row in watch.get("stages") or []:
                if (
                    not isinstance(row, dict)
                    or row.get("stage") != "T-5"
                    or row.get("formal_admission_pending") is not True
                ):
                    continue
                snapshot_id = row.get("formal_admission_snapshot_id")
                kickoff = parse_time(
                    row.get("kickoff_hkt") or row.get("kickoff")
                    or watch.get("kickoff_hkt") or watch.get("kickoff")
                )
                staged_row = next((
                    candidate
                    for staged_watch in (staged.get("watch") or {}).values()
                    if isinstance(staged_watch, dict)
                    for candidate in staged_watch.get("stages") or []
                    if isinstance(candidate, dict)
                    and candidate.get("formal_admission_snapshot_id") == snapshot_id
                ), None)
                if (
                    isinstance(snapshot_id, str)
                    and kickoff is not None and final_now >= kickoff
                    and isinstance(staged_row, dict)
                    and staged_row.get("formal_admission_status") == "COMPLETED"
                ):
                    crossed_ids.add(snapshot_id)
        if crossed_ids:
            expired = copy.deepcopy(base)
            for watch in (expired.get("watch") or {}).values():
                if not isinstance(watch, dict):
                    continue
                for row in watch.get("stages") or []:
                    if (
                        isinstance(row, dict)
                        and row.get("formal_admission_snapshot_id") in crossed_ids
                    ):
                        row["formal_admission_pending"] = False
                        row["formal_admission_status"] = "EXPIRED"
                        row["formal_admission_reason"] = (
                            "kickoff_reached_before_formal_admission_persist"
                        )
                        row["formal_admission_completed_at"] = iso_hkt()
            remaining = (
                _deadline_remaining(deadline) if deadline is not None else 0.5
            )
            _bounded_optional_save(
                config, expired, budget=max(0.0, remaining - 0.01),
            )
            return []
        if staged.get("log"):
            staged["log"][-1]["n_changes"] = len(emitted)
            staged["log"][-1]["changes"] = emitted or ["今次無模擬注動作"]
        remaining = (
            _deadline_remaining(deadline) if deadline is not None else 0.5
        )
        completed_t5_kickoffs = [
            parse_time(
                row.get("kickoff_hkt") or row.get("kickoff")
                or watch.get("kickoff_hkt") or watch.get("kickoff")
            )
            for watch in (staged.get("watch") or {}).values()
            if isinstance(watch, dict)
            for row in watch.get("stages") or []
            if isinstance(row, dict)
            and row.get("stage") == "T-5"
            and row.get("formal_admission_status") == "COMPLETED"
        ]
        completed_t5_kickoffs = [
            value for value in completed_t5_kickoffs if value is not None
        ]
        save_budget = remaining - 0.01
        if completed_t5_kickoffs:
            save_budget = min(
                save_budget,
                (min(completed_t5_kickoffs) - datetime.now(HKT)).total_seconds()
                - kickoff_safety_margin,
            )
        if not _bounded_optional_save(
            config, staged, budget=max(0.0, save_budget),
        ):
            return []
    return emitted


def _commit_stage_predictions(
    config: Settings,
    mode: str,
    stage_predictions: list[dict[str, Any]],
    *,
    deadline: float | None = None,
) -> tuple[list[str], list[dict[str, str]], list[str], int]:
    """Commit a completed batch while holding the state lock only briefly."""
    if not stage_predictions:
        _drain_pending_formal_admissions(config, deadline=deadline)
        return [], [], [], len(load_predictions(config))
    _timing.record(
        "commit_stage_predictions_entered", deadline=deadline,
        extra={"batch_size": len(stage_predictions)},
    )
    lock_wait = (
        min(_TIMED_STAGE_LOCK_WAIT_SECONDS, _deadline_remaining(deadline))
        if deadline is not None else None
    )
    with state_lock(config, timeout_seconds=lock_wait) as acquired:
        if not acquired:
            _timing.record("commit_stage_predictions_lock_not_acquired", deadline=deadline)
            # Another short state writer (normally sweep/settle final merge)
            # is still committing.  Leave this native stage unpersisted so
            # the next tick retries rather than waiting through its deadline.
            return [], [], [], len(load_predictions(config))
        # Reload after every small batch: another mode can have committed
        # independently while this provider process was working.
        ledger = load_ledger(config)
        emitted: list[str] = []
        fresh_condition_predictions: list[dict[str, str]] = []
        evidence_projection_stages: list[str] = []
        committed_predictions: list[dict[str, Any]] = []
        for prediction in stage_predictions:
            kickoff = datetime.fromisoformat(str(prediction["kickoff_hkt"]))
            if kickoff.tzinfo is None:
                kickoff = kickoff.replace(tzinfo=HKT)
            if kickoff <= datetime.now(HKT):
                # A request admitted before kickoff is no longer a valid
                # pre-kickoff observation if it returns after kickoff.
                continue
            stage = str(prediction.get("stage") or "")
            match_id = str(prediction.get("match_id") or "")
            prior_stage = any(
                row.get("stage") == stage
                for row in ((ledger.get("watch") or {}).get(match_id, {}).get("stages") or [])
                if isinstance(row, dict)
            )
            created = sync_prediction(
                ledger,
                prediction,
                config,
                defer_auxiliary_recompute=(
                    stage != "T-5" or prediction.get("status") == "DATA_MISSING"
                ),
                # T-30 has no formal admission, and an unavailable T-5 has
                # no condition evidence to evaluate.  Both must commit their
                # immutable native attempt/snapshot before every consumer.
                # A quote-complete T-5 remains on the normal formal-admission
                # path so an exact frozen match still creates its observation
                # in the same valid pre-kickoff transaction.
                deadline_critical_snapshot=(
                    mode == "tick"
                    and (stage == "T-30" or prediction.get("status") == "DATA_MISSING")
                ),
                defer_formal_admission=True,
            )
            emitted.extend(created)
            prediction["stages"] = list(
                ledger["watch"].get(match_id, {}).get("stages") or []
            )
            # The reverse bridge receives a durable idempotent work item in
            # the same native write as the T-5 snapshot.  This is constant
            # local bookkeeping only: provider work/matching/evaluation are
            # drained later by the protected non-deadline server pass.
            if stage == "T-5" and not prior_stage:
                enqueue_committed_t5(
                    ledger, (ledger.get("watch") or {}).get(match_id) or {},
                )
            committed_predictions.append(prediction)
            if stage in {"首預", "T-30", "T-5"}:
                # This only records which native stage was durably committed.
                # The optional sidecar producer runs after the lock and never
                # participates in notification/outbox eligibility.
                evidence_projection_stages.append(stage)
            if stage in {"T-30", "T-5"} and prediction.get("status") != "DATA_MISSING" and (
                not prior_stage or (stage == "T-5" and bool(created))
            ) and any(
                row.get("stage") == stage for row in prediction["stages"]
                if isinstance(row, dict)
            ):
                fresh_condition_predictions.append({"match_id": match_id, "stage": stage})
        # A timed 首預/T-30 row is authoritative immediately after its atomic
        # snapshot write.  Portfolio/challenger aggregation is not required to
        # preserve that evidence and can be rebuilt by ordinary reconciliation.
        # Keep formal T-5 recomputation in-line until its own persistence path
        # is split, because it owns the active frozen observation projection.
        ledger["log"].append({
            "ts": iso_hkt(), "kind": mode, "n_changes": len(emitted),
            "changes": emitted or ["今次無模擬注動作"], "simulation_only": True,
        })
        ledger["log"] = ledger["log"][-100:]
        _timing.record("commit_stage_predictions_before_save_ledger", deadline=deadline)
        save_ledger(config, ledger)
        _timing.record("commit_stage_predictions_after_save_ledger", deadline=deadline)
        retained = merge_predictions(config, committed_predictions)
    emitted.extend(_drain_pending_formal_admissions(config, deadline=deadline))
    if mode != "tick":
        schedule_footbreak_execution_evidence_projection(
            config, evidence_projection_stages,
        )
    _timing.record(
        "commit_stage_predictions_returning", deadline=deadline,
        extra={"emitted": len(emitted)},
    )
    return emitted, fresh_condition_predictions, evidence_projection_stages, len(retained)


def _local_bulk_stage_prediction(
    titan: dict[str, Any],
    config: Settings,
    crown_snapshot: dict[str, Any],
    stage: str,
) -> dict[str, Any]:
    """Build a timed card from a persisted identity and current Crown odds.

    This deliberately supplies no HKJC/PinnAPI bridge.  A validated current
    ID-3 board is sufficient to durably record the native Crown observation.
    The empty bridge is diagnostic only; it must never trigger fixture
    discovery or matching for this deadline-bound path.
    """
    no_provider_match = Match(
        None, False, 0.0, f"deferred_for_local_bulk_{stage.lower()}"
    )
    prediction = _prediction(
        titan,
        BridgeMatch(
            no_provider_match,
            no_provider_match,
            f"local_bulk_{stage.lower()}",
            f"optional_providers_deferred_for_{stage.lower()}",
        ),
        None,
        stage,
        config,
        None,  # Valid bulk evidence prevents the existing direct-page branch.
        None,  # Valid bulk evidence prevents the existing PinnAPI branch.
        crown_snapshot,
    )
    # The ordinary prediction function retains a low-confidence WDL baseline
    # for incomplete provider passes.  This route has only Crown HDC/HIL
    # evidence, so do not represent that baseline as an independently observed
    # result probability.  Market no-vig forecasts remain tied to the exact
    # current two-sided Crown quotes, and EV/Kelly remain absent.
    prediction.update({
        "outcome": None,
        "forecast": None,
        "probability": None,
        "likely_score": None,
        "prediction_source": None,
    })
    prediction.pop("probabilities", None)
    prediction.pop("baseline_low_confidence", None)
    prediction["collection_attempt"] = {
        "at": iso_hkt(),
        "status": "AVAILABLE",
        "reason": None,
        "source": str(crown_snapshot.get("quote_source") or _CROWN_BULK_ID3_SOURCE),
    }
    return prediction


def _unavailable_timed_stage_prediction(
    titan: dict[str, Any],
    stage: str,
    reason: str,
) -> dict[str, Any]:
    """Create a retryable, pre-kickoff audit record without inventing a quote."""
    kickoff = parse_time(titan.get("kickoff"))
    return {
        "schema_version": "crown-prediction-v2",
        "matching_version": MATCHING_VERSION,
        "generated_at": iso_hkt(),
        "match_id": str(titan.get("id") or ""),
        "league": titan.get("league") or "",
        "home": titan.get("home") or "",
        "away": titan.get("away") or "",
        "kickoff_hkt": iso_hkt(kickoff) if kickoff else "",
        "discovered_at": iso_hkt(),
        "stage": stage,
        "titan_match_id": str(titan.get("id") or ""),
        "status": "DATA_MISSING",
        "verdict": "資料未能取得",
        "no_bet_reason": "皇冠原生計時盤暫不可用；未建立模擬注。",
        "candidates": [],
        "forecast_candidates": [],
        "market_sources": {
            "HDC": "titan007-crown-id-3",
            "HIL": "titan007-crown-id-3",
        },
        "source_status": "crown_id3_unavailable",
        # This record is atomically saved with the stage snapshot.  It is not a
        # completed stage: completed_stages() deliberately leaves it due for a
        # bounded retry, and no post-kickoff caller may create it.
        "collection_attempt": {
            "at": iso_hkt(),
            "status": "DATA_MISSING",
            "reason": reason,
            "source": "titan007-crown-id-3",
        },
    }


def _journal_timed_stage_attempts(
    config: Settings, rows: list[dict[str, Any]], *, reason: str,
) -> int:
    """Write-ahead journal every due native stage before any collection work.

    The journal is intentionally outside ``stages``: it proves a scheduled
    attempt and a crash/timeout without pretending an absent quote was a
    prediction.  A later atomic snapshot commit advances the same keyed record
    to COMMITTED or FAILED.
    """
    journaled = 0
    now = datetime.now(HKT)
    _timing.record(
        "journal_timed_stage_attempts_entered",
        extra={"row_count": len(rows), "reason": reason},
    )
    with state_lock(config, timeout_seconds=1.0) as acquired:
        if not acquired:
            _timing.record("journal_timed_stage_attempts_lock_not_acquired")
            return 0
        ledger = load_ledger(config)
        watches = ledger.setdefault("watch", {})
        for row in rows:
            stage = str(row.get("_due_stage") or "")
            match_id = str(row.get("id") or "")
            kickoff = parse_time(row.get("kickoff"))
            if stage not in {"首預", "T-30", "T-5"} or not match_id or kickoff is None:
                continue
            if kickoff <= now:
                continue
            watch = watches.setdefault(match_id, {
                "match_id": match_id, "league": row.get("league") or "",
                "home": row.get("home") or "", "away": row.get("away") or "",
                "kickoff": iso_hkt(kickoff), "titan_match_id": match_id,
                "matching_version": MATCHING_VERSION,
                "prediction_era": PREDICTION_ERA, "stages": [],
                "discovered_at": iso_hkt(),
            })
            ensure_stage_jobs(watch, kickoff)
            attempts = watch.setdefault("stage_attempts", {})
            if not isinstance(attempts, dict):
                attempts = {}
                watch["stage_attempts"] = attempts
            previous = attempts.get(stage)
            retry_count = int(previous.get("retry_count") or 0) + 1 if isinstance(previous, dict) else 1
            attempts[stage] = {
                "stage": stage, "state": "STARTED", "started_at": iso_hkt(),
                "reason": reason, "source": "titan007-crown-id-3",
                "retry_count": retry_count,
            }
            job = (watch.get("stage_jobs") or {}).get(stage)
            if isinstance(job, dict):
                job.update({
                    "state": "STARTED", "started_at": iso_hkt(),
                    "reason": reason, "retry_count": retry_count,
                })
            journaled += 1
        if journaled:
            save_ledger(config, ledger)
    _timing.record(
        "journal_timed_stage_attempts_returning",
        extra={"journaled": journaled},
    )
    return journaled


def _expire_lapsed_timed_stage_attempts(config: Settings, now: datetime) -> int:
    """Close a started journal after kickoff without creating a late stage.

    This is an append-only operational incident marker only.  It never writes
    a prediction, quote, result, or formal observation after kickoff.

    A same-kickoff batch large enough to exhaust the tick deadline can be
    killed before the child ever journals a ``STARTED`` attempt at all.  Such
    a job is due, past kickoff, and was never even attempted -- that is a
    genuine missed stage, not merely an abandoned one, and it must receive
    the same honest, visible ``EXPIRED`` incident.  This never invents a
    ``stage_attempts`` history that did not happen; it only records the
    terminal outcome of the durable job that truly was due.
    """
    expired = 0
    with state_lock(config, timeout_seconds=0.5) as acquired:
        if not acquired:
            return 0
        ledger = load_ledger(config)
        for watch in (ledger.get("watch") or {}).values():
            if not isinstance(watch, dict):
                continue
            kickoff = parse_time(
                watch.get("kickoff_utc") or watch.get("kickoff_hkt") or watch.get("kickoff")
            )
            if kickoff is None or kickoff > now:
                continue
            attempts = watch.get("stage_attempts")
            if not isinstance(attempts, dict):
                attempts = {}
                watch["stage_attempts"] = attempts
            jobs = watch.get("stage_jobs") if isinstance(watch.get("stage_jobs"), dict) else {}
            stages_to_close: set[str] = {
                stage for stage, attempt in attempts.items()
                if isinstance(attempt, dict) and attempt.get("state") == "STARTED"
            }
            for stage, job in jobs.items():
                if stage not in ("T-30", "T-5") or not isinstance(job, dict):
                    continue
                if job.get("state") in {"COMMITTED", "STARTED", "EXPIRED"}:
                    continue
                existing_attempt = attempts.get(stage)
                if isinstance(existing_attempt, dict) and existing_attempt.get("state") in {
                    "COMMITTED", "EXPIRED",
                }:
                    continue
                stages_to_close.add(stage)
            for stage in stages_to_close:
                attempt = attempts.get(stage)
                if not isinstance(attempt, dict):
                    attempt = {"stage": stage}
                    attempts[stage] = attempt
                attempt.update({
                    "state": "EXPIRED",
                    "updated_at": iso_hkt(now),
                    "reason": "deadline_exhausted_before_native_stage_commit",
                })
                job = jobs.get(stage)
                if isinstance(job, dict) and job.get("state") != "COMMITTED":
                    job.update({
                        "state": "EXPIRED",
                        "updated_at": iso_hkt(now),
                        "reason": "deadline_exhausted_before_native_stage_commit",
                    })
                incidents = watch.setdefault("stage_incidents", [])
                if isinstance(incidents, list) and not any(
                    isinstance(row, dict)
                    and row.get("stage") == stage
                    and row.get("reason") == "deadline_exhausted_before_native_stage_commit"
                    for row in incidents
                ):
                    incidents.append({
                        "stage": stage,
                        "kind": "MISSING_PRE_KICKOFF_STAGE",
                        "reason": "deadline_exhausted_before_native_stage_commit",
                        "recorded_at": iso_hkt(now),
                        "started_at": attempt.get("started_at"),
                    })
                expired += 1
        if expired:
            save_ledger(config, ledger)
    return expired


def persist_timed_stage_timeout_failures(config: Settings, now: datetime | None = None) -> int:
    """Terminally record timed-out native work before the service deadline.

    The deadline-owning parent calls this only after terminating a stalled tick
    child.  It performs no provider request and converts its pre-existing
    STARTED journals into retryable, immutable DATA_MISSING stage records while
    kickoff is still in the future.

    A same-kickoff batch large enough to exhaust the tick deadline can be
    killed before the child ever reaches the in-process write-ahead journal
    (``_journal_timed_stage_attempts``).  That earlier omission must not be
    silent: a job whose durable ``due_at_utc`` has already elapsed, but whose
    attempt was never marked ``STARTED`` at all, is exactly as timed-out as one
    that was marked ``STARTED`` and then abandoned.  Both receive the same
    honest, retryable DATA_MISSING outcome here so the parent's safety net
    covers a child that dies before its own journal write, not only one that
    dies after it.
    """
    now = (now or datetime.now(HKT)).astimezone(HKT)
    ledger = load_ledger(config)
    rows: list[dict[str, Any]] = []
    for key, watch in (ledger.get("watch") or {}).items():
        if not isinstance(watch, dict):
            continue
        kickoff = parse_time(watch.get("kickoff_utc") or watch.get("kickoff_hkt") or watch.get("kickoff"))
        if kickoff is None or kickoff <= now:
            continue
        attempts = watch.get("stage_attempts") if isinstance(watch.get("stage_attempts"), dict) else {}
        jobs = watch.get("stage_jobs") if isinstance(watch.get("stage_jobs"), dict) else {}
        for stage in ("T-30", "T-5"):
            attempt = attempts.get(stage)
            attempt_started = isinstance(attempt, dict) and attempt.get("state") == "STARTED"
            job = jobs.get(stage)
            job_due_unstarted = False
            if not attempt_started and isinstance(job, dict) and job.get("state") not in {
                "COMMITTED", "STARTED",
            }:
                due_at = parse_time(job.get("due_at_utc"))
                if due_at is not None and now.astimezone(timezone.utc) >= due_at.astimezone(timezone.utc):
                    job_due_unstarted = True
            if not attempt_started and not job_due_unstarted:
                continue
            rows.append({
                "id": str(watch.get("native_fixture_id") or watch.get("titan_match_id") or watch.get("match_id") or key),
                "league": watch.get("league") or "", "home": watch.get("home") or "",
                "away": watch.get("away") or "", "kickoff": kickoff, "_due_stage": stage,
            })
    if not rows:
        return 0
    predictions = [
        _unavailable_timed_stage_prediction(
            row, str(row["_due_stage"]), "native_stage_engine_deadline_exhausted",
        )
        for row in rows
    ]
    _commit_stage_predictions(
        config, "tick", predictions, deadline=time.monotonic() + 3.5,
    )
    return len(predictions)


def _run_local_bulk_timed_stages(
    config: Settings,
    rows: list[dict[str, Any]],
    deadline: float,
) -> dict[str, Any]:
    """Persist due stages from each locked native Crown fixture ID.

    Direct ID=3 reads are authoritative: no team-name rematch or broad-board
    inclusion can redirect a scheduled job.  The bulk board may only supply a
    same-ID fallback when a bounded direct read is unavailable.
    """
    _timing.record(
        "run_local_bulk_timed_stages_entered", deadline=deadline,
        extra={"fixture_count": len(rows)},
    )
    journaled = _journal_timed_stage_attempts(
        config, rows, reason="native_timed_stage_collection_started",
    )
    # Shadow instrumentation (Crown T-5 deadline-first patch, stage 2).
    # Default-off: when CROWN_NATIVE_STAGE_STORE_ENABLED is unset/false this
    # call constructs no store and performs no filesystem I/O whatsoever.
    # The callee already guards every exception internally; this call site
    # adds a second, defense-in-depth guard so that even an unanticipated
    # defect inside shadow bookkeeping can never propagate into this
    # deadline-critical path.  It mirrors the legacy write-ahead journal
    # above into a separate per-fixture store, strictly before any provider
    # collection begins, and never gates, delays, or replaces the legacy
    # journal/collection/commit below.
    try:
        _native_shadow.shadow_mark_started_batch(config, rows)
    except Exception:
        pass

    # Native per-fixture cutover (Crown T-5 deadline-first patch, stage 6).
    # Default-off: when CROWN_NATIVE_STAGE_CUTOVER_ENABLED is unset/false,
    # ``_on_direct_result`` below is never constructed and ``on_result`` is
    # passed as ``None``, so ``_collect_locked_direct_snapshots`` runs with
    # byte-identical behaviour to every prior stage.
    #
    # When the flag is on, this fires ONLY the native per-fixture durability
    # commit (write-ahead STARTED is already covered by
    # ``shadow_mark_started_batch`` above; this adds the terminal
    # COMMITTED/FAILED/DATA_MISSING snapshot) the instant each fixture's own
    # snapshot is collected -- strictly before the whole batch's collection
    # loop finishes. It deliberately does NOT call
    # ``native_stage_cutover.commit_fixture_result`` (which would also
    # decide whether to run/defer the *legacy* projection): the legacy
    # ledger/Wilson/dashboard/Telegram projection for every row in this
    # batch path continues to run exactly once, unconditionally, via the
    # existing ``stage_predictions``/``_commit_stage_predictions`` call
    # below -- never split, never duplicated, never skipped by this stage.
    # Splitting the *legacy* batch commit itself into N per-fixture legacy
    # commits here would reintroduce the very N-whole-ledger-write cost
    # this stage exists to avoid, and would also risk double-projecting a
    # fixture whose native commit is separately deferred by
    # ``commit_fixture_result`` elsewhere (the tick-mode path). Native
    # commit failure for one fixture is isolated (a bare ``try/except``
    # here, plus the ``on_result`` wrapper's own isolation in
    # ``_collect_locked_direct_snapshots``) and can never block collection
    # or the legacy commit for any other fixture.
    _on_direct_result = None
    if _native_cutover.cutover_enabled():
        def _on_direct_result(titan_row: dict[str, Any], snapshot: dict[str, Any]) -> None:
            stage = str(titan_row.get("_due_stage") or "")
            prediction = _local_bulk_stage_prediction(titan_row, config, snapshot, stage)
            try:
                native_state = _native_cutover.commit_native_only(config, prediction)
            except Exception:
                native_state = None
            if native_state == "COMMITTED":
                # Stage 7: the instant this fixture's own native snapshot is
                # durable, atomically enqueue a bounded/idempotent deferred
                # legacy-projection job referencing this exact prediction's
                # identity -- strictly BEFORE the unconditional whole-batch
                # legacy commit below. If that whole-batch commit is later
                # killed or times out, this queued item is the fixture's
                # only recorded path back to legacy/dashboard evidence; the
                # existing legacy whole-batch commit succeeding first just
                # makes this item an idempotent no-op ACK on its next drain.
                # No-op (returns False, no I/O) when the deferred-projection
                # flag is off; isolated (never raises) either way.
                try:
                    _native_cutover.enqueue_committed_snapshot(config, prediction)
                except Exception:
                    pass

    usable_snapshots, direct_attempted = _collect_locked_direct_snapshots(
        config, rows, deadline, on_result=_on_direct_result,
    )

    fallback_rows = [
        row for row in rows
        if str(row.get("id") or "") not in usable_snapshots
    ]
    bulk_fallback_attempted = 0
    if fallback_rows:
        _timing.record(
            "bulk_fallback_collection_entered", deadline=deadline,
            extra={"fallback_row_count": len(fallback_rows)},
        )
        snapshots, attempted_bulk = _collect_same_id_bulk_fallback(config, deadline)
        _timing.record("bulk_fallback_collection_returning", deadline=deadline)
        if attempted_bulk:
            bulk_fallback_attempted = len(fallback_rows)
        for titan in fallback_rows:
            match_id = str(titan.get("id") or "")
            kickoff = parse_time(titan.get("kickoff"))
            snapshot = snapshots.get(match_id)
            if (
                kickoff is not None
                and snapshot
                and snapshot.get("quote_source") == _CROWN_BULK_ID3_SOURCE
                and _valid_pre_kickoff_bulk_snapshot(snapshot, kickoff)
                and snapshot.get("prices")
            ):
                usable_snapshots[match_id] = snapshot

    stage_predictions: list[dict[str, Any]] = []
    retained = len(load_predictions(config))
    for titan in rows:
        stage = str(titan.get("_due_stage") or "")
        snapshot = usable_snapshots.get(str(titan.get("id") or ""))
        if snapshot is None:
            # Every due stage receives an explicit failure snapshot before
            # kickoff.  This preserves the retryable absence for T-5 as well
            # as T-30 and prevents a process timeout from looking successful.
            stage_predictions.append(_unavailable_timed_stage_prediction(
                titan,
                stage,
                "locked_direct_id3_and_bulk_fallback_unavailable",
            ))
            continue
        stage_predictions.append(
            _local_bulk_stage_prediction(titan, config, snapshot, stage)
        )
    unavailable = sum(
        str(titan.get("id") or "") not in usable_snapshots
        for titan in rows
    )

    # Shadow instrumentation (Crown T-5 deadline-first patch, stage 2).
    # Runs strictly after the legacy path's own bounded provider collection
    # above has already produced a result -- a usable snapshot or an
    # explicit DATA_MISSING -- for every row, and strictly before the legacy
    # commit below. The shadow STARTED journal already happened once, right
    # before collection began; only the per-fixture shadow commit runs here.
    # Default-off: constructs no store and performs no filesystem I/O when
    # CROWN_NATIVE_STAGE_STORE_ENABLED is unset/false.  The callee already
    # guards every exception internally; this call site adds a second,
    # defense-in-depth guard so shadow bookkeeping can never mutate
    # ``stage_predictions`` or delay/gate the legacy commit that immediately
    # follows -- the legacy ledger remains the sole authority for Wilson
    # admission, dashboard projection and Telegram notification either way.
    try:
        _native_shadow.shadow_commit_stage_predictions(config, stage_predictions)
    except Exception:
        pass

    # Stage 7: bounded, single-transaction recovery drain, placed after this
    # tick's own native per-fixture collection has already finished but
    # strictly BEFORE the unbounded legacy whole-batch commit immediately
    # below. This is the primary reachable-recovery opportunity per the
    # Stage 7 design mandate: if an EARLIER tick's legacy whole-batch commit
    # was killed/timed out after native commits (and enqueues) had already
    # happened for some fixtures, those queued items are drained here, in
    # this tick, before this tick adds its own (unrelated) unbounded legacy
    # commit work. A strict, small time budget is carved out of the
    # remaining deadline so this can never compete with or delay this
    # tick's own urgent due-stage collection/commit; if the remaining budget
    # is at or below the reserve this drain needs, it is skipped entirely
    # (never partially run) and left for the next opportunity. Default-off
    # (no-op, no I/O) when CROWN_NATIVE_STAGE_DEFERRED_PROJECTION_ENABLED is
    # unset/false.
    _stage7_recovery_reserve = _NATIVE_STAGE7_RECOVERY_DRAIN_RESERVE_SECONDS
    if _deadline_remaining(deadline) > _stage7_recovery_reserve:
        try:
            _native_cutover.drain_deferred_projections_batch(
                config,
                max_items=_NATIVE_DEFERRED_DRAIN_MAX_ITEMS,
                max_seconds=min(
                    _stage7_recovery_reserve, _deadline_remaining(deadline),
                ),
            )
        except Exception:
            pass

    # The commit helper already rejects a prediction that has crossed kickoff,
    # preserves stage idempotency, and evaluates each T-5 once. One batch
    # avoids repeating ledger read/recompute/write work for same-kickoff
    # fixtures while retaining those per-prediction protections.
    emitted, fresh_condition_predictions, evidence_projection_stages, retained = _commit_stage_predictions(
        config, "tick", stage_predictions, deadline=deadline,
    )
    _timing.record(
        "run_local_bulk_timed_stages_returning", deadline=deadline,
        extra={
            "fixture_count": len(rows),
            "unavailable": unavailable,
            "emitted": len(emitted),
        },
    )
    # Bounded deferred-projection drain (Crown T-5 deadline-first patch,
    # stage 6; Stage 7 correction below). This native ID=3 batch path itself
    # now DOES enqueue into the deferred queue -- see the
    # ``_on_direct_result``/``enqueue_committed_snapshot`` call above -- and
    # Stage 7 added the *primary* recovery-drain opportunity earlier in this
    # same function, strictly before the whole-batch legacy commit above,
    # using ``drain_deferred_projections_batch``'s single shared transaction.
    # This per-item ``drain`` call remains as a secondary, non-critical,
    # post-commit, best-effort catch-up only -- every item it processes is
    # independently idempotent, so running both drains in the same tick can
    # only ever re-ACK an already-projected item, never double-project one.
    # Default-off/no-op when CROWN_NATIVE_STAGE_DEFERRED_PROJECTION_ENABLED
    # is unset/false.
    try:
        _native_cutover.drain_deferred_projections(
            config,
            lambda prediction: _commit_stage_predictions(
                config, "tick", [prediction], deadline=deadline,
            ),
            max_items=_NATIVE_DEFERRED_DRAIN_MAX_ITEMS,
        )
    except Exception:
        pass
    return {
        "ok": True,
        "mode": "tick",
        "fast_t5_bulk": any(row.get("_due_stage") == "T-5" for row in rows),
        "fast_timed_stage_bulk": True,
        "predictions": len(stage_predictions),
        "retained_predictions": retained,
        "simulations_created": len(emitted),
        "fresh_condition_predictions": fresh_condition_predictions,
        "evidence_projection_stages": evidence_projection_stages,
        "fresh_t5_predictions": [
            item["match_id"] for item in fresh_condition_predictions
            if item["stage"] == "T-5"
        ],
        "bulk_unavailable_predictions": unavailable,
        "direct_id_attempted": direct_attempted,
        "direct_id_predictions": sum(
            snapshot.get("quote_source") == _CROWN_ID3_SOURCE
            for snapshot in usable_snapshots.values()
        ),
        "bulk_fallback_attempted": bulk_fallback_attempted,
        "pinnapi_fixtures": 0,
        "hkjc_fixtures": 0,
        "deferred_predictions": unavailable,
        "failed_predictions": 0,
        "write_ahead_attempts": journaled,
    }


def run(
    mode: str,
    config: Settings,
    *,
    tick_pass_deadline: float | None = None,
) -> dict[str, Any]:
    """Run a remote pass only when the explicit validation gate and PinnAPI key exist."""
    if mode not in {"tick", "sweep", "round-update", "first-look-reconcile", "settle", "refresh"}:
        raise ValueError("mode must be tick, sweep, round-update, first-look-reconcile, settle, or refresh")
    if not config.enabled:
        return {"ok": False, "reason": "CROWN_ENABLED=0; no network call was made"}
    if mode == "refresh":
        return refresh_current_quotes(config)
    if mode == "settle":
        if not config.pinnapi_configured:
            return {"ok": False, "reason": "PinnAPI credentials are not configured; no network call was made"}
        from .settle import settle_due
        # settle_due owns a separate long-running settlement lock and takes
        # state_lock only for its final merge.  Never hold the commit lock
        # while result providers are read: a T-5 commit is deadline-bound.
        return settle_due(config)
    expired_stage_attempts = 0
    if mode == "tick":
        expired_stage_attempts = _expire_lapsed_timed_stage_attempts(
            config, datetime.now(HKT),
        )
    ledger = load_ledger(config)
    existing_predictions = load_predictions(config)
    if mode == "tick":
        # Provider/discovery work must never consume the final outbox window.
        # `crown.run` owns the enclosing absolute deadline.  Do not start a
        # fresh provider clock after local state reads: a slow JSON read would
        # otherwise silently consume the reserved notification window.
        provider_seconds = _tick_provider_deadline_seconds()
        reserve_seconds = max(
            0.0, _tick_pass_deadline_seconds() - provider_seconds,
        )
        local_provider_deadline = time.monotonic() + provider_seconds
        tick_deadline = (
            min(local_provider_deadline, tick_pass_deadline - reserve_seconds)
            if tick_pass_deadline is not None
            else local_provider_deadline
        )
        repaired_stage_jobs = _repair_durable_stage_jobs(config, datetime.now(HKT))
        ledger = load_ledger(config)
        titan_rows = _tick_rows_from_predictions(
            existing_predictions, ledger, datetime.now(HKT)
        )
        if not titan_rows:
            # Stage 7: the fast-noop path has no urgent due-stage work this
            # tick by definition (``titan_rows`` is empty), so it is always
            # safe to spend the same small, strictly-bounded recovery-drain
            # budget here -- this can never delay or compete with due-stage
            # collection because there is none pending in this tick. This is
            # the restart-safe backlog-drain opportunity for a process that
            # was killed during a previous tick's legacy whole-batch commit
            # and then restarted directly into a tick with nothing newly due.
            try:
                _native_cutover.drain_deferred_projections_batch(
                    config,
                    max_items=_NATIVE_DEFERRED_DRAIN_MAX_ITEMS,
                    max_seconds=_NATIVE_STAGE7_RECOVERY_DRAIN_RESERVE_SECONDS,
                )
            except Exception:
                pass
            pending_emitted = _drain_pending_formal_admissions(
                config, deadline=tick_deadline,
            )
            return {
                "ok": True, "mode": mode, "fast_noop": True,
                "predictions": 0, "retained_predictions": len(existing_predictions),
                "simulations_created": len(pending_emitted),
                "repaired_stage_jobs": repaired_stage_jobs,
                "expired_stage_attempts": expired_stage_attempts,
            }
        titan_rows = _prioritize_tick_rows(titan_rows)
        if titan_rows:
            # This return is intentionally before PinnAPI credentials, policy
            # reads, fixture discovery and bridge mapping.  Every known native
            # stage, including a late missing 首預, uses the bounded ID=3 path.
            # T-5 stays first, while no optional reference or counterpart
            # worker can block durable stage evidence.
            result = _run_local_bulk_timed_stages(config, titan_rows, tick_deadline)
            result["expired_stage_attempts"] = expired_stage_attempts
            return result
        # This is deliberately measured before any provider work.  The
        # systemd 55-second limit remains the final safeguard for an upstream
        # fixture-list call that cannot be interrupted in-process.
    else:
        tick_deadline = None
    native_first_look_reconcile = mode == "first-look-reconcile"
    if not config.pinnapi_configured and not native_first_look_reconcile:
        return {"ok": False, "reason": "PinnAPI credentials are not configured; no network call was made"}
    # This can read the local learning store.  Keep it after the local tick
    # due check so a genuine no-op has no expensive work or provider calls.
    entry_policies = (
        {}
        if native_first_look_reconcile else {
            code: market_entry_thresholds(ledger, code, config)
            for code in ("HDC", "HIL", "CHL")
        }
    )
    titan_client = TitanClient(config)
    pinnapi_client = None if native_first_look_reconcile else PinnapiClient(config)
    bulk_crown_quotes: dict[str, dict[str, Any]] = {}
    if mode == "tick":
        # A single company-ID-3 bulk read serves every due local card.  Do not
        # turn a transport/parser failure into a fabricated quote; the
        # per-fixture/cache paths below remain guarded fallbacks.
        try:
            remaining = _deadline_remaining(tick_deadline)
            fetched_bulk = (
                titan_client.crown_bulk_price_snapshots(max_seconds=remaining)
                if remaining >= _MIN_DEADLINE_CALL_SECONDS else {}
            )
            bulk_crown_quotes = fetched_bulk if isinstance(fetched_bulk, dict) else {}
        except OSError:
            bulk_crown_quotes = {}
    sweep_mode = mode in {"sweep", "round-update", "first-look-reconcile"}
    if sweep_mode:
        now = datetime.now(HKT)
        window_contains = in_future_round_update_window if mode == "round-update" else in_current_period
        try:
            provider_rows = titan_client.fixtures(**(
                {"max_seconds": _FIRST_LOOK_RECONCILE_FIXTURE_SECONDS}
                if native_first_look_reconcile else {}
            ))
        except (OSError, ValueError, TypeError) as exc:
            if mode == "first-look-reconcile":
                _record_hourly_first_look_reconciliation_incident(
                    config, f"native_fixture_board_{type(exc).__name__}", now,
                )
                return {
                    "ok": True, "mode": mode,
                    "origin": "hourly_first_look_reconciliation",
                    "provider_status": "unavailable_audited",
                    "reconciled_first_look": 0,
                }
            raise
        titan_rows = _sweep_rows_with_due_existing(
            provider_rows,
            existing_predictions,
            ledger,
            now,
            window_contains=window_contains,
        )
        if mode == "first-look-reconcile":
            titan_rows = _hourly_first_look_reconciliation_rows(
                titan_rows, ledger, now,
            )
    if native_first_look_reconcile:
        # This hourly reconciliation has one responsibility: exact native
        # Crown IDs that lack 首預.  It must not request PinnAPI, HKJC, any
        # cross-book bridge, policy store, or Footbreak projection.  Those
        # optional calls previously kept this three-minute service alive until
        # systemd terminated it, leaving the native-only repair unable to
        # commit its first-look snapshots.
        if len(titan_rows) > _FIRST_LOOK_RECONCILE_MAX_FIXTURES:
            _record_hourly_first_look_reconciliation_incident(
                config,
                "native_first_look_reconcile_batch_limit_"
                f"{len(titan_rows) - _FIRST_LOOK_RECONCILE_MAX_FIXTURES}",
                now,
            )
        titan_rows = titan_rows[:_FIRST_LOOK_RECONCILE_MAX_FIXTURES]
        pinnapi_rows = []
        pinnapi_fixture_status = "not_requested_native_only"
        hkjc_rows = []
    else:
        pinnapi_fixture_status = "available"
        try:
            if mode == "tick":
                remaining = _deadline_remaining(tick_deadline)
                pinnapi_rows = (
                    pinnapi_client.fixtures(max_seconds=remaining)
                    if remaining >= _MIN_DEADLINE_CALL_SECONDS else []
                )
            else:
                pinnapi_rows = pinnapi_client.fixtures()
        except (OSError, ValueError, TypeError):
            # PinnAPI is an optional reference for bridge/EV work.  A transport
            # failure or malformed response must fail closed for that reference,
            # but must not abort Crown/Titan first-look discovery and persistence.
            pinnapi_rows = []
            pinnapi_fixture_status = "unavailable_fail_closed"
        # A deadline-bound Crown tick is native-Crown only.  Do not spend the
        # urgent scheduler budget on the optional Footbreak/HKJC bridge; sweep may
        # still retain its noncritical research/mapping collection separately.
        hkjc_rows = [] if mode == "tick" else fetch_matches()
    h_events = [(event_from_match(row), row) for row in hkjc_rows]
    h_events = [(event, row) for event, row in h_events if event]
    reconciled_hkjc_identities = 0
    if not native_first_look_reconcile and h_events:
        reconciled_hkjc_identities = _reconcile_hkjc_identities(
            config, [event for event, _row in h_events],
        )
        if reconciled_hkjc_identities:
            # Publish only from already durable local state.  The worker is
            # independently bounded and cannot gate this sweep or any tick.
            schedule_footbreak_execution_evidence_projection(config, ["首預"])
            existing_predictions = load_predictions(config)
    p_events = [_event_from_pinnapi(row) for row in pinnapi_rows]
    predictions = []
    stage_predictions: list[dict[str, Any]] = []
    pending_predictions: list[
        tuple[
            dict[str, Any], BridgeMatch, dict[str, Any] | None, str,
            dict[str, Any] | None, list[dict[str, Any]],
        ]
    ] = []
    current_predictions = {
        str(row.get("match_id")): row
        for row in existing_predictions
        if row.get("match_id")
    }
    refresh_quotes: dict[str, dict[str, Any]] = {}
    if sweep_mode:
        refresh_rows = []
        for titan in titan_rows:
            event = _event_from_titan(titan)
            if not window_contains(event.kickoff, now) or event.kickoff <= now:
                continue
            refresh_rows.append(titan)
        if refresh_rows:
            # Titan's two quote pages per fixture are independent network
            # reads.  A small bounded pool prevents a 100+ match refresh from
            # blocking the two-minute T-30/T-5 worker for most of the window.
            with ThreadPoolExecutor(max_workers=min(6, len(refresh_rows))) as pool:
                futures = {
                    pool.submit(
                        titan_client.crown_price_snapshot,
                        str(row["id"]),
                        **(
                            {"max_seconds": _FIRST_LOOK_RECONCILE_QUOTE_SECONDS}
                            if native_first_look_reconcile else {}
                        ),
                    ): str(row["id"])
                    for row in refresh_rows
                }
                for future in as_completed(futures):
                    match_id = futures[future]
                    try:
                        refresh_quotes[match_id] = future.result()
                    except Exception:
                        refresh_quotes[match_id] = {
                            "prices": [], "asian_ok": False, "total_ok": False,
                        }
    mapping = {
        "titan_due": 0, "titan_to_hkjc_mapped": 0, "hkjc_to_pinnapi_mapped": 0,
        "direct_same_script_mapped": 0, "unmapped_titan_to_hkjc": 0, "unmapped_hkjc_to_pinnapi": 0,
        "reversed_identity_mapped": 0,
        "reasons": {},
    }
    # Provider order is not a scheduling guarantee.  Nearest kickoff first
    # prevents a large T-30 batch from starving T-5.
    titan_rows.sort(key=lambda row: row["kickoff"])
    for titan in titan_rows:
        event = _event_from_titan(titan)
        if not (window_contains(event.kickoff, now) if sweep_mode else in_current_period(event.kickoff)):
            continue
        watch = ledger["watch"].get(event.id, {})
        done = completed_stages(watch, MATCHING_VERSION, PREDICTION_ERA)
        minutes = (event.kickoff - datetime.now(HKT)).total_seconds() / 60
        # Started fixtures remain visible until the 12:00 board rollover, but
        # no pass spends provider calls rebuilding prices after kickoff.
        if minutes <= 0:
            continue
        previous = current_predictions.get(event.id)
        if sweep_mode and previous is not None and "首預" in done:
            predictions.append(
                _refresh_crown_quote(
                    previous,
                    titan,
                    titan_client,
                    refresh_quotes.get(event.id),
                )
            )
            continue
        crown_snapshot = (
            refresh_quotes.get(event.id)
            if sweep_mode else bulk_crown_quotes.get(event.id)
        )
        if mode == "tick" and not _valid_pre_kickoff_bulk_snapshot(
            crown_snapshot, event.kickoff
        ):
            # A malformed/in-play bulk row is not direct evidence.  Clear it
            # before cache selection so it cannot accidentally suppress the
            # strict saved-cache fallback or masquerade as a fresh quote.
            crown_snapshot = None
        if _skip_new_confirmed_empty_crown(crown_snapshot, previous):
            # The fixture exists on Titan's complete schedule but Crown has
            # neither supported market and has no earlier valid Crown quote.
            # Keep a brand-new empty fixture off the Crown board and avoid
            # unnecessary HKJC/PinnAPI matching until the next sweep.
            #
            # An existing card must continue into _prediction(): its last
            # valid Crown quote is forecast-only, while a current HKJC corner
            # market can still produce a CHL learning forecast.  Returning
            # here used to strand old first-look cards before corner handling.
            continue
        stage = stage_for(minutes, sweep_mode, done)
        if not stage:
            continue
        if mode == "tick" and stage == "T-5" and crown_snapshot is None:
            # This is intentionally after stage selection: the narrow saved
            # cache can only suppress a per-fixture direct page for a due
            # native T-5, never for ordinary sweeps/earlier stages.
            crown_snapshot = _cached_t5_crown_snapshot(titan, previous)
        mapping["titan_due"] += 1
        if native_first_look_reconcile:
            bridge = _native_only_bridge()
        else:
            bridge = bridge_titan_to_pinnapi(event, [item[0] for item in h_events], p_events)
            if bridge.hkjc.event:
                mapping["titan_to_hkjc_mapped"] += 1
                if bridge.reversed:
                    mapping["reversed_identity_mapped"] += 1
            elif bridge.path != "direct_same_script":
                mapping["unmapped_titan_to_hkjc"] += 1
            if bridge.event:
                if bridge.path == "hkjc_bilingual_bridge":
                    mapping["hkjc_to_pinnapi_mapped"] += 1
                elif bridge.path == "direct_same_script":
                    mapping["direct_same_script_mapped"] += 1
            elif bridge.hkjc.event:
                mapping["unmapped_hkjc_to_pinnapi"] += 1
            if bridge.reason:
                mapping["reasons"][bridge.reason] = mapping["reasons"].get(bridge.reason, 0) + 1
        # Keep the complete Crown/Titan fixture board visible.  Crown's own
        # complete market can always create a forecast-learning snapshot;
        # only the strict HKJC -> PinnAPI bridge can unlock edge or a bet.
        h_row = next(
            (row for candidate, row in h_events
             if bridge.hkjc.event and candidate.id == bridge.hkjc.event.id),
            None,
        )
        previous_crown_prices = list(
            ((previous or {}).get("book_odds") or {}).get("crown") or []
        )
        pending_predictions.append((
            titan, bridge, h_row, stage, crown_snapshot, previous_crown_prices
        ))

    # A same-kickoff batch can contain dozens of fixtures.  Each prediction
    # performs independent Crown and PinnAPI reads, so serial execution could
    # consume the entire ten-minute T-5 window.  Keep concurrency bounded, but
    # finish T-5 rows before T-30/first-look rows and commit only after every
    # result is complete.
    pending_predictions.sort(
        key=lambda job: (
            0 if job[3] == "T-5" else 1 if job[3] == "T-30" else 2,
            job[0]["kickoff"],
        )
    )
    if mode == "tick":
        payloads = [
            (
                titan, bridge, h_row, stage, config, titan_client,
                pinnapi_client, crown_snapshot, previous_crown_prices,
                entry_policies,
            )
            for (
                titan, bridge, h_row, stage, crown_snapshot,
                previous_crown_prices,
            ) in pending_predictions
        ]
        emitted: list[str] = []
        fresh_condition_predictions: list[dict[str, str]] = []
        evidence_projection_stages: list[str] = []
        retained = len(existing_predictions)

        def commit_completed(prediction: dict[str, Any]) -> None:
            nonlocal retained
            created, fresh, projected_stages, retained = _commit_stage_predictions(
                config, mode, [prediction], deadline=tick_deadline,
            )
            emitted.extend(created)
            fresh_condition_predictions.extend(fresh)
            evidence_projection_stages.extend(projected_stages)

        # NOTE (Crown T-5 deadline-first patch, stage 6 -- source-review
        # finding, verified both statically and empirically against the
        # full existing test suite): this ``commit_completed`` /
        # ``_run_tick_predictions`` call site is UNREACHABLE for
        # ``mode == "tick"``. Earlier in this same branch of ``run()``,
        # ``mode == "tick"`` always returns before this point -- either via
        # the ``fast_noop`` short-circuit when ``_tick_rows_from_predictions``
        # finds nothing due, or via ``_run_local_bulk_timed_stages`` when it
        # finds something due. ``titan_rows`` is reassigned only inside
        # ``if sweep_mode:`` (sweep/round-update/first-look-reconcile),
        # never for tick, so ``pending_predictions``/``payloads`` stay empty
        # for every ``mode == "tick"`` invocation and this callback never
        # fires. This is confirmed pre-existing at Stage 5 (`2f21f4d`) and at
        # the original base (`f11bafedf2db70ad83a5096a8750544b91e0f486`) --
        # not introduced by this patch series. See
        # ``CROWN_STAGE6_CRITICAL_FINDING_20260824.md`` and
        # ``crown/tests/test_native_stage_cutover_callsite.py``'s
        # control-flow regression test for the full analysis and proof.
        # Deliberately left UNWIRED to ``native_stage_cutover`` here: wiring
        # dead code would misrepresent it as live durability protection,
        # which is the exact failure mode this stage exists to avoid.
        runtime = _run_tick_predictions(
            payloads, tick_deadline if tick_deadline is not None else time.monotonic(),
            commit_completed,
        )
        return {
            "ok": True, "mode": mode, "predictions": runtime["completed"],
            "retained_predictions": retained, "simulations_created": len(emitted),
            "mapping": mapping, "pinnapi_fixtures": len(pinnapi_rows),
            "pinnapi_fixture_status": pinnapi_fixture_status,
            "titan_fixtures": len(titan_rows), "hkjc_fixtures": len(h_events),
            "fresh_condition_predictions": fresh_condition_predictions,
            "evidence_projection_stages": evidence_projection_stages,
            "fresh_t5_predictions": [
                item["match_id"] for item in fresh_condition_predictions
                if item["stage"] == "T-5"
            ],
            "deadline_seconds": _tick_pass_deadline_seconds(),
            "deferred_predictions": runtime["deferred"],
            "failed_predictions": runtime["failed"],
        }
    if pending_predictions:
        with ThreadPoolExecutor(max_workers=min(10, len(pending_predictions))) as pool:
            futures = {
                pool.submit(
                    _prediction,
                    titan,
                    bridge,
                    h_row,
                    stage,
                    config,
                    titan_client,
                    pinnapi_client,
                    crown_snapshot,
                    previous_crown_prices,
                    entry_policies,
                ): str(titan["id"])
                for (
                    titan, bridge, h_row, stage, crown_snapshot,
                    previous_crown_prices,
                ) in pending_predictions
            }
            completed: dict[str, dict[str, Any]] = {}
            for future in as_completed(futures):
                completed[futures[future]] = future.result()
        for titan, _bridge, _h_row, _stage, _snapshot, _previous in pending_predictions:
            prediction = completed[str(titan["id"])]
            if mode == "first-look-reconcile":
                # The audit label describes why this *new* immutable
                # first-look was collected.  It is attached before the stage
                # snapshot is committed and does not rewrite existing cards.
                prediction["origin"] = "hourly_first_look_reconciliation"
                snapshot = _snapshot or {}
                if not snapshot.get("asian_ok") and not snapshot.get("total_ok"):
                    prediction["status"] = "DATA_MISSING"
                    prediction["verdict"] = "原生皇冠盤暫不可用"
                    prediction["native_snapshot_status"] = "DATA_MISSING"
                    prediction["native_snapshot_reason"] = "native_crown_quote_unavailable"
            stage_predictions.append(prediction)
            # The dashboard card keeps all completed stages while the top-level
            # fields remain the latest stage snapshot.  This also survives a
            # later empty tick through merge_predictions().
            prediction["stages"] = list(
                ledger["watch"].get(str(titan["id"]), {}).get("stages") or []
            )
            predictions.append(prediction)
    with state_lock(config):
        # Reload the latest state because another provider pass may have
        # committed while this one was fetching quotes.
        ledger = load_ledger(config)
        emitted: list[str] = []
        # These are IDs only, collected after a T-5 snapshot has been newly
        # persisted.  The caller passes them to notifications; it never scans
        # old cards/history after a deploy.
        fresh_condition_predictions: list[dict[str, str]] = []
        evidence_projection_stages: list[str] = []
        for prediction in stage_predictions:
            kickoff = datetime.fromisoformat(str(prediction["kickoff_hkt"]))
            if kickoff.tzinfo is None:
                kickoff = kickoff.replace(tzinfo=HKT)
            if kickoff <= datetime.now(HKT):
                # A quote request admitted just before kickoff may return after
                # the match starts.  It must never become a T-5 bet.
                continue
            stage = str(prediction.get("stage") or "")
            match_id = str(prediction.get("match_id") or "")
            prior_stage = any(
                row.get("stage") == stage
                for row in ((ledger.get("watch") or {}).get(match_id, {}).get("stages") or [])
                if isinstance(row, dict)
            )
            emitted += sync_prediction(
                ledger, prediction, config, defer_formal_admission=True,
            )
            prediction["stages"] = list(
                ledger["watch"].get(str(prediction["match_id"]), {}).get("stages") or []
            )
            if stage in {"首預", "T-30", "T-5"}:
                evidence_projection_stages.append(stage)
            if stage in {"T-30", "T-5"} and (
                not prior_stage or (stage == "T-5" and bool(emitted))
            ) and any(
                row.get("stage") == stage for row in prediction["stages"]
                if isinstance(row, dict)
            ):
                fresh_condition_predictions.append({"match_id": match_id, "stage": stage})
        ledger["log"].append({"ts": iso_hkt(), "kind": mode, "n_changes": len(emitted),
                              "changes": emitted or ["今次無模擬注動作"], "simulation_only": True})
        ledger["log"] = ledger["log"][-100:]
        save_ledger(config, ledger)
        retained = merge_predictions(config, predictions)
    emitted += _drain_pending_formal_admissions(config)
    if mode not in {"tick", "first-look-reconcile"}:
        schedule_footbreak_execution_evidence_projection(
            config, evidence_projection_stages,
        )
    return {"ok": True, "mode": mode, "predictions": len(predictions), "retained_predictions": len(retained),
            "simulations_created": len(emitted), "mapping": mapping,
            "pinnapi_fixtures": len(pinnapi_rows),
            "pinnapi_fixture_status": pinnapi_fixture_status,
            "titan_fixtures": len(titan_rows), "hkjc_fixtures": len(h_events),
            "reconciled_hkjc_identities": reconciled_hkjc_identities,
            "fresh_condition_predictions": fresh_condition_predictions,
            "evidence_projection_stages": evidence_projection_stages,
            # Compatibility only; notification dispatch uses the explicit
            # stage list above and never scans historical cards.
            "fresh_t5_predictions": [item["match_id"] for item in fresh_condition_predictions if item["stage"] == "T-5"]}
