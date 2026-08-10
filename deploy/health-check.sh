#!/usr/bin/env bash
set -euo pipefail

APP_DIR="${APP_DIR:-/opt/footbreak}"
FOOTBREAK_DATA="${FOOTBREAK_DATA:-/var/www/footbreak/data.json}"
CROWN_DATA="${CROWN_DATA:-/var/www/crown/data.json}"

echo "=== production health $(TZ=Asia/Hong_Kong date '+%F %T %Z') ==="

for unit in \
  footbreak-tick.timer footbreak-sweep.timer footbreak-settle.timer footbreak-backtest.timer \
  crown-tick.timer crown-sweep.timer crown-settle.timer; do
  systemctl is-enabled --quiet "$unit" || {
    state="$(systemctl is-enabled "$unit" 2>&1 || true)"
    echo "FAIL timer $unit enabled_state=$state" >&2
    exit 1
  }
  systemctl is-active --quiet "$unit" || {
    systemctl show "$unit" -p LoadState -p ActiveState -p SubState -p Result
    echo "FAIL timer $unit is not active" >&2
    exit 1
  }
  echo "OK timer $unit"
done

if systemctl is-active --quiet footbreak-t30.timer ||
   systemctl is-enabled --quiet footbreak-t30.timer; then
  echo "FAIL retired timer footbreak-t30.timer is still active or enabled" >&2
  exit 1
fi
echo "OK retired timer footbreak-t30.timer is inactive and disabled"

systemctl is-active --quiet crown-dashboard-api.service || {
  systemctl status crown-dashboard-api.service --no-pager
  echo "FAIL crown-dashboard-api.service is not active" >&2
  exit 1
}
echo "OK service crown-dashboard-api.service active"
systemctl is-active --quiet footbreak-dashboard-api.service || {
  systemctl status footbreak-dashboard-api.service --no-pager
  echo "FAIL footbreak-dashboard-api.service is not active" >&2
  exit 1
}
echo "OK service footbreak-dashboard-api.service active"
api_ready=0
for _ in $(seq 1 10); do
  if python3 - <<'PY'
import json
from urllib.request import urlopen

with urlopen("http://127.0.0.1:8765/api/data", timeout=3) as response:
    payload = json.load(response)
assert payload.get("schema_version") == "crown-dashboard-v2"
PY
  then
    api_ready=1
    break
  fi
  sleep 1
done
if [ "$api_ready" != 1 ]; then
  systemctl status crown-dashboard-api.service --no-pager -l || true
  journalctl -u crown-dashboard-api.service --since "-5 minutes" --no-pager -n 200 || true
  echo "FAIL Crown dashboard API /api/data is unreachable" >&2
  exit 1
fi
echo "OK Crown dashboard API /api/data"
python3 - <<'PY'
import json
from urllib.request import urlopen

with urlopen("http://127.0.0.1:8766/api/data", timeout=3) as response:
    payload = json.load(response)
assert "prediction_history" in payload
PY
echo "OK Footbreak dashboard API /api/data"

for service in footbreak-tick.service footbreak-settle.service crown-tick.service crown-sweep.service crown-settle.service; do
  result="$(systemctl show "$service" -p Result --value)"
  status="$(systemctl show "$service" -p ExecMainStatus --value)"
  # Timed jobs deliberately return EX_TEMPFAIL (75) when a higher-priority
  # pass or the same mode already owns its lock.  The timers retry; this is
  # scheduler pre-emption / duplicate-trigger rejection, not a provider or
  # prediction failure.
  expected_preemption=false
  case "$service:$status" in
    footbreak-tick.service:75|footbreak-settle.service:75|\
    crown-tick.service:75|crown-sweep.service:75|crown-settle.service:75)
      expected_preemption=true
      ;;
  esac
  # 足破到期 T-30/T-5 可以有意以 SIGTERM(15) 搶佔一個正執行緊嘅
  # settlement。只要定時器仍 active，呢個係正常讓路，唔係資料源壞。
  if [ "$service" = footbreak-settle.service ] &&
     [ "$result" = signal ] &&
     [ "$status" = 15 ] &&
     systemctl is-active --quiet footbreak-settle.timer; then
    expected_preemption=true
  fi
  { [ "$result" = success ] && [ "$status" = 0 ]; } || "$expected_preemption" || {
    echo "FAIL service $service result=$result status=$status" >&2
    exit 1
  }
  if "$expected_preemption"; then
    echo "OK service $service preempted(status=75); timer retry active"
  else
    echo "OK service $service"
  fi
done

