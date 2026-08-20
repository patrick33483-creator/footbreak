#!/usr/bin/env bash
# Crown / 皇冠 wrapper.  Simulation-only; no real-betting command exists.
set -euo pipefail

APP_DIR="${APP_DIR:-/opt/footbreak}"
CROWN_APP_DIR="${CROWN_APP_DIR:-$APP_DIR/crown}"
CROWN_STATE_DIR="${CROWN_STATE_DIR:-/var/lib/footbreak/crown}"
CROWN_WEB_ROOT="${CROWN_WEB_ROOT:-/var/www/crown}"
MODE="${1:-tick}"
CROWN_LOCK_DIR="${CROWN_LOCK_DIR:-/var/lock}"

# Existing PinnAPI Edge credentials may already be held in Footbreak's secure
# environment.  Crown's own file is a later, separate override.  Neither is
# ever copied, printed, or committed.
if [ -f /etc/footbreak.env ]; then set -a; . /etc/footbreak.env; set +a; fi
if [ -f /etc/footbreak-crown.env ]; then set -a; . /etc/footbreak-crown.env; set +a; fi
export CROWN_APP_DIR CROWN_STATE_DIR CROWN_WEB_ROOT TZ=Asia/Hong_Kong
ALERT_HELPER="$APP_DIR/system/incident_alert.py"
SERVICE_UNIT="crown-${MODE}.service"
ALERT_TIMEOUT_SECONDS="${CROWN_RUNNER_ALERT_TIMEOUT_SECONDS:-2}"
run_alert_helper() {
  [ -f "$ALERT_HELPER" ] || return 0
  # Incident bookkeeping is best-effort.  It may itself attempt Telegram, so
  # it must never consume the tick's service shutdown margin.
  timeout "${ALERT_TIMEOUT_SECONDS}s" "${PYTHON:-python3}" "$ALERT_HELPER" "$@" >/dev/null 2>&1 || true
}
report_runner_failure() {
  rc=$?
  case "${CROWN_ENABLED:-0}" in
    1|true|TRUE|yes|YES|on|ON) crown_alertable=true ;;
    *) crown_alertable=false ;;
  esac
  if [ "$rc" -ne 0 ] && [ "$rc" -ne 75 ] && "$crown_alertable" && [ -f "$ALERT_HELPER" ]; then
    run_alert_helper event --system crown \
      --unit "$SERVICE_UNIT" --invocation "${INVOCATION_ID:-}"
  fi
}
trap report_runner_failure EXIT
export LEARNING_DB_PATH="${LEARNING_DB_PATH:-/var/lib/footbreak/learning/predictions.sqlite}"
export ODDS_RECOVERY_SIDECAR="${ODDS_RECOVERY_SIDECAR:-/var/lib/footbreak/private/odds-recovery-overlay.json}"

PYTHON="$APP_DIR/.venv/bin/python3"
[ -x "$PYTHON" ] || PYTHON=python3
install -d -m 0700 "$CROWN_STATE_DIR"
install -d -m 0700 "$(dirname "$LEARNING_DB_PATH")"
install -d -m 0700 "$(dirname "$ODDS_RECOVERY_SIDECAR")"
# The runner may atomically replace data.json.  Keep only the static web tree
# readable by nginx; private state remains in CROWN_STATE_DIR.
install -d -m 0755 "$(dirname "$CROWN_WEB_ROOT")" "$CROWN_WEB_ROOT"

# Provider reads for the 30-minute board sweep and the time-critical tick may
# run concurrently. Python serializes only the short state commit.
install -d -m 0755 "$CROWN_LOCK_DIR"
exec 9>"$CROWN_LOCK_DIR/footbreak-crown-${MODE}.lock"
if ! flock -n 9; then
  echo "$(date '+%F %T') Crown $MODE already running; duplicate trigger rejected" >&2
  exit 75
fi

cd "$APP_DIR"
# Keep the non-blocking lock owned by this wrapper while Python runs, but do
# not let fd 9 cross the exec boundary. Provider helpers spawned by Python
# therefore cannot keep a stale runner lock alive after the service is killed.
"$PYTHON" -m crown.run "$MODE" 9>&-

if [ -f "$ALERT_HELPER" ]; then
  run_alert_helper clear-service --system crown --unit "$SERVICE_UNIT" \
    --invocation "${INVOCATION_ID:-}"
  run_alert_helper check --system crown
fi
