#!/usr/bin/env bash
# 監察 unified-dashboard conf 邊個 process 修改
set -euo pipefail

CONF=/etc/nginx/sites-available/unified-dashboard

echo "===== Backup conf ====="
cp "$CONF" /tmp/pre-watch.conf

echo "===== 用 inotifywait 監察 (30s) ====="
apt-get install -y inotify-tools 2>&1 | tail -3
# Watch for writes to the conf. inotifywait doesn't give PID directly on all systems, but
# can we use auditctl?
if command -v auditctl >/dev/null 2>&1; then
  auditctl -w "$CONF" -p w -k unifiedwatch 2>&1 || true
  echo "auditd rules:"
  auditctl -l | grep unifiedwatch
  echo "-- monitoring for 60s --"
  sleep 60
  echo "-- audit search --"
  ausearch -k unifiedwatch --start recent 2>&1 | head -100 || echo "no events"
  auditctl -W "$CONF" -p w -k unifiedwatch 2>&1 || true
else
  echo "no auditd, install:"
  apt-get install -y auditd 2>&1 | tail -3
  systemctl start auditd
  sleep 3
  auditctl -w "$CONF" -p w -k unifiedwatch 2>&1
  sleep 60
  ausearch -k unifiedwatch --start recent 2>&1 | head -100
  auditctl -W "$CONF" -p w -k unifiedwatch 2>&1
fi

echo ""
echo "===== 檢查有無變化 ====="
if ! diff -q "$CONF" /tmp/pre-watch.conf > /dev/null; then
  echo "❗ Conf changed during watch"
  diff "$CONF" /tmp/pre-watch.conf | head -30
else
  echo "-- conf unchanged during watch --"
fi
echo ""
echo "===== 顯示 conf mtime ====="
stat "$CONF" | grep -E "Modify|Change"

echo ""
echo "===== 檢查所有 recent process (systemd/timers/other) ====="
systemctl list-timers --all --no-pager | head -30
echo ""
echo "===== recent journal errors related ====="
journalctl --since '5 min ago' 2>/dev/null | grep -Ei "nginx|unified-dashboard|kwan|footbreak" | head -30 || echo "no matches"
