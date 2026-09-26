"""Read-only reconstruction of the four 20:00 notified fixtures, never send here."""
import json,sqlite3,subprocess
from pathlib import Path
ROOT=Path("/opt/crown-radar-v2")
KO=1790424000000
db=sqlite3.connect(f"file:{ROOT}/data/crown.db?mode=ro",uri=True)
db.row_factory=sqlite3.Row;db.execute("PRAGMA query_only=ON");db.execute("BEGIN")
matches=[dict(r) for r in db.execute("""
 SELECT m.* FROM matches m WHERE m.kickoff_utc=?
 AND EXISTS(SELECT 1 FROM heavy_notified_rule n WHERE n.sid=m.sid)
 ORDER BY m.league,m.sid
""",(KO,))]
for m in matches:
    m["notices"]=[dict(r) for r in db.execute("SELECT * FROM heavy_notified_rule WHERE sid=? ORDER BY notified_at,rule_id",(m["sid"],))]
    m["snapshots"]={f"{s['stage']}_{s['market']}":{"h":s["handicap"],"home":s["home_odds"],"away":s["away_odds"],"captured_at":s["captured_at"]}
        for s in db.execute("SELECT * FROM crown_snapshots WHERE sid=?",(m["sid"],))}
    m["sublines"]={}
    r=db.execute("SELECT payload_json FROM alow_notification_receipts WHERE sid=?",(m["sid"],)).fetchone()
    m["alow_receipt"]=json.loads(r[0]) if r else None
recent=[dict(r) for r in db.execute("""
 SELECT n.*,m.league,m.home,m.away,m.kickoff_utc FROM heavy_notified_rule n
 JOIN matches m ON m.sid=n.sid WHERE n.notified_at>=? ORDER BY n.notified_at
""",(KO-3600000,))]
db.close()
node="""
import fs from 'node:fs';import vm from 'node:vm';import {matchAll} from '/app/rule_matcher.js';
const matches=JSON.parse(fs.readFileSync(0,'utf8'));
const rules=JSON.parse(fs.readFileSync('/app/data/rules.json','utf8')).rules;
const server=fs.readFileSync('/app/server.js','utf8');
const code=server.slice(server.indexOf('function formatHeavyMsg(hit) {'),server.indexOf('async function sendTelegram'));
const box={escapeHtml:s=>String(s).replaceAll('&','&amp;').replaceAll('<','&lt;').replaceAll('>','&gt;'),
 formatKickoffHKT:ms=>new Date(ms+28800000).toISOString().slice(5,16).replace('T',' '),
 lookupCrownT5:()=>null,crownDirectLink:()=>null};
vm.createContext(box);vm.runInContext(code,box);
console.log(JSON.stringify(matches.map(m=>{
 const hits=matchAll(m,rules).filter(h=>m.notices.some(n=>n.rule_id===h.rule.id));
 const missing=m.notices.filter(n=>!hits.some(h=>h.rule.id===n.rule_id));
 const sentRules=hits.map(h=>{
   const receipt=m.alow_receipt;
   if(h.rule.id==='ch-Alow'&&receipt) {
      h.match={...h.match,pick:receipt.pick,pickLabel:receipt.display,t5Line:receipt.line,t5Dec:receipt.odds,
        triggerHomeDec:receipt.trigger_home_dec,priceBand:receipt.price_band};
      h.rule={...h.rule,pick:receipt.pick,side:receipt.side,pick_label:receipt.display};
   }
   return {...h.rule,match:h.match};
 });
 let text=null;
 if(sentRules.length) {
   text=box.formatHeavyMsg({...m,rules:sentRules}).replace('<b>皇冠賽前投注通知</b>',
     '<b>20:00賽事｜補發核對／格式測試</b>\\n<b>已開賽：以下是原賽前訊號，唔係新投注建議，請勿當成即場下注。</b>');
   text+='\\n\\n原通知時間（香港）：'+m.notices.map(n=>box.formatKickoffHKT(n.notified_at)).join('、')+
      '\\n賠率取自已保存的原皇冠T-5快照／通知收據，並非現時即場賠率。\\n本次補發不新增投注紀錄。';
 }
 return {sid:m.sid,league:m.league,home:m.home,away:m.away,kickoff_utc:m.kickoff_utc,
   notices:m.notices,selections:sentRules.map(r=>({id:r.id,side:r.match.pick,line:r.match.t5Line,odds:r.match.t5Dec})),
   missing_rule_replay:missing,text};
})));
"""
p=subprocess.run(["docker","exec","-i","crown-radar-v2","node","--input-type=module","-e",node],
    input=json.dumps(matches,ensure_ascii=False),capture_output=True,text=True,check=True,timeout=90)
out={"matches":json.loads(p.stdout),"recent_notices":recent,"count":len(matches)}
print(json.dumps(out,ensure_ascii=False,indent=2))
