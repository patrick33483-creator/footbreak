#!/usr/bin/env bash
set -uo pipefail

echo "===== A. 相關 timers 狀態 ====="
systemctl list-timers --all 2>&1 | grep -iE "crown|footbreak|tick|reverse|t5|t30" | head -30

echo ""
echo "===== B. crown-tick.service 最近 log ====="
journalctl -u crown-tick.service --since '4 hours ago' --no-pager 2>&1 | tail -40

echo ""
echo "===== C. crown-reverse-t5-drain.service 最近 log ====="
journalctl -u crown-reverse-t5-drain.service --since '12 hours ago' --no-pager 2>&1 | tail -40

echo ""
echo "===== D. crown-round-update.service 最近 log tail ====="
journalctl -u crown-round-update.service --since '24 hours ago' --no-pager 2>&1 | tail -30

echo ""
echo "===== E. data.json 頂層 keys ====="
python3 <<'PY'
import json
for path,name in [('/var/www/crown/data.json','crown'),('/var/www/footbreak/data.json','fb')]:
    try:
        d=json.load(open(path))
        print(f'--- {name} ---')
        if isinstance(d,dict):
            for k,v in list(d.items())[:20]:
                if isinstance(v,list): print(f'  {k}: list len={len(v)}')
                elif isinstance(v,dict): print(f'  {k}: dict keys={list(v.keys())[:8]}')
                else: print(f'  {k}: {str(v)[:80]}')
        elif isinstance(d,list):
            print(f'  array len={len(d)}')
            if d: print(f'  sample keys: {list(d[0].keys())[:15]}')
    except Exception as e:
        print(f'{name} err: {e}')
PY

echo ""
echo "===== F. Crown data.json — T5/T30 分佈 ====="
python3 <<'PY'
import json
d=json.load(open('/var/www/crown/data.json'))
# 揾裝住 fixtures 嘅 list
def walk(o, prefix=''):
    if isinstance(o,dict):
        for k,v in o.items():
            if isinstance(v,list) and v and isinstance(v[0],dict):
                yield prefix+k, v
            elif isinstance(v,dict):
                yield from walk(v, prefix+k+'.')
containers=list(walk(d))
for name, arr in containers[:6]:
    sample = arr[0]
    t5_keys = [k for k in sample.keys() if 't5' in k.lower() or 't-5' in k.lower() or k.lower() in ('t5','t_5')]
    t30_keys = [k for k in sample.keys() if 't30' in k.lower() or 't-30' in k.lower() or k.lower() in ('t30','t_30')]
    print(f'{name}: len={len(arr)}, sample keys sample={list(sample.keys())[:20]}')
    print(f'  t5-ish keys: {t5_keys}')
    print(f'  t30-ish keys: {t30_keys}')
    if t5_keys or t30_keys:
        for k in (t5_keys+t30_keys):
            filled=sum(1 for r in arr if r.get(k) not in (None,'',{},[]))
            print(f'  {k}: filled {filled}/{len(arr)}')
        # 舉例前 5 場：
        print('  sample first 3 fixtures with these fields:')
        for r in arr[:3]:
            keys=t5_keys+t30_keys
            print('   ', {k:r.get(k) for k in keys}, ' | match:', r.get('match') or r.get('fixture') or r.get('name') or r.get('id'))
        break
PY

echo ""
echo "===== G. Footbreak data.json — T5/T30 分佈 ====="
python3 <<'PY'
import json
d=json.load(open('/var/www/footbreak/data.json'))
def walk(o, prefix=''):
    if isinstance(o,dict):
        for k,v in o.items():
            if isinstance(v,list) and v and isinstance(v[0],dict):
                yield prefix+k, v
            elif isinstance(v,dict):
                yield from walk(v, prefix+k+'.')
containers=list(walk(d))
for name, arr in containers[:6]:
    sample = arr[0]
    t5_keys = [k for k in sample.keys() if 't5' in k.lower() or 't-5' in k.lower() or k.lower() in ('t5','t_5')]
    t30_keys = [k for k in sample.keys() if 't30' in k.lower() or 't-30' in k.lower() or k.lower() in ('t30','t_30')]
    print(f'{name}: len={len(arr)}, sample keys={list(sample.keys())[:20]}')
    print(f'  t5-ish keys: {t5_keys}')
    print(f'  t30-ish keys: {t30_keys}')
    if t5_keys or t30_keys:
        for k in (t5_keys+t30_keys):
            filled=sum(1 for r in arr if r.get(k) not in (None,'',{},[]))
            print(f'  {k}: filled {filled}/{len(arr)}')
        print('  sample first 3:')
        for r in arr[:3]:
            keys=t5_keys+t30_keys
            print('   ', {k:r.get(k) for k in keys}, ' | id:', r.get('match') or r.get('fixture') or r.get('id'))
        break
PY

echo ""
echo "===== H. Ledger 頂層 stats ====="
[ -f /opt/footbreak/crown/ledger.json ] && python3 -c "
import json
d=json.load(open('/opt/footbreak/crown/ledger.json'))
if isinstance(d,dict):
    for k in ['fixtures','predictions','meta','generated_at']:
        if k in d:
            v=d[k]
            if isinstance(v,list): print(f'{k}: len={len(v)}')
            elif isinstance(v,dict): print(f'{k}: {list(v.keys())[:8]}')
            else: print(f'{k}: {v}')
"

echo ""
echo "===== I. 最近 failed services ====="
systemctl --failed --no-pager 2>&1 | head -20
