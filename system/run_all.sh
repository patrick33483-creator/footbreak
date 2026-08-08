#!/usr/bin/env bash
# 足破 · 一鍵流程
#   ./run_all.sh sweep   —— 每晚 23:59:掃馬會全板,每場做一次「首預」
#   ./run_all.sh tick    —— 每 5 分鐘:只跑啱啱踏入 T-30 / T-5 窗口而未做過嘅場
#   ./run_all.sh settle  —— 淨係結算
# 只有新注單通知由 notify.py 負責,冪等(notify_state.json),重跑唔會重複發
set -euo pipefail
cd "$(dirname "$0")"
MODE="${1:-tick}"

echo "═══ $(TZ=Asia/Hong_Kong date '+%F %H:%M') HKT · 模式 $MODE ═══"

case "$MODE" in
  sweep)  python3 run_predict.py --sweep 2160 ;;
  tick)   python3 run_predict.py 90 ;;
  settle) ;;
  *)      echo "未知模式 $MODE"; exit 2 ;;
esac

if [ "$MODE" != "settle" ]; then
  echo "--- 寫入模擬倉 ---"
  python3 record_picks.py

  # Telegram 只通知真正建立的模擬注單。
  if [ "$MODE" = "tick" ]; then
    echo "--- Telegram 落注通知 ---"
    python3 notify.py || echo "!! 通知失敗(唔影響落注記錄)"
  fi
fi

echo "--- 賽果結算 ---"
python3 settle.py

echo "--- 準繩度記分板 ---"
python3 accuracy.py || echo "!! 準繩度計算失敗(唔影響其他)"

echo "--- 產生前端資料 ---"
python3 gen_app_data.py
