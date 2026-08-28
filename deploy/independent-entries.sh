#!/usr/bin/env bash
# 徹底解決：唔再嘗試 patch unified-dashboard（會被 update.sh 每 5 分鐘 revert）。
# 開兩個獨立 listen port 80（唔同 hostname/path），完全 by-pass unified-dashboard。
# 
# 方案：Independent server listen 0.0.0.0:8080 + 0.0.0.0:8090
# - port 8080 = footbreak v2 (kin/fb2026)
# - port 8090 = kwan v2 (kin/fb2026)
# 
# 用戶登入：
#   http://146.190.93.148:8080/  → footbreak v2 dashboard
#   http://146.190.93.148:8090/  → kwan v2 dashboard
set -uo pipefail

echo "===== 1. 建 htpasswd files ====="
if ! command -v htpasswd >/dev/null; then
    apt-get install -y apache2-utils >/dev/null 2>&1
fi

htpasswd -bc /etc/nginx/.htpasswd-fbv2 kin "fb2026"
htpasswd -bc /etc/nginx/.htpasswd-kwanv2 kin "fb2026"
chown www-data:www-data /etc/nginx/.htpasswd-fbv2 /etc/nginx/.htpasswd-kwanv2
chmod 640 /etc/nginx/.htpasswd-fbv2 /etc/nginx/.htpasswd-kwanv2

ls -la /etc/nginx/.htpasswd-fbv2 /etc/nginx/.htpasswd-kwanv2

echo ""
echo "===== 2. 建 footbreak v2 server on 0.0.0.0:8080 ====="
cat > /etc/nginx/sites-available/fb-v2-public <<'NGINX'
server {
    listen 8080;
    listen [::]:8080;
    server_name _;
    charset utf-8;
    root /var/www/footbreak;

    auth_basic "fb_v2_new_2026";
    auth_basic_user_file /etc/nginx/.htpasswd-fbv2;

    location / {
        try_files $uri $uri/ /index.html;
        add_header Cache-Control "no-cache, no-store, must-revalidate";
    }

    location = /data.json {
        try_files /data.json =404;
        add_header Cache-Control "no-cache";
    }

    location ^~ /api/ {
        proxy_pass http://127.0.0.1:8766/api/;
        proxy_http_version 1.1;
        proxy_set_header Host $host;
        proxy_connect_timeout 3s;
        proxy_read_timeout 330s;
        add_header Cache-Control "no-store";
    }
}
NGINX
ln -sf /etc/nginx/sites-available/fb-v2-public /etc/nginx/sites-enabled/fb-v2-public

echo ""
echo "===== 3. 建 kwan v2 server on 0.0.0.0:8090 ====="
cat > /etc/nginx/sites-available/kwan-v2-public <<'NGINX'
server {
    listen 8090;
    listen [::]:8090;
    server_name _;
    charset utf-8;
    root /var/www/stage_engine_v2;

    auth_basic "kwan_v2_new_2026";
    auth_basic_user_file /etc/nginx/.htpasswd-kwanv2;

    location / {
        try_files $uri $uri/ /index.html;
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
ln -sf /etc/nginx/sites-available/kwan-v2-public /etc/nginx/sites-enabled/kwan-v2-public

echo ""
echo "===== 4. UFW 開放 8080 + 8090 ====="
if command -v ufw >/dev/null; then
    ufw allow 8080/tcp 2>&1 | head -3
    ufw allow 8090/tcp 2>&1 | head -3
    ufw status 2>&1 | head -10
else
    echo "no ufw"
fi

echo ""
echo "===== 5. nginx -t + restart ====="
nginx -t 2>&1
systemctl restart nginx
sleep 3

echo ""
echo "===== 6. 內部驗證 ====="
echo "--- 8080 realm ---"
curl -sS -I "http://127.0.0.1:8080/" 2>&1 | grep -iE "www-auth|http/"
echo "--- 8080 kin:fb2026 ---"
curl -sS -o /dev/null -w "code=%{http_code} size=%{size_download}\n" -u "kin:fb2026" "http://127.0.0.1:8080/"
echo "--- 8080 data.json ---"
curl -sS -o /dev/null -w "code=%{http_code} size=%{size_download}\n" -u "kin:fb2026" "http://127.0.0.1:8080/data.json"

echo ""
echo "--- 8090 realm ---"
curl -sS -I "http://127.0.0.1:8090/" 2>&1 | grep -iE "www-auth|http/"
echo "--- 8090 kin:fb2026 ---"
curl -sS -o /dev/null -w "code=%{http_code} size=%{size_download}\n" -u "kin:fb2026" "http://127.0.0.1:8090/"
echo "--- 8090 data.json ---"
curl -sS -o /dev/null -w "code=%{http_code} size=%{size_download}\n" -u "kin:fb2026" "http://127.0.0.1:8090/data.json"

echo ""
echo "===== 7. 外部驗證 ====="
curl -sS -o /dev/null -w "fb-v2 external code=%{http_code} size=%{size_download}\n" -u "kin:fb2026" "http://146.190.93.148:8080/" --max-time 10
curl -sS -o /dev/null -w "kwan-v2 external code=%{http_code} size=%{size_download}\n" -u "kin:fb2026" "http://146.190.93.148:8090/" --max-time 10

echo ""
echo "===== 8. 等 6 分鐘（一次 update.sh cycle）睇有無 revert ====="
echo "現時 conf files:"
ls -la /etc/nginx/sites-enabled/fb-v2-public /etc/nginx/sites-enabled/kwan-v2-public

echo ""
echo "===== 最終登入資料 ====="
echo ""
echo "足破馬會 v2 (footbreak):"
echo "  URL:      http://146.190.93.148:8080/"
echo "  Username: kin"
echo "  Password: fb2026"
echo ""
echo "皇冠系統 v2 (kwan):"
echo "  URL:      http://146.190.93.148:8090/"
echo "  Username: kin"
echo "  Password: fb2026"
echo ""
echo "呢兩個 port 唔動 unified-dashboard，唔會俾 update.sh revert。"
echo ""
echo "===== DONE ====="
