#!/usr/bin/env bash
# READ-ONLY
set -uo pipefail

echo "===== A. Footbreak data.json 結構 ====="
python3 <<'PY'
import json
d = json.load(open('/var/www/footbreak/data.json'))
print(f'top keys: {list(d.keys())[:30]}')
print(f'schema_version: {d.get("schema_version")}')
print(f'generated_at: {d.get("generated_at")}')
print(f'generated_at_hkt: {d.get("generated_at_hkt")}')

matches = d.get('matches') or d.get('predictions') or d.get('fixtures') or []
print(f'\nmatches count: {len(matches)}')
if matches:
    m = matches[0]
    print(f'\nfirst match keys ({len(m.keys())}):')
    for k in sorted(m.keys()):
        v = m[k]
        vt = type(v).__name__
        vs = str(v)[:80] if not isinstance(v,(list,dict)) else f'({vt}, len={len(v) if hasattr(v,"__len__") else "?"})'
        print(f'  {k} [{vt}]: {vs}')

    # 睇 stages 結構
    st = m.get('stages')
    print(f'\nstages type: {type(st).__name__}')
    if isinstance(st, list) and st:
        print(f'stages list len: {len(st)}')
        s0 = st[0]
        if isinstance(s0, dict):
            print(f'first stage keys: {list(s0.keys())}')
            for k in s0.keys():
                v = s0[k]
                if not isinstance(v,(list,dict)):
                    print(f'  {k}: {str(v)[:80]}')
    elif isinstance(st, dict):
        print(f'stages dict keys: {list(st.keys())}')
        for sk, sv in st.items():
            if isinstance(sv, dict):
                print(f'  stages["{sk}"]: {list(sv.keys())[:15]}')

    # 睇 pick / probability / conviction 呢類欄位
    print(f'\ncore fields:')
    for k in ('match_id','id','hkjc_match_id','pinnapi_event_id','home','away','league','kickoff_hkt','stage','pick','probability','conviction','lead_view','condition_matches','no_bet_reason'):
        if k in m:
            v = m[k]
            print(f'  {k}: {str(v)[:120]}')
PY

echo ""
echo "===== B. Footbreak vs Crown data.json schema 差異 ====="
python3 <<'PY'
import json
fb = json.load(open('/var/www/footbreak/data.json'))
cw = json.load(open('/var/www/crown/data.json'))
fbm = (fb.get('matches') or [])[:1]
cwm = (cw.get('matches') or [])[:1]
fbk = set(fbm[0].keys()) if fbm else set()
cwk = set(cwm[0].keys()) if cwm else set()
print(f'fb match keys count: {len(fbk)}')
print(f'crown match keys count: {len(cwk)}')
print(f'\nfb only (top 20): {sorted(fbk - cwk)[:20]}')
print(f'\ncrown only (top 20): {sorted(cwk - fbk)[:20]}')
print(f'\ncommon count: {len(fbk & cwk)}')
PY

echo ""
echo "===== C. V2 crown fixtures.py 讀邏輯 ====="
if [ -f /opt/footbreak/stage_engine_v2/fixtures.py ]; then
    cat /opt/footbreak/stage_engine_v2/fixtures.py
fi

echo ""
echo "===== D. V2 crown predictor.py / stage 抽取邏輯 ====="
ls /opt/footbreak/stage_engine_v2/*.py 2>&1
echo ""
if [ -f /opt/footbreak/stage_engine_v2/predictor.py ]; then
    echo "-- predictor.py head --"
    head -100 /opt/footbreak/stage_engine_v2/predictor.py
fi

echo ""
echo "===== E. V2 crown cli.py + tick 主邏輯 ====="
if [ -f /opt/footbreak/stage_engine_v2/cli.py ]; then
    cat /opt/footbreak/stage_engine_v2/cli.py
fi

echo ""
echo "===== F. V2 ledger.py 寫入結構 ====="
if [ -f /opt/footbreak/stage_engine_v2/ledger.py ]; then
    cat /opt/footbreak/stage_engine_v2/ledger.py
fi

echo ""
echo "===== G. 揾 tick.py ====="
find /opt/footbreak/stage_engine_v2 -name '*.py' | head -20
