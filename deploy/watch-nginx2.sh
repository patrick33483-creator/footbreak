#!/usr/bin/env bash
# 用 fuser + polling 監察 unified-dashboard conf 邊個 process 修改
set -euo pipefail

CONF=/etc/nginx/sites-available/unified-dashboard

echo "===== conf 目前 mtime ====="
stat "$CONF" | grep Modify

echo "===== 現時開住 CONF 嘅 process (fuser + lsof) ====="
fuser -v "$CONF" 2>&1 || echo "no fuser output"
lsof "$CONF" 2>&1 | head -10 || echo "no lsof"

echo ""
echo "===== 監察 modify (每 5s check mtime, 共 3 分鐘) ====="
prev_mtime=$(stat -c %Y "$CONF")
prev_size=$(stat -c %s "$CONF")
echo "  start: mtime=$prev_mtime size=$prev_size"
for i in {1..36}; do
  sleep 5
  m=$(stat -c %Y "$CONF")
  s=$(stat -c %s "$CONF")
  if [ "$m" != "$prev_mtime" ] || [ "$s" != "$prev_size" ]; then
    echo "  ⚠ CHANGE @ iter=$i mtime=$m size=$s (was $prev_mtime/$prev_size)"
    ps -eo pid,ppid,user,cmd --forest 2>&1 | grep -Ei "footbreak|kwan|nginx|python.*deploy|python.*install|python.*fix|update.sh|updater" | grep -v grep | head -20
    echo "  ---all recent nginx-related processes---"
    ps -ef | grep -Ei "nginx|footbreak|/etc/nginx" | grep -v grep | head -20
    prev_mtime=$m; prev_size=$s
  fi
done

echo ""
echo "===== 最終 mtime ====="
stat "$CONF" | grep -E "Modify|Change"
