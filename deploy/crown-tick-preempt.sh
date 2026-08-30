#!/usr/bin/env bash
# Give a due Crown T-5 a clear path through slow, non-deadline work.
set -euo pipefail

APP_DIR="${APP_DIR:-/opt/footbreak}"
STATE_DIR="${CROWN_STATE_DIR:-/var/lib/footbreak/crown}"
MARKER="${CROWN_T5_PRIORITY_MARKER:-/run/crown-t5-priority}"
PYTHON="${CROWN_PYTHON:-$APP_DIR/.venv/bin/python3}"

# Use only each durable stage job's persisted due time, kickoff, and state.
# Broad clock windows can stop a sweep before the scheduler has made any job
# due. Unreadable state exits 2 and fails closed below.
if "$PYTHON" "$APP_DIR/deploy/crown_tick_preempt.py" "$STATE_DIR/ledger.json"
then
  if [ "${CROWN_PREEMPT_CHECK_ONLY:-0}" = 1 ]; then
    echo "Crown urgent timed stage due"
    exit 0
  fi
  /usr/bin/touch "$MARKER"
  # No wait here: once the marker is present, new slow jobs are conditioned
  # out and their existing work is asked to stop while the tick starts.
  #
  # Every non-deadline writer must yield while a native T-30/T-5 job is due.
  # The first-look and early-admission reconcilers can be retried later; a
  # true timed-stage quote cannot be reconstructed after kickoff.
  /usr/bin/systemctl stop --no-block crown-round-update.service crown-sweep.service crown-settle.service \
    crown-reverse-t5-drain.service crown-first-look-reconcile.service \
    crown-early-admission-reconcile.service
  echo "Crown urgent timed stage due; blocking slow jobs preempted"
else
  status=$?
  if [ "$status" -ne 1 ]; then
    echo "Crown urgent-stage state unavailable; tick failed closed" >&2
    exit 2
  fi
  if [ "${CROWN_PREEMPT_CHECK_ONLY:-0}" = 1 ]; then
    echo "Crown no missing urgent timed stage"
    exit 1
  fi
  /usr/bin/rm -f "$MARKER"
  echo "Crown no missing urgent timed stage; slow jobs left running"
fi
