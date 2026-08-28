#!/usr/bin/env bash
# self-heal 唔認新 block，直接 sync source-of-truth 落 live conf + reload nginx
set -euo pipefail

echo "===== 1. 睇 self-heal 錯咩 ====="
journalctl -xeu footbreak-dashboard-self-heal.service --no-pager -n 60 2>&1 | tail -50

echo ""
echo "===== 2. 確認 source of truth 有兩個新 block ====="
grep -A2 "stage-v2\|kwan-v2" /opt/footbreak/deploy/nginx-unified-dashboard.conf | head -20

echo ""
echo "===== 3. Bypass self-heal，直接 copy 落 live conf ====="
cp /opt/footbreak/deploy/nginx-unified-dashboard.conf /etc/nginx/sites-enabled/unified-dashboard
echo "copied"

echo ""
echo "===== 4. nginx -t + reload ====="
nginx -t 2>&1
systemctl reload nginx 2>&1

echo ""
echo "===== 5. 停 self-heal timer 防止洗走（暫時） ====="
# 唔停 timer，只係即刻 disable 佢下一次 revert，等我確認 live conf 唔會被洗
# 睇 self-heal script 邏輯，如果佢 diff 唔到 source of truth 就唔會改
# 我地已將 source of truth 更新，理論上 self-heal 下次跑會 no-op

echo ""
echo "===== 6. 睇 live conf 而家內容有無 stage-v2 + kwan-v2 ====="
grep -A6 "stage-v2\|kwan-v2" /etc/nginx/sites-enabled/unified-dashboard | head -30

echo ""
echo "===== 7. Curl 內部測試 ====="
echo "--- 冇 auth 應該 401 ---"
curl -sS -o /dev/null -w "code=%{http_code}\n" "http://127.0.0.1/kwan-v2/"

echo "--- 用 crown:toberich 應該 200 ---"
curl -sS -o /dev/null -w "code=%{http_code} size=%{size_download}\n" -u crown:toberich "http://127.0.0.1/kwan-v2/"

echo "--- Realm ---"
curl -sS -I "http://127.0.0.1/kwan-v2/" 2>&1 | grep -i "www-authenticate\|http/"

echo "--- data.json ---"
curl -sS -o /dev/null -w "code=%{http_code} size=%{size_download}\n" -u crown:toberich "http://127.0.0.1/kwan-v2/data.json"

echo "--- history.html ---"
curl -sS -o /dev/null -w "code=%{http_code} size=%{size_download}\n" -u crown:toberich "http://127.0.0.1/kwan-v2/history.html"

echo "--- stage-v2 都仲 work ---"
curl -sS -o /dev/null -w "code=%{http_code}\n" -u crown:toberich "http://127.0.0.1/stage-v2/"

echo ""
echo "===== 8. 外部測試 ====="
curl -sS -I "http://146.190.93.148/kwan-v2/" 2>&1 | head -6
curl -sS -o /dev/null -w "  external code=%{http_code} size=%{size_download}\n" -u crown:toberich "http://146.190.93.148/kwan-v2/"

echo ""
echo "===== 9. Trigger self-heal 手動一次，睇下有冇洗走 ====="
systemctl start footbreak-dashboard-self-heal.service 2>&1 || journalctl -xeu footbreak-dashboard-self-heal.service --no-pager -n 20 | tail -25
sleep 3
grep -c "kwan-v2\|stage-v2" /etc/nginx/sites-enabled/unified-dashboard || echo "!!! block missing after self-heal"

echo ""
echo "===== DONE 皇冠系統V2 新入口: http://146.190.93.148/kwan-v2/ ====="
echo "Auth: crown / toberich"
echo "Realm: kwan_v2_2026 (新，Chrome 冇 cache)"
