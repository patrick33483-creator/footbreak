#!/usr/bin/env bash
# 揾究竟邊個 layer / 邊條 conf 定義 realm=Odds Radar
set -uo pipefail

echo "===== 1. Port 80 邊個 process listen ====="
ss -tlnp 2>&1 | grep ":80 " || netstat -tlnp 2>&1 | grep ":80 "

echo ""
echo "===== 2. Nginx enabled sites 全部（listen 80 邊個） ====="
for f in /etc/nginx/sites-enabled/*; do
    echo "--- $f ---"
    grep -A1 "listen\s*80\|server_name\|auth_basic\|realm" "$f" 2>/dev/null | head -30
    echo ""
done

echo ""
echo "===== 3. 全部有 'Odds Radar' 字眼嘅 conf ====="
grep -rn "Odds Radar" /etc/nginx/ /opt/footbreak/ 2>/dev/null | grep -v ".bak" | head -20

echo ""
echo "===== 4. Nginx 全部 vhost 樹（root config） ====="
nginx -T 2>&1 | grep -E "^\s*(server_name|listen|location|auth_basic\b|auth_basic_user_file|proxy_pass|root|alias)" | head -80

echo ""
echo "===== 5. 全部有 auth_basic 嘅 location，配對 realm ====="
nginx -T 2>&1 | awk '
/auth_basic\s+/ { realm=$0; in_auth=1 }
/location/ { loc=$0 }
/server_name/ { sn=$0 }
in_auth {
    print "loc:", loc
    print "  realm:", realm
    in_auth=0
}
' | head -40

echo ""
echo "===== 6. 有無 Express / Node process 監 port 5001 或其他 upstream ====="
ss -tlnp 2>&1 | head -30

echo ""
echo "===== 7. 頭 200 行 unified-dashboard 全 conf ====="
head -200 /etc/nginx/sites-enabled/unified-dashboard 2>&1

echo ""
echo "===== DONE ====="
