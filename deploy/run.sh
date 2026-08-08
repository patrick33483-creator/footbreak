#!/usr/bin/env bash
# 足破 · 執行包裝器(systemd 同手動都用呢個)
#   run.sh tick    每 2 分鐘,跑啱啱踏入 T-30 / T-5 窗口嘅場
#   run.sh sweep   每晚 23:59,全板首預
#   run.sh settle  只結算
set -uo pipefail

APP_DIR="${APP_DIR:-/opt/footbreak}"
WEB_ROOT="${WEB_ROOT:-/var/www/footbreak}"
MODE="${1:-tick}"

# 環境變數(OpticOdds key / Telegram token)
if [ -f /etc/footbreak.env ]; then
  set -a; . /etc/footbreak.env; set +a
fi

export PATH="$APP_DIR/bin:$PATH"
export TZ="Asia/Hong_Kong"

# venv 有就用,冇就用系統 python3
if [ -x "$APP_DIR/.venv/bin/python3" ]; then
  export PATH="$APP_DIR/.venv/bin:$PATH"
fi

cd "$APP_DIR/system"

# 同一時間只准跑一個,避免 tick 同 sweep 撞到一齊寫 ledger
exec 9>/var/lock/footbreak.lock
if ! flock -n 9; then
  echo "$(date '+%F %T') 上一次仲跑緊,今次跳過"
  exit 0
fi

bash run_all.sh "$MODE"
rc=$?

# 把新出嘅 data.json 推去 nginx web root
if [ -f "$APP_DIR/hkjc-dashboard/data.json" ] && [ -d "$WEB_ROOT" ]; then
  install -m 0644 "$APP_DIR/hkjc-dashboard/data.json" "$WEB_ROOT/data.json"
fi

exit $rc
