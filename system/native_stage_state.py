"""Minimal durable Footbreak-native scheduling evidence.

This module deliberately has no provider, Crown, Telegram, betting, dashboard, or
consumer imports.  It owns only local manifest/attempt metadata that wraps the
existing HKJC native prediction snapshots.
"""
from __future__ import annotations

from collections import Counter
from datetime import datetime, timedelta, timezone
from typing import Any
import uuid

HKT = timezone(timedelta(hours=8))
UTC = timezone.utc
SCHEDULED_STAGES: tuple[tuple[str, int], ...] = (("T-30", 30), ("T-5", 5))
TERMINAL = frozenset({"COMMITTED", "FAILED", "DATA_MISSING", "EXPIRED"})


def parse_time(value: Any) -> datetime | None:
    try:
        parsed = datetime.fromisoformat(str(value or "").replace("Z", "+00:00"))
    except (TypeError, ValueError):
        return None
    return parsed.replace(tzinfo=HKT) if parsed.tzinfo is None else parsed


def iso_utc(value: datetime) -> str:
    return value.astimezone(UTC).isoformat()


def iso_hkt(value: datetime) -> str:
    return value.astimezone(HKT).isoformat()


def _identity(watch: dict[str, Any]) -> tuple[str, datetime] | None:
    match_id = str(watch.get("match_id") or "").strip()
    kickoff = parse_time(watch.get("kickoff_at_utc") or watch.get("kickoff") or watch.get("kickoff_hkt"))
    return (match_id, kickoff) if match_id and kickoff is not None else None


def _manifest(watch: dict[str, Any]) -> dict[str, Any] | None:
    value = watch.get("native_stage_manifest")
    return value if isinstance(value, dict) else None


def ensure_manifest(
    watch: dict[str, Any], *, origin: str, now: datetime | None = None,
) -> bool:
    """Create one immutable scheduling manifest for a known, future HKJC card.

    Manifest creation records metadata only; it never fabricates a T-30/T-5
    snapshot or an attempt.  A changed kickoff is not silently substituted for
    an existing identity bridge.
    """
    identity = _identity(watch)
    if identity is None:
        return False
    match_id, kickoff = identity
    current = _manifest(watch)
    kickoff_utc = iso_utc(kickoff)
    if current is not None:
        stored = current.get("identity") if isinstance(current.get("identity"), dict) else {}
        # Existing schedule identity is immutable.  A later provider change is
        # retained by native snapshot validation, never rewritten here.
        return False if (
            str(stored.get("hkjc_match_id") or "") == match_id
            and str(stored.get("kickoff_at_utc") or "") == kickoff_utc
        ) else False
    at = now or datetime.now(HKT)
    # A first-look record observed after kickoff is not a legitimate way to
    # manufacture historical timed work. It remains outside this prospective
    # scheduling state machine.
    if kickoff.astimezone(UTC) <= at.astimezone(UTC):
        return False
    jobs: dict[str, dict[str, Any]] = {}
    for stage, minutes in SCHEDULED_STAGES:
        due = kickoff.astimezone(UTC) - timedelta(minutes=minutes)
        jobs[stage] = {
            "stage": stage,
            "due_at_utc": iso_utc(due),
            "due_at_hkt": iso_hkt(due),
        }
    watch["native_stage_manifest"] = {
        "schema_version": 1,
        "origin": origin,
        "created_at": iso_hkt(at),
        "identity": {
            "hkjc_match_id": match_id,
            "kickoff_at_utc": kickoff_utc,
        },
        "kickoff_at_utc": kickoff_utc,
        "kickoff_at_hkt": iso_hkt(kickoff),
        "jobs": jobs,
    }
    return True


def ensure_first_look_manifest(watch: dict[str, Any], *, now: datetime | None = None) -> bool:
    """Atomically attach expected timed work when a genuine first look saves."""
    return ensure_manifest(watch, origin="first_look", now=now)


