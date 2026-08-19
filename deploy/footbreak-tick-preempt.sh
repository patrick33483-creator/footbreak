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
if not path.is_file():
    raise SystemExit(2)
try:
    ledger = json.loads(path.read_text(encoding="utf-8"))
except (OSError, ValueError, TypeError):
    raise SystemExit(2)

hkt = timezone(timedelta(hours=8))
now = datetime.now(hkt)
for watch in (ledger.get("watch") or {}).values():
    raw = watch.get("kickoff")
    try:
        kickoff = datetime.strptime(raw, "%Y-%m-%d %H:%M").replace(tzinfo=hkt)
    except (TypeError, ValueError):
        continue
    minutes = (kickoff - now).total_seconds() / 60.0
    stages = {
        str(stage.get("stage"))
        for stage in (watch.get("stages") or [])
        if isinstance(stage, dict)
    }
    t30_due = 5.0 < minutes <= 30.5 and "T-30" not in stages
    # T-5 名稱保留，但操作窗口由開賽前 10 分鐘開始。立即搶佔慢任務，
    # 唔好等到只剩 5 分鐘先啟動，否則 Telegram 到達時已無落注時間。
    t5_due = 0.0 < minutes <= 10.5 and "T-5" not in stages
    if t30_due or t5_due:
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
