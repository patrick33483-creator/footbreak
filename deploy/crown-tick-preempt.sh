#!/usr/bin/env bash
# Give a due Crown T-5 a clear path through slow, non-deadline work.
set -euo pipefail

APP_DIR="${APP_DIR:-/opt/footbreak}"
STATE_DIR="${CROWN_STATE_DIR:-/var/lib/footbreak/crown}"
MARKER="${CROWN_T5_PRIORITY_MARKER:-/run/crown-t5-priority}"
PYTHON="${CROWN_PYTHON:-$APP_DIR/.venv/bin/python3}"

# This reads only local, already-persisted identity and stage state.  It never
# makes provider calls.  A missing T-5 remains urgent until kickoff, which
# preserves retries for an earlier DATA_MISSING/quote-missing attempt.
if "$PYTHON" - "$STATE_DIR/predictions.json" "$STATE_DIR/ledger.json" <<'PY'
import json
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

predictions_path, ledger_path = map(Path, sys.argv[1:3])
try:
    predictions = json.loads(predictions_path.read_text(encoding="utf-8"))
    ledger = json.loads(ledger_path.read_text(encoding="utf-8"))
except (OSError, ValueError, TypeError):
    raise SystemExit(1)
if not isinstance(predictions, list) or not isinstance(ledger, dict):
    raise SystemExit(1)

hkt = timezone(timedelta(hours=8))
now = datetime.now(hkt)
watch = ledger.get("watch") if isinstance(ledger.get("watch"), dict) else {}
for card in predictions:
    if not isinstance(card, dict):
        continue
    match_id = str(card.get("match_id") or "")
    raw = card.get("kickoff_hkt") or card.get("kickoff")
    try:
        kickoff = datetime.fromisoformat(str(raw).replace("Z", "+00:00")).astimezone(hkt)
    except (TypeError, ValueError):
        continue
    minutes = (kickoff - now).total_seconds() / 60.0
    stages = (watch.get(match_id) or {}).get("stages") or []
    t5_complete = any(
        isinstance(stage, dict)
        and stage.get("stage") == "T-5"
        and stage.get("status") != "DATA_MISSING"
        and (
            stage.get("odds_status") is None
            or (
                stage.get("odds_status") == "available"
                and bool(stage.get("market_predictions"))
            )
        )
        for stage in stages
    )
    if 0.0 < minutes <= 10.5 and not t5_complete:
        raise SystemExit(0)
raise SystemExit(1)
PY
then
  /usr/bin/touch "$MARKER"
  # No wait here: once the marker is present, new slow jobs are conditioned
  # out and their existing work is asked to stop while the tick starts.
  /usr/bin/systemctl stop --no-block crown-sweep.service crown-settle.service
  echo "Crown urgent T-5 due; slow jobs preempted"
else
  /usr/bin/rm -f "$MARKER"
  echo "Crown no missing urgent T-5; slow jobs left running"
fi
