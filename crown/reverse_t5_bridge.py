"""Durable, post-commit Crown T-5 to HKJC public-board bridge.

The deadline-owned native T-5 transaction only appends a tiny idempotent job.
A dedicated non-deadline Crown server worker drains that job in a separately
bounded child; sweep retains only safe recovery coverage.  Provider I/O,
fixture matching, formal-condition evaluation, and counterpart selection all
run from an immutable snapshot *outside* the shared native state lock.  The
final lock reacquires only long enough to revalidate the exact persisted T-5
evidence and merge isolated outcomes.
"""
from __future__ import annotations

import copy
import fcntl
import hashlib
import json
import os
from contextlib import contextmanager
from datetime import datetime
from pathlib import Path
from typing import Any, Iterable

from .common import HKT, iso_hkt, parse_time, read_json, write_json_atomic
from .config import Settings
from .hkjc import event_from_match, fetch_matches
from .matching import Event, same_event_for_hkjc
from .state import load_ledger, save_ledger, state_lock

ENV_ENABLED = "CROWN_REVERSE_T5_BRIDGE_ENABLED"
JOB_NAMESPACE = "crown_reverse_t5_bridge"
_JOB_LIMIT = 1600
# T-5 comparison evidence is useful only close to the committed native stage.
# 60 seconds permits the 30-second server cadence, one 15-second service
# budget, and ordinary scheduler jitter without allowing a boot catch-up or a
# repeatedly stalled queue to pair a stale native signal with a current board.
MAX_STAGE_AGE_SECONDS = 60
# A drain may visit at most the retained job window.  This is a defensive
# corruption guard, not a service-throughput policy: the 15-second killable
# parent is the real wall-clock bound and fast supported fixture batches must
# not be split by an arbitrary small count.
_REMOTE_TIMEOUT_SECONDS = 1.0
_SELLABLE_STATUSES = {"SELLING", "SELLINGSTARTED", "AVAILABLE"}
_MARKETS = ("HDC", "HIL", "CHL")
_SIDES = {"HDC": {"H", "A"}, "HIL": {"H", "L"}, "CHL": {"H", "L"}}


def is_enabled() -> bool:
    """Default-off rollout switch; enqueue/drain are both inert when disabled."""
    return os.getenv(ENV_ENABLED, "0").strip().lower() in {"1", "true", "yes", "on"}


def _canonical(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), default=str)


def _number(value: Any) -> float | None:
    try:
        value = float(value)
    except (TypeError, ValueError):
        return None
    return value if value == value and value not in {float("inf"), float("-inf")} else None


def _t5_stage(watch: dict[str, Any]) -> dict[str, Any] | None:
    rows = [
        row for row in (watch.get("stages") or [])
        if isinstance(row, dict) and str(row.get("stage") or "") == "T-5"
    ]
    return rows[0] if len(rows) == 1 else None


def t5_evidence_fingerprint(watch: dict[str, Any]) -> str | None:
    """Stable identity for one immutable native T-5 evidence version."""
    stage = _t5_stage(watch)
    fixture = str(watch.get("match_id") or "").strip()
    kickoff = watch.get("kickoff_hkt") or watch.get("kickoff")
    if not fixture or stage is None or parse_time(kickoff) is None:
        return None
    evidence = {
        "fixture": fixture,
        "kickoff": str(kickoff),
        "stage": copy.deepcopy(stage),
    }
    return hashlib.sha256(_canonical(evidence).encode("utf-8")).hexdigest()


def _ensure_jobs(ledger: dict[str, Any]) -> dict[str, Any]:
    ns = ledger.setdefault(JOB_NAMESPACE, {})
    if not isinstance(ns, dict):
        raise ValueError("reverse T-5 bridge namespace must be an object")
    ns.setdefault("schema_version", 2)
    ns.setdefault("jobs", [])
    if not isinstance(ns["jobs"], list):
        raise ValueError("reverse T-5 bridge jobs must be an array")
    return ns


