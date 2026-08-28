#!/usr/bin/env bash
set -uo pipefail

echo "===== A. update.sh source ====="
head -100 /opt/footbreak/deploy/update.sh 2>&1 | head -100

echo ""
echo "===== B. 邊個 timer 觸發 update.sh ====="
grep -rn "update\.sh" /etc/systemd/ 2>/dev/null | head -20
find /etc/cron* -type f 2>/dev/null | xargs grep -l "update\.sh\|footbreak" 2>/dev/null | head -10
crontab -l 2>&1 | head -20
for u in root www-data footbreak; do
    echo "--- crontab -u $u ---"
    crontab -l -u "$u" 2>&1 | head -10
done

echo ""
echo "===== C. Stage engine v2 靜態檔位置 ====="
ls -la /var/www/stage_engine_v2/ 2>&1 | head -20
ls -la /var/www/ 2>&1

echo ""
echo "===== D. Kwan v2 之前 nginx block 讀邊個 alias ====="
grep -B2 -A15 "stage_engine_v2\|kwan" /opt/footbreak/deploy/nginx-unified-dashboard.conf 2>&1 | head -40

echo ""
echo "===== DONE ====="
