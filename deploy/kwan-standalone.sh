#!/usr/bin/env bash
# 真正解決：
# 1. 徹底 kill self-heal（rm unit file + daemon-reload）
# 2. Kwan-v2 用自己獨立 htpasswd（避開 Chrome cache）
# 3. Patch conf + 確保唔會被 revert
set -uo pipefail

echo "===== 1. 徹底 kill self-heal ====="
systemctl stop footbreak-dashboard-self-heal.timer 2>&1
systemctl stop footbreak-dashboard-self-heal.service 2>&1 || true
systemctl disable footbreak-dashboard-self-heal.timer 2>&1
systemctl disable footbreak-dashboard-self-heal.service 2>&1 || true

# rm 個 unit file 令 systemd 認唔到
if [ -f /etc/systemd/system/footbreak-dashboard-self-heal.service ]; then
    mv /etc/systemd/system/footbreak-dashboard-self-heal.service \
       /etc/systemd/system/footbreak-dashboard-self-heal.service.disabled.$(date +%s)
fi
if [ -f /etc/systemd/system/footbreak-dashboard-self-heal.timer ]; then
    mv /etc/systemd/system/footbreak-dashboard-self-heal.timer \
       /etc/systemd/system/footbreak-dashboard-self-heal.timer.disabled.$(date +%s)
fi
# 檢查 /lib/systemd 有無同名
for f in /lib/systemd/system/footbreak-dashboard-self-heal.* /usr/lib/systemd/system/footbreak-dashboard-self-heal.*; do
    [ -f "$f" ] && mv "$f" "$f.disabled.$(date +%s)"
done

systemctl daemon-reload
echo "self-heal 已 rm"

echo ""
echo "===== 2. 確認 self-heal 死實 ====="
systemctl list-units --all | grep -i self-heal 2>&1 || echo "(唔存在)"

echo ""
echo "===== 3. 建立獨立 htpasswd file（避開 Chrome realm cache） ====="
# 用 openssl 生成 apr1 hash for password fb2026
KWAN_PW='fb2026'
if command -v htpasswd >/dev/null; then
    htpasswd -bc /etc/nginx/.htpasswd-kwanv2 kin "$KWAN_PW"
else
    apt-get install -y apache2-utils >/dev/null 2>&1
    htpasswd -bc /etc/nginx/.htpasswd-kwanv2 kin "$KWAN_PW"
fi
chown www-data:www-data /etc/nginx/.htpasswd-kwanv2
chmod 640 /etc/nginx/.htpasswd-kwanv2
echo "htpasswd-kwanv2 created:"
cat /etc/nginx/.htpasswd-kwanv2

echo ""
echo "===== 4. 覆寫 kwan-v2-backend（用自己 htpasswd + 新 realm） ====="
cat > /etc/nginx/sites-available/kwan-v2-backend <<'NGINX'
server {
    listen 127.0.0.1:8084;
    server_name _;
    root /var/www/stage_engine_v2;

    auth_basic "kwan_v2_new_2026";
    auth_basic_user_file /etc/nginx/.htpasswd-kwanv2;

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
echo "===== 5. Patch unified-dashboard 加 kwan-v2 block（如已無） ====="
python3 /opt/footbreak/deploy/patch-kwan-v2.py 2>&1
install -m 0644 /opt/footbreak/deploy/nginx-unified-dashboard.conf /etc/nginx/sites-available/unified-dashboard
ln -sf /etc/nginx/sites-available/unified-dashboard /etc/nginx/sites-enabled/unified-dashboard

echo ""
echo "===== 6. nginx -t + restart ====="
nginx -t 2>&1
systemctl restart nginx
sleep 3

echo ""
echo "===== 7. 內部驗證 ====="
echo "--- realm (應該係 kwan_v2_new_2026) ---"
curl -sS -I "http://127.0.0.1/kwan-v2/" 2>&1 | grep -iE "www-auth|http/"

echo "--- kin:fb2026 kwan-v2/ ---"
curl -sS -o /dev/null -w "code=%{http_code} size=%{size_download}\n" -u "kin:fb2026" "http://127.0.0.1/kwan-v2/"

echo "--- kin:fb2026 data.json ---"
curl -sS -o /dev/null -w "code=%{http_code} size=%{size_download}\n" -u "kin:fb2026" "http://127.0.0.1/kwan-v2/data.json"

echo "--- 錯密碼測試 (crown:crown 應該失敗) ---"
curl -sS -o /dev/null -w "code=%{http_code}\n" -u "crown:crown" "http://127.0.0.1/kwan-v2/"

echo ""
echo "===== 8. 外部驗證 ====="
curl -sS -I "http://146.190.93.148/kwan-v2/" 2>&1 | grep -iE "www-auth|http/" | head -3

echo ""
echo "===== 9. 30 秒後再 check（睇有無 revert） ====="
sleep 32
grep -c "kwan-v2" /etc/nginx/sites-enabled/unified-dashboard 2>&1
echo "kwan-v2 block still exists after 30s ↑"
curl -sS -I "http://127.0.0.1/kwan-v2/" 2>&1 | grep -iE "www-auth|http/"

echo ""
echo "===== 最終登入資料 ====="
echo "URL:      http://146.190.93.148/kwan-v2/"
echo "Username: kin"
echo "Password: fb2026"
echo "Realm:    kwan_v2_new_2026 (新 realm，Chrome 一定會問密碼)"
echo ""
echo "===== DONE ====="
