#!/usr/bin/env bash
set -uo pipefail

echo "===== A. /var/www/stage_engine_v2/ 內容 ====="
ls -la /var/www/stage_engine_v2/ 2>&1 | head -20

echo ""
echo "===== B. index.html 有無 + size + mtime ====="
if [ -f /var/www/stage_engine_v2/index.html ]; then
    stat -c '%s bytes, mtime=%y' /var/www/stage_engine_v2/index.html
    head -c 300 /var/www/stage_engine_v2/index.html
    echo ""
fi

echo ""
echo "===== C. data.json size + generated_at ====="
if [ -f /var/www/stage_engine_v2/data.json ]; then
    stat -c '%s bytes, mtime=%y' /var/www/stage_engine_v2/data.json
    python3 -c "
import json
d=json.load(open('/var/www/stage_engine_v2/data.json'))
print('keys:', list(d.keys())[:10] if isinstance(d,dict) else 'array')
if isinstance(d,dict):
    print('generated_at:', d.get('generated_at'))
    print('schema_version:', d.get('schema_version'))
" 2>&1 | head -10
fi

echo ""
echo "===== D. stage-engine-v2-tick 最近 log ====="
journalctl -u stage-engine-v2-tick.service --since '30 min ago' --no-pager 2>&1 | tail -20

echo ""
echo "===== E. history.html 有無 ====="
ls /var/www/stage_engine_v2/history*.html /var/www/stage_engine_v2/history*.json 2>&1 | head -10
