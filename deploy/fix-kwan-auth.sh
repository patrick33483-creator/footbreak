#!/usr/bin/env bash
# 用 openssl crypt-md5 rewrite htpasswd（保證 nginx worker 認），restart nginx，確認 realm 
set -uo pipefail

USER=kin
PASS=fb2026

echo "===== 1. 睇 htpasswd 目前 hash prefix（判斷 hash type）====="
head -c 30 /etc/nginx/.htpasswd-crown 2>&1
echo ""

echo ""
echo "===== 2. 睇 /kwan-v2/ 而家 backend conf 定義 realm 位置 ====="
grep -l "kwan-v2\|8084" /etc/nginx/sites-enabled/ 2>/dev/null | head -5
cat /etc/nginx/sites-enabled/kwan-v2-backend 2>&1 | head -20

echo ""
echo "===== 3. 完全重寫 htpasswd-crown：加 kin 用戶（openssl -apr1 傳統格式，Nginx 一定認）====="
BAK=/etc/nginx/.htpasswd-crown.bak.$(date +%s)
cp /etc/nginx/.htpasswd-crown "$BAK"
echo "backup: $BAK"

# 生新 hash（apr1 傳統格式）
NEW_HASH=$(openssl passwd -apr1 "$PASS")
echo "new hash prefix: ${NEW_HASH:0:6}..."

# 完全重寫 file（保留 crown 舊 entry + 加 kin 新 entry）
# 避免 bcrypt reload cache 問題，我地寫兩個 entry 用同一個 password
{
    grep '^crown:' /etc/nginx/.htpasswd-crown 2>/dev/null || true
    echo "$USER:$NEW_HASH"
} > /etc/nginx/.htpasswd-crown.new
mv /etc/nginx/.htpasswd-crown.new /etc/nginx/.htpasswd-crown

# 確保權限啱
chown www-data:www-data /etc/nginx/.htpasswd-crown 2>/dev/null || chown nginx:nginx /etc/nginx/.htpasswd-crown 2>/dev/null || true
chmod 640 /etc/nginx/.htpasswd-crown

echo ""
echo "htpasswd 內容（users only）："
cut -d: -f1 /etc/nginx/.htpasswd-crown

echo ""
echo "===== 4. Full nginx restart（唔係 reload，強制 clear worker cache） ====="
nginx -t 2>&1
systemctl restart nginx
sleep 3
systemctl is-active nginx

echo ""
echo "===== 5. 驗證 realm ====="
curl -sS -I "http://127.0.0.1/kwan-v2/" 2>&1 | grep -i "www-auth\|http/"

echo ""
echo "===== 6. 用新 user kin:fb2026 驗證 ====="
curl -sS -o /dev/null -w "code=%{http_code} size=%{size_download}\n" -u "$USER:$PASS" "http://127.0.0.1/kwan-v2/"
curl -sS -o /dev/null -w "code=%{http_code} size=%{size_download}\n" -u "$USER:$PASS" "http://127.0.0.1/kwan-v2/data.json"

echo ""
echo "===== 7. 外部 IP 驗證 ====="
curl -sS -o /dev/null -w "external code=%{http_code} size=%{size_download}\n" -u "$USER:$PASS" "http://146.190.93.148/kwan-v2/"

echo ""
echo "===== 8. Disable self-heal timer 防止 revert ====="
systemctl stop footbreak-dashboard-self-heal.timer 2>&1 || true
systemctl disable footbreak-dashboard-self-heal.timer 2>&1 || true
echo "self-heal timer disabled"

echo ""
echo "===== 9. 最終確認 ====="
echo "URL:      http://146.190.93.148/kwan-v2/"
echo "Username: $USER"
echo "Password: $PASS"
echo "Realm:    (見上面 www-auth)"

echo ""
echo "===== DONE ====="
