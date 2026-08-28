#!/usr/bin/env bash
# 診斷點解你入唔到 /kwan-v2/
set -euo pipefail

echo "===== 1. 你 IP 最新 hit /kwan-v2/ 嘅 nginx access log ====="
grep "kwan-v2\|stage-v2" /var/log/nginx/access.log 2>&1 | tail -20 || echo "no matches"

echo ""
echo "===== 2. nginx error log 最新 auth 錯 ====="
tail -30 /var/log/nginx/error.log 2>&1 | grep -i "kwan-v2\|stage-v2\|password\|user\|htpasswd" || echo "no matches"

echo ""
echo "===== 3. htpasswd-crown 內容（sanitized） ====="
if [ -f /etc/nginx/.htpasswd-crown ]; then
    echo "行數：$(wc -l < /etc/nginx/.htpasswd-crown)"
    echo "已存在 user："
    cut -d: -f1 /etc/nginx/.htpasswd-crown
else
    echo "!!! htpasswd-crown 唔存在"
fi

echo ""
echo "===== 4. 用 crown:toberich 從內部 verify auth ====="
curl -sS -o /dev/null -w "code=%{http_code} size=%{size_download}\n" -u crown:toberich "http://127.0.0.1/kwan-v2/"
curl -sS -o /dev/null -w "code=%{http_code} size=%{size_download}\n" -u crown:toberich "http://127.0.0.1/kwan-v2/data.json"

echo ""
echo "===== 5. 重置 htpasswd-crown：確保 crown:toberich 一定 work ====="
apt-get install -y apache2-utils >/dev/null 2>&1 || true

# 備份
cp /etc/nginx/.htpasswd-crown /etc/nginx/.htpasswd-crown.bak.$(date +%s) 2>/dev/null || true

# 重新寫 crown:toberich（保留其他 user）
# 用 -c 會 overwrite；用不 -c 會 update 對應 user
htpasswd -bB /etc/nginx/.htpasswd-crown crown "toberich" 2>&1
echo "htpasswd updated"

echo ""
echo "===== 6. 再 verify ====="
curl -sS -o /dev/null -w "code=%{http_code} size=%{size_download}\n" -u crown:toberich "http://127.0.0.1/kwan-v2/"

echo ""
echo "===== 7. 外部 IP verify ====="
curl -sS -o /dev/null -w "external code=%{http_code} size=%{size_download}\n" -u crown:toberich "http://146.190.93.148/kwan-v2/"

echo ""
echo "===== 8. Realm 確認 ====="
curl -sS -I "http://146.190.93.148/kwan-v2/" 2>&1 | grep -i "www-authenticate\|http/"

echo ""
echo "===== 9. 你 IP 58.82.211.231 最近 5 分鐘所有 request ====="
awk -v date="$(date +'%d/%b/%Y:%H')" '$1 == "58.82.211.231" && $4 ~ date' /var/log/nginx/access.log 2>&1 | tail -30 || echo "冇 log 或 log 位置唔同"

echo ""
echo "===== DONE ====="
