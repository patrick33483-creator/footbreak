#!/usr/bin/env bash
# READ-ONLY
set -uo pipefail

echo "===== A. V2 data.json 概況 ====="
stat -c 'size=%s bytes, mtime=%y' /var/www/stage_engine_v2/data.json

echo ""
echo "===== B. 詳細 stage 分佈 ====="
python3 <<'PY'
import json, collections, datetime
d = json.load(open('/var/www/stage_engine_v2/data.json'))
print(f'schema_version: {d.get("schema_version")}')
print(f'generated_at_utc: {d.get("generated_at_utc")}')
print(f'generated_at_hkt: {d.get("generated_at_hkt")}')
print(f'fixtures_count: {d.get("fixtures_count")}')

fixtures = d.get('fixtures', [])
print(f'\nActual fixtures len: {len(fixtures)}')

if not fixtures:
    print('NO FIXTURES')
    raise SystemExit()

# 睇第一個 fixture 完整結構
print(f'\nFirst fixture keys: {list(fixtures[0].keys())}')
print(f'First fixture stages key type: {type(fixtures[0].get("stages"))}')
stages_val = fixtures[0].get('stages')
if isinstance(stages_val, dict):
    print(f'  stages keys: {list(stages_val.keys())}')
    for sk, sv in stages_val.items():
        print(f'  stages["{sk}"] type: {type(sv).__name__}, keys/val: {list(sv.keys()) if isinstance(sv,dict) else str(sv)[:80]}')
elif isinstance(stages_val, list):
    print(f'  stages list len: {len(stages_val)}')
    if stages_val:
        print(f'  first stage: {stages_val[0]}')

# 統計每場有咩 stage
stage_names = collections.Counter()
per_fixture_stages = []
for f in fixtures:
    st = f.get('stages')
    names = []
    if isinstance(st, dict):
        names = list(st.keys())
    elif isinstance(st, list):
        for s in st:
            if isinstance(s, dict):
                sn = s.get('stage') or s.get('name') or s.get('label')
                if sn: names.append(sn)
    per_fixture_stages.append(set(names))
    for n in names:
        stage_names[n] += 1

print(f'\n===== stage 總體出現次數 =====')
for n, c in stage_names.most_common():
    print(f'  {n}: {c} / {len(fixtures)}')

print(f'\n===== 每場齊唔齊 =====')
has_t30 = sum(1 for s in per_fixture_stages if any('30' in x or 'T-30' in x for x in s))
has_t5 = sum(1 for s in per_fixture_stages if any('T-5' in x or (('5' in x) and ('30' not in x)) for x in s))
has_first = sum(1 for s in per_fixture_stages if any('首預' in x or 'first' in x.lower() for x in s))
print(f'  有 T-30: {has_t30} / {len(fixtures)}')
print(f'  有 T-5: {has_t5} / {len(fixtures)}')
print(f'  有 首預: {has_first} / {len(fixtures)}')

# 揾 3 場 sample，睇實際結構
print(f'\n===== 頭 3 場 sample =====')
for i, f in enumerate(fixtures[:3]):
    print(f'\n--- Fixture {i} ---')
    print(f'  id: {f.get("id")}')
    print(f'  {f.get("home","?")} vs {f.get("away","?")}')
    print(f'  kickoff_hkt: {f.get("kickoff_hkt")}')
    st = f.get('stages')
    if isinstance(st, dict):
        for sk, sv in st.items():
            if isinstance(sv, dict):
                print(f'  stages["{sk}"]: {list(sv.keys())[:12]}')
            else:
                print(f'  stages["{sk}"]: {str(sv)[:120]}')
    elif isinstance(st, list):
        for s in st:
            if isinstance(s, dict):
                sn = s.get('stage') or s.get('name') or s.get('label') or '?'
                keys = [k for k in s.keys() if k not in ('stage','name','label')][:10]
                print(f'  stage="{sn}": {keys}')

# 已開波場 vs 未開波
now_hkt = datetime.datetime.now(datetime.timezone(datetime.timedelta(hours=8)))
future = []
past = []
for f in fixtures:
    ko = f.get('kickoff_hkt') or ''
    try:
        koi = datetime.datetime.fromisoformat(ko.replace('Z','+00:00'))
        if koi.tzinfo is None:
            koi = koi.replace(tzinfo=datetime.timezone(datetime.timedelta(hours=8)))
        if koi > now_hkt:
            future.append(f)
        else:
            past.append(f)
    except:
        pass
print(f'\n===== 時序分佈 =====')
print(f'  未開波: {len(future)}')
print(f'  已開波: {len(past)}')

# 未開波場 T-30/T-5 齊度
def stage_names_of(f):
    st = f.get('stages')
    if isinstance(st, dict):
        return set(st.keys())
    if isinstance(st, list):
        return {s.get('stage') or s.get('name') or s.get('label') for s in st if isinstance(s,dict)}
    return set()

fut_t30 = sum(1 for f in future if any('30' in x for x in stage_names_of(f) if x))
fut_t5 = sum(1 for f in future if any('T-5' in x or x=='5' for x in stage_names_of(f) if x))
print(f'  未開波有 T-30: {fut_t30} / {len(future)}')
print(f'  未開波有 T-5: {fut_t5} / {len(future)}')
PY

echo ""
echo "===== C. Legacy Crown data.json 比對（同一時刻）====="
python3 <<'PY'
import json
d = json.load(open('/var/www/crown/data.json'))
sc = d.get('stage_completeness', {})
print(f'legacy schema: {d.get("schema_version")}')
print(f'legacy stage_completeness:')
for name in ('首預','T-30','T-5'):
    s = sc.get('stages', {}).get(name, {})
    print(f'  {name}: recorded={s.get("recorded")}, due={s.get("due")}, missing_due={s.get("missing_due")}, completeness={s.get("completeness")}')
PY

echo ""
echo "===== D. V2 tick log 最近 5 分鐘 ====="
tail -50 /var/log/footbreak/stage-v2-tick.log 2>&1 | tail -40
