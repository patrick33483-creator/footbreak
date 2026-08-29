#!/usr/bin/env bash
set -euo pipefail
echo "===== update.sh (last 200 lines) ====="
tail -200 /opt/footbreak/deploy/update.sh
echo ""
echo "===== update.sh nginx handling ====="
grep -nE "nginx|unified-dashboard|htpasswd|v2/crown|v2/footbreak" /opt/footbreak/deploy/update.sh || echo "(no matches)"
echo ""
echo "===== update.sh full length ====="
wc -l /opt/footbreak/deploy/update.sh
echo ""
echo "===== nginx template dir (deploy/nginx/*) ====="
ls -la /opt/footbreak/deploy/nginx/ 2>/dev/null || echo "no deploy/nginx dir"
find /opt/footbreak/deploy -name "*.conf" -o -name "*.tmpl" 2>/dev/null | head -20
echo ""
echo "===== Where is unified-dashboard rendered? ====="
grep -rl "unified-dashboard" /opt/footbreak/deploy /opt/footbreak/nginx 2>/dev/null | head -10
