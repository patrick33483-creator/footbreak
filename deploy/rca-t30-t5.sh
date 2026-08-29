#!/usr/bin/env bash
# READ-ONLY: 唔改任何嘢
set -uo pipefail

echo "===== 1A. crown-early-admission-reconcile — Python 層 stderr / stdout ====="
# 揾 unit file，睇 ExecStart 同 TimeoutSec
systemctl cat crown-early-admission-reconcile.service --no-pager 2>&1 | head -40

echo ""
echo "===== 1B. journalctl 帶埋 Python traceback ====="
journalctl -u crown-early-admission-reconcile.service --since '30 min ago' --no-pager -o cat 2>&1 | grep -vE "^$" | tail -80

echo ""
echo "===== 1C. 手動 run 一次 admission reconcile（唔開 systemd，60 秒 timeout）====="
# 讀 ExecStart 出嚟直接跑，攞到 Python traceback
EXEC=$(systemctl show crown-early-admission-reconcile.service -p ExecStart --value 2>&1 | head -1)
echo "ExecStart raw: $EXEC"
# 拆出 command
CMD=$(echo "$EXEC" | sed -n 's/^{ path=\([^ ]*\) ; argv\[\]=\([^;]*\).*/\2/p')
echo "Command to run: $CMD"
if [ -n "$CMD" ]; then
    cd /opt/footbreak
    timeout 60 bash -c "$CMD" 2>&1 | tail -60 || echo "[exited with $?]"
fi

echo ""
echo "===== 2A. 近 72 小時 commit 影響 wilson_validation / crown early admission ====="
cd /opt/footbreak
git log --since='72 hours ago' --oneline -- analysis/wilson_validation.py system/settle.py system/record_picks.py crown/ 2>&1 | head -40

echo ""
echo "===== 2B. wilson_validation.py 4635 附近 code（引入 error 嘅 guard）====="
sed -n '4600,4680p' /opt/footbreak/analysis/wilson_validation.py 2>&1

echo ""
echo "===== 2C. git blame 個 raise 一行 ====="
git blame -L 4630,4645 analysis/wilson_validation.py 2>&1 | head -20

echo ""
echo "===== 3A. Crown data.json — T-30 / T-5 場數 ====="
python3 <<'PY'
import json, collections
d = json.load(open('/var/www/crown/data.json'))
matches = d.get('matches', [])
print(f'total matches: {len(matches)}')

# stage 分佈
stages = collections.Counter()
first_stage_dist = collections.Counter()
t30_present = 0
t5_present = 0
for m in matches:
    st = m.get('stage') or m.get('current_stage') or '?'
    stages[st] += 1
    fs = m.get('first_stage') or '?'
    first_stage_dist[fs] += 1
    # 揾 T-30 / T-5 field
    for k in m.keys():
        if 't30' in k.lower() or 't-30' in k.lower() or k == 'T30':
            if m.get(k) not in (None, '', {}, []):
                t30_present += 1
                break
    for k in m.keys():
        if 't5' in k.lower() or 't-5' in k.lower() or k == 'T5':
            if m.get(k) not in (None, '', {}, []):
                t5_present += 1
                break

print('stage dist (top 15):')
for s,c in stages.most_common(15): print(f'  {s}: {c}')
print('first_stage dist (top 15):')
for s,c in first_stage_dist.most_common(15): print(f'  {s}: {c}')
print(f'matches with T-30 field: {t30_present}')
print(f'matches with T-5 field: {t5_present}')

# 睇一場即將開波嘅 match 有咩 field
import datetime
now = datetime.datetime.now().timestamp()
def ko(m):
    for k in ['kickoff','kickoff_ts','kickoff_hkt','kickoff_at']:
        v = m.get(k)
        if v: return str(v)
    return ''
upcoming = [m for m in matches if 'stage' in m]
print(f'\nsample fields of first 2 matches:')
for m in matches[:2]:
    keys = list(m.keys())
    print(f'  match keys: {keys}')
    print(f'    stage={m.get("stage")}, first_stage={m.get("first_stage")}, ko={ko(m)}')
    # print 所有帶 t5/t30/stage 嘅 key value
    for k in keys:
        if any(x in k.lower() for x in ['t5','t-5','t30','t-30','stage','admission']):
            v = m[k]
            vs = str(v)[:80]
            print(f'    {k}: {vs}')
PY

echo ""
echo "===== 3B. Ledger stage_completeness — 系統自我報告 ====="
python3 <<'PY'
import json
d = json.load(open('/var/www/crown/data.json'))
sc = d.get('stage_completeness', {})
print(json.dumps(sc, ensure_ascii=False, indent=2)[:2000])
PY

echo ""
echo "===== 3C. Ledger bets — first_native_pre_kickoff_t5 分佈 ====="
python3 <<'PY'
import json
d = json.load(open('/var/www/crown/data.json'))
bets = d.get('ledger', {}).get('bets', [])
print(f'ledger.bets: {len(bets)}')
for b in bets:
    print(f'  bet {b.get("bet_id")}: first_native_pre_kickoff_t5={b.get("first_native_pre_kickoff_t5")}, first_stage={b.get("first_stage")}, stage_history_len={len(b.get("history",[]))}')
PY

echo ""
echo "===== 3D. Log 過去 24h — 邊個時刻 admission 開始壞 ====="
journalctl -u crown-early-admission-reconcile.service --since '30 hours ago' --no-pager 2>&1 | grep -E "Finished|Failed|start operation timed out" | head -30

echo ""
echo "===== 3E. tick 過去 20 分鐘 predictions/retained 變化 ====="
journalctl -u crown-tick.service --since '20 min ago' --no-pager -o cat 2>&1 | grep -oE "'predictions': [0-9]+, 'retained_predictions': [0-9]+" | tail -20

echo ""
echo "===== DONE ====="
