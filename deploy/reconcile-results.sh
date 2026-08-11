#!/usr/bin/env bash
# Every 30 minutes, retry all due Footbreak and Crown prediction results.
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

exit "$failed"