def enqueue_committed_t5(ledger: dict[str, Any], watch: dict[str, Any]) -> bool:
    """Atomically append the tiny retryable job beside the native T-5 commit.

    This function performs no provider work, matching, formal evaluation, or
    notification work.  It is intentionally safe to call while the native
    commit owns the shared state lock.
    """
    if not is_enabled() or not isinstance(watch, dict):
        return False
    fingerprint = t5_evidence_fingerprint(watch)
    fixture = str(watch.get("match_id") or "").strip()
    stage = _t5_stage(watch)
    if not fingerprint or not fixture or stage is None:
        return False
    kickoff = watch.get("kickoff_hkt") or watch.get("kickoff")
    job_id = f"reverse-t5:{fixture}:{fingerprint}"
    ns = _ensure_jobs(ledger)
    for existing in ns["jobs"]:
        if isinstance(existing, dict) and str(existing.get("job_id") or "") == job_id:
            return False
    ns["jobs"].append({
        "job_id": job_id,
        "match_id": fixture,
        "stage": "T-5",
        "t5_evidence_fingerprint": fingerprint,
        "stage_at": stage.get("ts") or stage.get("source_snapshot_at"),
        "kickoff": kickoff,
        "state": "PENDING",
        "created_at": iso_hkt(),
        "attempts": [],
    })
    ns["jobs"] = ns["jobs"][-_JOB_LIMIT:]
    return True


def _sellable(value: Any) -> bool:
    return str(value or "").strip().upper() in _SELLABLE_STATUSES


def _board_quotes(match: dict[str, Any], *, observed_at: str) -> tuple[list[dict[str, Any]], str | None]:
    """Normalize only explicitly active public-board market lines.

    The underlying compatibility flattener deliberately retains non-selling
    data for display.  This execution bridge instead reads the raw pool/line
    statuses and fails closed on closed, suspended, or unknown availability.
    """
    match_id = str(match.get("id") or match.get("frontEndId") or "").strip()
    quotes: list[dict[str, Any]] = []
    rejected_status = False
    for pool in match.get("foPools") or []:
        if not isinstance(pool, dict):
            continue
        market = str(pool.get("oddsType") or "").upper()
        if market not in _MARKETS:
            continue
        if not _sellable(pool.get("status")):
            rejected_status = True
            continue
        for raw_line in pool.get("lines") or []:
            if not isinstance(raw_line, dict):
                continue
            if not _sellable(raw_line.get("status")):
                rejected_status = True
                continue
            asian_line = _number(raw_line.get("condition"))
            if asian_line is None:
                continue
            for combination in raw_line.get("combinations") or []:
                if not isinstance(combination, dict):
                    continue
                selections = combination.get("selections") or [{}]
                selection = selections[0] if isinstance(selections, list) and selections else {}
                side = str((selection or {}).get("str") or combination.get("str") or "").upper()
                if side not in _SIDES[market]:
                    continue
                quotes.append({
                    "code": market, "side": side, "line": asian_line,
                    "odds": combination.get("currentOdds"),
                    "source": "hkjc_public_board", "observed_at": observed_at,
                    "fixture_identity": {"hkjc_match_id": match_id},
                })
    return quotes, ("hkjc_pool_or_line_not_sellable" if rejected_status else None)


def _target(watch: dict[str, Any]) -> Event | None:
    kickoff = parse_time(watch.get("kickoff_hkt") or watch.get("kickoff"))
    fixture = str(watch.get("match_id") or "").strip()
    if not fixture or kickoff is None:
        return None
    return Event(
        fixture, str(watch.get("league") or ""), str(watch.get("home") or ""),
        str(watch.get("away") or ""), kickoff, None,
    )


def _job_is_current(ledger: dict[str, Any], job: dict[str, Any]) -> tuple[dict[str, Any] | None, str | None]:
    return _job_is_current_at(ledger, job)


