#!/usr/bin/env bash
# Deploy V2 footbreak side-car service.
# 1) 部署 stage_engine_v2_fb Python 模組到 /opt/footbreak/stage_engine_v2_fb/
# 2) 部署 systemd unit + timer
# 3) 部署 /var/www/stage_engine_v2_fb/index.html
# 4) 更新 nginx conf 加 /v2/footbreak/ route
# 5) 啟動 timer + 立刻 tick 一次驗證
set -euo pipefail

STAGE=/tmp/v2fb-stage
CONF_DST=/etc/nginx/sites-available/unified-dashboard
CODE_SRC="${STAGE}/stage_engine_v2_fb"
CODE_DST=/opt/footbreak/stage_engine_v2_fb
ANALYSIS_SRC="${STAGE}/analysis"
ANALYSIS_DST=/opt/footbreak/analysis
WWW_DST=/var/www/stage_engine_v2_fb
LEDGER_DIR=/var/lib/footbreak/stage_engine_v2_fb
LOG_DIR=/var/log/footbreak

echo "===== 1. 檢查 stage payload ====="
ls -R "$STAGE"

echo "===== 2. 部署 Python 模組 ====="
mkdir -p "$CODE_DST"
cp "$CODE_SRC/__init__.py" "$CODE_SRC/__main__.py" "$CODE_SRC/predictor_fb.py" "$CODE_SRC/cli_fb.py" "$CODE_DST/"
mkdir -p "$ANALYSIS_DST"
cp "$ANALYSIS_SRC/footbreak_direction_path_conditions.py" \
   "$ANALYSIS_SRC/direction_path_conditions.py" \
   "$ANALYSIS_SRC/three_stage_historical_backtest.py" "$ANALYSIS_DST/"
ls -l "$CODE_DST/"

echo "===== 3. 部署 dashboard html + 建立 dirs ====="
mkdir -p "$WWW_DST" "$LEDGER_DIR" "$LOG_DIR"
cp "$CODE_SRC/index.html" "$WWW_DST/index.html"
cp "$CODE_SRC/conditions.html" "$WWW_DST/conditions.html"
# 生成 initial empty data.json 避免第一次 fetch 404
if [ ! -f "$WWW_DST/data.json" ]; then
  echo '{"schema_version":"stage-engine-v2-fb","fixtures_count":0,"fixtures":[]}' > "$WWW_DST/data.json"
fi
ls -l "$WWW_DST/"

echo "===== 4. 檢查 legacy fb data.json 存在 ====="
ls -l /var/www/footbreak/data.json

echo "===== 5. Dry-run tick 驗證 code 正常 ====="
cd /opt/footbreak
DRY=$(PYTHONPATH=/opt/footbreak ./.venv/bin/python3 -m stage_engine_v2_fb dry-run --data /var/www/footbreak/data.json 2>&1)
echo "$DRY" | python3 -c "import sys, json; d=json.loads(sys.stdin.read()); print(f'dry-run OK: fixtures={d[\"fixtures_upcoming\"]}, fired={d[\"fired_count\"]}, elapsed={d[\"elapsed_seconds\"]:.3f}s'); s=d.get('fired',[]); p=sum(1 for x in s if x.get('publish')); print(f'  publish approved: {p}, gate rejected: {len(s)-p}')" || { echo 'DRY-RUN FAILED:'; echo "$DRY" | head -20; exit 1; }

echo "===== 6. 部署 systemd unit + timer ====="
cp "$CODE_SRC/stage-engine-v2-fb-tick.service" /etc/systemd/system/stage-engine-v2-fb-tick.service
cp "$CODE_SRC/stage-engine-v2-fb-tick.timer" /etc/systemd/system/stage-engine-v2-fb-tick.timer
cp "$CODE_SRC/footbreak-direction-path-conditions.service" /etc/systemd/system/footbreak-direction-path-conditions.service
cp "$CODE_SRC/footbreak-direction-path-conditions.timer" /etc/systemd/system/footbreak-direction-path-conditions.timer
systemctl daemon-reload

echo "===== 7. 首次 tick（唔啟用 timer 先驗證 service）====="
systemctl start stage-engine-v2-fb-tick.service
sleep 3
systemctl status stage-engine-v2-fb-tick.service --no-pager -l | head -15 || true
echo "-- last log --"
tail -30 /var/log/footbreak/stage-v2-fb-tick.log 2>&1 | head -60

echo "===== 8. 驗證 ledger + dashboard 有寫入 ====="
ls -l "$LEDGER_DIR/" "$WWW_DST/" 2>&1
if [ -f "$LEDGER_DIR/ledger.json" ]; then
  python3 -c "import json; d=json.load(open('$LEDGER_DIR/ledger.json')); print(f'ledger fixtures: {len(d.get(\"fixtures\",{}))}'); [print(f'  {v.get(\"home\")} vs {v.get(\"away\")} - stages: {list((v.get(\"stages\") or {}).keys())}') for k,v in list(d.get('fixtures',{}).items())[:5]]"