def migrate_future_manifests(ledger: dict[str, Any], *, now: datetime) -> int:
    """Repair only future legacy watch cards without inventing historical stages."""
    watches = ledger.get("watch") if isinstance(ledger.get("watch"), dict) else {}
    changed = 0
    for raw_id, watch in watches.items():
        if not isinstance(watch, dict) or _manifest(watch) is not None:
            continue
        if not watch.get("match_id"):
            watch["match_id"] = str(raw_id)
        identity = _identity(watch)
        if identity is None or identity[1].astimezone(UTC) <= now.astimezone(UTC):
            continue
        if ensure_manifest(watch, origin="migration_existing_future_card", now=now):
            changed += 1
    return changed


def _event_key(watch: dict[str, Any], stage: str) -> tuple[str, str, str] | None:
    manifest = _manifest(watch)
    identity = manifest.get("identity") if isinstance(manifest, dict) else None
    if not isinstance(identity, dict):
        return None
    match_id = str(identity.get("hkjc_match_id") or "")
    kickoff = str(identity.get("kickoff_at_utc") or "")
    return (match_id, kickoff, stage) if match_id and kickoff and stage in dict(SCHEDULED_STAGES) else None


def _job(watch: dict[str, Any], stage: str) -> dict[str, Any] | None:
    manifest = _manifest(watch)
    jobs = manifest.get("jobs") if isinstance(manifest, dict) else None
    value = jobs.get(stage) if isinstance(jobs, dict) else None
    return value if isinstance(value, dict) else None


def _events(ledger: dict[str, Any]) -> list[dict[str, Any]]:
    value = ledger.get("native_stage_attempts")
    if not isinstance(value, list):
        value = []
        ledger["native_stage_attempts"] = value
    return value


def latest_attempts(ledger: dict[str, Any]) -> dict[tuple[str, str, str], dict[str, Any]]:
    latest: dict[tuple[str, str, str], dict[str, Any]] = {}
    for event in ledger.get("native_stage_attempts") or []:
        if not isinstance(event, dict):
            continue
        key = (
            str(event.get("hkjc_match_id") or ""),
            str(event.get("kickoff_at_utc") or ""),
            str(event.get("stage") or ""),
        )
        if all(key):
            latest[key] = event
    return latest


def _event_payload(
    watch: dict[str, Any], stage: str, *, attempt_id: str, status: str,
    now: datetime, reason: str | None = None,
) -> dict[str, Any]:
    key = _event_key(watch, stage)
    job = _job(watch, stage)
    if key is None or job is None:
        raise ValueError("native_stage_manifest_missing_or_invalid")
    match_id, kickoff_utc, _ = key
    row = {
        "schema_version": 1,
        "attempt_id": attempt_id,
        "status": status,
        "at": iso_hkt(now),
        "hkjc_match_id": match_id,
        "kickoff_at_utc": kickoff_utc,
        "kickoff_at_hkt": str((_manifest(watch) or {}).get("kickoff_at_hkt") or ""),
        "stage": stage,
        "due_at_utc": str(job.get("due_at_utc") or ""),
        "due_at_hkt": str(job.get("due_at_hkt") or ""),
    }
    if reason:
        row["reason"] = reason
    return row


def start_attempt(
    ledger: dict[str, Any], watch: dict[str, Any], stage: str, *, now: datetime,
) -> dict[str, Any]:
    """Write one STARTED event before any provider or analysis operation.

    If a process died after the write, the same non-terminal attempt is returned
    on restart, allowing an in-window retry without inventing a second attempt.
    """
    key = _event_key(watch, stage)
    if key is None:
        raise ValueError("native_stage_manifest_missing_or_invalid")
    current = latest_attempts(ledger).get(key)
    if isinstance(current, dict):
        state = str(current.get("status") or "")
        if state == "STARTED":
            return current
        if state in TERMINAL:
            raise ValueError("native_stage_attempt_already_terminal")
    event = _event_payload(
        watch, stage, attempt_id=uuid.uuid4().hex, status="STARTED", now=now,
    )
    _events(ledger).append(event)
    return event


