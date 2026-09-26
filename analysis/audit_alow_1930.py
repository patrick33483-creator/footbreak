"""Read-only 19:30 HKT A+ incident audit. No notification or production changes."""
import datetime as dt
import hashlib,json,re,sqlite3,subprocess,time
from pathlib import Path
ROOT=Path("/opt/crown-radar-v2")
def run(a,**kw):return subprocess.run(a,check=True,capture_output=True,text=True,timeout=100,**kw).stdout
def ms(s):return int(dt.datetime.fromisoformat(s).timestamp()*1000)
start=ms("2026-09-26T19:00:00+08:00")
end=ms("2026-09-26T20:00:00+08:00")
now=int(time.time()*1000)
db=sqlite3.connect(f"file:{ROOT}/data/crown.db?mode=ro",uri=True,timeout=15)
db.row_factory=sqlite3.Row;db.execute("PRAGMA query_only=ON");db.execute("BEGIN")
matches=[dict(r) for r in db.execute("SELECT * FROM matches WHERE kickoff_utc>=? AND kickoff_utc<=? ORDER BY kickoff_utc,sid",(start,end))]
for m in matches:
    for table,key in [("crown_snapshots","crown"),("odds_snapshots","pinnacle")]:
        m[key]=[dict(r) for r in db.execute(f"SELECT * FROM {table} WHERE sid=? ORDER BY stage,market",(m["sid"],))]
    m["notices"]=[dict(r) for r in db.execute("SELECT * FROM heavy_notified_rule WHERE sid=?",(m["sid"],))]
    m["receipt"]=[dict(r) for r in db.execute("SELECT * FROM alow_notification_receipts WHERE sid=?",(m["sid"],))]
    m["snapshots"]={f"{s['stage']}_{s['market']}":{"h":s["handicap"],"home":s["home_odds"],"away":s["away_odds"],"captured_at":s["captured_at"]} for s in m["crown"]}
    m["sublines"]={}
config=json.loads((ROOT/"data/rules.json").read_text())
rule=next(r for r in config["rules"] if r["id"]=="ch-Alow")
all_notices=[dict(r) for r in db.execute("""
 SELECT n.*,m.league,m.home,m.away,m.kickoff_utc FROM heavy_notified_rule n
 JOIN matches m ON m.sid=n.sid WHERE n.rule_id='ch-Alow' AND n.notified_at>=? ORDER BY n.notified_at
""",(rule["version_effective_at_ms"],))]
receipts=[dict(r) for r in db.execute("SELECT * FROM alow_notification_receipts")]
recent=db.execute("SELECT stage,market,COUNT(*) AS n,MAX(captured_at) AS latest FROM crown_snapshots WHERE captured_at>=? GROUP BY stage,market",(start-3600000,)).fetchall()
db.close()
node="""
import fs from 'node:fs';
import {matchAll} from '/app/rule_matcher.js';
const {matches,rule,rules}=JSON.parse(fs.readFileSync(0,'utf8'));
const original={...rule,enabled:true,alow_price_direction_v2:false,require_t5_after_version:false,side:'H',pick:'home',pick_label:'買主'};
const out=matches.map(m=>{
 const hits=matchAll(m,[rule]), old=matchAll(m,[original]);
 const s=m.snapshots,ah=s.T5_AH,ou=s.T5_OU,t30=s.T30_AH,init=s.initial_AH;
 const reasons=[];
 for(const k of ['initial_AH','T30_AH','T5_AH','T5_OU'])if(!s[k])reasons.push('缺少'+k);
 if(ah&&Number(ah.h)!==0)reasons.push('T5不是平手盤');
 if(ah&&t30&&Math.abs(Number(ah.h)-Number(t30.h))>=.001)reasons.push('T30至T5讓球盤改變');
 if(ou&&Number(ou.h)>2.25)reasons.push('大小盤高於2.25');
 const home=ah?Math.round((Number(ah.home)+1)*1000)/1000:null;
 const away=ah?Math.round((Number(ah.away)+1)*1000)/1000:null;
 if(ah&&!(home>=1.70&&home<1.80||home>=2))reasons.push('主賠不在指定區間');
 for(const k of ['T5_AH','T5_OU'])if(s[k]){
   if(Number(s[k].captured_at)<rule.version_effective_at_ms)reasons.push(k+'早於啟用');
   if(Number(s[k].captured_at)>=m.kickoff_utc)reasons.push(k+'開賽後才收');
 }
 return {sid:m.sid,league:m.league,home:m.home,away:m.away,has_crown:m.has_crown,kickoff_utc:m.kickoff_utc,
   home_dec:home,away_dec:away,ah:ah?.h??null,t30_ah:t30?.h??null,ou:ou?.h??null,
   hits,old_base_hits:old.length,reasons,all_channel_hits:matchAll(m,rules.filter(r=>r.type==='channel')).map(h=>({id:h.rule.id,pick:h.match.pick,odds:h.match.t5Dec})),
   notices:m.notices};
});
console.log(JSON.stringify(out));
"""
evaluated=json.loads(run(["docker","exec","-i","crown-radar-v2","node","--input-type=module","-e",node],
    input=json.dumps({"matches":matches,"rule":rule,"rules":config["rules"]})))
state_path=ROOT/"ops/alow-first-tg/state.json"
state=json.loads(state_path.read_text()) if state_path.exists() else None
logs=run(["docker","logs","--since","2026-09-26T10:55:00Z","--tail","4000","crown-radar-v2"])
safe_logs=[]
for line in logs.splitlines():
    if not any(k in line for k in ["checkpoint","heavy_notify","tg_send","notify_only","provider_missing","refresh"]):continue
    # Sports operational fields only. Never return URL/token/error strings.
    try:
        j=json.loads(line[line.index("{"):])
        safe={k:v for k,v in j.items() if k in {"ts","t","time","at","event","msg","type","sid","stage","market","provider","attempted","savedAH","savedOU","savedCrownAH","savedCrownOU","candidates","t30","t5","checked","sent","errors","t5_sids","missingPin","missingCrown","attempt"} and not isinstance(v,(dict,list))}
        for k in ["msg"]:
            if k in safe and not re.fullmatch(r"[A-Za-z0-9_]+",str(safe[k])):safe.pop(k)
        if safe:safe_logs.append(safe)
    except Exception:pass
server=(ROOT/"server.js").read_text()
out={"checked_at_ms":now,"rule":rule,"matches":matches,"evaluated":evaluated,
    "new_alow_notices":all_notices,"receipts":receipts,"monitor_state":state,
    "monitor_service":run(["systemctl","show","crown-alow-first-tg-watch.service","-p","Result","-p","ExecMainStatus","-p","ExecMainExitTimestamp"]),
    "monitor_timer":run(["systemctl","list-timers","crown-alow-first-tg-watch.timer","--no-pager"]),
    "recent_crown_stage_counts":[dict(r) for r in recent],"safe_logs":safe_logs[-250:],
    "hashes":{n:hashlib.sha256((ROOT/n).read_bytes()).hexdigest() for n in ["server.js","rule_matcher.js","data/rules.json"]},
    "container":run(["docker","inspect","--format","{{.State.Status}} {{.State.StartedAt}}","crown-radar-v2"])}
print(json.dumps(out,ensure_ascii=False,indent=2))
