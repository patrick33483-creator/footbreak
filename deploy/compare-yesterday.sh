#!/usr/bin/env bash
set -uo pipefail

echo "===== A. /var/www/footbreak/index.html metadata ====="
ls -la /var/www/footbreak/index.html /var/www/footbreak/data.json /var/www/footbreak/app.js 2>&1

echo ""
echo "===== B. /var/www/crown/index.html metadata ====="
ls -la /var/www/crown/index.html /var/www/crown/data.json /var/www/crown/app.js 2>&1 2>/dev/null | head -10
ls -la /var/www/crown/ 2>&1 | head -20

echo ""
echo "===== C. footbreak data.json 頂層結構 ====="
python3 -c "
import json
d = json.load(open('/var/www/footbreak/data.json'))
print('keys:', list(d.keys()) if isinstance(d, dict) else 'array')
if isinstance(d, dict):
    for k in d.keys():
        v = d[k]
        if isinstance(v, list):
            print(f'  {k}: list len={len(v)}')
        elif isinstance(v, dict):
            print(f'  {k}: dict keys={list(v.keys())[:8]}')
        else:
            print(f'  {k}: {str(v)[:80]}')
"

echo ""
echo "===== D. crown data.json 頂層結構 ====="
python3 -c "
import json
d = json.load(open('/var/www/crown/data.json'))
print('keys:', list(d.keys()) if isinstance(d, dict) else 'array')
if isinstance(d, dict):
    for k in d.keys():
        v = d[k]
        if isinstance(v, list):
            print(f'  {k}: list len={len(v)}')
        elif isinstance(v, dict):
            print(f'  {k}: dict keys={list(v.keys())[:8]}')
        else:
            print(f'  {k}: {str(v)[:80]}')
"

echo ""
echo "===== E. 昨晚 git log（過去 24 小時 conf/deploy 改動）====="
cd /opt/footbreak
git log --since='24 hours ago' --oneline 2>&1 | head -30

echo ""
echo "===== F. 過去 24 小時 conf/deploy 改動明細 ====="
git log --since='24 hours ago' --pretty=format:'%h %s' -- deploy/ 2>&1 | head -20

echo ""
echo "===== G. index.html tail (footbreak) ====="
tail -c 800 /var/www/footbreak/index.html

echo ""
echo "===== H. index.html tail (crown) ====="
tail -c 800 /var/www/crown/index.html