def _job_is_current_at(
    ledger: dict[str, Any], job: dict[str, Any], *, now: datetime | None = None,
) -> tuple[dict[str, Any] | None, str | None]:
    """Validate immutable native evidence and the strict reverse-T-5 age cap."""
    fixture = str(job.get("match_id") or "")
    watch = (ledger.get("watch") or {}).get(fixture)
    if not isinstance(watch, dict):
        return None, "native_watch_missing"
    if str(job.get("stage") or "") != "T-5":
        return None, "native_job_stage_invalid"
    fingerprint = t5_evidence_fingerprint(watch)
    if not fingerprint or fingerprint != str(job.get("t5_evidence_fingerprint") or ""):
        return None, "native_t5_evidence_changed_or_missing"
    now = now or datetime.now(HKT)
    stage_at = parse_time(job.get("stage_at"))
    # Missing/unparseable timestamps cannot prove a fresh T-5 observation and
    # fail closed using the same durable reason as an over-age bridge job.
    if stage_at is None or (now - stage_at).total_seconds() > MAX_STAGE_AGE_SECONDS:
        return None, "reverse_t5_stage_too_old"
    kickoff = parse_time(watch.get("kickoff_hkt") or watch.get("kickoff"))
    if kickoff is None:
        return None, "native_kickoff_missing"
    if kickoff <= now:
        return None, "native_t5_job_expired"
    return watch, None


def _append_attempt(job: dict[str, Any], state: str, reason: str | None = None) -> None:
    attempts = job.setdefault("attempts", [])
    if not isinstance(attempts, list):
        attempts = job["attempts"] = []
    attempts.append({"at": iso_hkt(), "state": state, "reason": reason})
    job["attempts"] = attempts[-24:]


def _terminal_state(reason: str | None) -> str:
    return (
        "EXPIRED"
        if reason in {"native_t5_job_expired", "reverse_t5_stage_too_old"}
        else "CANCELLED"
    )


def _terminalize(job: dict[str, Any], reason: str | None) -> None:
    job["state"] = _terminal_state(reason)
    job["completed_at"] = iso_hkt()
    _append_attempt(job, job["state"], reason)


def worker_telemetry_path(config: Settings) -> Path:
    """Return the local-only bounded worker health record path."""
    return Path(config.state_dir) / "reverse-t5-bridge-health.json"


def _oldest_retryable_stage_at(ledger: dict[str, Any]) -> str | None:
    ns = ledger.get(JOB_NAMESPACE) if isinstance(ledger, dict) else None
    jobs = ns.get("jobs") if isinstance(ns, dict) else None
    values = [
        str(job.get("stage_at"))
        for job in jobs or []
        if isinstance(job, dict)
        and str(job.get("state") or "") in {"PENDING", "RUNNING"}
        and parse_time(job.get("stage_at")) is not None
    ]
    return min(values) if values else None


def _record_worker_telemetry(
    config: Settings, *, status: str, claimed: int | None = None,
    completed: int | None = None,
) -> None:
    """Persist compact liveness only; never include provider or fixture data."""
    path = worker_telemetry_path(config)
    current = read_json(path, {})
    data = current if isinstance(current, dict) else {}
    at = iso_hkt()
    data["last_status"] = status
    if status == "started":
        data["last_started"] = at
    elif status == "timeout":
        data["last_timeout"] = at
        try:
            previous_timeouts = int(data.get("consecutive_timeouts") or 0)
        except (TypeError, ValueError):
            previous_timeouts = 0
        data["consecutive_timeouts"] = max(0, previous_timeouts) + 1
    else:
        data["last_completed"] = at
        data["consecutive_timeouts"] = 0
    if claimed is not None:
        data["claimed"] = int(claimed)
    if completed is not None:
        data["completed"] = int(completed)
    try:
        data["oldest_pending_stage_at"] = _oldest_retryable_stage_at(
            load_ledger(config),
        )
        write_json_atomic(path, data)
    except OSError:
        # Telemetry must not alter durable bridge retry semantics.
        pass


def record_worker_timeout(config: Settings) -> None:
    """Record an outer-child timeout after the parent has reaped that child."""
    _record_worker_telemetry(config, status="timeout")


@contextmanager
def _drain_lock(config: Settings):
    """Serialize bridge drains without ever contending for the native lock."""
    config.state_dir.mkdir(parents=True, exist_ok=True)
    path = Path(config.state_dir) / ".reverse-t5-bridge-drain.lock"
    with path.open("a+", encoding="utf-8") as handle:
        try:
            fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            yield False
            return
        try:
            yield True
        finally:
            fcntl.flock(handle.fileno(), fcntl.LOCK_UN)


