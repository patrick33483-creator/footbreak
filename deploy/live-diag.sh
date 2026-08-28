#!/usr/bin/env bash
# 即時診斷：kwan-v2 而家 work 唔 work，如果唔 work 睇邊個 revert 咗
set -uo pipefail

echo "===== 1. 目前外部 request status ====="
curl -sS -I "http://127.0.0.1/kwan-v2/" 2>&1 | head -5
echo "---"
curl -sS -o /dev/null -w "kin:fb2026 code=%{http_code} size=%{size_download}\n" -u "kin:fb2026" "http://127.0.0.1/kwan-v2/"

echo ""
echo "===== 2. Live conf 有無 kwan-v2 block ====="
grep -c "kwan-v2" /etc/nginx/sites-enabled/unified-dashboard 2>&1
grep -c "kwan-v2" /etc/nginx/sites-available/unified-dashboard 2>&1
grep -c "kwan-v2" /opt/footbreak/deploy/nginx-unified-dashboard.conf 2>&1

echo ""
echo "===== 3. 個個 conf mtime ====="
stat -c "%y %n" /etc/nginx/sites-enabled/unified-dashboard 2>&1
stat -c "%y %n" /etc/nginx/sites-available/unified-dashboard 2>&1
stat -c "%y %n" /opt/footbreak/deploy/nginx-unified-dashboard.conf 2>&1

echo ""
echo "===== 4. Symlink 目前指向邊 ====="
ls -la /etc/nginx/sites-enabled/unified-dashboard 2>&1

echo ""
echo "===== 5. htpasswd-crown 有無 kin 用戶 ====="
grep "^kin:\|^crown:" /etc/nginx/.htpasswd-crown 2>&1

echo ""
echo "===== 6. 最近 5 分鐘 nginx / footbreak journal ====="
journalctl --since "5 minutes ago" --no-pager 2>&1 | grep -iE "nginx|unified|htpasswd|kwan|self-heal|reload|restart" | grep -v "kwan-v2/data.json\|kwan-v2/history" | tail -30

echo ""
echo "===== 7. Self-heal 而家 status ====="
systemctl status footbreak-dashboard-self-heal.service --no-pager 2>&1 | head -10
systemctl status footbreak-dashboard-self-heal.timer --no-pager 2>&1 | head -8

echo ""
echo "===== 8. 有無其他 timer 或 unit 引到 nginx reload ====="
grep -rln "nginx.*reload\|reload nginx\|systemctl.*nginx" /etc/systemd/ /opt/footbreak/ 2>/dev/null | head -10

echo ""
echo "===== 9. nginx worker 有幾多個（有無 pending reload） ====="
ps auxf | grep nginx | head -10

echo ""
echo "===== 10. Access log 最近 20 條 kwan-v2 request ====="
tail -50 /var/log/nginx/access.log 2>&1 | grep "kwan-v2" | tail -20

echo ""
echo "===== 11. Error log 最近 20 條 ====="
tail -30 /var/log/nginx/error.log 2>&1 | tail -20

echo ""
echo "===== 12. 用戶 IP 58.82.211.231 最近 3 分鐘 request ====="
grep "58.82.211.231" /var/log/nginx/access.log 2>&1 | tail -15

echo ""
echo "===== DONE ====="