def finish_attempt(
    ledger: dict[str, Any], attempt: dict[str, Any], status: str, *, now: datetime,
    reason: str | None = None,
) -> dict[str, Any]:
    """Append one allowed terminal transition; events are never rewritten."""
    if status not in TERMINAL:
        raise ValueError("native_stage_terminal_status_required")
    match_id = str(attempt.get("hkjc_match_id") or "")
    kickoff_utc = str(attempt.get("kickoff_at_utc") or "")
    stage = str(attempt.get("stage") or "")
    attempt_id = str(attempt.get("attempt_id") or "")
    if not all((match_id, kickoff_utc, stage, attempt_id)):
        raise ValueError("native_stage_attempt_identity_missing")
    latest = latest_attempts(ledger).get((match_id, kickoff_utc, stage))
    if isinstance(latest, dict) and str(latest.get("status") or "") in TERMINAL:
        return latest
    event = {
        key: attempt.get(key)
        for key in (
            "schema_version", "attempt_id", "hkjc_match_id", "kickoff_at_utc",
            "kickoff_at_hkt", "stage", "due_at_utc", "due_at_hkt",
        )
    }
    event.update({"status": status, "at": iso_hkt(now)})
    if reason:
        event["reason"] = reason
    _events(ledger).append(event)
    return event


def _stage_snapshot_present(watch: dict[str, Any], stage: str) -> bool:
    return any(
        isinstance(row, dict) and str(row.get("stage") or "") == stage
        for row in (watch.get("stages") or [])
    )


def due_stage_work(
    ledger: dict[str, Any], *, now: datetime, horizon_minutes: float | None = 90.0,
) -> list[dict[str, Any]]:
    """Return only legal `due_at <= now < kickoff` native work, provider-free."""
    current = now.astimezone(UTC)
    latest = latest_attempts(ledger)
    watches = ledger.get("watch") if isinstance(ledger.get("watch"), dict) else {}
    work: list[dict[str, Any]] = []
    for raw_id, watch in watches.items():
        if not isinstance(watch, dict):
            continue
        manifest = _manifest(watch)
        identity = manifest.get("identity") if isinstance(manifest, dict) else None
        kickoff = parse_time(identity.get("kickoff_at_utc")) if isinstance(identity, dict) else None
        match_id = str(identity.get("hkjc_match_id") or raw_id) if isinstance(identity, dict) else ""
        if kickoff is None or not match_id:
            continue
        kickoff_utc = kickoff.astimezone(UTC)
        if not current < kickoff_utc:
            continue
        if horizon_minutes is not None and (kickoff_utc - current).total_seconds() > horizon_minutes * 60:
            continue
        for stage, _minutes in SCHEDULED_STAGES:
            job = _job(watch, stage)
            due_at = parse_time(job.get("due_at_utc")) if isinstance(job, dict) else None
            if due_at is None or current < due_at.astimezone(UTC):
                continue
            if _stage_snapshot_present(watch, stage):
                continue
            latest_event = latest.get((match_id, iso_utc(kickoff_utc), stage))
            if isinstance(latest_event, dict) and str(latest_event.get("status") or "") in TERMINAL:
                continue
            work.append({
                "hkjc_match_id": match_id,
                "watch_key": str(raw_id),
                "watch": watch,
                "stage": stage,
                "kickoff_at_utc": iso_utc(kickoff_utc),
                "kickoff_at_hkt": iso_hkt(kickoff),
                "due_at_utc": iso_utc(due_at),
                "due_at_hkt": iso_hkt(due_at),
            })
    work.sort(key=lambda row: (row["stage"] != "T-5", row["kickoff_at_utc"], row["hkjc_match_id"]))
    return work


