#!/usr/bin/env bash
set -uo pipefail

echo "===== 1. self-heal service unit file ====="
systemctl cat footbreak-dashboard-self-heal.service 2>&1 | head -40

echo ""
echo "===== 2. self-heal script 位置 ====="
find /opt -name "*self-heal*" -type f 2>&1 | head -10

echo ""
echo "===== 3. self-heal script 完整內容 ====="
for f in $(find /opt -name "*self-heal*" -type f 2>&1); do
    echo "--- $f ---"
    cat "$f" 2>&1 | head -200
    echo ""
done

echo ""
echo "===== 4. 睇 nginx-unified-dashboard.conf template 用邊到（可能係唔同 file） ====="
find /opt -name "nginx-unified*" 2>&1
find /etc -name "nginx-unified*" 2>&1 | head -5

echo ""
echo "===== 5. 目前 nginx-unified-dashboard.conf source of truth 內容 ====="
head -80 /opt/footbreak/deploy/nginx-unified-dashboard.conf 2>&1

echo ""
echo "===== 6. self-heal 最近幾次跑 log ====="
journalctl -u footbreak-dashboard-self-heal.service --no-pager -n 40 --since "10 minutes ago" 2>&1 | tail -40

echo ""
echo "===== 7. tick script 有無 call self-heal ====="
grep -rn "self-heal\|dashboard-self-heal\|systemctl.*self-heal" /opt/footbreak/stage_engine_v2/ /opt/footbreak/deploy/ 2>/dev/null | head -20

echo ""
echo "===== DONE ====="
