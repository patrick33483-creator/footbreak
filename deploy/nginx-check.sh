#!/usr/bin/env bash
set -euo pipefail
echo "===== 完整 unified-dashboard conf ====="
cat -n /etc/nginx/sites-available/unified-dashboard
echo ""
echo "===== htpasswd contents ====="
sudo cat /etc/nginx/.htpasswd-footbreak | head -5
echo "---"
echo "===== 用 kin:fb2026 由 localhost 直接測試 ====="
curl -v -u kin:fb2026 http://127.0.0.1/v2/footbreak/data.json 2>&1 | head -30
echo ""
echo "===== nginx error log tail ====="
tail -20 /var/log/nginx/error.log
echo ""
echo "===== nginx access log for /v2/footbreak ====="
tail -20 /var/log/nginx/access.log | grep "v2/footbreak" || echo "(no matching lines)"
