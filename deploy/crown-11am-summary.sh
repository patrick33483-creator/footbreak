#!/usr/bin/env bash
# 讀取 crown-round-update 11:00 跑完嘅結果，output JSON summary。
# 用喺 crown-11am-summary.yml workflow 內。
set -uo pipefail

TODAY_HKT=$(TZ=Asia/Hong_Kong date +%F)
LEDGER=/var/lib/footbreak/stage_engine_v2/ledger.json

echo "===== BEGIN_JSON ====="
python3 <<PY
import json, subprocess, re, os
from datetime import datetime, timezone, timedelta

TZ = timezone(timedelta(hours=8))
now = datetime.now(TZ)
today = now.strftime("%F")

out = {
    "checked_at_hkt": now.isoformat(timespec="seconds"),
    "round_update": {},
    "ledger": {},
    "future_kickoffs": {},
    "errors": [],
}

# 1) journalctl 抓 crown-round-update 今日 11:00 個 run
try:
    since = f"{today} 10:55:00"
    r = subprocess.run(
        ["journalctl", "-u", "crown-round-update.service", "--since", since, "--no-pager", "-o", "cat"],
        capture_output=True, text=True, timeout=30,
    )
    log = r.stdout
    # 揾嗰個 dict 輸出 line（開頭 {'ok': True）
    m = re.search(r"(\{'ok':\s*True.*'evidence_projection_stages':.*?\})\s*$", log, re.MULTILINE | re.DOTALL)
    if not m:
        m = re.search(r"(\{'ok':.*'mode':\s*'round-update'.*?\})\s*$", log, re.MULTILINE | re.DOTALL)
    if m:
        raw = m.group(1)
        # 好肉酸 —— 用 ast.literal_eval
        import ast
        try:
            payload = ast.literal_eval(raw)
            mapping = payload.get("mapping", {})
            out["round_update"] = {
                "ok": payload.get("ok"),
                "predictions_new": payload.get("predictions", 0),
                "predictions_retained": payload.get("retained_predictions", 0),
                "simulations_created": payload.get("simulations_created", 0),
                "pinnapi_fixtures": payload.get("pinnapi_fixtures", 0),
                "titan_fixtures": payload.get("titan_fixtures", 0),
                "hkjc_fixtures": payload.get("hkjc_fixtures", 0),
                "titan_to_hkjc_mapped": mapping.get("titan_to_hkjc_mapped", 0),
                "titan_due": mapping.get("titan_due", 0),
                "hkjc_to_pinnapi_mapped": mapping.get("hkjc_to_pinnapi_mapped", 0),
                "evidence_stages_count": len(payload.get("evidence_projection_stages", [])),
            }
        except Exception as e:
            out["errors"].append(f"payload parse: {e}")
            out["round_update"]["raw_snippet"] = raw[:400]
    else:
        out["errors"].append("no round-update payload found in journal")

    # 抓 CPU time
    cpu_m = re.search(r"Consumed ([\d\w\s\.]+) CPU time", log)
    if cpu_m:
        out["round_update"]["cpu_time"] = cpu_m.group(1).strip()

    # 抓 Finished timestamp
    fin_m = re.search(r"^(\S+\s+\d+\s+\d+:\d+:\d+).*Finished crown-round-update", log, re.MULTILINE)
    if fin_m:
        out["round_update"]["finished_at"] = fin_m.group(1)

except Exception as e:
    out["errors"].append(f"journal read: {e}")

# 2) Ledger 讀取
try:
    with open("$LEDGER") as f:
        L = json.load(f)
    fixtures = L.get("fixtures", {})
    out["ledger"]["total_fixtures"] = len(fixtures)

    # 未來 3 日 kickoff 分佈
    day_buckets = {}
    upcoming = 0
    for fid, fx in fixtures.items():
        ko = fx.get("kickoff_hkt") or fx.get("kickoff") or ""
        if not ko: continue
        try:
            dt = datetime.fromisoformat(ko.replace("Z","+00:00"))
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=TZ)
            dt = dt.astimezone(TZ)
        except Exception:
            continue
        if dt < now: continue
        upcoming += 1
        d = dt.strftime("%m/%d")
        day_buckets[d] = day_buckets.get(d, 0) + 1

    out["ledger"]["upcoming_fixtures"] = upcoming
    # top 4 days
    sorted_days = sorted(day_buckets.items())[:4]
    out["future_kickoffs"] = dict(sorted_days)

except Exception as e:
    out["errors"].append(f"ledger read: {e}")

# 3) 服務 status
try:
    r = subprocess.run(
        ["systemctl", "is-active", "crown-round-update.service"],
        capture_output=True, text=True, timeout=10,
    )
    out["round_update"]["service_state"] = r.stdout.strip()
except Exception:
    pass

# Print JSON marker so parser can extract cleanly
print("JSON_START")
print(json.dumps(out, ensure_ascii=False, indent=2))
print("JSON_END")
PY
echo "===== END_JSON ====="
