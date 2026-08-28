#!/usr/bin/env bash
# 加 /fb-v2/ 獨立入口 to /var/www/footbreak/
# - 獨立 htpasswd（.htpasswd-fbv2）避開 Chrome realm cache
# - 新 realm fb_v2_new_2026
# - 8085 backend server block
# - 對原本 /footbreak/ 完全零改動
set -uo pipefail

echo "===== 1. 建 htpasswd-fbv2 ====="
if ! command -v htpasswd >/dev/null; then
    apt-get install -y apache2-utils >/dev/null 2>&1
fi
htpasswd -bc /etc/nginx/.htpasswd-fbv2 kin "fb2026"
chown www-data:www-data /etc/nginx/.htpasswd-fbv2
chmod 640 /etc/nginx/.htpasswd-fbv2
cat /etc/nginx/.htpasswd-fbv2

echo ""
echo "===== 2. 建 /fb-v2/ backend on 127.0.0.1:8085 ====="
cat > /etc/nginx/sites-available/fb-v2-backend <<'NGINX'
server {
    listen 127.0.0.1:8085;
    server_name _;
    root /var/www/footbreak;

    auth_basic "fb_v2_new_2026";
    auth_basic_user_file /etc/nginx/.htpasswd-fbv2;

    location / {
        try_files $uri $uri/ =404;
        add_header Cache-Control "no-cache, no-store, must-revalidate";
    }

    location = /data.json {
        try_files /data.json =404;
        add_header Cache-Control "no-cache";
    }

    # /fb-v2/api/ 內部 proxy 落 8766（footbreak API）
    location ^~ /api/ {
        proxy_pass http://127.0.0.1:8766/api/;
        proxy_http_version 1.1;
        proxy_set_header Host $host;
        proxy_set_header X-Real-IP $remote_addr;
        proxy_connect_timeout 3s;
        proxy_read_timeout 330s;
        add_header Cache-Control "no-store";
    }
}
NGINX
ln -sf /etc/nginx/sites-available/fb-v2-backend /etc/nginx/sites-enabled/fb-v2-backend

echo ""
echo "===== 3. Patch unified-dashboard 加 /fb-v2/ block ====="
python3 <<'PY'
from pathlib import Path
import re

p = Path("/opt/footbreak/deploy/nginx-unified-dashboard.conf")
txt = p.read_text()

block = """    location ^~ /fb-v2/ {
        proxy_pass http://127.0.0.1:8085/;
        proxy_set_header Host $host;
        proxy_set_header X-Real-IP $remote_addr;
        proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
        proxy_set_header X-Forwarded-Proto $scheme;
    }

"""

if "/fb-v2/" in txt:
    print("→ /fb-v2/ block 已存在，skip")
else:
    m = re.search(r"(    location / \{)", txt)
    if not m:
        print("!!! fallback 揾唔到")
        raise SystemExit(1)
    insert_pos = m.start()
    new_txt = txt[:insert_pos] + block + txt[insert_pos:]
    p.write_text(new_txt)
    print(f"patched OK (insert_pos={insert_pos})")
PY

install -m 0644 /opt/footbreak/deploy/nginx-unified-dashboard.conf /etc/nginx/sites-available/unified-dashboard
ln -sf /etc/nginx/sites-available/unified-dashboard /etc/nginx/sites-enabled/unified-dashboard

echo ""
echo "===== 4. nginx -t + restart ====="
nginx -t 2>&1
systemctl restart nginx
sleep 3

echo ""
echo "===== 5. 內部驗證 ====="
echo "--- realm ---"
curl -sS -I "http://127.0.0.1/fb-v2/" 2>&1 | grep -iE "www-auth|http/"

echo "--- kin:fb2026 fb-v2/ ---"
curl -sS -o /dev/null -w "code=%{http_code} size=%{size_download}\n" -u "kin:fb2026" "http://127.0.0.1/fb-v2/"

echo "--- kin:fb2026 fb-v2/data.json ---"
curl -sS -o /dev/null -w "code=%{http_code} size=%{size_download}\n" -u "kin:fb2026" "http://127.0.0.1/fb-v2/data.json"

echo "--- 錯密碼測試 ---"
curl -sS -o /dev/null -w "code=%{http_code}\n" -u "footbreak:footbreak" "http://127.0.0.1/fb-v2/"

echo ""
echo "===== 6. 外部驗證 ====="
curl -sS -I "http://146.190.93.148/fb-v2/" 2>&1 | grep -iE "www-auth|http/" | head -3
curl -sS -o /dev/null -w "external code=%{http_code} size=%{size_download}\n" -u "kin:fb2026" "http://146.190.93.148/fb-v2/"

echo ""
echo "===== 7. 30 秒後 revert check ====="
sleep 32
grep -c "fb-v2" /etc/nginx/sites-enabled/unified-dashboard 2>&1
curl -sS -I "http://127.0.0.1/fb-v2/" 2>&1 | grep -iE "www-auth|http/"

echo ""
echo "===== 8. 順便再 double check kwan-v2 仍 alive ====="
grep -c "kwan-v2" /etc/nginx/sites-enabled/unified-dashboard 2>&1
curl -sS -o /dev/null -w "kwan-v2 code=%{http_code}\n" -u "kin:fb2026" "http://127.0.0.1/kwan-v2/"

echo ""
echo "===== 最終登入資料 ====="
echo "URL:      http://146.190.93.148/fb-v2/"
echo "Username: kin"
echo "Password: fb2026"
echo "Realm:    fb_v2_new_2026"
echo ""
echo "===== DONE ====="
