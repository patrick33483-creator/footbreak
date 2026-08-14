#!/usr/bin/env bash
set -euo pipefail

APP_DIR="${APP_DIR:-/opt/footbreak}"
FOOTBREAK_DATA="${FOOTBREAK_DATA:-/var/www/footbreak/data.json}"
CROWN_DATA="${CROWN_DATA:-/var/www/crown/data.json}"
FOOTBREAK_ENV_FILE="${FOOTBREAK_ENV_FILE:-/etc/footbreak.env}"
CROWN_ENV_FILE="${CROWN_ENV_FILE:-/etc/footbreak-crown.env}"

set -a
[ ! -f "$FOOTBREAK_ENV_FILE" ] || . "$FOOTBREAK_ENV_FILE"
[ ! -f "$CROWN_ENV_FILE" ] || . "$CROWN_ENV_FILE"
set +a

crown_is_enabled() {
  case "${CROWN_ENABLED:-0}" in
    1|true|TRUE|yes|YES|on|ON) return 0 ;;
    *) return 1 ;;
  esac
}

echo "=== production health $(TZ=Asia/Hong_Kong date '+%F %T %Z') ==="

for unit in \
  footbreak-tick.timer footbreak-sweep.timer footbreak-settle.timer footbreak-backtest.timer \
  footbreak-result-reconcile.timer; do
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

if crown_is_enabled; then
  for unit in crown-tick.timer crown-sweep.timer crown-settle.timer; do
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
else
  echo "OK Crown validation gate disabled; Crown timers are not required"
fi

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
for app_js in /var/www/footbreak/app.js /var/www/crown/app.js; do
  grep -q 'historyCornerResult' "$app_js" || {
    echo "FAIL stale dashboard asset: $app_js has no corner-result display" >&2
    exit 1
  }
  echo "OK dashboard corner-result asset $app_js"
done
grep -q 'Number.isFinite(Number(rawLine))' /var/www/crown/app.js || {
  echo "FAIL stale Crown dashboard asset: finite-line guard missing" >&2
  exit 1
}
echo "OK Crown dashboard finite-line guard"
grep -q 'Number.isFinite(Number(rawLine))' /var/www/footbreak/app.js || {
  echo "FAIL stale Footbreak dashboard asset: finite-line guard missing" >&2
  exit 1
}
echo "OK Footbreak dashboard finite-line guard"
grep -q '最新開賽時間優先' /var/www/crown/app.js || {
  echo "FAIL stale Crown dashboard asset: global chronological history missing" >&2
  exit 1
}
if grep -q 'const gradedRows =' /var/www/crown/app.js; then
  echo "FAIL stale Crown dashboard asset: status grouping still breaks chronological order" >&2
  exit 1
fi
echo "OK Crown dashboard global chronological history"
api_ready=0
for _ in $(seq 1 10); do
  if python3 - <<'PY'
import json
from urllib.request import urlopen

with urlopen("http://127.0.0.1:8765/api/health", timeout=3) as response:
    crown = json.load(response)
with urlopen("http://127.0.0.1:8766/api/health", timeout=3) as response:
    footbreak = json.load(response)
assert crown == {"ok": True, "service": "crown-dashboard-api"}
assert footbreak == {"ok": True, "service": "footbreak-dashboard-api"}
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
  echo "FAIL dashboard API health endpoints are unreachable" >&2
  exit 1
fi
echo "OK Crown dashboard API /api/health"
echo "OK Footbreak dashboard API /api/health"
dashboard_data_ready=0
for _ in $(seq 1 10); do
  if python3 - <<'PY'
import json
from urllib.request import urlopen

with urlopen("http://127.0.0.1:8765/api/data", timeout=5) as response:
    crown = json.load(response)
with urlopen("http://127.0.0.1:8766/api/data", timeout=5) as response:
    footbreak = json.load(response)
assert crown.get("schema_version") == "crown-dashboard-v2"
assert "prediction_history" in footbreak
assert "by_stage_market" in (
    (crown.get("prediction_history") or {}).get("stats") or {}
)
assert "by_stage_market" in (
    (footbreak.get("prediction_history") or {}).get("stats") or {}
)
PY
  then
    dashboard_data_ready=1
    break
  fi
  sleep 2
done
if [ "$dashboard_data_ready" != 1 ]; then
  systemctl status crown-dashboard-api.service footbreak-dashboard-api.service \
    --no-pager -l || true
  echo "FAIL dashboard API /api/data did not become ready after retries" >&2
  exit 1
fi
echo "OK Crown dashboard API /api/data"
echo "OK Footbreak dashboard API /api/data"

for dashboard in footbreak:8081 crown:8082; do
  name="${dashboard%%:*}"
  port="${dashboard##*:}"
  status="$(curl --silent --show-error --output /dev/null \
    --write-out '%{http_code}' --max-time 5 "http://127.0.0.1:${port}/" || true)"
  if [ "$status" != 401 ]; then
    echo "FAIL nginx $name entrypoint returned HTTP ${status:-unreachable}, expected 401" >&2
    exit 1
  fi
  echo "OK nginx $name entrypoint HTTP 401 auth challenge"
done

python3 - <<'PY'
import json
from pathlib import Path

