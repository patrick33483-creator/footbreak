#!/usr/bin/env bash
set -euo pipefail

APP_DIR="/opt/footbreak"
STATE_DIR="/var/lib/footbreak/backtest"

install -d -o root -g root -m 0700 "$STATE_DIR"
install -d -o root -g www-data -m 0755 /var/www/footbreak /var/www/crown

cd "$APP_DIR"
FOOTBREAK_HISTORY="$APP_DIR/system/accuracy_history.json"
if [ ! -s "$FOOTBREAK_HISTORY" ]; then
  (
    cd "$APP_DIR/system"
    "$APP_DIR/.venv/bin/python3" accuracy.py --no-fetch
  )
fi
[ -s "$FOOTBREAK_HISTORY" ] || {
  echo "accuracy_history.json was not created; refusing a truncated baseline" >&2
  exit 1
}
exec "$APP_DIR/.venv/bin/python3" -m analysis.rolling_backtest \
  --footbreak "$FOOTBREAK_HISTORY" \
  --state "$STATE_DIR/state.json" \
  --out "$STATE_DIR/latest.json" \
  --public /var/www/footbreak/backtest.json \
  --public /var/www/crown/backtest.json
