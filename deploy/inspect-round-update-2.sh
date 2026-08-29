#!/usr/bin/env bash
set -u
echo "=== crown-run.sh (first 120 lines) ==="
head -120 /opt/footbreak/deploy/crown-run.sh 2>&1 || true
echo ""
echo "=== search 11:00:00 - 11:00:10 across all journals ==="
journalctl --since="2026-08-29 10:59:55" --until="2026-08-29 11:00:15" --no-pager 2>&1 | grep -iE "crown|round-update|systemctl|stop|kill|sigterm|footbreak-mutex|priority" | head -80
echo ""
echo "=== all crown-* / footbreak-* units running at 11:00 (recent state) ==="
for u in $(systemctl list-units 'crown-*' 'footbreak-*' --all --no-legend --no-pager 2>/dev/null | awk '{print $1}' | grep -v '\.mount$'); do
    st=$(systemctl show "$u" -p ActiveState,SubState,Result --value 2>/dev/null | paste -sd '/' -)
    echo "  $u  → $st"
done
echo ""
echo "=== who might issue systemctl stop crown-round-update? ==="
grep -rEn "crown-round-update|stop.*crown|systemctl.*stop" /opt/footbreak/deploy/ 2>&1 | grep -v Binary | head -30
echo ""
echo "=== crown-recovery.timer state ==="
systemctl status crown-recovery.timer --no-pager 2>&1 | head -15 || true
echo ""
echo "=== /run/crown-* markers ==="
ls -la /run/crown-* /run/footbreak-* 2>&1 | head -20
