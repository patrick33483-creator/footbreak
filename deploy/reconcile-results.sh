#!/usr/bin/env bash
# Every 15 minutes, retry all due Footbreak and Crown prediction results, then
# verify the published history ordering and statistics directly from raw rows.
# The two systems are deliberately isolated: one busy/failed runner must not
# prevent the other system from reconciling its outstanding scores/corners.
set -u

APP_DIR="${APP_DIR:-/opt/footbreak}"
failed=0

run_reconciler() {
  local name="$1"
  shift
  echo "=== $(TZ=Asia/Hong_Kong date '+%F %T') $name result reconciliation ==="
  "$@"
  local rc=$?
  if [ "$rc" -eq 0 ]; then
    echo "$name reconciliation OK"
    return
  fi
  if [ "$rc" -eq 75 ]; then
    echo "$name reconciliation busy; automatic retry remains scheduled" >&2
    return
  fi
  echo "$name reconciliation failed rc=$rc" >&2
  failed=1
}

run_reconciler "Footbreak" "$APP_DIR/deploy/run.sh" settle
run_reconciler "Crown" "$APP_DIR/deploy/crown-run.sh" settle

echo "=== $(TZ=Asia/Hong_Kong date '+%F %T') prediction-history integrity audit ==="
"$APP_DIR/.venv/bin/python3" "$APP_DIR/deploy/verify-result-integrity.py"
integrity_rc=$?
if [ "$integrity_rc" -ne 0 ]; then
  echo "Prediction-history integrity audit failed rc=$integrity_rc" >&2
  failed=1
else
  echo "Prediction-history integrity audit OK"
fi

exit "$failed"
