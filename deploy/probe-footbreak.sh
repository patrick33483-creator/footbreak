#!/usr/bin/env bash
# 探測足破馬會系統目前結構
set -uo pipefail

echo "===== 1. /footbreak/ 目前 nginx block ====="
grep -A15 "location.*footbreak" /etc/nginx/sites-enabled/unified-dashboard 2>&1 | head -60

echo ""
echo "===== 2. footbreak 用嘅 htpasswd ====="
ls -la /etc/nginx/.htpasswd-footbreak 2>&1
head -5 /etc/nginx/.htpasswd-footbreak 2>&1

echo ""
echo "===== 3. footbreak static files 位置 ====="
ls /var/www/footbreak/ 2>&1 | head -20

echo ""
echo "===== 4. 有無 stage-v2 級別嘅 footbreak（例如 footbreak-v2 或另一 build 目錄） ====="
find /var/www -maxdepth 2 -type d -name "footbreak*" 2>&1 | head -10
find /var/www -maxdepth 2 -type d -name "*fb*" 2>&1 | head -10
find /var/lib -maxdepth 3 -type d -name "*footbreak*" 2>&1 | head -10

echo ""
echo "===== 5. Footbreak backend API port ====="
netstat -tlnp 2>&1 | grep -E "8081|8766|footbreak" | head -10
ss -tlnp 2>&1 | grep -E "8081|8766" | head -10

echo ""
echo "===== 6. Footbreak 相關 systemd unit ====="
systemctl list-units --all --no-pager 2>&1 | grep -iE "^  footbreak-|●\s+footbreak-" | grep -v self-heal | head -20

echo ""
echo "===== 7. Footbreak self-heal 或會 revert conf 嘅 script ====="
# self-heal 已 rm，但仲有無其他嘢會 rewrite htpasswd？
grep -rln "htpasswd-footbreak\|footbreak-dashboard-password" /opt/footbreak/deploy/ 2>&1 | head -10

echo ""
echo "===== DONE ====="