def _last_attempt_at(job: dict[str, Any]) -> datetime | None:
    values = [
        parse_time(row.get("at"))
        for row in job.get("attempts") or []
        if isinstance(row, dict)
    ]
    return max((value for value in values if value is not None), default=None)


def _claim_one_snapshot(
    config: Settings, *, now: datetime | None = None,
    board_observed_at: datetime | None = None,
) -> dict[str, Any] | None:
    """Fairly claim one current job whose native stage predates this board."""
    now = now or datetime.now(HKT)
    with state_lock(config, timeout_seconds=0.20) as acquired:
        if not acquired:
            return None
        ledger = load_ledger(config)
        ns = _ensure_jobs(ledger)
        candidates: list[tuple[tuple[bool, datetime, str], dict[str, Any], dict[str, Any]]] = []
        changed = False
        for job in ns["jobs"]:
            if not isinstance(job, dict):
                continue
            if str(job.get("state") or "") not in {"PENDING", "RUNNING"}:
                continue
            watch, reason = _job_is_current_at(ledger, job, now=now)
            if watch is None:
                _terminalize(job, reason)
                changed = True
                continue
            stage_at = parse_time(job.get("stage_at"))
            # A job that arrived while the shared board request was in flight
            # must wait for the next board.  It may not evaluate a quote that
            # was observed before its committed native T-5 evidence.
            if board_observed_at is not None and (
                stage_at is None or stage_at > board_observed_at
            ):
                continue
            # Reaped RUNNING rows are retryable.  Least-recently-attempted
            # ordering prevents a repeatedly slow early row from permanently
            # starving later PENDING/RUNNING fixtures.
            attempted = _last_attempt_at(job)
            candidates.append((
                (
                    attempted is not None,
                    attempted or datetime.min.replace(tzinfo=HKT),
                    str(job.get("job_id") or ""),
                ),
                job,
                watch,
            ))
        snapshot = None
        if candidates:
            _sort, job, watch = min(candidates, key=lambda item: item[0])
            job["state"] = "RUNNING"
            job["claimed_at"] = iso_hkt()
            _append_attempt(job, "RUNNING", None)
            snapshot = {
                "job": copy.deepcopy(job),
                "watch": copy.deepcopy(watch),
                # Formal condition matching needs only immutable Wilson state;
                # no native top-level mutable portfolio data is copied/mutated.
                "wilson_validation": copy.deepcopy(ledger.get("wilson_validation") or {}),
            }
            changed = True
        if changed:
            save_ledger(config, ledger)
    return snapshot


def _prepare_eligible_jobs(config: Settings, *, now: datetime | None = None) -> bool:
    """Terminalize stale rows and report whether a current row merits one fetch.

    This short state transaction is intentionally not a claim.  It lets a
    no-job or boot-catch-up invocation avoid provider I/O while preserving the
    strict age check immediately before the one shared board request.
    """
    now = now or datetime.now(HKT)
    with state_lock(config, timeout_seconds=0.20) as acquired:
        if not acquired:
            return False
        ledger = load_ledger(config)
        ns = _ensure_jobs(ledger)
        changed = False
        eligible = False
        for job in ns["jobs"]:
            if not isinstance(job, dict):
                continue
            if str(job.get("state") or "") not in {"PENDING", "RUNNING"}:
                continue
            _watch, reason = _job_is_current_at(ledger, job, now=now)
            if _watch is None:
                _terminalize(job, reason)
                changed = True
            else:
                eligible = True
        if changed:
            save_ledger(config, ledger)
        return eligible


