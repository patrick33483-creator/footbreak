#!/usr/bin/env bash
# One safe, dashboard-only refresh after deployment.  It never runs a
# prediction stage, writes the simulation ledger, settles, bets, or notifies.
set -euo pipefail

APP_DIR="${APP_DIR:-/opt/footbreak}"
WEB_ROOT="${WEB_ROOT:-/var/www/footbreak}"
PRIVATE_DIR="${PRIVATE_DIR:-/var/lib/footbreak/private}"
if [ -f /etc/footbreak.env ]; then set -a; . /etc/footbreak.env; set +a; fi

PYTHON="$APP_DIR/.venv/bin/python3"
[ -x "$PYTHON" ] || PYTHON=python3
install -d -m 0700 "$PRIVATE_DIR"

exec "$PYTHON" "$APP_DIR/system/refresh_current_odds.py" \
  --predictions "$APP_DIR/system/predictions.json" \
  --dashboard-data "$WEB_ROOT/data.json" \
  --status "$PRIVATE_DIR/footbreak-current-odds-refresh.json"
