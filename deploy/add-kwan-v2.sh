#!/usr/bin/env bash
# 新增 /kwan-v2/ subpath — proxy 去 port 8084 backend（新 realm）
set -euo pipefail

echo "===== 1. Backup 現行 unified-dashboard source of truth ====="
BAK=/opt/footbreak/deploy/nginx-unified-dashboard.conf.bak.$(date +%s)
cp /opt/footbreak/deploy/nginx-unified-dashboard.conf "$BAK"
echo "backup: $BAK"

echo ""
echo "===== 2. Patch unified-dashboard source of truth（加 kwan-v2 location） ====="
if grep -q "kwan-v2" /opt/footbreak/deploy/nginx-unified-dashboard.conf; then
    echo "already patched, skip"
else
    /opt/footbreak/.venv/bin/python3 /opt/footbreak/deploy/patch-kwan-v2.py
fi

echo ""
echo "===== 3. 建立 port 8084 backend nginx conf（新 realm=kwan_v2_2026） ====="
cat > /etc/nginx/sites-available/kwan-v2-backend <<'NGINX'
server {
    listen 127.0.0.1:8084;
    server_name _;
    root /var/www/stage_engine_v2;

    auth_basic "kwan_v2_2026";
    auth_basic_user_file /etc/nginx/.htpasswd-crown;

    location / {
        try_files $uri $uri/ =404;
        add_header Cache-Control "no-cache, no-store, must-revalidate";
    }

    location = /data.json {
        try_files /data.json =404;
        add_header Cache-Control "no-cache";
    }

    location = /history.html {
        try_files /history.html =404;
        add_header Cache-Control "no-cache";
    }
}
NGINX
ln -sf /etc/nginx/sites-available/kwan-v2-backend /etc/nginx/sites-enabled/kwan-v2-backend

echo ""
echo "===== 4. nginx -t 驗證 ====="
nginx -t 2>&1

echo ""
echo "===== 5. Trigger self-heal 令 unified-dashboard 生效 ====="
systemctl start footbreak-dashboard-self-heal.service 2>&1 | head -5
sleep 3
systemctl reload nginx 2>&1

echo ""
echo "===== 6. 驗證 unified-dashboard 有 kwan-v2 block ====="
grep -A6 "kwan-v2" /etc/nginx/sites-enabled/unified-dashboard 2>&1 | head -15

echo ""
echo "===== 7. Curl 驗證 ====="
echo "--- 冇 auth: 應該 401 ---"
curl -sS -o /dev/null -w "code=%{http_code}\n" "http://127.0.0.1/kwan-v2/"

echo "--- 用 crown:toberich: 應該 200 ---"
curl -sS -o /dev/null -w "code=%{http_code} size=%{size_download}\n" -u crown:toberich "http://127.0.0.1/kwan-v2/"

echo "--- Realm ---"
curl -sS -I "http://127.0.0.1/kwan-v2/" 2>&1 | grep -i "www-authenticate\|http/"

echo "--- data.json ---"
curl -sS -o /dev/null -w "code=%{http_code} size=%{size_download}\n" -u crown:toberich "http://127.0.0.1/kwan-v2/data.json"

echo "--- history.html ---"
curl -sS -o /dev/null -w "code=%{http_code} size=%{size_download}\n" -u crown:toberich "http://127.0.0.1/kwan-v2/history.html"

echo ""
echo "===== 8. 外部 IP 測試 ====="
curl -sS -I "http://146.190.93.148/kwan-v2/" 2>&1 | head -8

echo ""
echo "===== DONE ====="
echo "新 URL: http://146.190.93.148/kwan-v2/"
echo "Realm:  kwan_v2_2026"
echo "Auth:   crown / toberich"
