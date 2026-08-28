"""Read-only Crown durable-stage preemption decision."""
from __future__ import annotations

import json
import os
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


def _time(value: Any) -> datetime:
    parsed = datetime.fromisoformat(str(value or "").replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        raise ValueError("timezone missing")
    return parsed.astimezone(timezone.utc)


def urgent_stage_due(ledger: dict[str, Any], now: datetime) -> bool:
    """Return true only for a due, pre-kickoff, uncommitted durable job.

    Historical legacy corruption is ignored when it cannot describe an active
    fixture. Once a row has a future kickoff, malformed jobs fail closed.
    """
    watches = ledger.get("watch")
    if not isinstance(watches, dict):
        raise ValueError("watch state missing")
    current = now.astimezone(timezone.utc)
    urgent = False
    for watch in watches.values():
        if not isinstance(watch, dict):
            continue
        jobs = watch.get("stage_jobs")
        if jobs is None:
            continue
        watch_kickoff = None
        try:
            watch_kickoff = _time(
                watch.get("kickoff_utc")
                or watch.get("kickoff_hkt")
                or watch.get("kickoff")
            )
        except (TypeError, ValueError):
            pass
        # Clearly ended fixtures cannot be urgent and their old job schema is
        # irrelevant to today's preemption decision.
        if watch_kickoff is not None and watch_kickoff <= current:
            continue
        if not isinstance(jobs, dict):
            if watch_kickoff is not None:
                raise ValueError("active stage_jobs invalid")
            continue
        for stage in ("T-30", "T-5"):
            job = jobs.get(stage)
            if job is None:
                continue
            if not isinstance(job, dict):
                if watch_kickoff is not None:
                    raise ValueError("active stage job invalid")
                continue
            try:
                kickoff = _time(job.get("kickoff_utc")) if job.get("kickoff_utc") else watch_kickoff
            except (TypeError, ValueError):
                kickoff = watch_kickoff
            # A legacy job with no usable kickoff cannot establish that it is
            # an active preemption candidate. In-process reconciliation owns it.
            if kickoff is None or kickoff <= current:
                continue
            due_at = _time(job.get("due_at_utc"))
            if current < due_at:
                continue
            state = job.get("state")
            if not isinstance(state, str) or not state.strip():
                raise ValueError("stage job state invalid")
            if state != "COMMITTED":
                urgent = True
    return urgent


def main() -> int:
    if len(sys.argv) != 2:
        return 2
    try:
        ledger = json.loads(Path(sys.argv[1]).read_text(encoding="utf-8"))
        now_raw = os.getenv("CROWN_PREEMPT_NOW")
        now = _time(now_raw) if now_raw else datetime.now(timezone.utc)
        return 0 if urgent_stage_due(ledger, now) else 1
    except (OSError, TypeError, ValueError, json.JSONDecodeError):
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
