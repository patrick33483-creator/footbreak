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

for service in footbreak-tick.service crown-tick.service crown-sweep.service crown-settle.service; do
  result="$(systemctl show "$service" -p Result --value)"
  status="$(systemctl show "$service" -p ExecMainStatus --value)"
  # Footbreak timed jobs deliberately return EX_TEMPFAIL (75) when a
  # higher-priority T-5 pass owns the shared lock.  The timers retry; this is
  # scheduler pre-emption, not a provider or prediction failure.
  expected_preemption=false
  case "$service:$status" in
    footbreak-tick.service:75)
      expected_preemption=true
      ;;
  esac
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
from datetime import datetime, timezone
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

history = foot.get("prediction_history") or {}
stats = history.get("stats") or {}
rows = history.get("rows") or []
graded = [row for row in rows if row.get("actual") and row.get("score")]
if not graded:
    raise SystemExit("FAIL Footbreak prediction history has no verified result rows")
if int(stats.get("graded") or 0) != len(graded):
    raise SystemExit(
        "FAIL Footbreak graded count mismatch: "
        f"stats={stats.get('graded')} rows={len(graded)}"
    )
print(
    "OK Footbreak results "
    f"matches={stats.get('matches')} predictions={stats.get('predictions')} "
    f"graded={len(graded)} hits={stats.get('hits')} accuracy={stats.get('accuracy')}"
)

accuracy = foot.get("accuracy") or {}
if int(accuracy.get("n_matches") or 0) <= 0:
    raise SystemExit("FAIL Footbreak accuracy has no settled matches")
print(
    "OK Footbreak accuracy "
    f"matches={accuracy.get('n_matches')} predictions={accuracy.get('n_preds')} "
    f"missing_results={accuracy.get('n_missing_result')}"
)
for missing in accuracy.get("missing_results") or []:
    print(
        "WARN missing result "
        f"match_id={missing.get('match_id')} fixture_id={missing.get('fixture_id')} "
        f"{missing.get('home')} v {missing.get('away')} "
        f"kickoff={missing.get('kickoff')} reason={missing.get('reason')}"
    )

crown_matches = crown.get("matches") or crown.get("predictions") or []
print(f"OK Crown dashboard matches={len(crown_matches)}")
PY

echo "=== production health PASS ==="