def expire_lapsed_work(ledger: dict[str, Any], *, now: datetime) -> int:
    """Append EXPIRED evidence after kickoff; this function never reads quotes."""
    current = now.astimezone(UTC)
    latest = latest_attempts(ledger)
    watches = ledger.get("watch") if isinstance(ledger.get("watch"), dict) else {}
    expired = 0
    for _raw_id, watch in watches.items():
        if not isinstance(watch, dict):
            continue
        identity = ((_manifest(watch) or {}).get("identity") or {})
        kickoff = parse_time(identity.get("kickoff_at_utc")) if isinstance(identity, dict) else None
        if kickoff is None or current < kickoff.astimezone(UTC):
            continue
        for stage, _minutes in SCHEDULED_STAGES:
            if _stage_snapshot_present(watch, stage):
                continue
            key = _event_key(watch, stage)
            if key is None:
                continue
            last = latest.get(key)
            if isinstance(last, dict) and str(last.get("status") or "") in TERMINAL:
                continue
            attempt = last if isinstance(last, dict) else _event_payload(
                watch, stage, attempt_id=f"expired-{uuid.uuid4().hex}", status="STARTED", now=now,
            )
            # A pending job receives an EXPIRED terminal record directly; it
            # did not make a provider attempt and must never be rewritten as one.
            if last is None:
                attempt["status"] = "EXPIRED"
                attempt["reason"] = "kickoff_elapsed_before_native_attempt"
                _events(ledger).append(attempt)
            else:
                finish_attempt(ledger, attempt, "EXPIRED", now=now, reason="kickoff_elapsed")
            latest[key] = latest_attempts(ledger).get(key, attempt)
            expired += 1
    return expired


def enrich_snapshot(snapshot: dict[str, Any], watch: dict[str, Any], stage: str) -> dict[str, Any]:
    """Redundantly retain native identity/schedule fields in one immutable stage."""
    manifest = _manifest(watch)
    identity = manifest.get("identity") if isinstance(manifest, dict) else None
    job = _job(watch, stage)
    if not isinstance(identity, dict) or not isinstance(job, dict):
        return snapshot
    snapshot.update({
        "match_id": str(identity.get("hkjc_match_id") or ""),
        "kickoff_at_utc": str(identity.get("kickoff_at_utc") or ""),
        "kickoff_at_hkt": str(manifest.get("kickoff_at_hkt") or ""),
        "due_at_utc": str(job.get("due_at_utc") or ""),
        "due_at_hkt": str(job.get("due_at_hkt") or ""),
    })
    return snapshot


def completeness_projection(ledger: dict[str, Any], *, now: datetime) -> dict[str, Any]:
    """Read-only per-fixture native schedule completeness view."""
    latest = latest_attempts(ledger)
    rows: list[dict[str, Any]] = []
    counts: Counter[str] = Counter()
    watches = ledger.get("watch") if isinstance(ledger.get("watch"), dict) else {}
    for raw_id, watch in sorted(watches.items(), key=lambda item: str(item[0])):
        if not isinstance(watch, dict):
            continue
        manifest = _manifest(watch)
        identity = manifest.get("identity") if isinstance(manifest, dict) else None
        if not isinstance(identity, dict):
            continue
        match_id = str(identity.get("hkjc_match_id") or raw_id)
        kickoff_utc = str(identity.get("kickoff_at_utc") or "")
        stages: dict[str, dict[str, Any]] = {}
        for stage, _minutes in SCHEDULED_STAGES:
            job = _job(watch, stage) or {}
            event = latest.get((match_id, kickoff_utc, stage))
            status = (
                "COMMITTED" if _stage_snapshot_present(watch, stage)
                else str(event.get("status") or "PENDING") if isinstance(event, dict)
                else "PENDING"
            )
            stages[stage] = {
                "status": status,
                "due_at_utc": job.get("due_at_utc"),
                "due_at_hkt": job.get("due_at_hkt"),
                "attempt_id": event.get("attempt_id") if isinstance(event, dict) else None,
                "reason": event.get("reason") if isinstance(event, dict) else None,
            }
            counts[status] += 1
        rows.append({
            "hkjc_match_id": match_id,
            "kickoff_at_utc": kickoff_utc,
            "kickoff_at_hkt": manifest.get("kickoff_at_hkt"),
            "origin": manifest.get("origin"),
            "stages": stages,
        })
    return {
        "schema_version": 1,
        "generated_at": iso_hkt(now),
        "fixtures": rows,
        "counts": dict(sorted(counts.items())),
    }
