#!/usr/bin/env bash
# 揾 crown 舊入口真實 password（因為 access log 見到 crown user 可以 200 拎 /crown/data.json）
# 同時檢查 realm 由咩 config 決定
set -euo pipefail

echo "===== 1. 檢查 htpasswd-crown 內容 raw ====="
cat /etc/nginx/.htpasswd-crown

echo ""
echo "===== 2. htpasswd 檔案權限 ====="
ls -la /etc/nginx/.htpasswd-crown

echo ""
echo "===== 3. Nginx worker 讀唔讀到 htpasswd（by nginx user） ====="
id nginx 2>/dev/null || id www-data
sudo -u www-data cat /etc/nginx/.htpasswd-crown 2>&1 | head -3

echo ""
echo "===== 4. 揾邊個 config file 定義 /kwan-v2/ 個 realm（而家係 Odds Radar） ====="
grep -rn "Odds Radar\|kwan_v2\|realm" /etc/nginx/ 2>/dev/null | head -30

echo ""
echo "===== 5. 睇 nginx 目前 loaded /kwan-v2/ 對應 config ====="
grep -rn "kwan-v2\|8084" /etc/nginx/sites-enabled/ 2>/dev/null

echo ""
echo "===== 6. 睇 /etc/nginx/sites-enabled/kwan-v2-backend 有無變 ====="
cat /etc/nginx/sites-enabled/kwan-v2-backend 2>&1 || echo "唔存在"

echo ""
echo "===== 7. 睇 self-heal script 邏輯（究竟做咩） ====="
ls -la /opt/footbreak/deploy/*self-heal* 2>&1
cat /opt/footbreak/deploy/dashboard-self-heal.sh 2>&1 | head -50 || \
  find /opt/footbreak -name "*self-heal*" -exec ls -la {} \; 2>&1 | head -10

echo ""
echo "===== 8. Nginx 全部 vhost 用 realm ====="
grep -rn 'auth_basic\s*"' /etc/nginx/ 2>&1 | grep -v ".bak" | head -20

echo ""
echo "===== 9. 試多個 hash type：用當前 htpasswd 內個 hash + toberich 對比 ====="
HASH=$(grep '^crown:' /etc/nginx/.htpasswd-crown | cut -d: -f2)
echo "current hash prefix: ${HASH:0:10}..."
# Python bcrypt/crypt verify
python3 <<PY 2>&1
import subprocess, sys
hash_str = """$HASH"""
print(f"hash: {hash_str[:15]}... (len={len(hash_str)})")
# 用 openssl passwd 或 python bcrypt
try:
    import bcrypt
    ok = bcrypt.checkpw(b"toberich", hash_str.encode())
    print(f"bcrypt verify 'toberich': {ok}")
except ImportError:
    print("no bcrypt module")
    # fallback openssl
    r = subprocess.run(["openssl", "passwd", "-apr1", "-salt", "test", "toberich"], capture_output=True, text=True)
    print(f"openssl apr1 test: {r.stdout.strip()}")
PY

echo ""
echo "===== 10. 用 crown user + 內部 curl 分別試多個 possible password ====="
for pw in toberich crown123 password letmein "" ; do
    code=$(curl -sS -o /dev/null -w "%{http_code}" -u "crown:$pw" "http://127.0.0.1/crown/data.json")
    echo "  crown:$pw → /crown/data.json = $code"
done

for pw in toberich crown123 password letmein ; do
    code=$(curl -sS -o /dev/null -w "%{http_code}" -u "crown:$pw" "http://127.0.0.1/kwan-v2/")
    echo "  crown:$pw → /kwan-v2/       = $code"
done

echo ""
echo "===== 11. Reload nginx，確保新 htpasswd 生效 ====="
nginx -t 2>&1
systemctl reload nginx 2>&1
sleep 1

echo ""
echo "===== 12. Reload 後再試 toberich ====="
curl -sS -o /dev/null -w "code=%{http_code}\n" -u crown:toberich "http://127.0.0.1/kwan-v2/"
curl -sS -o /dev/null -w "code=%{http_code}\n" -u crown:toberich "http://127.0.0.1/crown/data.json"

echo ""
echo "===== 13. 強制 restart nginx（唔係 reload，避免 config cache） ====="
systemctl restart nginx
sleep 2

echo ""
echo "===== 14. Restart 後再試 ====="
curl -sS -o /dev/null -w "code=%{http_code}\n" -u crown:toberich "http://127.0.0.1/kwan-v2/"
curl -sS -I "http://127.0.0.1/kwan-v2/" 2>&1 | grep -i "www-auth\|http/"

echo ""
echo "===== DONE ====="
