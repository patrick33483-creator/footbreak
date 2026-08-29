#!/usr/bin/env bash
# READ-ONLY
set -uo pipefail

echo "===== A. V2 馬會 — 揾 tick engine / data / dashboard ====="
ls -la /var/www/stage_engine_v2_footbreak/ 2>&1 | head -5 || true
ls -la /var/www/footbreak_v2/ 2>&1 | head -5 || true
ls -la /var/www/v2_footbreak/ 2>&1 | head -5 || true
systemctl list-units --all --no-pager 2>&1 | grep -iE "stage.*v2|v2.*footbreak|footbreak.*v2" | head -20

echo ""
echo "===== B. Stage engine v2 tick — 睇實際處理咩 ====="
systemctl cat stage-engine-v2-tick.service --no-pager 2>&1 | head -30

echo ""
echo "===== C. V2 tick script 睇下產生咩 data ====="
cat /opt/footbreak/deploy/stage-engine-v2-tick.sh 2>&1 | head -60 || \
cat /opt/footbreak/stage_engine_v2/tick.py 2>&1 | head -40 || \
find /opt/footbreak -name '*stage_engine_v2*' -o -name '*stage-engine-v2*' 2>&1 | head -20

echo ""
echo "===== D. V2 data.json 有無 footbreak / hkjc 相關內容 ====="
python3 <<'PY'
import json
try:
    d = json.load(open('/var/www/stage_engine_v2/data.json'))
    print('top keys:', list(d.keys())[:20])
    if 'fixtures' in d:
        fs = d['fixtures']
        print(f'fixtures count: {len(fs)}')
        if fs:
            f0 = fs[0]
            print(f'first fixture keys: {list(f0.keys())[:30]}')
            # 睇有無 hkjc / footbreak
            for k in f0.keys():
                v = f0[k]
                if isinstance(v,(str,int,float,bool)) and ('hkjc' in k.lower() or 'footbreak' in k.lower()):
                    print(f'  {k}: {str(v)[:80]}')
except Exception as e:
    print(f'error: {e}')
PY

echo ""
echo "===== E. 舊時盤路雷達 — service / URL ====="
systemctl list-units --all --no-pager 2>&1 | grep -iE "radar|odds|pinnacle|hkjc" | grep -v crown | head -20
echo "---"
# 揾 nginx location
grep -rE "location.*(radar|odds)" /etc/nginx/ 2>&1 | head -10
echo "---"
# 揾 www 目錄
ls /var/www/ 2>&1

echo ""
echo "===== F. Nginx conf 全部 location ====="
nginx -T 2>&1 | grep -E "^\s*(location|server_name|listen)" | head -50
