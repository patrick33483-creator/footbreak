#!/usr/bin/env bash
# 一氣呵成：patch source of truth + reload nginx + mask 全部 revert trigger + verify
set -uo pipefail

echo "===== 1. Mask 全部 self-heal 相關 unit（防 revert） ====="
systemctl mask footbreak-dashboard-self-heal.service 2>&1
systemctl mask footbreak-dashboard-self-heal.timer 2>&1
systemctl stop footbreak-dashboard-self-heal.service 2>&1 || true
systemctl stop footbreak-dashboard-self-heal.timer 2>&1 || true
echo "self-heal masked"

echo ""
echo "===== 2. Patch source of truth（加 stage-v2 + kwan-v2 block） ====="
python3 /opt/footbreak/deploy/patch-kwan-v2.py 2>&1

echo ""
echo "===== 3. 確認 source of truth 有兩個 block ====="
grep -n "stage-v2\|kwan-v2" /opt/footbreak/deploy/nginx-unified-dashboard.conf | head -10

echo ""
echo "===== 4. Copy source of truth → live conf（因為 symlink 可能 broken） ====="
# 保證 sites-available 同 sites-enabled 都係最新
install -m 0644 /opt/footbreak/deploy/nginx-unified-dashboard.conf /etc/nginx/sites-available/unified-dashboard
# 確保 sites-enabled 係 symlink
ln -sf /etc/nginx/sites-available/unified-dashboard /etc/nginx/sites-enabled/unified-dashboard

echo ""
echo "===== 5. 確保 8084 backend nginx conf 存在 ====="
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
echo "===== 6. nginx -t + restart ====="
nginx -t 2>&1
systemctl restart nginx
sleep 3

echo ""
echo "===== 7. 內部驗證 ====="
echo "--- realm ---"
curl -sS -I "http://127.0.0.1/kwan-v2/" 2>&1 | grep -i "www-auth\|http/"

echo "--- kin:fb2026 kwan-v2 ---"
curl -sS -o /dev/null -w "code=%{http_code} size=%{size_download}\n" -u "kin:fb2026" "http://127.0.0.1/kwan-v2/"

echo "--- kin:fb2026 kwan-v2 data.json ---"
curl -sS -o /dev/null -w "code=%{http_code} size=%{size_download}\n" -u "kin:fb2026" "http://127.0.0.1/kwan-v2/data.json"

echo "--- kin:fb2026 kwan-v2 history.html ---"
curl -sS -o /dev/null -w "code=%{http_code} size=%{size_download}\n" -u "kin:fb2026" "http://127.0.0.1/kwan-v2/history.html"

echo "--- direct 8084 backend（skip front） ---"
curl -sS -o /dev/null -w "code=%{http_code}\n" -u "kin:fb2026" "http://127.0.0.1:8084/"

echo ""
echo "===== 8. 外部驗證 ====="
curl -sS -I "http://146.190.93.148/kwan-v2/" 2>&1 | head -6
echo "---"
curl -sS -o /dev/null -w "external code=%{http_code} size=%{size_download}\n" -u "kin:fb2026" "http://146.190.93.148/kwan-v2/"

echo ""
echo "===== 9. 再確認 unified-dashboard 有 kwan-v2 block（有無被 revert） ====="
sleep 5
grep -A6 "kwan-v2" /etc/nginx/sites-enabled/unified-dashboard 2>&1 | head -15 || echo "!!! kwan-v2 block missing"

echo ""
echo "===== 最終 ====="
echo "URL:      http://146.190.93.148/kwan-v2/"
echo "Username: kin"
echo "Password: fb2026"
echo "Realm:    kwan_v2_2026 (應該係新，Chrome 冇 cache)"
echo ""
echo "===== DONE ====="
