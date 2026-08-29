#!/usr/bin/env bash
set -uo pipefail
echo "===== fb dashboard 讀嘅 fields (排列) ====="
grep -oE "match\.(stage|lead|final|fc|conviction|pick|no_bet_reason|market_predictions|home|away|kickoff_hkt)[a-z_\.\[\]0-9]*|stages?\[[0-9\-]+\]\.[a-z_]+|latestStage\.[a-z_\.]+" /var/www/footbreak/index.html 2>&1 | sort | uniq -c | sort -rn | head -40

echo ""
echo "===== 揾 lead 相關代碼 ====="
grep -nE "\.lead[\.\[]|lead\." /var/www/footbreak/index.html 2>&1 | head -20

echo ""
echo "===== 對比 crown dashboard 讀嘅 fields ====="
grep -oE "match\.(stage|lead|final|fc|conviction|pick|no_bet_reason|market_predictions|home|away|kickoff_hkt)[a-z_\.\[\]0-9]*|stages?\[[0-9\-]+\]\.[a-z_]+|latestStage\.[a-z_\.]+" /var/www/crown/index.html 2>&1 | sort | uniq -c | sort -rn | head -20
