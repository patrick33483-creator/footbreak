#!/usr/bin/env bash
# READ-ONLY
set -uo pipefail

echo "===== 1. V2 保留機制 — ledger.json ====="
LEDGER=/var/lib/footbreak/stage_engine_v2/ledger.json
if [ -f "$LEDGER" ]; then
    stat -c 'size=%s bytes, mtime=%y' "$LEDGER"
    python3 <<'PY'
import json
p = '/var/lib/footbreak/stage_engine_v2/ledger.json'
d = json.load(open(p))
print(f'top keys: {list(d.keys())[:20]}')
if isinstance(d, dict):
    fx = d.get('fixtures') or d.get('records') or {}
    if isinstance(fx, dict):
        print(f'fixtures/records dict len: {len(fx)}')
        # 舊嘅場保留幾耐
        if fx:
            k0 = next(iter(fx.keys()))
            print(f'sample key: {k0}')
            print(f'sample value keys: {list(fx[k0].keys())[:10] if isinstance(fx[k0],dict) else type(fx[k0])}')
    elif isinstance(fx, list):
        print(f'fixtures list len: {len(fx)}')
        if fx:
            print(f'sample: {list(fx[0].keys())[:10] if isinstance(fx[0],dict) else type(fx[0])}')
    # 揾 retention / prune / window
    for k in d.keys():
        if any(x in k.lower() for x in ('retention','prune','window','purge','expire')):
            print(f'  retention key: {k} = {str(d[k])[:120]}')
PY
else
    echo "NO LEDGER FILE"
fi

echo ""
echo "===== 2. Stage engine v2 code 保留邏輯 ====="
grep -rE "window_hours|--window-hours|prune|retention|older_than|purge" /opt/footbreak/stage_engine_v2/ 2>&1 | grep -v __pycache__ | head -30

echo ""
echo "===== 3. Snapshot workflow ====="
if [ -f /opt/footbreak/.github/workflows/snapshot-stage-engine-v2.yml ]; then
    cat /opt/footbreak/.github/workflows/snapshot-stage-engine-v2.yml | head -50
fi

echo ""
echo "===== 4. Snapshot 已存嗰啲 ====="
ls -la /var/lib/footbreak/stage_engine_v2/ 2>&1 | head -20
find /var/lib/footbreak -name '*.json*' -mtime -3 2>&1 | head -20
find /opt/footbreak -name 'snapshot*' -o -name '*ledger*.json*' 2>&1 | head -20

echo ""
echo "===== 5. 揾內卡薩 vs 藍十字 —— V2 有無 ====="
python3 <<'PY'
import json
d = json.load(open('/var/www/stage_engine_v2/data.json'))
for f in d.get('fixtures', []):
    h = f.get('home','')
    a = f.get('away','')
    if '內卡' in h or '内卡' in h or '藍十字' in a or '蓝十字' in a or '內卡' in a or '内卡' in a or '藍十字' in h or '蓝十字' in h:
        print(f'MATCH: {h} vs {a}')
        print(f'  id: {f.get("id")}')
        print(f'  kickoff_hkt: {f.get("kickoff_hkt")}')
        print(f'  league: {f.get("league")}')
        stages = f.get('stages', {})
        for sk, sv in stages.items():
            if isinstance(sv, dict):
                print(f'  stages["{sk}"]:')
                for k,v in sv.items():
                    print(f'    {k}: {str(v)[:100]}')
PY

echo ""
echo "===== 6. Legacy Crown data.json 揾同一場 ====="
python3 <<'PY'
import json
d = json.load(open('/var/www/crown/data.json'))
for m in d.get('matches', []):
    h = m.get('home','') or ''
    a = m.get('away','') or ''
    if '內卡' in h or '内卡' in h or '藍十字' in a or '蓝十字' in a or '內卡' in a or '内卡' in a or '藍十字' in h or '蓝十字' in h:
        print(f'MATCH: {h} vs {a}')
        print(f'  match_id: {m.get("match_id")}')
        print(f'  kickoff_hkt: {m.get("kickoff_hkt")}')
        print(f'  league: {m.get("league")}')
        print(f'  stage: {m.get("stage")}')
        print(f'  pick: {m.get("pick")}')
        print(f'  probability: {m.get("probability")}')
        print(f'  conviction: {m.get("conviction")}')
        print(f'  no_bet_reason: {m.get("no_bet_reason")}')
        # 揾 condition matches
        cm = m.get('condition_matches')
        if cm:
            print(f'  condition_matches type: {type(cm)}')
            if isinstance(cm, list):
                for c in cm[:5]:
                    if isinstance(c, dict):
                        print(f'    - condition: {c.get("condition") or c.get("id") or c.get("name")}, {list(c.keys())[:8]}')
            elif isinstance(cm, dict):
                for k,v in list(cm.items())[:5]:
                    print(f'    {k}: {str(v)[:100]}')
        # 揾 lead_view / stages
        st = m.get('stages')
        if isinstance(st, list):
            print(f'  stages (list len={len(st)}):')
            for s in st:
                if isinstance(s, dict):
                    print(f'    stage={s.get("stage")}, pick={s.get("pick")}, probability={s.get("probability")}, conviction={s.get("conviction")}')
        lv = m.get('lead_view')
        if lv:
            print(f'  lead_view: {str(lv)[:200]}')
PY

echo ""
echo "===== 7. History.html 有無 archive 呢場？====="
ls -la /var/www/stage_engine_v2/history.html /var/www/crown/history*.html /var/www/crown/history*.json 2>&1 | head -10
grep -l "內卡\|内卡\|藍十字\|蓝十字" /var/www/crown/*.json /var/www/crown/*.html 2>&1 | head -10 || true

echo ""
echo "===== DONE ====="
