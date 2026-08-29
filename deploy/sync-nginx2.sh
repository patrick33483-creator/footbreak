#!/usr/bin/env bash
# scp'd conf file 已存喺 /tmp/nginx-unified-dashboard.conf
set -euo pipefail

CONF_SRC=/tmp/nginx-unified-dashboard.conf
CONF_DST=/etc/nginx/sites-available/unified-dashboard

echo "===== 用 scp'd conf 直接覆寫 legacy repo 之 conf ====="
# Backup legacy repo's file
cp /opt/footbreak/deploy/nginx-unified-dashboard.conf /opt/footbreak/deploy/nginx-unified-dashboard.conf.bak-$(date +%s)
cp "$CONF_SRC" /opt/footbreak/deploy/nginx-unified-dashboard.conf
echo "Legacy repo conf updated: $(md5sum /opt/footbreak/deploy/nginx-unified-dashboard.conf)"

echo ""
echo "===== install nginx conf ====="
install -m 0644 "$CONF_SRC" "$CONF_DST"
grep -n "v2/footbreak" "$CONF_DST" | head -5

echo ""
echo "===== nginx test + reload ====="
nginx -t
systemctl reload nginx

echo ""
echo "===== verify /v2/footbreak/ ====="
sleep 1
for cred in "kin:fb2026" "kwan:crown2026"; do
  code=$(curl -s -o /dev/null -w "%{http_code}" -u "$cred" http://127.0.0.1/v2/footbreak/data.json)
  echo "  $cred → HTTP $code"
done
echo ""
echo "===== .htpasswd-fbv2 users ====="
awk -F: '{print $1}' /etc/nginx/.htpasswd-fbv2 2>&1
