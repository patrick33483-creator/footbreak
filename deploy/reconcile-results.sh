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

# 資料健康報告(唯讀診斷)。喺同一個 15 分鐘週期平價重生,但係完全隔離:
# 佢唔會改任何預測、結算、注碼或儀表板資料,亦唔會令呢個 job 失敗。
# 每日 12:20 嘅 backtest 排程另外保證至少一日一次重寫。
LEARNING_DB="${LEARNING_DB:-/var/lib/footbreak/learning/predictions.sqlite}"
DATA_HEALTH_DIR="${DATA_HEALTH_DIR:-/var/lib/footbreak/data-health}"
if [ -s "$LEARNING_DB" ]; then
  install -d -o root -g root -m 0700 "$DATA_HEALTH_DIR" 2>/dev/null || true
  echo "=== $(TZ=Asia/Hong_Kong date '+%F %T') 資料健康報告重生 ==="
  if PYTHONPATH="$APP_DIR" "$APP_DIR/.venv/bin/python3" -m analysis.data_health \
    --learning-db "$LEARNING_DB" \
    --out "$DATA_HEALTH_DIR/latest.json" \
    --public-footbreak /var/www/footbreak/data-health.json \
    --public-crown /var/www/crown/data-health.json \
    --lock /var/lock/footbreak-data-health.lock >/dev/null; then
    echo "資料健康報告 OK"
  else
    echo "資料健康報告生成失敗；結算與整合性審核不受影響，下個週期會重試" >&2
  fi
else
  echo "未有 learning 資料庫，略過資料健康報告"
fi

exit "$failed"
