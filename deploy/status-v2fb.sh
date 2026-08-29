#!/usr/bin/env bash
set -euo pipefail
echo "===== timer status ====="
systemctl status stage-engine-v2-fb-tick.timer --no-pager | head -8
systemctl list-timers stage-engine-v2-fb-tick.timer --no-pager | head -3
echo ""
echo "===== recent tick log (last 40) ====="
tail -40 /var/log/footbreak/stage-v2-fb-tick.log
echo ""
echo "===== ledger stats ====="
python3 <<PY
import json
try:
    d = json.load(open('/var/lib/footbreak/stage_engine_v2_fb/ledger.json'))
except Exception as e:
    print(f'ledger read error: {e}')
    raise SystemExit
fx = d.get('fixtures') or {}
print(f'fixtures: {len(fx)}')
stage_count = {}
publish_count = {'yes': 0, 'no': 0}
for k, v in fx.items():
    stages = v.get('stages') or {}
    for s, row in stages.items():
        stage_count[s] = stage_count.get(s, 0) + 1
        if row.get('publish_decision'):
            publish_count['yes'] += 1
        else:
            publish_count['no'] += 1
print(f'stages: {stage_count}')
print(f'publish: {publish_count}')
# Show 3 sample entries
for i, (k, v) in enumerate(list(fx.items())[:3]):
    print(f'\\n--- fixture {i+1} ({k}) ---')
    print(f'  {v.get("home")} vs {v.get("away")} @ {v.get("kickoff_hkt")}')
    for s, row in (v.get('stages') or {}).items():
        print(f'  {s}: lead={row.get("lead_market")} {row.get("lead_label")} @ {row.get("lead_odds")}, prob={row.get("lead_prob")}, ev={row.get("lead_ev")}, conv={row.get("conviction")}, publish={row.get("publish_decision")}/{row.get("publish_reason")}')
PY

echo ""
echo "===== fb legacy data.json matches summary ====="
python3 <<PY
import json
from datetime import datetime, timezone, timedelta
d = json.load(open('/var/www/footbreak/data.json'))
now_hkt = datetime.now(timezone(timedelta(hours=8)))
matches = d.get('matches') or []
print(f'total matches: {len(matches)}')
past = future = 0
for m in matches:
    ks = m.get('kickoff_hkt') or ''
    try:
        k = datetime.strptime(ks, '%Y-%m-%d %H:%M').replace(tzinfo=timezone(timedelta(hours=8)))
        if k > now_hkt:
            future += 1
        else:
            past += 1
    except Exception:
        pass
print(f'  future: {future}, past: {past}')
print(f'  now HKT: {now_hkt.isoformat()}')
PY
