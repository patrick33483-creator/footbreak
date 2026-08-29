#!/usr/bin/env bash
set -euo pipefail

CONF_SRC="/tmp/nginx-unified-dashboard.conf"
CONF_DST="/etc/nginx/sites-available/footbreak"
BACKUP="/etc/nginx/sites-available/footbreak.bak.$(date +%Y%m%d-%H%M%S)"

echo "===== 1. Backup ====="
sudo cp "$CONF_DST" "$BACKUP"
echo "backup: $BACKUP"

echo ""
echo "===== 2. Install new conf ====="
sudo cp "$CONF_SRC" "$CONF_DST"

echo ""
echo "===== 3. nginx -t ====="
if ! sudo nginx -t 2>&1; then
    echo "!!! nginx -t FAILED, rolling back"
    sudo cp "$BACKUP" "$CONF_DST"
    sudo nginx -t
    exit 1
fi

echo ""
echo "===== 4. Reload ====="
sudo systemctl reload nginx
sleep 2

echo ""
echo "===== 5. Verify (internal) ====="
echo "-- /v2/crown/ (should 401 without auth) --"
curl -sSI http://127.0.0.1/v2/crown/ 2>&1 | head -3
echo "-- /v2/crown/ with kin:fb2026 --"
curl -sSI -u kin:fb2026 http://127.0.0.1/v2/crown/ 2>&1 | head -3
echo "-- /v2/crown/data.json --"
curl -sSI -u kin:fb2026 http://127.0.0.1/v2/crown/data.json 2>&1 | head -3
echo "-- /radar/ --"
curl -sSI -u kin:fb2026 http://127.0.0.1/radar/ 2>&1 | head -3
echo "-- /radar-challenger/ --"
curl -sSI -u kin:fb2026 http://127.0.0.1/radar-challenger/ 2>&1 | head -3

echo ""
echo "===== 6. Verify (external) ====="
echo "-- /v2/crown/ --"
curl -sSI -u kin:fb2026 http://146.190.93.148/v2/crown/ 2>&1 | head -3
echo "-- /radar/ --"
curl -sSI -u kin:fb2026 http://146.190.93.148/radar/ 2>&1 | head -3

echo ""
echo "===== 7. Existing routes still work? ====="
echo "-- /footbreak/ --"
curl -sSI -u kin:fb2026 http://127.0.0.1/footbreak/ 2>&1 | head -1
echo "-- /crown/ --"
curl -sSI -u kin:fb2026 http://127.0.0.1/crown/ 2>&1 | head -1

echo ""
echo "===== DONE ====="
