#!/usr/bin/env bash
set -euo pipefail
echo "===== nginx conf ====="
cat /etc/nginx/sites-available/unified-dashboard
echo ""
echo "===== .htpasswd files ====="
ls -la /etc/nginx/.htpasswd*
echo ""
echo "===== crown v2 fixtures loader src ====="
grep -n "refresh_fixtures\|def refresh\|match_id\|['\"]id['\"]\\|kickoff" /opt/footbreak/stage_engine_v2/fixtures.py | head -60
echo ""
echo "===== fb data.json schema sample ====="
python3 -c "import json; d=json.load(open('/var/www/footbreak/data.json')); m=d['matches'][0] if isinstance(d,dict) else d[0]; print('top keys:', list(d.keys()) if isinstance(d,dict) else 'list'); print('match keys:', list(m.keys())); print('kickoff_hkt:', m.get('kickoff_hkt')); print('id/match_id:', m.get('id'), m.get('match_id')); print('stages count:', len(m.get('stages',[])))"
