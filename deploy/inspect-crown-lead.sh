#!/usr/bin/env bash
set -uo pipefail
python3 <<'PY'
import json
d = json.load(open('/var/www/crown/data.json'))
matches = d.get('matches', [])
print(f'crown matches: {len(matches)}')

# 統計 stage.lead 存在率
have_lead = 0
have_pick = 0
missing_lead = 0
lead_matches_mp_top_ev = 0
lead_differs_from_top_ev = 0

def top_ev_row(mp):
    if not isinstance(mp, list): return None
    best = None
    best_ev = -1e9
    for row in mp:
        if not isinstance(row, dict): continue
        p = row.get('probability') or row.get('prob')
        o = row.get('odds')
        if p is None or o is None: continue
        try:
            p = float(p); o = float(o)
        except: continue
        if o <= 1 or not (0 < p <= 1): continue
        ev = p*o - 1.0
        if ev > best_ev:
            best_ev = ev
            best = row
    return best

samples_shown = 0
for m in matches:
    stages = m.get('stages') or []
    if not isinstance(stages, list): continue
    for s in stages:
        if not isinstance(s, dict): continue
        lead = s.get('lead')
        pick = s.get('pick')
        mp = s.get('market_predictions')
        if isinstance(lead, dict) and lead:
            have_lead += 1
            if pick: have_pick += 1
            # compare lead vs top-EV of mp
            top = top_ev_row(mp) if isinstance(mp, list) else None
            if top:
                lead_label = lead.get('label')
                top_label = top.get('label') or top.get('selection')
                if lead_label == top_label:
                    lead_matches_mp_top_ev += 1
                else:
                    lead_differs_from_top_ev += 1
                    if samples_shown < 5:
                        print(f'\n[DIFF] {m.get("home")} vs {m.get("away")} stage={s.get("stage")}:')
                        print(f'  stage.lead: {lead.get("market")}/{lead.get("label")} odds={lead.get("odds")} prob={lead.get("prob")}')
                        print(f'  top-EV mp: {top.get("market")}/{top.get("label")} odds={top.get("odds")} prob={top.get("probability") or top.get("prob")}')
                        samples_shown += 1
        else:
            missing_lead += 1

print(f'\n=== Crown stage counts ===')
print(f'  have stage.lead: {have_lead}')
print(f'  missing stage.lead: {missing_lead}')
print(f'  have stage.pick: {have_pick}')
print(f'  stage.lead matches top-EV mp: {lead_matches_mp_top_ev}')
print(f'  stage.lead DIFFERS from top-EV mp: {lead_differs_from_top_ev}')

# Footbreak 同樣統計
print(f'\n=== Footbreak stage counts ===')
d2 = json.load(open('/var/www/footbreak/data.json'))
matches2 = d2.get('matches', [])
have_lead2 = 0
missing_lead2 = 0
have_pick2 = 0
for m in matches2:
    stages = m.get('stages') or []
    if not isinstance(stages, list): continue
    for s in stages:
        if not isinstance(s, dict): continue
        lead = s.get('lead')
        if isinstance(lead, dict) and lead:
            have_lead2 += 1
            if s.get('pick'): have_pick2 += 1
        else:
            missing_lead2 += 1
print(f'  fb have stage.lead: {have_lead2}')
print(f'  fb missing stage.lead: {missing_lead2}')
print(f'  fb have stage.pick: {have_pick2}')

# 特別揾內卡薩對藍十字 2997385
print(f'\n=== Match 2997385 (內卡薩 vs 藍十字) ===')
for m in matches:
    if str(m.get('match_id') or m.get('id') or '') == '2997385':
        stages = m.get('stages') or []
        for s in stages:
            if not isinstance(s, dict): continue
            print(f'  stage {s.get("stage")} @ {s.get("ts")}:')
            lead = s.get('lead')
            if isinstance(lead, dict):
                print(f'    lead: {json.dumps(lead, ensure_ascii=False)}')
            mp = s.get('market_predictions')
            if isinstance(mp, list):
                for row in mp:
                    if isinstance(row, dict):
                        print(f'    mp: {row.get("market")}/{row.get("label")} odds={row.get("odds")} prob={row.get("probability") or row.get("prob")}')
PY
