#!/usr/bin/env bash
# 快速 check：tick timer + 週末場次 coverage
set -euo pipefail

echo "===== 1. Tick timer 狀態 ====="
systemctl is-active stage-engine-v2-tick.timer 2>&1
systemctl list-timers stage-engine-v2-tick.timer --no-pager 2>&1 | head -5

echo ""
echo "===== 2. 最近 tick log（頭 15 行，睇有無 miss） ====="
journalctl -u stage-engine-v2-tick.service --no-pager -n 15 --since "10 minutes ago" 2>&1 | tail -20

echo ""
echo "===== 3. Ledger 統計 ====="
python3 <<'PY'
import json
from datetime import datetime, timezone, timedelta
from pathlib import Path

p = Path("/var/lib/footbreak/stage_engine_v2/ledger.json")
if not p.exists():
    print("!!! ledger 唔存在")
    raise SystemExit(1)

data = json.loads(p.read_text())
fx = data.get("fixtures", {})
if not fx:
    fx = {k: v for k, v in data.items() if isinstance(v, dict) and "kickoff_hkt" in v}

print(f"ledger 總 fixture 數：{len(fx)}")

hkt = timezone(timedelta(hours=8))
now = datetime.now(hkt)

# 分類：已完（KO 前 3h+）/ 進行中 / 未開波
past, ongoing, upcoming = [], [], []
weekend_upcoming = []  # 星期六日 KO

for fid, info in fx.items():
    ko = info.get("kickoff_hkt") or info.get("kickoff")
    if not ko:
        continue
    try:
        ko_dt = datetime.fromisoformat(ko.replace("Z", "+00:00")).astimezone(hkt)
    except Exception:
        continue
    delta = (ko_dt - now).total_seconds()
    if delta < -10800:  # KO 3小時前 = 完場
        past.append((fid, info, ko_dt))
    elif delta < 0:
        ongoing.append((fid, info, ko_dt))
    else:
        upcoming.append((fid, info, ko_dt))
        # 星期六(5)日(6) 週末
        if ko_dt.weekday() in (5, 6):
            weekend_upcoming.append((fid, info, ko_dt))

print(f"  已完場：{len(past)}")
print(f"  進行中：{len(ongoing)}")
print(f"  未開波：{len(upcoming)}")
print(f"    其中週末（Sat/Sun）KO：{len(weekend_upcoming)}")

print()
print("--- 未來 24 小時所有場次（睇今晚週五夜 + 星期六早） ---")
next24 = [(f, i, d) for (f, i, d) in upcoming if (d - now).total_seconds() < 86400]
next24.sort(key=lambda x: x[2])
for fid, info, ko_dt in next24[:30]:
    home = info.get("home", "?")
    away = info.get("away", "?")
    dow = ["一","二","三","四","五","六","日"][ko_dt.weekday()]
    stages = info.get("stages", {}) or {}
    committed_stages = [s for s in ("first_publish","t_minus_30","t_minus_5") if s in stages]
    print(f"  {ko_dt.strftime('%m/%d %H:%M')}(週{dow}) {home[:12]:12} vs {away[:12]:12}  stages_done={','.join(committed_stages) or '-'}")

print()
print(f"--- 週末場次總覽（Sat/Sun，未開波） ---")
weekend_upcoming.sort(key=lambda x: x[2])
by_day = {}
for fid, info, ko_dt in weekend_upcoming:
    key = ko_dt.strftime("%m/%d (週%s)") % ("六" if ko_dt.weekday()==5 else "日")
    by_day.setdefault(key, 0)
    by_day[key] += 1
for day, cnt in sorted(by_day.items()):
    print(f"  {day}: {cnt} 場")

print()
print("--- 有無 gap（fixture 但 zero stages committed） ---")
zero_stages = [(fid, info, d) for (fid, info, d) in upcoming if not (info.get("stages") or {})]
print(f"  未開波但 stages 空白：{len(zero_stages)}（正常，未到 T-30 就未有 snapshot）")

PY

echo ""
echo "===== 4. Self-heal fail 但唔影響 tick — 睇 self-heal 錯（reference only） ====="
journalctl -u footbreak-dashboard-self-heal.service --no-pager -n 5 --since "5 minutes ago" 2>&1 | tail -8

echo ""
echo "===== 5. Crown data.json 最後 refresh ====="
stat -c "%y" /var/www/crown/data.json 2>&1

echo ""
echo "===== 6. V2 data.json 最後 refresh ====="
stat -c "%y" /var/www/stage_engine_v2/data.json 2>&1

echo ""
echo "===== DONE ====="
