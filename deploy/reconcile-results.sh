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

# The two settlement runners above actively retry unresolved outcomes through
# their existing strict HKJC/Titan/exact-fixture paths.  Once they finish, make
# the local immutable learning projection consistent before publishing health.
# This is SQLite-only: it makes no provider, Perplexity, model, or connector
# request and can run safely on every 15-minute timer invocation.
LEARNING_DB="${LEARNING_DB:-/var/lib/footbreak/learning/predictions.sqlite}"
if [ -s "$LEARNING_DB" ]; then
  echo "=== $(TZ=Asia/Hong_Kong date '+%F %T') learning-store reconciliation ==="
  if PYTHONPATH="$APP_DIR" "$APP_DIR/.venv/bin/python3" -m analysis.reconcile_learning \
    --learning-db "$LEARNING_DB"; then
    echo "learning-store reconciliation OK"
  else
    echo "learning-store reconciliation failed; raw immutable rows remain intact" >&2
    failed=1
  fi
fi

echo "=== $(TZ=Asia/Hong_Kong date '+%F %T') prediction-history integrity audit ==="
"$APP_DIR/.venv/bin/python3" "$APP_DIR/deploy/verify-result-integrity.py"
integrity_rc=$?
if [ "$integrity_rc" -ne 0 ]; then
  echo "Prediction-history integrity audit failed rc=$integrity_rc" >&2
  failed=1
else
  echo "Prediction-history integrity audit OK"
fi

# 資料健康報告(唯讀診斷)。喺同一個 15 分鐘週期平價重生,完全唔會改
# 預測、結算、注碼或儀表板資料。生成異常會交俾下面嘅 Telegram 健康
# 告警；告警傳送失敗必須令 service 失敗，確保下個週期可見及重試。
# 每日 12:20 嘅 backtest 排程另外保證至少一日一次重寫。
DATA_HEALTH_DIR="${DATA_HEALTH_DIR:-/var/lib/footbreak/data-health}"
SHADOW_CONDITIONS_DIR="${SHADOW_CONDITIONS_DIR:-/var/lib/footbreak/shadow-conditions}"
health_generation_failed=0
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
    health_generation_failed=1
  fi
else
  echo "未有 learning 資料庫；資料健康報告無法生成" >&2
  health_generation_failed=1
fi

# PinnAPI source health is a separate read-only diagnostic.  It has no provider
# request, no model/ledger/result write, and deliberately never changes this
# reconciliation service's exit status: settlement must remain available even
# if the optional source-health artifact cannot be regenerated.
PINNAPI_SOURCE_HEALTH_DIR="${PINNAPI_SOURCE_HEALTH_DIR:-/var/lib/footbreak/pinnapi-source-health}"
if [ -s "$LEARNING_DB" ]; then
  install -d -o root -g root -m 0700 "$PINNAPI_SOURCE_HEALTH_DIR" 2>/dev/null || true
  echo "=== $(TZ=Asia/Hong_Kong date '+%F %T') PinnAPI 來源健康報告重生 ==="
  if PYTHONPATH="$APP_DIR" "$APP_DIR/.venv/bin/python3" -m analysis.pinnapi_source_health \
    --learning-db "$LEARNING_DB" \
    --out "$PINNAPI_SOURCE_HEALTH_DIR/latest.json" \
    --public /var/www/footbreak/pinnapi-source-health.json \
    --lock /var/lock/footbreak-pinnapi-source-health.lock >/dev/null; then
    echo "PinnAPI 來源健康報告 OK"
  else
    echo "PinnAPI 來源健康報告生成失敗；不影響結算、完整性審核或通知" >&2
  fi
fi

# A cheap 15-minute read-only regeneration.  It deliberately has no impact on
# data-health guards, settlement exit status, or any notification path.
if [ -s "$LEARNING_DB" ]; then
  install -d -o root -g root -m 0700 "$SHADOW_CONDITIONS_DIR" 2>/dev/null || true
  if ! PYTHONPATH="$APP_DIR" "$APP_DIR/.venv/bin/python3" -m analysis.shadow_conditions \
    --learning-db "$LEARNING_DB" \
    --state "$SHADOW_CONDITIONS_DIR/state.json" \
    --public-footbreak /var/www/footbreak/shadow-condition-report.json \
    --public-crown /var/www/crown/shadow-condition-report.json >/dev/null; then
    echo "條件影子報告生成失敗；結算及資料健康狀態不受影響" >&2
  fi
fi

# 只讀兩個公開 aggregate 報告。健康／結算異常 Telegram 通知已按用戶要求
# 停用；正常及異常都只寫本地 journal，唔會影響其他預測／投注通知。
echo "=== $(TZ=Asia/Hong_Kong date '+%F %T') 資料健康本地稽核 ==="
health_alert_args=(
  --footbreak-report /var/www/footbreak/data-health.json
  --crown-report /var/www/crown/data-health.json
  --no-telegram
)
if [ "$health_generation_failed" -eq 1 ]; then
  health_alert_args+=(--generation-failed)
fi
if PYTHONPATH="$APP_DIR" "$APP_DIR/.venv/bin/python3" -m analysis.health_alert \
  "${health_alert_args[@]}"; then
  echo "資料健康告警檢查 OK"
else
  echo "資料健康本地稽核失敗；保留所有預測與結算資料，下個週期會重試" >&2
  failed=1
fi

exit "$failed"
