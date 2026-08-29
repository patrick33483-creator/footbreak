#!/usr/bin/env bash
echo "=== timer file ==="
cat /etc/systemd/system/crown-round-update.timer 2>&1
echo ""
echo "=== timer show ==="
systemctl show crown-round-update.timer --no-pager | grep -E "^(OnCalendar|TimersCalendar|NextElapse|LastTrigger|ActiveState)"
