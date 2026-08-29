#!/usr/bin/env bash
# Inspect crown round-update service state
set -u
echo "=== systemctl status ==="
systemctl status crown-round-update.service --no-pager -l 2>&1 | head -40 || true
echo ""
echo "=== systemctl show ==="
systemctl show crown-round-update.service --no-pager 2>&1 | grep -E "^(ActiveState|SubState|Result|ExecMainStatus|ExecMainCode|ExecMainStartTimestamp|ExecMainExitTimestamp|StatusText|ExecStart=|Environment=)" | head -30
echo ""
echo "=== last 200 journal lines (this-boot) ==="
journalctl -u crown-round-update.service -n 200 --no-pager 2>&1 | tail -100
echo ""
echo "=== systemctl show timer ==="
systemctl show crown-round-update.timer --no-pager 2>&1 | grep -E "^(NextElapse|LastTrigger|Result|ActiveState|OnCalendar)" || true
echo ""
echo "=== list service unit file ==="
ls -la /etc/systemd/system/crown-round-update.* 2>&1 || true
echo ""
echo "=== unit content ==="
cat /etc/systemd/system/crown-round-update.service 2>&1 | head -40 || true
