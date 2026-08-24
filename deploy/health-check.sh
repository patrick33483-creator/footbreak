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

# Health-check failures are deployment/runtime incidents, not betting events.
# EXIT (rather than ERR) also covers this script's explicit `exit 1` guards.
ALERT_HELPER="$APP_DIR/system/incident_alert.py"
report_health_check_exit() {
  rc=$?
  if [ -f "$ALERT_HELPER" ]; then
    if [ "$rc" -eq 0 ]; then
      "$ALERT_HELPER" clear --system footbreak \
        --kind health_check_failure >/dev/null 2>&1 || true
    else
      "$ALERT_HELPER" event --system footbreak \
        --kind health_check_failure >/dev/null 2>&1 || true
    fi
  fi
  exit "$rc"
}
trap report_health_check_exit EXIT

crown_is_enabled() {
  case "${CROWN_ENABLED:-0}" in
    1|true|TRUE|yes|YES|on|ON) return 0 ;;
    *) return 1 ;;
  esac
}

echo "=== production health $(TZ=Asia/Hong_Kong date '+%F %T %Z') ==="

for unit in \
  footbreak-tick.timer footbreak-sweep.timer footbreak-settle.timer footbreak-backtest.timer \
  footbreak-result-reconcile.timer footbreak-dashboard-self-heal.timer \
  footbreak-server-health-monitor.timer; do
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
  for unit in crown-round-update.timer crown-first-look-reconcile.timer crown-tick.timer crown-sweep.timer crown-settle.timer crown-reverse-t5-drain.timer; do
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
for auth_spec in \
  /etc/nginx/.htpasswd-footbreak:footbreak \
  /etc/nginx/.htpasswd-crown:crown; do
  auth_file="${auth_spec%%:*}"
  expected_user="${auth_spec##*:}"
  if [ ! -s "$auth_file" ] || ! grep -q "^${expected_user}:" "$auth_file"; then
    echo "FAIL dashboard auth identity $expected_user is missing from $auth_file" >&2
    exit 1
  fi
  runuser -u www-data -- test -r "$auth_file" || {
    echo "FAIL nginx worker cannot read dashboard auth file: $auth_file" >&2
    exit 1
  }
  echo "OK dashboard auth identity $expected_user"
done
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

for dashboard_spec in \
  "footbreak:8081:/root/footbreak-dashboard-password.txt:footbreak-dashboard" \
  "crown:8082:/root/crown-dashboard-password.txt:crown-dashboard-v2"; do
  IFS=: read -r dashboard_user dashboard_port password_file expected_schema \
    <<< "$dashboard_spec"
  if [ ! -s "$password_file" ]; then
    echo "FAIL dashboard password backup missing for $dashboard_user" >&2
    exit 1
  fi
  IFS= read -r dashboard_password < "$password_file"
  if ! curl --silent --show-error --fail --max-time 8 \
      --user "${dashboard_user}:${dashboard_password}" \
      "http://127.0.0.1:${dashboard_port}/data.json?health=$(date +%s)" \
      | python3 -c 'import json,sys; p=json.load(sys.stdin); c=sys.argv[1]; assert (c=="crown-dashboard-v2" and p.get("schema_version")==c) or (c=="footbreak-dashboard" and isinstance(p.get("matches"),list) and isinstance(p.get("ledger"),dict) and bool(p.get("generated_at")))' "$expected_schema" \
      >/dev/null; then
    unset dashboard_password
    echo "FAIL nginx $dashboard_user /data.json is not valid dashboard JSON" >&2
    exit 1
  fi
  unset dashboard_password
  echo "OK nginx $dashboard_user /data.json valid JSON schema=$expected_schema"
done

# The user-facing dashboards are subpaths on port 80.  Validate both the
# static boot payload and the API fallback there; healthy private :8081/:8082
# listeners are not enough if the unified route is missing or misconfigured.
for public_spec in \
  "footbreak:/root/footbreak-dashboard-password.txt:footbreak-dashboard" \
  "crown:/root/crown-dashboard-password.txt:crown-dashboard-v2"; do
  IFS=: read -r dashboard_user password_file expected_schema <<< "$public_spec"
  IFS= read -r dashboard_password < "$password_file"
  for endpoint in data.json api/data; do
    if ! curl --silent --show-error --fail --max-time 15 \
        --user "${dashboard_user}:${dashboard_password}" \
        "http://127.0.0.1/${dashboard_user}/${endpoint}?health=$(date +%s)" \
        | python3 -c 'import json,sys; p=json.load(sys.stdin); c=sys.argv[1]; assert (c=="crown-dashboard-v2" and p.get("schema_version")==c) or (c=="footbreak-dashboard" and isinstance(p.get("matches"),list) and isinstance(p.get("ledger"),dict) and bool(p.get("generated_at")))' "$expected_schema" \
        >/dev/null; then
      unset dashboard_password
      echo "FAIL public /$dashboard_user/$endpoint is not valid dashboard JSON" >&2
      exit 1
    fi
    echo "OK public /$dashboard_user/$endpoint valid JSON schema=$expected_schema"
  done
  unset dashboard_password
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
  services+=(crown-tick.service crown-sweep.service crown-settle.service crown-reverse-t5-drain.service)
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
    crown-tick.service:75|crown-sweep.service:75|crown-settle.service:75|\
    crown-reverse-t5-drain.service:75)
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
if [ "${INCIDENT_ALERT_ENABLED:-1}" = 1 ]; then
  INCIDENT_PRIVATE_CHAT_ID="${INCIDENT_TELEGRAM_CHAT_ID:-703318555}"
  [ "$INCIDENT_PRIVATE_CHAT_ID" = 703318555 ] || {
    echo "FAIL private incident Telegram chat ID is not PPlai" >&2
    exit 1
  }
  echo "OK private incident Telegram recipient configured"
else
  echo "OK private incident Telegram disabled by INCIDENT_ALERT_ENABLED=0"
fi
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


def resolve_history(label, main_path, payload, expected_schema):
    inline = payload.get("prediction_history")
    if isinstance(inline, dict) and isinstance(inline.get("rows"), list):
        return inline

    data_url = str(payload.get("history_data_url") or "").strip()
    if not data_url:
        raise SystemExit(f"FAIL {label} history sidecar marker missing")
    sidecar_path = (main_path.parent / data_url).resolve()
    if sidecar_path.parent != main_path.parent.resolve():
        raise SystemExit(f"FAIL {label} history sidecar must be a sibling file")
    _, sidecar = load(sidecar_path)
    if sidecar.get("schema_version") != expected_schema:
        raise SystemExit(
            f"FAIL {label} history sidecar schema mismatch: "
            f"{sidecar.get('schema_version')}"
        )
    expected_version = payload.get("history_data_version")
    actual_version = sidecar.get("history_data_version")
    if not expected_version or expected_version != actual_version:
        raise SystemExit(f"FAIL {label} dashboard/history sidecar version mismatch")
    history = sidecar.get("prediction_history")
    if not isinstance(history, dict) or not isinstance(history.get("rows"), list):
        raise SystemExit(f"FAIL {label} history sidecar rows missing")
    return history


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


history = resolve_history(
    "Footbreak", foot_path, foot, "footbreak-history-v1"
)
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
crown_history = resolve_history(
    "Crown", crown_path, crown, "crown-history-v1"
)
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

# Reuse the existing deployment/health pass for bounded ledger monitoring;
# this deliberately creates no additional polling scheduler.
if [ -f "$ALERT_HELPER" ]; then
  "$ALERT_HELPER" check --system all >/dev/null 2>&1 || true
fi

echo "=== production health PASS ==="
