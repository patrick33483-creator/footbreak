#!/usr/bin/env bash
set -uo pipefail
echo "===== fb index.html size ====="
ls -l /var/www/footbreak/index.html
echo ""
echo "===== 首 50 行 ====="
head -50 /var/www/footbreak/index.html
echo ""
echo "===== 揾 lead 字 (raw grep) ====="
grep -nE 'lead' /var/www/footbreak/index.html | head -20
echo ""
echo "===== 揾 market_predictions ====="
grep -nE 'market_predictions' /var/www/footbreak/index.html | head -10
echo ""
echo "===== 揾 stages ====="
grep -nE 'stages' /var/www/footbreak/index.html | head -10
echo ""
echo "===== 揾 pick ====="
grep -nE '\.pick|"pick"' /var/www/footbreak/index.html | head -10
echo ""
echo "===== 揾 conviction ====="
grep -nE 'conviction' /var/www/footbreak/index.html | head -10
