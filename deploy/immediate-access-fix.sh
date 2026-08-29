#!/usr/bin/env bash
# 即刻解決 /footbreak/ 401：加 kin 到 .htpasswd-footbreak
# 同埋 force install 新 conf（帶新 realm）
set -uo pipefail

echo "===== A. 目前 /opt/footbreak repo HEAD ====="
cd /opt/footbreak
git log --oneline -3 2>&1 | head -3
git status --short 2>&1 | head -5

echo ""
echo "===== B. 嘗試 update.sh 拉最新 ====="
# update.sh 應該可以拉到（因為佢係 GitHub Actions push webhook 觸發，用專門 deploy key）
# 我哋 direct call 佢
/opt/footbreak/deploy/update.sh main 2>&1 | tail -30 || echo "update.sh returned non-zero"

echo ""
echo "===== C. Pull 之後 conf 有無新 realm ====="
grep -E "auth_basic|htpasswd-" /opt/footbreak/deploy/nginx-unified-dashboard.conf | head -15

echo ""
echo "===== D. 安全網：無論如何，都加 kin 到 .htpasswd-footbreak ====="
if ! command -v htpasswd >/dev/null; then apt-get install -y apache2-utils >/dev/null 2>&1; fi
# -b 唔洗 prompt；appen (無 -c) 保留 footbreak 用戶
htpasswd -b /etc/nginx/.htpasswd-footbreak kin "fb2026"
cat /etc/nginx/.htpasswd-footbreak
chown www-data:www-data /etc/nginx/.htpasswd-footbreak
chmod 640 /etc/nginx/.htpasswd-footbreak

echo ""
echo "===== E. 保險：install source conf 落 nginx 即刻生效 ====="
install -m 0644 /opt/footbreak/deploy/nginx-unified-dashboard.conf /etc/nginx/sites-available/unified-dashboard

nginx -t 2>&1
systemctl reload nginx
sleep 2

echo ""
echo "===== F. 內部驗證 ====="
echo "--- /footbreak/ realm ---"
curl -sS -I "http://127.0.0.1/footbreak/" 2>&1 | grep -iE "www-auth|http/"
echo "--- kin fb2026 ---"
curl -sS -o /dev/null -w "footbreak/  code=%{http_code} size=%{size_download}\n" -u "kin:fb2026" "http://127.0.0.1/footbreak/"
curl -sS -o /dev/null -w "footbreak/data.json  code=%{http_code} size=%{size_download}\n" -u "kin:fb2026" "http://127.0.0.1/footbreak/data.json"

echo ""
echo "--- /crown/ realm ---"
curl -sS -I "http://127.0.0.1/crown/" 2>&1 | grep -iE "www-auth|http/"
echo "--- kin fb2026 ---"
curl -sS -o /dev/null -w "crown/  code=%{http_code} size=%{size_download}\n" -u "kin:fb2026" "http://127.0.0.1/crown/"
curl -sS -o /dev/null -w "crown/data.json  code=%{http_code} size=%{size_download}\n" -u "kin:fb2026" "http://127.0.0.1/crown/data.json"

echo ""
echo "===== G. 外部（public IP）驗證 ====="
curl -sS -I "http://146.190.93.148/footbreak/" 2>&1 | grep -iE "www-auth|http/" | head -3
curl -sS -o /dev/null -w "external /footbreak/  code=%{http_code} size=%{size_download}\n" -u "kin:fb2026" "http://146.190.93.148/footbreak/" --max-time 8
curl -sS -o /dev/null -w "external /crown/  code=%{http_code} size=%{size_download}\n" -u "kin:fb2026" "http://146.190.93.148/crown/" --max-time 8

echo ""
echo "===== H. Data freshness ====="
curl -sS -u "kin:fb2026" "http://127.0.0.1/crown/data.json" 2>&1 | head -c 500 | python3 -c "import json,sys; d=json.loads(sys.stdin.read()); print('crown generated_at:', d.get('generated_at', d.get('meta',{}).get('generated_at','?')))" 2>&1 | head -3
curl -sS -u "kin:fb2026" "http://127.0.0.1/footbreak/data.json" 2>&1 | head -c 500 | python3 -c "import json,sys; d=json.loads(sys.stdin.read()); print('fb generated_at:', d.get('generated_at', d.get('meta',{}).get('generated_at','?')))" 2>&1 | head -3

echo ""
echo "===== 最終登入資料 ====="
echo ""
echo "URL:      http://146.190.93.148/footbreak/"
echo "URL:      http://146.190.93.148/crown/"
echo "Username: kin"
echo "Password: fb2026"
echo ""
echo "同一 port 80，Chrome 可能仲 cache 住舊 realm 名 —"
echo "如出現 Chrome 401 loop：私隱瀏覽模式試，或者 clear cache 一次。"
echo ""
echo "===== DONE ====="
