#!/usr/bin/env bash
# Give a due Crown T-5 a clear path through slow, non-deadline work.
set -euo pipefail

APP_DIR="${APP_DIR:-/opt/footbreak}"
STATE_DIR="${CROWN_STATE_DIR:-/var/lib/footbreak/crown}"
MARKER="${CROWN_T5_PRIORITY_MARKER:-/run/crown-t5-priority}"
PYTHON="${CROWN_PYTHON:-$APP_DIR/.venv/bin/python3}"

# This reads only local, already-persisted identity and stage state.  It never
# makes provider calls.  A missing T-5 remains urgent until kickoff, which
# preserves retries for an earlier DATA_MISSING/quote-missing attempt.
if "$PYTHON" - "$STATE_DIR/ledger.json" <<'PY'
import json
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

ledger_path = Path(sys.argv[1])
try:
    ledger = json.loads(ledger_path.read_text(encoding="utf-8"))
    watch = ledger.get("watch")
    if not isinstance(watch, dict):
        raise ValueError("state shape")
except (OSError, ValueError, TypeError):
    # Never report all-clear when the authoritative persisted schedule is unreadable.
    raise SystemExit(2)

hkt = timezone(timedelta(hours=8)); now = datetime.now(hkt)
for _match_id, row in watch.items():
    if not isinstance(row, dict):
        continue
    # The ledger, not a dashboard card, is both the identity/stage authority
    # and the durable kickoff schedule.  A missing or malformed projection is
    # irrelevant to this preemption decision.
    try:
        kickoff = datetime.fromisoformat(str(row.get("kickoff") or "").replace("Z", "+00:00"))
        kickoff = kickoff.replace(tzinfo=hkt) if kickoff.tzinfo is None else kickoff.astimezone(hkt)
    except (TypeError, ValueError):
        continue
    minutes = (kickoff - now).total_seconds() / 60.0
    stages = row.get("stages") if isinstance(row.get("stages"), list) else []
    def complete(name):
        # DATA_MISSING is a temporary attempt, not a native completed stage.
        # Any other persisted native stage (including a valid no-pick/Wilson
        # rejection) is complete regardless of dashboard quote projection.
        return any(
            isinstance(stage, dict)
            and stage.get("stage") == name
            and stage.get("status") != "DATA_MISSING"
            for stage in stages
        )
    if (0.0 < minutes <= 10.5 and not complete("T-5")) or (20.0 <= minutes <= 40.5 and not complete("T-30")):
        raise SystemExit(0)
raise SystemExit(1)
PY
then
  /usr/bin/touch "$MARKER"
  # No wait here: once the marker is present, new slow jobs are conditioned
  # out and their existing work is asked to stop while the tick starts.
  /usr/bin/systemctl stop --no-block crown-sweep.service crown-settle.service
  echo "Crown urgent timed stage due; slow jobs preempted"
else
  status=$?
  if [ "$status" -ne 1 ]; then
    echo "Crown urgent-stage state unavailable; tick failed closed" >&2
    exit 2
  fi
  /usr/bin/rm -f "$MARKER"
  echo "Crown no missing urgent timed stage; slow jobs left running"
fi