set -a
[ ! -f /etc/footbreak.env ] || . /etc/footbreak.env
[ ! -f /etc/footbreak-crown.env ] || . /etc/footbreak-crown.env
set +a
for name in TELEGRAM_BOT_TOKEN TELEGRAM_CHAT_ID PINNAPI_API_KEY; do
  [ -n "${!name:-}" ] || {
    echo "FAIL missing $name" >&2
    exit 1
  }
  echo "OK credential $name configured"
done
[ "${CROWN_TELEGRAM_ENABLED:-0}" = 1 ] || {
  echo "FAIL Crown Telegram disabled" >&2
  exit 1
}
echo "OK Crown Telegram enabled"

"$APP_DIR/.venv/bin/python3" - "$FOOTBREAK_DATA" "$CROWN_DATA" <<'PY'
import json
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path


def load(path):
    target = Path(path)
    if not target.is_file():
        raise SystemExit(f"FAIL missing dashboard data: {target}")
    return target, json.loads(target.read_text(encoding="utf-8"))


foot_path, foot = load(sys.argv[1])
crown_path, crown = load(sys.argv[2])
now = datetime.now(timezone.utc).timestamp()
for label, path, max_age in (
    ("Footbreak", foot_path, 10 * 60),
    ("Crown", crown_path, 40 * 60),
):
    age = now - path.stat().st_mtime
    if age > max_age:
        raise SystemExit(f"FAIL {label} data stale: {age:.0f}s")
    print(f"OK {label} data age={age:.0f}s")


def scoreable_market_row(row):
    for prediction in row.get("market_predictions") or []:
        if not isinstance(prediction, dict):
            continue
        if prediction.get("code") not in {"HDC", "HIL", "CHL"}:
            continue
        if prediction.get("side") not in {"H", "A", "L"}:
            continue
        if prediction.get("condition", prediction.get("line")) is None:
            continue
        return True
    return False


def kickoff_timestamp(row):
    text = str(row.get("kickoff") or "").strip().replace("Z", "+00:00")
    if not text:
        return float("-inf")
    value = datetime.fromisoformat(text)
    if value.tzinfo is None:
        value = value.replace(tzinfo=timezone(timedelta(hours=8)))
    return value.timestamp()


history = foot.get("prediction_history") or {}
stats = history.get("stats") or {}
rows = history.get("rows") or []
bad_rows = [row for row in rows if not scoreable_market_row(row)]
if bad_rows:
    raise SystemExit(
        "FAIL Footbreak prediction history contains "
        f"{len(bad_rows)} non-scoreable WDL-only/empty row(s)"
    )
print(f"OK Footbreak prediction history market rows={len(rows)}")
graded = [row for row in rows if row.get("actual") and row.get("score")]
reported_graded = int(stats.get("graded") or 0)
if reported_graded != len(graded):
    raise SystemExit(
        "FAIL Footbreak graded count mismatch: "
        f"stats={stats.get('graded')} rows={len(graded)}"
    )
if graded:
    print(
        "OK Footbreak results "
        f"matches={stats.get('matches')} predictions={stats.get('predictions')} "
        f"graded={len(graded)} hits={stats.get('hits')} accuracy={stats.get('accuracy')}"
    )
else:
    print(
        "WARN Footbreak prediction history has no settled result rows yet; "
        "new-era collection remains healthy"
    )

accuracy = foot.get("accuracy") or {}
if int(accuracy.get("n_matches") or 0) > 0:
    print(
        "OK Footbreak accuracy "
        f"matches={accuracy.get('n_matches')} predictions={accuracy.get('n_preds')} "
        f"missing_results={accuracy.get('n_missing_result')}"
    )
else:
    print("WARN Footbreak accuracy is pending the first settled new-era match")
for missing in accuracy.get("missing_results") or []:
    print(
        "WARN missing result "
        f"match_id={missing.get('match_id')} fixture_id={missing.get('fixture_id')} "
        f"{missing.get('home')} v {missing.get('away')} "
        f"kickoff={missing.get('kickoff')} reason={missing.get('reason')}"
    )

crown_matches = crown.get("matches") or crown.get("predictions") or []
print(f"OK Crown dashboard matches={len(crown_matches)}")
crown_history = crown.get("prediction_history") or {}
crown_rows = crown_history.get("rows") or []
crown_bad = [row for row in crown_rows if not scoreable_market_row(row)]
if crown_bad:
    raise SystemExit(
        "FAIL Crown prediction history contains "
        f"{len(crown_bad)} non-scoreable WDL-only/empty row(s)"
    )
crown_kickoffs = [kickoff_timestamp(row) for row in crown_rows]
if crown_kickoffs != sorted(crown_kickoffs, reverse=True):
    raise SystemExit("FAIL Crown prediction history is not newest-kickoff-first")
print(f"OK Crown prediction history market rows={len(crown_rows)} newest-first")
PY

echo "=== production health PASS ==="
