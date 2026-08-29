#!/usr/bin/env bash
# Apply the canonical crown-round-update.timer shift (11:00 → 11:15) to the droplet.
# Also overwrite /opt/footbreak/deploy/systemd/ template so subsequent update.sh runs
# reinstall the shifted timer, not the legacy 11:00 version.
set -eu

BACKUP="/etc/systemd/system/crown-round-update.timer.bak-$(date +%s)"
CANONICAL="/opt/footbreak/deploy/systemd/crown-round-update.timer"
ACTIVE="/etc/systemd/system/crown-round-update.timer"

echo "=== current state ==="
systemctl show crown-round-update.timer --no-pager | grep -E "^(OnCalendar|NextElapse|ActiveState)"

# 1. Backup active timer
cp -a "$ACTIVE" "$BACKUP"
echo "Backup: $BACKUP"

# 2. Overwrite template + active
cat > "$CANONICAL" <<'EOF'
[Unit]
Description=足破 · 皇冠每日 11:15 future-round update (offset from T-5 burst)

[Timer]
OnCalendar=*-*-* 11:15:00 Asia/Hong_Kong
Persistent=true
AccuracySec=30s
Unit=crown-round-update.service

[Install]
WantedBy=timers.target
EOF
install -m 0644 "$CANONICAL" "$ACTIVE"
echo "Installed template + active."

# 3. Reload + restart timer
systemctl daemon-reload
systemctl restart crown-round-update.timer
echo "Timer reloaded/restarted."

echo ""
echo "=== new state ==="
systemctl show crown-round-update.timer --no-pager | grep -E "^(OnCalendar|NextElapse|LastTrigger|ActiveState|Result)"
echo ""
cat "$ACTIVE"
