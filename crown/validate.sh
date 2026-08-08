#!/usr/bin/env bash
# Offline Crown release gate.  It never calls PinnAPI, Titan007, HKJC, or Telegram.
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
PYTHON="${PYTHON:-python3}"
STATE_DIR="${CROWN_STATE_DIR:-$ROOT/crown/state}"
WEB_ROOT="${CROWN_WEB_ROOT:-$STATE_DIR/dashboard}"

cd "$ROOT"
"$PYTHON" -m compileall -q crown
"$PYTHON" -m unittest discover -s crown/tests -t .
CROWN_STATE_DIR="$STATE_DIR" CROWN_WEB_ROOT="$WEB_ROOT" "$PYTHON" -m crown.health
CROWN_STATE_DIR="$STATE_DIR" CROWN_WEB_ROOT="$WEB_ROOT" "$PYTHON" -m crown.run tick --dry-run
CROWN_STATE_DIR="$STATE_DIR" CROWN_WEB_ROOT="$WEB_ROOT" "$PYTHON" -m crown.dashboard_data --out "$WEB_ROOT/data.json"
echo "Crown offline validation passed"
