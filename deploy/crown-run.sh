#!/usr/bin/env bash
# Crown / 皇冠 wrapper.  Simulation-only; no real-betting command exists.
set -euo pipefail

APP_DIR="${APP_DIR:-/opt/footbreak}"
CROWN_APP_DIR="${CROWN_APP_DIR:-$APP_DIR/crown}"
CROWN_STATE_DIR="${CROWN_STATE_DIR:-/var/lib/footbreak/crown}"
CROWN_WEB_ROOT="${CROWN_WEB_ROOT:-/var/www/crown}"
MODE="${1:-tick}"

# Existing PinnAPI Edge credentials may already be held in Footbreak's secure
# environment.  Crown's own file is a later, separate override.  Neither is
# ever copied, printed, or committed.
if [ -f /etc/footbreak.env ]; then set -a; . /etc/footbreak.env; set +a; fi
if [ -f /etc/footbreak-crown.env ]; then set -a; . /etc/footbreak-crown.env; set +a; fi
export CROWN_APP_DIR CROWN_STATE_DIR CROWN_WEB_ROOT TZ=Asia/Hong_Kong

PYTHON="$APP_DIR/.venv/bin/python3"
[ -x "$PYTHON" ] || PYTHON=python3
install -d -m 0700 "$CROWN_STATE_DIR"
# The runner may atomically replace data.json.  Keep only the static web tree
# readable by nginx; private state remains in CROWN_STATE_DIR.
install -d -m 0755 "$(dirname "$CROWN_WEB_ROOT")" "$CROWN_WEB_ROOT"

exec 9>/var/lock/footbreak-crown.lock
if ! flock -n 9; then
  echo "$(date '+%F %T') Crown previous pass is still running; skipped"
  exit 0
fi

cd "$APP_DIR"
"$PYTHON" -m crown.run "$MODE"