def _fetch_board() -> tuple[list[dict[str, Any]], list[Event], str]:
    previous_timeout = os.environ.get("FOOTBREAK_REMOTE_TIMEOUT_SECONDS")
    os.environ["FOOTBREAK_REMOTE_TIMEOUT_SECONDS"] = str(_REMOTE_TIMEOUT_SECONDS)
    try:
        matches = fetch_matches()
    finally:
        if previous_timeout is None:
            os.environ.pop("FOOTBREAK_REMOTE_TIMEOUT_SECONDS", None)
        else:
            os.environ["FOOTBREAK_REMOTE_TIMEOUT_SECONDS"] = previous_timeout
    rows = [row for row in matches if isinstance(row, dict)]
    events = [event_from_match(row) for row in rows]
    # Keep microseconds here: the board ordering guard must not turn a T-5
    # committed later in the same wall-clock second into a pre-capture quote.
    return rows, [event for event in events if event is not None], datetime.now(HKT).isoformat()


def _evaluate_snapshot(snapshot: dict[str, Any], matches: list[dict[str, Any]], events: list[Event], observed_at: str) -> dict[str, Any]:
    """Do all matching and bilateral evaluation on a detached local copy."""
    from . import hkjc_execution_test as reciprocal

    job, watch = snapshot["job"], snapshot["watch"]
    fixture = str(job.get("match_id") or "")
    working = {
        "watch": {fixture: copy.deepcopy(watch)},
        "wilson_validation": copy.deepcopy(snapshot.get("wilson_validation") or {}),
    }
    ns = reciprocal.ensure_namespace(working)
    target = _target(watch)
    if target is None:
        reciprocal.record_research_observation(
            ns, fixture=fixture, market="*", reason="native_fixture_identity_invalid",
            stage_at=job.get("stage_at"), captured_at=observed_at,
        )
        return ns
    matched = same_event_for_hkjc(target, events)
    if matched.event is None or matched.reversed:
        reciprocal.record_research_observation(
            ns, fixture=fixture, market="*",
            reason=f"hkjc_fixture_strict_identity_{matched.reason or 'unavailable'}",
            stage_at=job.get("stage_at"), captured_at=observed_at,
        )
        return ns
    hkjc_id = str(matched.event.id)
    existing = str(watch.get("hkjc_match_id") or "").strip()
    if existing and existing != hkjc_id:
        reciprocal.record_research_observation(
            ns, fixture=fixture, market="*", reason="hkjc_fixture_identity_conflicts_with_durable_mapping",
            hkjc_match_id=existing, stage_at=job.get("stage_at"), captured_at=observed_at,
        )
        return ns
    board = next((row for row in matches if str(row.get("id") or row.get("frontEndId") or "") == hkjc_id), None)
    if not isinstance(board, dict):
        reciprocal.record_research_observation(
            ns, fixture=fixture, market="*", reason="hkjc_strict_match_raw_board_missing",
            hkjc_match_id=hkjc_id, stage_at=job.get("stage_at"), captured_at=observed_at,
        )
        return ns
    quotes, unavailable_reason = _board_quotes(board, observed_at=observed_at)
    evaluation_watch = {**watch, "hkjc_match_id": hkjc_id}
    reciprocal.evaluate_new_t5(
        working, evaluation_watch, ranking=[], counterpart_quotes=quotes,
        counterpart_captured_at=observed_at, require_complete_history=True,
        record_native_observation=False,
        counterpart_unavailable_reason=unavailable_reason,
    )
    return working[reciprocal.NAMESPACE]


def _merge_isolated_namespace(ledger: dict[str, Any], outcome: dict[str, Any]) -> None:
    """Merge only idempotent bilateral/research rows; never touch native state.

    The bilateral contract owns its own 4,000-row retention rule.  This merge
    must not apply the bridge's smaller job/research cap, because that could
    silently discard pending outbox deliveries or valid historical decisions.
    It therefore only bounds research-only observations and leaves bilateral
    attempts, decisions, outbox rows, and isolated simulation bets intact.
    """
    from . import hkjc_execution_test as reciprocal

    current = reciprocal.ensure_namespace(ledger)
    for key, identity, limit in (
        ("research_observations", "fingerprint", _JOB_LIMIT),
        ("counterpart_attempts", "fingerprint", None),
        ("decisions", "decision_id", None),
        ("decision_outbox", "outbox_id", None),
        ("bets", "bet_id", None),
    ):
        existing = {
            str(row.get(identity) or "") for row in current.get(key) or []
            if isinstance(row, dict) and str(row.get(identity) or "")
        }
        for row in outcome.get(key) or []:
            value = str(row.get(identity) or "") if isinstance(row, dict) else ""
            if value and value not in existing:
                current[key].append(copy.deepcopy(row))
                existing.add(value)
        if limit is not None:
            current[key] = current[key][-limit:]


