#!/usr/bin/env bash
# 揾邊個 process rewrite /opt/footbreak/deploy/nginx-unified-dashboard.conf
set -uo pipefail

echo "===== 1. Conf file 目前 mtime + inode ====="
stat /opt/footbreak/deploy/nginx-unified-dashboard.conf 2>&1

echo ""
echo "===== 2. 邊啲 file 有 unified-dashboard.conf 字眼（可能 rewrite source） ====="
grep -rln "nginx-unified-dashboard" /opt/footbreak/ /etc/systemd/ 2>/dev/null | head -20

echo ""
echo "===== 3. 邊個 file 有寫 auth_basic 'Footbreak Crown' + 'Footbreak'（template source） ====="
grep -rln 'auth_basic "Footbreak"' /opt/footbreak/ 2>/dev/null | grep -v ".bak" | head -10

echo ""
echo "===== 4. update.sh 內有無 rewrite unified conf 邏輯 ====="
grep -n "unified-dashboard\|htpasswd\|Odds Radar" /opt/footbreak/deploy/update.sh 2>&1 | head -20

echo ""
echo "===== 5. 全部 systemd unit File Path 睇有無 unified-dashboard rewrite ====="
systemctl list-units --type=service --all --no-pager 2>&1 | grep -i "footbreak\|crown\|dashboard\|nginx" | head -20

echo ""
echo "===== 6. auditd 或 inotify — 睇 file 最近點被寫 ====="
which auditctl 2>&1 || echo "no auditctl"
# 用 inotifywait 睇下（run 10 秒睇有無 write）
if command -v inotifywait >/dev/null; then
    timeout 10 inotifywait -m -e modify,create,attrib /opt/footbreak/deploy/nginx-unified-dashboard.conf 2>&1 | head -20
else
    echo "no inotifywait; install..."
    apt-get install -y inotify-tools >/dev/null 2>&1
    timeout 15 inotifywait -m -e modify,create,attrib /opt/footbreak/deploy/ 2>&1 | head -30
fi

echo ""
echo "===== 7. 過去 5 分鐘 systemd unit 有無 fire footbreak-* ====="
journalctl --since "5 minutes ago" --no-pager 2>&1 | grep -iE "footbreak|nginx|unified|crown" | grep -viE "kwan-v2|stage-v2|python|access|dashboard-api|module" | head -30

echo ""
echo "===== 8. 5001 docker container 係咩 ====="
docker ps 2>&1 | head -20 || echo "no docker or no perm"
docker inspect $(docker ps -q --filter "publish=5001" 2>/dev/null) 2>&1 | head -30 || true

echo ""
echo "===== DONE ====="
