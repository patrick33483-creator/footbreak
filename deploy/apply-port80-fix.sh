#!/usr/bin/env bash
# 加 push 到 remote 之後手動觸發 install + reload nginx。
# 因為 update.sh 只喺 push 之後幾分鐘先 run，等唔切。
set -uo pipefail

echo "===== A. 確保兩個 htpasswd 都仲喺（up 過都存在）====="
ls -la /etc/nginx/.htpasswd-fbv2 /etc/nginx/.htpasswd-kwanv2 2>&1
# 如唔存在，重建
if [ ! -f /etc/nginx/.htpasswd-fbv2 ]; then
    if ! command -v htpasswd >/dev/null; then apt-get install -y apache2-utils >/dev/null 2>&1; fi
    htpasswd -bc /etc/nginx/.htpasswd-fbv2 kin "fb2026"
    chown www-data:www-data /etc/nginx/.htpasswd-fbv2
    chmod 640 /etc/nginx/.htpasswd-fbv2
fi
if [ ! -f /etc/nginx/.htpasswd-kwanv2 ]; then
    htpasswd -bc /etc/nginx/.htpasswd-kwanv2 kin "fb2026"
    chown www-data:www-data /etc/nginx/.htpasswd-kwanv2
    chmod 640 /etc/nginx/.htpasswd-kwanv2
fi
ls -la /etc/nginx/.htpasswd-fbv2 /etc/nginx/.htpasswd-kwanv2 2>&1

echo ""
echo "===== B. 拉最新 code + install source conf 落 nginx ====="
cd /opt/footbreak && git pull --ff-only 2>&1 | tail -10
install -m 0644 /opt/footbreak/deploy/nginx-unified-dashboard.conf /etc/nginx/sites-available/unified-dashboard

echo ""
echo "===== C. 順手 clean 走 8080/8090 conf（唔再需要）====="
rm -f /etc/nginx/sites-enabled/fb-v2-public /etc/nginx/sites-enabled/kwan-v2-public
rm -f /etc/nginx/sites-enabled/fb-v2-backend /etc/nginx/sites-enabled/kwan-v2-backend /etc/nginx/sites-enabled/stage-v2-backend
ls /etc/nginx/sites-enabled/

echo ""
echo "===== D. nginx -t + reload ====="
nginx -t 2>&1
systemctl reload nginx
sleep 2

echo ""
echo "===== E. 內部測試 /footbreak/ ====="
curl -sS -I "http://127.0.0.1/footbreak/" 2>&1 | grep -iE "www-auth|http/" | head -3
curl -sS -o /dev/null -w "kin fb2026 /footbreak/  code=%{http_code} size=%{size_download}\n" -u "kin:fb2026" "http://127.0.0.1/footbreak/"
curl -sS -o /dev/null -w "kin fb2026 /footbreak/data.json  code=%{http_code} size=%{size_download}\n" -u "kin:fb2026" "http://127.0.0.1/footbreak/data.json"

echo ""
echo "===== F. 內部測試 /crown/ ====="
curl -sS -I "http://127.0.0.1/crown/" 2>&1 | grep -iE "www-auth|http/" | head -3
curl -sS -o /dev/null -w "kin fb2026 /crown/  code=%{http_code} size=%{size_download}\n" -u "kin:fb2026" "http://127.0.0.1/crown/"
curl -sS -o /dev/null -w "kin fb2026 /crown/data.json  code=%{http_code} size=%{size_download}\n" -u "kin:fb2026" "http://127.0.0.1/crown/data.json"

echo ""
echo "===== G. 外部（public IP）測試 ====="
curl -sS -o /dev/null -w "external /footbreak/ code=%{http_code} size=%{size_download}\n" -u "kin:fb2026" "http://146.190.93.148/footbreak/" --max-time 8
curl -sS -o /dev/null -w "external /crown/ code=%{http_code} size=%{size_download}\n" -u "kin:fb2026" "http://146.190.93.148/crown/" --max-time 8

echo ""
echo "===== H. Data freshness ====="
curl -sS -u "kin:fb2026" "http://127.0.0.1/crown/data.json" 2>&1 | python3 -c "import json,sys; d=json.load(sys.stdin); print('crown generated_at:', d.get('generated_at', d.get('meta',{}).get('generated_at','?')))" 2>&1 | head -3
curl -sS -u "kin:fb2026" "http://127.0.0.1/footbreak/data.json" 2>&1 | python3 -c "import json,sys; d=json.load(sys.stdin); print('fb generated_at:', d.get('generated_at', d.get('meta',{}).get('generated_at','?')))" 2>&1 | head -3

echo ""
echo "===== I. 舊 realm 應該 401（Chrome cache 唔啱）====="
curl -sS -o /dev/null -w "old crown pw code=%{http_code}\n" -u "crown:crown" "http://127.0.0.1/crown/"
curl -sS -o /dev/null -w "old fb pw code=%{http_code}\n" -u "footbreak:footbreak" "http://127.0.0.1/footbreak/"

echo ""
echo "===== 最終登入資料 ====="
echo ""
echo "URL:      http://146.190.93.148/footbreak/"
echo "Username: kin"
echo "Password: fb2026"
echo "Realm:    fb_2026"
echo ""
echo "URL:      http://146.190.93.148/crown/"
echo "Username: kin"
echo "Password: fb2026"
echo "Realm:    crown_2026"
echo ""
echo "同一 port 80，唔靠 8080/8090，家用 network 一定通。"
echo ""
echo "===== DONE ====="
