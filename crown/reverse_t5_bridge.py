"""Durable, post-commit Crown T-5 to HKJC public-board bridge.

The deadline-owned native T-5 transaction only appends a tiny idempotent job.
An existing non-deadline Crown server pass later drains that job in a separately
bounded child.  Provider I/O, fixture matching, formal-condition evaluation,
and counterpart selection all run from an immutable snapshot *outside* the
shared native state lock.  The final lock reacquires only long enough to
revalidate the exact persisted T-5 evidence and merge isolated outcomes.
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

from .common import HKT, iso_hkt, parse_time
from .config import Settings
from .hkjc import event_from_match, fetch_matches
from .matching import Event, same_event_for_hkjc
from .state import load_ledger, save_ledger, state_lock

ENV_ENABLED = "CROWN_REVERSE_T5_BRIDGE_ENABLED"
JOB_NAMESPACE = "crown_reverse_t5_bridge"
_JOB_LIMIT = 1600
_MAX_DRAIN_JOBS = 12
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
    fixture = str(job.get("match_id") or "")
    watch = (ledger.get("watch") or {}).get(fixture)
    if not isinstance(watch, dict):
        return None, "native_watch_missing"
    if str(job.get("stage") or "") != "T-5":
        return None, "native_job_stage_invalid"
    fingerprint = t5_evidence_fingerprint(watch)
    if not fingerprint or fingerprint != str(job.get("t5_evidence_fingerprint") or ""):
        return None, "native_t5_evidence_changed_or_missing"
    kickoff = parse_time(watch.get("kickoff_hkt") or watch.get("kickoff"))
    if kickoff is None:
        return None, "native_kickoff_missing"
    if kickoff <= datetime.now(HKT):
        return None, "native_t5_job_expired"
    return watch, None


def _append_attempt(job: dict[str, Any], state: str, reason: str | None = None) -> None:
    attempts = job.setdefault("attempts", [])
    if not isinstance(attempts, list):
        attempts = job["attempts"] = []
    attempts.append({"at": iso_hkt(), "state": state, "reason": reason})
    job["attempts"] = attempts[-24:]


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


def _claim_snapshot(config: Settings, *, max_jobs: int) -> list[dict[str, Any]]:
    """Take only compact immutable input copies while briefly holding state."""
    with state_lock(config, timeout_seconds=0.20) as acquired:
        if not acquired:
            return []
        ledger = load_ledger(config)
        ns = _ensure_jobs(ledger)
        claimed: list[dict[str, Any]] = []
        changed = False
        for job in ns["jobs"]:
            if len(claimed) >= max_jobs or not isinstance(job, dict):
                continue
            if str(job.get("state") or "") not in {"PENDING", "RUNNING"}:
                continue
            watch, reason = _job_is_current(ledger, job)
            if watch is None:
                job["state"] = "EXPIRED" if reason == "native_t5_job_expired" else "CANCELLED"
                job["completed_at"] = iso_hkt()
                _append_attempt(job, job["state"], reason)
                changed = True
                continue
            # A terminated prior worker leaves RUNNING behind.  The separate
            # drain lock proves no live bridge worker can be concurrent here,
            # so it is safely recovered as a new attempt.
            job["state"] = "RUNNING"
            job["claimed_at"] = iso_hkt()
            _append_attempt(job, "RUNNING", None)
            claimed.append({
                "job": copy.deepcopy(job),
                "watch": copy.deepcopy(watch),
                # Formal condition matching needs only immutable Wilson state;
                # no native top-level mutable portfolio data is copied/mutated.
                "wilson_validation": copy.deepcopy(ledger.get("wilson_validation") or {}),
            })
            changed = True
        if changed:
            save_ledger(config, ledger)
    return claimed


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
    return rows, [event for event in events if event is not None], iso_hkt()


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


def _merge_completed(config: Settings, outcomes: list[tuple[dict[str, Any], dict[str, Any]]]) -> int:
    """Revalidate T-5 evidence and atomically merge already-computed outcomes."""
    with state_lock(config, timeout_seconds=0.20) as acquired:
        if not acquired:
            return 0
        ledger = load_ledger(config)
        ns = _ensure_jobs(ledger)
        jobs = {str(job.get("job_id") or ""): job for job in ns["jobs"] if isinstance(job, dict)}
        completed = 0
        changed = False
        for claimed, outcome in outcomes:
            job_id = str(claimed.get("job_id") or "")
            job = jobs.get(job_id)
            if not isinstance(job, dict) or str(job.get("state") or "") != "RUNNING":
                continue
            _watch, reason = _job_is_current(ledger, job)
            if _watch is None:
                job["state"] = "EXPIRED" if reason == "native_t5_job_expired" else "CANCELLED"
                job["completed_at"] = iso_hkt()
                _append_attempt(job, job["state"], reason)
                changed = True
                continue
            _merge_isolated_namespace(ledger, outcome)
            job["state"] = "COMPLETED"
            job["completed_at"] = iso_hkt()
            _append_attempt(job, "COMPLETED", None)
            completed += 1
            changed = True
        if changed:
            save_ledger(config, ledger)
        return completed


def drain_pending_jobs(config: Settings, *, max_jobs: int = _MAX_DRAIN_JOBS) -> dict[str, int]:
    """Drain durable jobs from a bounded, killable post-commit child.

    Callers must put this function behind a killable outer process.  It owns no
    native-stage work and holds native state only for two brief
    snapshot/revalidation merges.
    """
    if not is_enabled():
        return {"claimed": 0, "completed": 0}
    with _drain_lock(config) as acquired:
        if not acquired:
            return {"claimed": 0, "completed": 0}
        snapshots = _claim_snapshot(config, max_jobs=max(1, min(_MAX_DRAIN_JOBS, max_jobs)))
        if not snapshots:
            return {"claimed": 0, "completed": 0}
        matches, events, observed_at = _fetch_board()
        completed = 0
        for item in snapshots:
            outcome = _evaluate_snapshot(item, matches, events, observed_at)
            # Commit progress after each detached evaluation.  A bounded parent
            # may terminate this worker while a later fixture is slow; earlier
            # completed evidence must remain durable while untouched RUNNING
            # jobs stay safely reclaimable on the next pass.
            completed += _merge_completed(config, [(item["job"], outcome)])
        return {"claimed": len(snapshots), "completed": completed}
