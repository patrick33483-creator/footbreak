"""Local-only enablement and liveness checks for the reverse T-5 worker.

The server rollout path writes an enablement marker once, without extending it
on later deploys or condition-skipped timer firings.  Health grants a short,
bounded first-completion grace after that marker, then requires a recent,
parseable successful worker completion.
"""
from __future__ import annotations

import argparse
import os
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

from .common import HKT, parse_time, read_json, write_json_atomic

WORKER_COMPLETION_MAX_AGE_SECONDS = 90
ENABLEMENT_GRACE_SECONDS = 120
MAX_RETRYABLE_STAGE_AGE_SECONDS = 60
MARKER_NAME = "reverse-t5-bridge-enabled.json"
TELEMETRY_NAME = "reverse-t5-bridge-health.json"


def enablement_marker_path(state_dir: Path) -> Path:
    return Path(state_dir) / MARKER_NAME


def telemetry_path(state_dir: Path) -> Path:
    return Path(state_dir) / TELEMETRY_NAME


def mark_enabled(state_dir: Path, *, now: datetime | None = None) -> bool:
    """Record first enablement without extending an existing bounded grace."""
    path = enablement_marker_path(state_dir)
    existing = read_json(path, {})
    if isinstance(existing, dict) and parse_time(existing.get("enabled_at")) is not None:
        return False
    write_json_atomic(path, {
        "schema_version": 1,
        "enabled_at": (now or datetime.now(HKT)).isoformat(),
    })
    return True


def mark_disabled(state_dir: Path) -> None:
    """Remove lifecycle state so a later enable cannot reuse old liveness."""
    for path in (enablement_marker_path(state_dir), telemetry_path(state_dir)):
        try:
            path.unlink()
        except FileNotFoundError:
            pass


def _as_hkt(value: Any) -> datetime | None:
    parsed = parse_time(value)
    return parsed.astimezone(HKT) if parsed is not None else None


def liveness_status(
    state_dir: Path, *, now: datetime | None = None,
    require_completion: bool = False,
) -> tuple[bool, str]:
    """Return bounded enabled-worker liveness without reading provider data."""
    now = now or datetime.now(HKT)
    ledger = read_json(Path(state_dir) / "ledger.json", {})
    jobs = (
        (ledger.get("crown_reverse_t5_bridge") or {}).get("jobs") or []
        if isinstance(ledger, dict) else []
    )
    retryable_ages = [
        (now - stage_at).total_seconds()
        for job in jobs if isinstance(job, dict)
        and str(job.get("state") or "") in {"PENDING", "RUNNING"}
        for stage_at in [_as_hkt(job.get("stage_at"))] if stage_at is not None
    ]
    if retryable_ages and max(retryable_ages) > MAX_RETRYABLE_STAGE_AGE_SECONDS:
        return False, "FAIL enabled reverse T-5 bridge has aged retryable work"
    marker = read_json(enablement_marker_path(state_dir), {})
    enabled_at = _as_hkt(marker.get("enabled_at")) if isinstance(marker, dict) else None
    grace_active = (
        enabled_at is not None
        and 0.0 <= (now - enabled_at).total_seconds() <= ENABLEMENT_GRACE_SECONDS
    )

    telemetry = read_json(telemetry_path(state_dir), {})
    completed_at = _as_hkt(telemetry.get("last_completed")) if isinstance(telemetry, dict) else None
    if completed_at is None:
        if grace_active and not require_completion:
            return True, "OK enabled reverse T-5 bridge awaiting first completion within rollout grace"
        return False, "FAIL enabled reverse T-5 bridge completion telemetry missing or unparseable"
    completion_age = (now - completed_at).total_seconds()
    if completion_age < 0.0 or completion_age > WORKER_COMPLETION_MAX_AGE_SECONDS:
        return False, "FAIL enabled reverse T-5 bridge completion telemetry is stale"
    if enabled_at is not None and completed_at < enabled_at:
        return False, "FAIL enabled reverse T-5 bridge completion predates enablement"
    try:
        timeouts = int(telemetry.get("consecutive_timeouts") or 0)
    except (TypeError, ValueError):
        timeouts = 0
    if timeouts >= 2:
        return False, "FAIL enabled reverse T-5 bridge has consecutive worker timeouts"
    return True, (
        "OK enabled reverse T-5 bridge completion "
        f"age_seconds={completion_age:.1f} consecutive_timeouts={max(0, timeouts)}"
    )


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "command", choices=("mark-enabled", "mark-disabled", "check"),
    )
    parser.add_argument(
        "--state-dir",
        default=os.getenv("CROWN_STATE_DIR", "/var/lib/footbreak/crown"),
    )
    parser.add_argument("--require-completion", action="store_true")
    args = parser.parse_args()
    state_dir = Path(args.state_dir)
    if args.command == "mark-enabled":
        created = mark_enabled(state_dir)
        print(f"reverse_t5_bridge_enablement_marker={'created' if created else 'preserved'}")
        return 0
    if args.command == "mark-disabled":
        mark_disabled(state_dir)
        print("reverse_t5_bridge_enablement_marker=cleared")
        return 0
    ok, message = liveness_status(
        state_dir, require_completion=args.require_completion,
    )
    print(message)
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
