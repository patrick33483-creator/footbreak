#!/usr/bin/env bash
set -uo pipefail

echo "===== verify_at $(date -Iseconds) ====="

echo "--- update.sh cycle check（過去 10 分鐘）---"
grep "update\.sh" /var/log/auth.log 2>&1 | tail -5

echo ""
echo "--- 8080 (fb-v2) ---"
curl -sS -I "http://127.0.0.1:8080/" 2>&1 | grep -iE "www-auth|http/"
curl -sS -o /dev/null -w "internal code=%{http_code} size=%{size_download}\n" -u "kin:fb2026" "http://127.0.0.1:8080/"
curl -sS -o /dev/null -w "external code=%{http_code} size=%{size_download}\n" -u "kin:fb2026" "http://146.190.93.148:8080/" --max-time 10

echo ""
echo "--- 8090 (kwan-v2) ---"
curl -sS -I "http://127.0.0.1:8090/" 2>&1 | grep -iE "www-auth|http/"
curl -sS -o /dev/null -w "internal code=%{http_code} size=%{size_download}\n" -u "kin:fb2026" "http://127.0.0.1:8090/"
curl -sS -o /dev/null -w "external code=%{http_code} size=%{size_download}\n" -u "kin:fb2026" "http://146.190.93.148:8090/" --max-time 10

echo ""
echo "--- Data freshness ---"
curl -sS -u "kin:fb2026" "http://127.0.0.1:8080/data.json" | python3 -c "import json,sys; d=json.load(sys.stdin); print('fb generated_at:', d.get('generated_at', d.get('meta',{}).get('generated_at','?')))" 2>&1
curl -sS -u "kin:fb2026" "http://127.0.0.1:8090/data.json" | python3 -c "import json,sys; d=json.load(sys.stdin); print('kwan generated_at:', d.get('generated_at', d.get('meta',{}).get('generated_at','?')))" 2>&1

echo ""
echo "--- conf files 仲存唔存在 ---"
ls -la /etc/nginx/sites-enabled/fb-v2-public /etc/nginx/sites-enabled/kwan-v2-public 2>&1
ls -la /etc/nginx/.htpasswd-fbv2 /etc/nginx/.htpasswd-kwanv2 2>&1

echo ""
echo "--- 有無 v2 相關 error 喺 nginx error log ---"
tail -20 /var/log/nginx/error.log 2>&1 | grep -iE "v2|htpasswd|kwan|8080|8090" | head -10

echo ""
echo "--- 皇冠 timers 仍 armed ---"
systemctl list-timers --all --no-pager 2>&1 | grep -iE "crown-round-update|stage-engine-v2-tick|crown-tick\b" | head -10

echo ""
echo "===== DONE ====="
