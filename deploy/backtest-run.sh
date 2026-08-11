#!/usr/bin/env bash
set -euo pipefail

APP_DIR="/opt/footbreak"
STATE_DIR="/var/lib/footbreak/backtest"
CHALLENGER_DIR="/var/lib/footbreak/challenger"
LEARNING_DB="/var/lib/footbreak/learning/predictions.sqlite"

if [ -f /etc/footbreak.env ]; then
  set -a
  . /etc/footbreak.env
  set +a
fi

install -d -o root -g root -m 0700 "$STATE_DIR"
install -d -o root -g root -m 0700 "$CHALLENGER_DIR"
install -d -o root -g www-data -m 0755 /var/www/footbreak /var/www/crown

cd "$APP_DIR"
FOOTBREAK_HISTORY="$APP_DIR/system/accuracy_history.json"
FOOTBREAK_DASHBOARD="$APP_DIR/system/accuracy.json"

# A legacy dashboard file is safe as a one-time full-history seed only when it
# explicitly contains every scored match and has not reached the 200-row cap.
if [ -s "$FOOTBREAK_DASHBOARD" ]; then
  seed_ok=$("$APP_DIR/.venv/bin/python3" - "$FOOTBREAK_DASHBOARD" "$FOOTBREAK_HISTORY" <<'PY'
import json
import sys
from pathlib import Path

dashboard = json.loads(Path(sys.argv[1]).read_text(encoding="utf-8"))
history_path = Path(sys.argv[2])
history = (
    json.loads(history_path.read_text(encoding="utf-8"))
    if history_path.exists()
    else {}
)
dashboard_count = int(dashboard.get("n_matches") or 0)
dashboard_rows = len(dashboard.get("matches") or [])
history_count = int(history.get("n_matches") or 0)
print("yes" if 0 < dashboard_count < 200
      and dashboard_count == dashboard_rows
      and history_count < dashboard_count else "no")
PY
  )
  if [ "$seed_ok" = yes ]; then
    install -m 0600 "$FOOTBREAK_DASHBOARD" "$FOOTBREAK_HISTORY"
  fi
fi

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
[ -s "$LEARNING_DB" ] || {
  echo "immutable learning database is missing; refusing mutable JSON backtest input" >&2
  exit 1
}
"$APP_DIR/.venv/bin/python3" -m analysis.rolling_backtest \
  --footbreak "$FOOTBREAK_HISTORY" \
  --learning-db "$LEARNING_DB" \
  --state "$STATE_DIR/state.json" \
  --out "$STATE_DIR/latest.json" \
  --public /var/www/footbreak/backtest.json \
  --public /var/www/crown/backtest.json

# Dedicated, read-only challenger artifact.  It is train/evaluate only: it
# never writes a live probability, pick, official/shadow ledger, or stake.
"$APP_DIR/.venv/bin/python3" -m analysis.challenger_model \
  --learning-db "$LEARNING_DB" \
  --out "$CHALLENGER_DIR/latest.json" \
  --public /var/www/footbreak/challenger-status.json \
  --public /var/www/crown/challenger-status.json

# Only passed safety gates produce a Telegram review request. notify.py keeps
# its own idempotent review keys, so the daily timer never repeats a candidate.
if ! "$APP_DIR/.venv/bin/python3" "$APP_DIR/system/notify.py" \
  --review --report "$STATE_DIR/latest.json"; then
  echo "模型審核通知失敗；回測結果已保存，下次排程會重試" >&2
fi
