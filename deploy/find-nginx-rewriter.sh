#!/usr/bin/env bash
# 揾邊個 job / script 會 rewrite nginx unified-dashboard conf
set -euo pipefail

CONF=/etc/nginx/sites-available/unified-dashboard

echo "===== conf mtime + size ====="
stat "$CONF"
echo ""
echo "===== conf backups ====="
ls -la /etc/nginx/sites-available/unified-dashboard.bak* 2>/dev/null | head -10
echo ""
echo "===== 揾 script 提及 unified-dashboard ====="
grep -rl "unified-dashboard" /etc/ /root/ /home/ /opt/ /usr/local/ 2>/dev/null | head -30
echo ""
echo "===== 揾 script 提及 /v2/crown ====="
grep -rl "v2/crown\|stage_engine_v2" /etc/ /root/ /opt/ 2>/dev/null | head -30
echo ""
echo "===== systemd timers ====="
systemctl list-timers --no-pager | head -25
echo ""
echo "===== cron jobs ====="
crontab -l 2>&1 | head -20
ls -la /etc/cron.d/ /etc/cron.daily/ /etc/cron.hourly/ 2>/dev/null | head -30
