#!/usr/bin/env bash
set -uo pipefail

echo "===== A. nginx service state ====="
systemctl status nginx --no-pager 2>&1 | head -15

echo ""
echo "===== B. 兩個 conf 仲喺唔喺 ====="
ls -la /etc/nginx/sites-enabled/fb-v2-public /etc/nginx/sites-enabled/kwan-v2-public 2>&1
ls -la /etc/nginx/sites-available/fb-v2-public /etc/nginx/sites-available/kwan-v2-public 2>&1
ls -la /etc/nginx/.htpasswd-fbv2 /etc/nginx/.htpasswd-kwanv2 2>&1

echo ""
echo "===== C. Port 8080 8090 有無 listen ====="
ss -tlnp 2>&1 | grep -E ":8080|:8090" | head -10

echo ""
echo "===== D. Nginx -T 睇有無 load 到我哋兩個 server ====="
nginx -T 2>&1 | grep -E "listen 8080|listen 8090|fb_v2_new|kwan_v2_new" | head -10

echo ""
echo "===== E. UFW / iptables 狀態 ====="
ufw status verbose 2>&1 | head -20
iptables -L INPUT -n 2>&1 | head -20

echo ""
echo "===== F. 內部連唔連得到 ====="
curl -sS -o /dev/null -w "8080 internal code=%{http_code}\n" -u "kin:fb2026" "http://127.0.0.1:8080/" --max-time 5
curl -sS -o /dev/null -w "8090 internal code=%{http_code}\n" -u "kin:fb2026" "http://127.0.0.1:8090/" --max-time 5

echo ""
echo "===== G. 外部（via public IP）連唔連得到 ====="
curl -sS -o /dev/null -w "8080 public code=%{http_code}\n" -u "kin:fb2026" "http://146.190.93.148:8080/" --max-time 8
curl -sS -o /dev/null -w "8090 public code=%{http_code}\n" -u "kin:fb2026" "http://146.190.93.148:8090/" --max-time 8

echo ""
echo "===== H. 過去 30 分鐘 error log ====="
tail -50 /var/log/nginx/error.log 2>&1 | tail -20

echo ""
echo "===== I. 過去 30 分鐘 update.sh 有無 disable 到我啲 conf ====="
grep -E "sites-enabled|fb-v2-public|kwan-v2-public|listen 8080|listen 8090" /var/log/auth.log 2>&1 | tail -10

echo ""
echo "===== J. 目前 sites-enabled 全部 ====="
ls /etc/nginx/sites-enabled/

echo ""
echo "===== K. 過去 30 分鐘有無 nginx restart / reload ====="
journalctl -u nginx --since "1 hour ago" --no-pager 2>&1 | tail -20

echo ""
echo "===== DONE ====="
