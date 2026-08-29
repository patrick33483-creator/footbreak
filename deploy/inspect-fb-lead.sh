#!/usr/bin/env bash
# READ-ONLY
set -uo pipefail

echo "===== A. Footbreak 邊個 field 係 legacy dashboard 顯示嘅 lead？ ====="
python3 <<'PY'
import json
d = json.load(open('/var/www/footbreak/data.json'))
matches = d.get('matches', [])
print(f'total matches: {len(matches)}')

# 統計 pick 有幾多有值
have_pick = sum(1 for m in matches if m.get('pick'))
print(f'matches with pick != None: {have_pick}')

# 睇 3 場有 pick 嘅
n = 0
for m in matches:
    if m.get('pick'):
        n += 1
        print(f'\n--- Match {n}: {m.get("home")} vs {m.get("away")} (id={m.get("match_id")}) ---')
        print(f'  stage: {m.get("stage")}')
        print(f'  pick: {m.get("pick")}')
        print(f'  conviction: {m.get("conviction")}')
        print(f'  no_bet_reason: {m.get("no_bet_reason")}')
        # 睇 final / fc / stages
        fnl = m.get('final')
        if isinstance(fnl, dict):
            print(f'  final keys: {list(fnl.keys())}')
            for k,v in fnl.items():
                print(f'    final.{k}: {str(v)[:120]}')
        fc = m.get('fc')
        if isinstance(fc, dict):
            print(f'  fc keys: {list(fc.keys())}')
            for k,v in fc.items():
                if not isinstance(v,(list,dict)):
                    print(f'    fc.{k}: {str(v)[:120]}')
        # stages 內 lead
        stages = m.get('stages') or []
        for i, s in enumerate(stages):
            if isinstance(s, dict):
                print(f'  stage[{i}] {s.get("stage")}: lead={s.get("lead")}, pick={s.get("pick")}, verdict={s.get("verdict")}')
                lead = s.get('lead')
                if isinstance(lead, dict):
                    print(f'    lead keys: {list(lead.keys())}')
                    for k,v in lead.items():
                        if not isinstance(v,(list,dict)):
                            print(f'      lead.{k}: {str(v)[:100]}')
        if n >= 3:
            break

if n == 0:
    print('\n冇任何 match 有 pick，睇 3 場冇 pick 嘅結構：')
    for i,m in enumerate(matches[:3]):
        print(f'\n--- Match {i}: {m.get("home")} vs {m.get("away")} ---')
        print(f'  stage: {m.get("stage")}, conviction: {m.get("conviction")}, no_bet_reason: {m.get("no_bet_reason")}')
        fnl = m.get('final')
        if isinstance(fnl, dict):
            for k,v in fnl.items():
                print(f'    final.{k}: {str(v)[:100]}')
        fc = m.get('fc')
        if isinstance(fc, dict):
            for k,v in fc.items():
                if not isinstance(v,(list,dict)):
                    print(f'    fc.{k}: {str(v)[:100]}')
        stages = m.get('stages') or []
        # 睇最新一個 stage
        if stages:
            s = stages[-1] if isinstance(stages[-1],dict) else None
            if s:
                print(f'  latest stage[{s.get("stage")}]:')
                for k in ('pick','lead','verdict','no_bet_reason','conviction','odds_status','market_predictions'):
                    v = s.get(k)
                    if v is not None and v != []:
                        vs = str(v)[:150]
                        print(f'    {k}: {vs}')
                # 特別 dump lead
                lead = s.get('lead')
                if isinstance(lead, dict):
                    for k,v in lead.items():
                        print(f'    lead.{k}: {str(v)[:100]}')
                # market_predictions
                mp = s.get('market_predictions')
                if isinstance(mp, list) and mp:
                    print(f'    market_predictions len: {len(mp)}')
                    print(f'    mp[0]: {json.dumps(mp[0], ensure_ascii=False)[:300]}')
PY

echo ""
echo "===== B. Crown match 對比：市場預測位置一樣嗎？ ====="
python3 <<'PY'
import json
d = json.load(open('/var/www/crown/data.json'))
matches = d.get('matches', [])
for m in matches[:2]:
    print(f'{m.get("home")} vs {m.get("away")} ({m.get("match_id")}):')
    stages = m.get('stages') or []
    for s in stages[:2] if isinstance(stages,list) else []:
        if not isinstance(s, dict): continue
        print(f'  stage[{s.get("stage")}]:')
        mp = s.get('market_predictions')
        if isinstance(mp, list) and mp:
            print(f'    market_predictions len: {len(mp)}')
            print(f'    mp[0]: {json.dumps(mp[0], ensure_ascii=False)[:250]}')
        lead = s.get('lead')
        if isinstance(lead, dict):
            print(f'    lead: {json.dumps(lead, ensure_ascii=False)[:300]}')
        print(f'    pick: {s.get("pick")}')
PY

echo ""
echo "===== C. Footbreak dashboard 顯示嗰陣讀邊個？睇 index.html ====="
grep -oE "lead[a-z_]*|pick|market_predictions|final\.|fc\." /var/www/footbreak/index.html 2>&1 | sort -u | head -30