for name in ("/var/www/footbreak/challenger-status.json", "/var/www/crown/challenger-status.json"):
    path = Path(name)
    if not path.is_file():
        # It is created by the daily 12:20 HKT timer.  A fresh deploy before
        # that timer must not fail health solely because no candidate report
        # has ever been produced.
        print(f"WARN challenger status pending first daily evaluation: {path}")
        continue
    payload = json.loads(path.read_text(encoding="utf-8"))
    policy = payload.get("policy") or {}
    if policy.get("mode") != "daily_train_evaluate_candidate_report_only":
        raise SystemExit(f"FAIL challenger artifact is not isolated daily evaluation: {path}")
    if policy.get("auto_apply") is not False:
        raise SystemExit(f"FAIL challenger artifact permits auto apply: {path}")
    print(f"OK isolated challenger status {path}")
PY

services=(
  footbreak-tick.service
  footbreak-settle.service
  footbreak-result-reconcile.service
)
if crown_is_enabled; then
  services+=(crown-tick.service crown-sweep.service crown-settle.service)
fi
for service in "${services[@]}"; do
  result="$(systemctl show "$service" -p Result --value)"
  status="$(systemctl show "$service" -p ExecMainStatus --value)"
  timer="${service%.service}.timer"
  recovered_after_timeout=false
  # A tick that reaches its hard runtime limit is still a real failure, but
  # the active timer immediately retries it.  Deployment often lands in the
  # short interval between that timeout and the retry, so verify the retry
  # instead of reporting a stale red state.  A second timeout, another error,
  # or no successful retry within 75 seconds still fails health.
  if [ "$result" = timeout ] &&
     [ "$status" = 15 ] &&
     systemctl is-active --quiet "$timer"; then
    echo "WARN service $service timed out; waiting for one timer retry"
    for _ in $(seq 1 15); do
      sleep 5
      result="$(systemctl show "$service" -p Result --value)"
      status="$(systemctl show "$service" -p ExecMainStatus --value)"
      if [ "$result" = success ] && [ "$status" = 0 ]; then
        recovered_after_timeout=true
        break
      fi
      if [ "$result" != timeout ] || [ "$status" != 15 ]; then
        break
      fi
    done
  fi
  # Timed jobs deliberately return EX_TEMPFAIL (75) when a higher-priority
  # pass or the same mode already owns its lock.  The timers retry; this is
  # scheduler pre-emption / duplicate-trigger rejection, not a provider or
  # prediction failure.
  expected_preemption=false
  case "$service:$status" in
    footbreak-tick.service:75|footbreak-settle.service:75|\
    footbreak-result-reconcile.service:75|\
    crown-tick.service:75|crown-sweep.service:75|crown-settle.service:75)
      expected_preemption=true
      ;;
  esac
  # 部署更新或到期 T-30/T-5 可以有意以 SIGTERM(15) 停止一個正執行緊嘅
  # 定時工作。只要對應 timer 仍 active，下一輪會正常重跑，唔係資料源壞。
  if [ "$result" = signal ] &&
     [ "$status" = 15 ] &&
     systemctl is-active --quiet "$timer"; then
    expected_preemption=true
  fi
  { [ "$result" = success ] && [ "$status" = 0 ]; } || "$expected_preemption" || {
    echo "FAIL service $service result=$result status=$status" >&2
    exit 1
  }
  if "$expected_preemption"; then
    echo "OK service $service preempted(status=75); timer retry active"
  elif "$recovered_after_timeout"; then
    echo "OK service $service recovered on timer retry"
  else
    echo "OK service $service"
  fi
done

for name in TELEGRAM_BOT_TOKEN TELEGRAM_CHAT_ID PINNAPI_API_KEY; do
  [ -n "${!name:-}" ] || {
    echo "FAIL missing $name" >&2
    exit 1
  }
  echo "OK credential $name configured"
done
if crown_is_enabled; then
  [ "${CROWN_TELEGRAM_ENABLED:-0}" = 1 ] || {
    echo "FAIL Crown Telegram disabled" >&2
    exit 1
  }
  echo "OK Crown Telegram enabled"
else
  echo "OK Crown Telegram not required while Crown is disabled"
fi

"$APP_DIR/.venv/bin/python3" - "$FOOTBREAK_DATA" "$CROWN_DATA" <<'PY'
import json
import math
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
        raw_line = prediction.get("line")
        if raw_line is None:
            raw_line = prediction.get("condition")
        try:
            line = float(raw_line)
        except (TypeError, ValueError):
            continue
        if not math.isfinite(line):
            continue
        return True
    return False


def invalid_market_predictions(rows):
    bad = []
    for row in rows:
        for prediction in row.get("market_predictions") or []:
            raw_line = prediction.get("line")
            if raw_line is None:
                raw_line = prediction.get("condition")
            try:
                line = float(raw_line)
            except (TypeError, ValueError):
                line = float("nan")
            if (
                prediction.get("code") not in {"HDC", "HIL", "CHL"}
                or prediction.get("side") not in {"H", "A", "L"}
                or not math.isfinite(line)
            ):
                bad.append((row, prediction))
    return bad


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
foot_invalid = invalid_market_predictions(rows)
if foot_invalid:
    raise SystemExit(
        "FAIL Footbreak prediction history contains "
        f"{len(foot_invalid)} invalid/non-finite market prediction(s)"
    )
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
crown_invalid = invalid_market_predictions(crown_rows)
if crown_invalid:
    raise SystemExit(
        "FAIL Crown prediction history contains "
        f"{len(crown_invalid)} invalid/non-finite market prediction(s)"
    )
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
