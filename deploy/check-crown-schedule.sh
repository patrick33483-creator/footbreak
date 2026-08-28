#!/usr/bin/env bash
set -uo pipefail

echo "===== 1. 全部 crown-* timer schedule ====="
systemctl list-timers --all --no-pager 2>&1 | grep -iE "crown|footbreak" | head -20

echo ""
echo "===== 2. crown-round-update timer 詳細 ====="
systemctl cat crown-round-update.timer 2>&1 | head -20

echo ""
echo "===== 3. crown-round-update service 做咩 ====="
systemctl cat crown-round-update.service 2>&1 | head -30

echo ""
echo "===== 4. crown-sweep timer（15 min discovery） ====="
systemctl cat crown-sweep.timer 2>&1 | head -15

echo ""
echo "===== 5. 最近 crown-round-update 有無跑過 ====="
journalctl -u crown-round-update.service --no-pager -n 30 --since "24 hours ago" 2>&1 | tail -20

echo ""
echo "===== 6. 目前 ledger fixture 數目 + 明日賽事預覽 ====="
python3 <<'PY'
import json
from datetime import datetime, timezone, timedelta
try:
    with open("/var/lib/footbreak/stage_engine_v2/ledger.json") as f:
        L = json.load(f)
    fixtures = L.get("fixtures", {})
    now = datetime.now(timezone(timedelta(hours=8)))
    tomorrow_start = (now + timedelta(days=1)).replace(hour=0, minute=0, second=0, microsecond=0)
    tomorrow_end = tomorrow_start + timedelta(days=1)
    day_after = tomorrow_end + timedelta(days=1)

    ko_today = 0
    ko_tomorrow = 0
    ko_day_after = 0
    for fid, f in fixtures.items():
        ko = f.get("kickoff_hkt") or f.get("kickoff") or ""
        if not ko: continue
        try:
            dt = datetime.fromisoformat(ko.replace("Z","+00:00"))
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=timezone(timedelta(hours=8)))
            dt = dt.astimezone(timezone(timedelta(hours=8)))
        except Exception:
            continue
        if dt < now:
            continue
        if dt < tomorrow_start:
            ko_today += 1
        elif dt < tomorrow_end:
            ko_tomorrow += 1
        elif dt < day_after:
            ko_day_after += 1

    print(f"Ledger 總 fixtures: {len(fixtures)}")
    print(f"今日尚未開波（now→午夜）: {ko_today}")
    print(f"明日（08/30 全日）: {ko_tomorrow}")
    print(f"後日（08/31 全日）: {ko_day_after}")
except Exception as e:
    print(f"讀 ledger 失敗: {e}")
PY

echo ""
echo "===== DONE ====="
