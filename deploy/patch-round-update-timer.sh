#!/usr/bin/env bash
# Shift crown-round-update.timer from 11:00 → 11:15 HKT to avoid preempt collision
# with T-5 tick burst window.
set -eu

TIMER=/etc/systemd/system/crown-round-update.timer
BACKUP="${TIMER}.bak-$(date +%s)"

echo "=== current timer ==="
cat "$TIMER"

# Backup + patch
cp -a "$TIMER" "$BACKUP"
echo ""
echo "Backup: $BACKUP"

# Change OnCalendar=... 11:00 → 11:15 (keep timezone)
sed -i 's/OnCalendar=.*11:00.*/OnCalendar=*-*-* 11:15:00 Asia\/Hong_Kong/' "$TIMER"

echo ""
echo "=== patched timer ==="
cat "$TIMER"

# Reload + restart timer
systemctl daemon-reload
systemctl restart crown-round-update.timer
echo ""
echo "=== new timer status ==="
systemctl show crown-round-update.timer --no-pager | grep -E "^(OnCalendar|NextElapse|LastTrigger|Result|ActiveState)"

echo ""
echo "=== done ==="
