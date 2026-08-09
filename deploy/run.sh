#!/usr/bin/env bash
# 足破 · 執行包裝器(systemd 同手動都用呢個)
#   run.sh tick    密集跑啱啱踏入 T-30 / T-5 窗口嘅場
#   run.sh t30     獨立 T-30 資料點
#   run.sh sweep   每晚 23:59,全板首預
#   run.sh settle  只結算
set -euo pipefail

APP_DIR="${APP_DIR:-/opt/footbreak}"
WEB_ROOT="${WEB_ROOT:-/var/www/footbreak}"
MODE="${1:-tick}"
LOCK_FILE="${FOOTBREAK_LOCK_FILE:-/var/lock/footbreak.lock}"
TICK_LOCK_WAIT_SECONDS="${FOOTBREAK_TICK_LOCK_WAIT_SECONDS:-2}"
PRIORITY_MARKER="${FOOTBREAK_PRIORITY_MARKER:-/run/footbreak-t5-priority}"

# Footbreak variables plus the already-paid PinnAPI Edge credentials.  Both
# files stay root-only and are sourced without echoing their contents.
if [ -f /etc/footbreak.env ]; then
  set -a; . /etc/footbreak.env; set +a
fi
if [ -f /etc/footbreak-crown.env ]; then
  set -a; . /etc/footbreak-crown.env; set +a
fi

export PATH="$APP_DIR/bin:$PATH"
export TZ="Asia/Hong_Kong"

# venv 有就用,冇就用系統 python3
if [ -x "$APP_DIR/.venv/bin/python3" ]; then
  export PATH="$APP_DIR/.venv/bin:$PATH"
fi

cd "$APP_DIR/system"

# 同一時間只准跑一個,避免狀態互相覆蓋。footbreak-tick.service 會先
# 中止可能長跑嘅 sweep / settle，再攞呢把鎖；因此慢工作唔可以霸住
# T-30/T-5 通道。tick 最多只等 2 秒處理上一輪收尾，之後交畀下一輪。
exec 9>"$LOCK_FILE"
if [ "$MODE" = "tick" ]; then
  if ! flock -w "$TICK_LOCK_WAIT_SECONDS" 9; then
    echo "$(date '+%F %T') Footbreak tick 等鎖超時；今次未有執行" >&2
    exit 75
  fi
elif [ -e "$PRIORITY_MARKER" ]; then
  echo "$(date '+%F %T') Footbreak $MODE 避讓 T-5；今次未有執行" >&2
  exit 75
elif ! flock -n 9; then
  echo "$(date '+%F %T') Footbreak $MODE 撞正另一個工作；今次未有執行" >&2
  exit 75
fi

if bash run_all.sh "$MODE"; then
  :
else
  rc=$?
  echo "$(date '+%F %T') Footbreak $MODE failed; dashboard was not published" >&2
  exit "$rc"
fi

# Only a fully successful pass may replace the nginx dashboard artifact.
if [ -f "$APP_DIR/hkjc-dashboard/data.json" ] && [ -d "$WEB_ROOT" ]; then
  install -m 0644 "$APP_DIR/hkjc-dashboard/data.json" "$WEB_ROOT/data.json"
fi

exit 0
