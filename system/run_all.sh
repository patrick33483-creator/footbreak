#!/usr/bin/env bash
# 足破 · 一鍵流程
#   ./run_all.sh sweep   —— 每15分鐘掃馬會全板,只為新場建立「首預」
#   ./run_all.sh tick    —— 一條到期隊列，T-5 優先，再做 T-30
#   ./run_all.sh t30     —— 舊相容入口，只做 T-30
#   ./run_all.sh settle  —— 淨係結算
# 條件注單通知由 record_picks.py 保存後處理；未確認送達的未開賽注單會重試，
# 已確認送達的 bet_id 由 notify_state.json 去重。
set -euo pipefail
cd "$(dirname "$0")"
MODE="${1:-tick}"

echo "═══ $(TZ=Asia/Hong_Kong date '+%F %H:%M') HKT · 模式 $MODE ═══"

case "$MODE" in
  sweep)  python3 run_predict.py --sweep 2160 ;;
  tick)   FOOTBREAK_REMOTE_TIMEOUT_SECONDS="${FOOTBREAK_REMOTE_TIMEOUT_SECONDS:-8}" python3 run_predict.py 90 ;;
  t30)    python3 run_predict.py --t30-only 90 ;;
  settle) ;;
  *)      echo "未知模式 $MODE"; exit 2 ;;
esac

if [ "$MODE" != "settle" ]; then
  echo "--- 寫入模擬倉 ---"
  # Full-board discovery only appends new 首預 rows.  It intentionally has no
  # Telegram side effect; timed tick remains the sole notification path.
  if [ "$MODE" = "sweep" ]; then
    python3 record_picks.py --no-notify
  elif [ "$MODE" != "tick" ]; then
    python3 record_picks.py
  else
    # Timed stages are already atomically saved one fixture at a time.  Delivery
    # is best-effort and hard-bounded; it can never delay that persistence.
    timeout 5s python3 record_picks.py --notify-only || true
  fi

fi

# 臨場 tick 同全板發現都必須保持輕量；結算與準繩度由獨立 timer 處理，
# 唔可以令每15分鐘 discovery 佔住 T-30/T-5 通道。
if [ "$MODE" = "settle" ]; then
  echo "--- 賽果結算 ---"
  python3 settle.py

  echo "--- 準繩度記分板 ---"
  python3 accuracy.py
fi

if [ "$MODE" != "tick" ]; then
  echo "--- 產生前端資料 ---"
  python3 gen_app_data.py
else
  echo "tick 已完成原子階段保存；儀表板由獨立 sweep 投影"
fi
