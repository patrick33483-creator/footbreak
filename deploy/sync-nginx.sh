#!/usr/bin/env bash
# 拉最新 legacy repo 之 nginx conf，然後 install + reload
set -euo pipefail

APP_DIR=/opt/footbreak

echo "===== git pull (fast forward only) ====="
cd "$APP_DIR"
sudo -u root git fetch origin main 2>&1 | tail -5
sudo -u root git reset --hard origin/main 2>&1 | tail -3
echo "HEAD: $(git rev-parse HEAD)"

echo ""
echo "===== install nginx conf ====="
install -m 0644 "$APP_DIR/deploy/nginx-unified-dashboard.conf" /etc/nginx/sites-available/unified-dashboard
grep -n "v2/footbreak" /etc/nginx/sites-available/unified-dashboard | head -5

echo ""
echo "===== nginx test + reload ====="
nginx -t
systemctl reload nginx

echo ""
echo "===== verify /v2/footbreak/ ====="
sleep 1
for cred in "kin:fb2026" "kwan:crown2026" "footbreak:changeit"; do
  code=$(curl -s -o /dev/null -w "%{http_code}" -u "$cred" http://127.0.0.1/v2/footbreak/data.json)
  echo "  $cred → HTTP $code"
done
echo ""
echo "===== .htpasswd-fbv2 users ====="
awk -F: '{print $1}' /etc/nginx/.htpasswd-fbv2 2>&1
