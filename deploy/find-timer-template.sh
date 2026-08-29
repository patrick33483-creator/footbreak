#!/usr/bin/env bash
echo "=== search update.sh for round-update timer ==="
grep -nE "crown-round-update|11:00" /opt/footbreak/deploy/update.sh 2>&1 | head -20
echo ""
echo "=== search all template files in /opt/footbreak/deploy/ ==="
grep -rnE "crown-round-update\.(timer|service)|OnCalendar.*11:00" /opt/footbreak/deploy/ 2>&1 | grep -v Binary | head -30
echo ""
echo "=== list template files ==="
ls /opt/footbreak/deploy/ | grep -iE "timer|service|crown" | head -20