fi
if [ -f "$WWW_DST/data.json" ]; then
  python3 -c "import json; d=json.load(open('$WWW_DST/data.json')); print(f'dashboard fixtures_count: {d.get(\"fixtures_count\")}')"
fi

echo "===== 9. Enable + start timer ====="
systemctl enable --now stage-engine-v2-fb-tick.timer
mkdir -p /var/lib/footbreak/footbreak-direction-path-conditions
systemctl enable --now footbreak-direction-path-conditions.timer
systemctl start footbreak-direction-path-conditions.service
systemctl list-timers stage-engine-v2-fb-tick.timer --no-pager | head -5
systemctl list-timers footbreak-direction-path-conditions.timer --no-pager | head -5

echo "===== 10. 更新 nginx conf 加 /v2/footbreak/ ====="
# 只有喺 unified-dashboard conf 未有 /v2/footbreak 時先加
if ! grep -q "location.*=.*/v2/footbreak" "$CONF_DST"; then
  echo "  -- 添加 /v2/footbreak/ blocks --"
  # 備份
  cp "$CONF_DST" "$CONF_DST.bak-$(date +%s)"
  # 揾 /v2/crown/ block 後面加 /v2/footbreak/ block
  python3 <<PY
import re
p = "$CONF_DST"
text = open(p).read()
insert = """
    # V2 footbreak（馬會 V2 shadow）
    location = /v2/footbreak { return 301 /v2/footbreak/; }
    location = /v2/footbreak/data.json {
        auth_basic "v2_2026";
        auth_basic_user_file /etc/nginx/.htpasswd-fbv2;
        alias /var/www/stage_engine_v2_fb/data.json;
        default_type application/json;
    }
    location ^~ /v2/footbreak/ {
        auth_basic "v2_2026";
        auth_basic_user_file /etc/nginx/.htpasswd-fbv2;
        alias /var/www/stage_engine_v2_fb/;
        index index.html;
        try_files \$uri \$uri/ /v2/footbreak/index.html;
    }
"""
# 揾 V2 crown block 嘅結尾（location ^~ /v2/crown/ 塊結束嘅 }）
pattern = r"(location \^~ /v2/crown/\s*\{[^}]*try_files[^;]+;\s*\})"
m = re.search(pattern, text)
if m:
    new_text = text[:m.end()] + "\n" + insert + text[m.end():]
    open(p, 'w').write(new_text)
    print("  ✅ nginx conf 已插入 /v2/footbreak/ blocks")
else:
    print("  ❌ 揾唔到 /v2/crown/ block，手動加")
PY
else
  echo "  -- /v2/footbreak/ 已存在 --"
fi

# 舊伺服器已有主 route 時，仍要補上條件 JSON 的 no-cache exact route。
if ! grep -q "location = /v2/footbreak/direction-path-conditions.json" "$CONF_DST"; then
  cp "$CONF_DST" "$CONF_DST.bak-conditions-$(date +%s)"
  python3 <<PY
p = "$CONF_DST"
text = open(p).read()
anchor = """    location ^~ /v2/footbreak/ {"""
block = """    location = /v2/footbreak/direction-path-conditions.json {
        auth_basic "v2_2026";
        auth_basic_user_file /etc/nginx/.htpasswd-fbv2;
        alias /var/www/stage_engine_v2_fb/direction-path-conditions.json;
        add_header Cache-Control "no-store, no-cache, must-revalidate" always;
        add_header Pragma "no-cache" always;
        expires -1;
    }
"""
if anchor not in text:
    raise SystemExit("missing /v2/footbreak/ nginx anchor")
open(p, "w").write(text.replace(anchor, block + anchor, 1))
print("  added Footbreak V2 conditions JSON route")
PY
fi

echo "===== 11. nginx test ====="
nginx -t

echo "===== 12. Reload nginx ====="
systemctl reload nginx
sleep 2
systemctl status nginx --no-pager -l | head -8 || true

echo "===== 13. Internal HTTP 驗證 ====="
echo "-- GET /v2/footbreak/ --"
curl -s -o /dev/null -w "HTTP %{http_code}\n" -u kin:fb2026 http://127.0.0.1/v2/footbreak/
echo "-- GET /v2/footbreak/data.json --"
curl -s -o /tmp/v2fb-data.json -w "HTTP %{http_code}\n" -u kin:fb2026 http://127.0.0.1/v2/footbreak/data.json
python3 -c "import json; d=json.load(open('/tmp/v2fb-data.json')); print(f'fixtures_count: {d.get(\"fixtures_count\")}, schema: {d.get(\"schema_version\")}')" 2>&1 | head -3
echo "-- GET /v2/footbreak/direction-path-conditions.json --"
curl -s -o /tmp/v2fb-conditions.json -w "HTTP %{http_code}\n" -u kin:fb2026 http://127.0.0.1/v2/footbreak/direction-path-conditions.json
python3 -c "import json; d=json.load(open('/tmp/v2fb-conditions.json')); assert d.get('system') == 'footbreak'; print(f'conditions: {d[\"summary\"][\"condition_count\"]}, historical: {d[\"summary\"][\"historical\"]}, prospective: {d[\"summary\"][\"prospective\"]}')"

echo "===== Deploy complete ====="
