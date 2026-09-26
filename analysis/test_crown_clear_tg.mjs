import fs from 'node:fs';
import vm from 'node:vm';
import assert from 'node:assert/strict';
const code=fs.readFileSync(process.argv[2],'utf8');
const box={escapeHtml:s=>String(s).replaceAll('&','&amp;').replaceAll('<','&lt;').replaceAll('>','&gt;'),
  formatKickoffHKT:()=> '09-26 20:00',lookupCrownT5:()=>null,crownDirectLink:()=>null,
  db:{prepare:()=>({get:()=>({league:'後備聯賽',home:'後備主隊',away:'後備客隊',kickoff_utc:1790424000000})})}};
vm.createContext(box);vm.runInContext(code,box);
const base={sid:'offline',league:'測試聯賽',home:'主隊甲',away:'客隊乙',kickoff_utc:1790424000000};
let checks=0;
const examples=[];
for(const [id,market,pick,line,odds,wanted] of [
  ['ch-Alow','AH','home',0,1.75,'買主（主隊甲）'],
  ['ch-Alow','AH','away',0,1.60,'買客（客隊乙）'],
  ['ch-N2','OU','over',2.25,1.70,'買大（全場入球）'],
  ['ch-BI','OU','over',3,1.85,'買大（全場入球）'],
  ['ch-U075-v2','OU','under',2.75,1.86,'買細（全場入球）'],
]) {
  const rule={id,market,pick,label:'測試條件 ≤ 2.25',match:{pick,t5Line:line,t5Dec:odds,
    triggerHomeDec:pick==='away'?2.15:odds,priceBand:'主賠分段'}};
  const text=box.formatHeavyMsg({...base,rules:[rule]});
  for(const v of ['聯賽：測試聯賽','主隊：主隊甲','客隊：客隊乙',wanted,`賠率：${odds.toFixed(2)}`])assert(text.includes(v));
  assert(text.indexOf('買乜：')<text.indexOf('條件備註'));
  assert(!text.includes('undefined'));assert(!text.includes('NaN'));
  if(pick==='away'){assert(text.includes('平手（0）'));assert(!text.includes('賠率：2.15'));}
  if(id==='ch-Alow'&&pick==='away')examples.push(text);
  checks++;
}
const r={id:'ch-Alow',market:'AH',match:{pick:'away',t5Line:-.75,t5Dec:1.8}};
assert(box.formatHeavyMsg({...base,rules:[r]}).includes('受讓 0.75 球'));checks++;
assert(box.formatHeavyMsg({sid:'offline',rules:[r]}).includes('聯賽：後備聯賽'));checks++;
assert.throws(()=>box.formatHeavyMsg({...base,sid:null,league:'',rules:[r]}),/fixture fields/);checks++;
for(const match of [{pick:'wrong',t5Line:0,t5Dec:1.8},{pick:'away',t5Line:null,t5Dec:1.8},{pick:'away',t5Line:0,t5Dec:NaN}]) {
  assert.throws(()=>box.formatHeavyMsg({...base,rules:[{...r,match}]}));checks++;
}
const both=box.formatHeavyMsg({...base,rules:[r,{id:'ch-N2',market:'OU',match:{pick:'over',t5Line:2.25,t5Dec:1.7}}]});
assert(both.includes('買客（客隊乙）')&&both.includes('買大（全場入球）'));checks++;
console.log(JSON.stringify({ok:true,checks,examples}));