def _merge_completed(
    config: Settings, claimed: dict[str, Any], outcome: dict[str, Any],
    *, now: datetime | None = None,
) -> int:
    """Revalidate T-5 evidence and atomically merge already-computed outcomes."""
    with state_lock(config, timeout_seconds=0.20) as acquired:
        if not acquired:
            return 0
        ledger = load_ledger(config)
        ns = _ensure_jobs(ledger)
        jobs = {str(job.get("job_id") or ""): job for job in ns["jobs"] if isinstance(job, dict)}
        changed = False
        job_id = str(claimed.get("job_id") or "")
        job = jobs.get(job_id)
        completed = 0
        if isinstance(job, dict) and str(job.get("state") or "") == "RUNNING":
            # This second age check is deliberately immediately before the
            # isolated merge.  A slow board/matcher may cross 60 seconds after
            # the pre-fetch claim, in which case no decision/outbox/bet is
            # created from stale T-5 evidence.
            _watch, reason = _job_is_current_at(ledger, job, now=now)
            if _watch is None:
                _terminalize(job, reason)
                changed = True
            else:
                _merge_isolated_namespace(ledger, outcome)
                job["state"] = "COMPLETED"
                job["completed_at"] = iso_hkt()
                _append_attempt(job, "COMPLETED", None)
                completed = 1
                changed = True
            changed = True
        if changed:
            save_ledger(config, ledger)
        return completed


def drain_pending_jobs(
    config: Settings, *, max_jobs: int = _JOB_LIMIT,
) -> dict[str, int]:
    """Drain durable jobs from a bounded, killable post-commit child.

    Callers must put this function behind a killable outer process.  One HKJC
    board is fetched per eligible invocation, then jobs already covered by
    that immutable board are fairly claimed, evaluated, and merged one at a
    time until the queue is exhausted or the outer service parent reaps the
    child.  ``_JOB_LIMIT`` is only a defensive retained-state ceiling; normal
    throughput is bounded by the service wall clock, never a small job count.
    """
    if not is_enabled():
        return {"claimed": 0, "completed": 0}
    try:
        limit = max(1, min(int(max_jobs), _JOB_LIMIT))
    except (TypeError, ValueError):
        limit = _JOB_LIMIT
    with _drain_lock(config) as acquired:
        if not acquired:
            return {"claimed": 0, "completed": 0}
        _record_worker_telemetry(config, status="started")
        result = {"claimed": 0, "completed": 0}
        # Check freshness immediately before the single provider request.  A
        # stale-only queue is terminalized without any remote board fetch.
        if not _prepare_eligible_jobs(config):
            _record_worker_telemetry(config, status="complete", **result)
            return result
        matches, events, observed_at = _fetch_board()
        board_observed_at = parse_time(observed_at)
        if board_observed_at is None:
            # _fetch_board owns this local timestamp, but fail closed rather
            # than letting an unparseable capture label pre-stage quotes.
            _record_worker_telemetry(config, status="complete", **result)
            return result
        for _ in range(limit):
            snapshot = _claim_one_snapshot(
                config, board_observed_at=board_observed_at,
            )
            if snapshot is None:
                break
            result["claimed"] += 1
            # Explicitly recheck the age before any remote I/O, even though
            # claim validated it.  This protects a process that was
            # descheduled between the short state transaction and the board
            # request.
            ledger = load_ledger(config)
            _watch, _reason = _job_is_current_at(ledger, snapshot["job"])
            if _watch is None:
                result["completed"] += _merge_completed(
                    config, snapshot["job"], {}, now=datetime.now(HKT),
                )
                continue
            outcome = _evaluate_snapshot(snapshot, matches, events, observed_at)
            result["completed"] += _merge_completed(config, snapshot["job"], outcome)
        _record_worker_telemetry(config, status="complete", **result)
        return result
