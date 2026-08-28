#!/usr/bin/env bash
set -euo pipefail

APP_DIR="${APP_DIR:-/opt/footbreak}"
LEDGER="${FOOTBREAK_LEDGER:-$APP_DIR/system/sim_ledger.json}"
MARKER="${FOOTBREAK_PRIORITY_MARKER:-/run/footbreak-t5-priority}"
PYTHON="${FOOTBREAK_PYTHON:-$APP_DIR/.venv/bin/python3}"
MODE="${1:-preempt}"

# Tick 本身仍然每 30 秒執行，但只在真正欠一個到期階段時搶佔慢任務。
# 已完成 T-30、而又未進入 T-5 窗口嘅賽事，唔可以反覆殺死賽果結算。
# sweep 會以 --yield-if-urgent 使用同一個 ledger 判定，但只會避讓，
# 不會自行停止服務；真正 tick 隨即會負責設 priority marker 及搶鎖。
if "$PYTHON" - "$LEDGER" <<'PY'
import json
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

path = Path(sys.argv[1])
try:
    ledger = json.loads(path.read_text(encoding="utf-8"))
    watch = ledger.get("watch")
    if not isinstance(watch, dict):
        raise ValueError("watch")
except (OSError, ValueError, TypeError):
    # A slow job must never be admitted on an unknown schedule.
    raise SystemExit(2)

hkt = timezone(timedelta(hours=8))
utc = timezone.utc
now = datetime.now(hkt)
attempts = ledger.get("native_stage_attempts")
attempts = attempts if isinstance(attempts, list) else []
latest = {}
for event in attempts:
    if not isinstance(event, dict):
        continue
    key = (
        str(event.get("hkjc_match_id") or ""),
        str(event.get("kickoff_at_utc") or ""),
        str(event.get("stage") or ""),
    )
    if all(key):
        latest[key] = event

for raw_id, row in watch.items():
    if not isinstance(row, dict):
        continue
    manifest = row.get("native_stage_manifest")
    if isinstance(manifest, dict):
        identity = manifest.get("identity")
        jobs = manifest.get("jobs")
        if not isinstance(identity, dict) or not isinstance(jobs, dict):
            raise SystemExit(2)
        match_id = str(identity.get("hkjc_match_id") or raw_id)
        kickoff_utc = str(identity.get("kickoff_at_utc") or "")
        try:
            kickoff = datetime.fromisoformat(kickoff_utc.replace("Z", "+00:00")).astimezone(utc)
        except (TypeError, ValueError):
            raise SystemExit(2)
        if now.astimezone(utc) >= kickoff:
            continue
        stages = {str(stage.get("stage")) for stage in (row.get("stages") or []) if isinstance(stage, dict)}
        for stage in ("T-30", "T-5"):
            job = jobs.get(stage)
            if not isinstance(job, dict):
                raise SystemExit(2)
            try:
                due_at = datetime.fromisoformat(
                    str(job.get("due_at_utc") or "").replace("Z", "+00:00")
                ).astimezone(utc)
            except (TypeError, ValueError):
                raise SystemExit(2)
            event = latest.get((match_id, kickoff_utc, stage))
            # FAILED and DATA_MISSING remain due before kickoff and must get
            # the same priority path as a first attempt.
            terminal = str((event or {}).get("status") or "") in {
                "COMMITTED", "EXPIRED",
            }
            if now.astimezone(utc) >= due_at and stage not in stages and not terminal:
                raise SystemExit(0)
        continue
    try:
        kickoff = datetime.fromisoformat(str(row.get("kickoff") or "").replace("Z", "+00:00"))
        kickoff = kickoff.replace(tzinfo=hkt) if kickoff.tzinfo is None else kickoff.astimezone(hkt)
    except (TypeError, ValueError):
        # Legacy malformed rows are tolerated; they cannot suppress a valid row.
        continue
    minutes = (kickoff - now).total_seconds() / 60.0
    stages = {str(stage.get("stage")) for stage in (row.get("stages") or []) if isinstance(stage, dict)}
    if (0.0 < minutes <= 10.5 and "T-5" not in stages) or (20.0 <= minutes <= 40.5 and "T-30" not in stages):
        raise SystemExit(0)
raise SystemExit(1)
PY
then
  if [ "$MODE" = "--yield-if-urgent" ]; then
    echo "Footbreak urgent stage due; full-board refresh yielded"
    # This is called by sweep as systemd ExecCondition. Any non-zero status
    # skips its low-priority ExecStart, rather than allowing it to acquire the
    # shared lock ahead of the urgent tick.
    exit 1
  fi
  /usr/bin/touch "$MARKER"
  /usr/bin/systemctl stop footbreak-sweep.service footbreak-settle.service
  echo "Footbreak urgent stage due; slow jobs preempted"
else
  status=$?
  # Do not emit an all-clear or admit unrelated work when authoritative state
  # cannot be parsed.  This avoids hiding a real due native stage.
  if [ "$status" -ne 1 ] && [ "$MODE" != "--yield-if-urgent" ]; then
    echo "Footbreak urgent-stage state unavailable; tick failed closed" >&2
    exit 2
  fi
  if [ "$MODE" = "--yield-if-urgent" ]; then
    # Missing or unreadable schedule state must not admit a slow full-board
    # job: an urgent T-30/T-5 could otherwise be hidden in that state.
    if [ "$status" -ne 1 ]; then
      echo "Footbreak urgent-stage state unavailable; full-board refresh yielded" >&2
      exit 2
    fi
    echo "Footbreak no missing urgent stage; full-board refresh may run"
    exit 0
  fi
  /usr/bin/rm -f "$MARKER"
  echo "Footbreak no missing urgent stage; settlement left running"
fi
