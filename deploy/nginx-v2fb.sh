#!/usr/bin/env bash
# 為 /v2/footbreak/ 加 nginx location blocks，重用 .htpasswd-footbreak 認證
set -euo pipefail
CONF=/etc/nginx/sites-available/unified-dashboard

if grep -q "location.*/v2/footbreak" "$CONF"; then
  echo "-- /v2/footbreak/ 已存在 --"
else
  echo "-- 備份並添加 blocks --"
  cp "$CONF" "$CONF.bak-$(date +%s)"
  # 揾 /v2/crown/ 塊嘅結尾（location ^~ /v2/crown/ { ... } 嘅收官 } ），
  # 然後喺後面插入 /v2/footbreak/ 塊
  python3 <<'PY'
import re
p = "/etc/nginx/sites-available/unified-dashboard"
text = open(p).read()
insert = """    # V2 馬會 shadow dashboard
    location = /v2/footbreak { return 301 /v2/footbreak/; }
    location = /v2/footbreak/data.json {
        auth_basic "v2_2026";
        auth_basic_user_file /etc/nginx/.htpasswd-footbreak;
        alias /var/www/stage_engine_v2_fb/data.json;
        add_header Cache-Control "no-store, must-revalidate";
        expires -1;
    }
    location ^~ /v2/footbreak/ {
        auth_basic "v2_2026";
        auth_basic_user_file /etc/nginx/.htpasswd-footbreak;
        alias /var/www/stage_engine_v2_fb/;
        index index.html;
    }
"""
# 揾 V2 crown 塊嘅結束：location ^~ /v2/crown/ { ... }
# 用 balanced brace 抓最後嗰個 }
m = re.search(r"location\s+\^~\s+/v2/crown/\s*\{", text)
if not m:
    raise SystemExit("cannot find /v2/crown/ block start")
# 由 open brace 開始搵匹配嘅 close brace
depth = 0
i = m.end() - 1  # 指向 {
end_idx = None
while i < len(text):
    if text[i] == '{':
        depth += 1
    elif text[i] == '}':
        depth -= 1
        if depth == 0:
            end_idx = i + 1
            break
    i += 1
if end_idx is None:
    raise SystemExit("cannot find /v2/crown/ block end")
new_text = text[:end_idx] + "\n" + insert + text[end_idx:]
open(p, 'w').write(new_text)
print("  ✅ nginx conf 已插入 /v2/footbreak/ blocks")
PY
fi

echo "-- nginx test --"
nginx -t
echo "-- reload --"
systemctl reload nginx
sleep 2

echo "-- 驗證 (kin/fb2026) --"
echo "GET /v2/footbreak/:"
curl -s -o /dev/null -w "HTTP %{http_code}\n" -u kin:fb2026 http://127.0.0.1/v2/footbreak/
echo "GET /v2/footbreak/data.json:"
curl -s -o /tmp/v2fb.json -w "HTTP %{http_code}\n" -u kin:fb2026 http://127.0.0.1/v2/footbreak/data.json
python3 -c "import json; d=json.load(open('/tmp/v2fb.json')); print(f'schema: {d.get(\"schema_version\")}, fixtures_count: {d.get(\"fixtures_count\")}')" 2>&1 || cat /tmp/v2fb.json | head -20
echo "GET /v2/footbreak/index.html:"
curl -s -o /dev/null -w "HTTP %{http_code}\n" -u kin:fb2026 http://127.0.0.1/v2/footbreak/index.html
echo "-- 錯 password 應該 401 --"
curl -s -o /dev/null -w "HTTP %{http_code}\n" -u kin:wrong http://127.0.0.1/v2/footbreak/
