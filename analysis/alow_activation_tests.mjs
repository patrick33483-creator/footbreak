import assert from 'node:assert/strict';
import fs from 'node:fs';
import vm from 'node:vm';
const p=JSON.parse(fs.readFileSync(process.argv[2],'utf8'));
const imp=async c=>import('data:text/javascript;base64,'+Buffer.from(c).toString('base64'));
const {matchAll}=await imp(p.matcher), {matchAll:oldMatch}=await imp(p.original_matcher);
const rule={...p.original_config.rules.find(r=>r.id==='ch-Alow'),...p.rule_patch,version_effective_at_ms:Date.parse('2026-09-26T10:00:00Z')};
let checks=0;
function fixture(home=1.75,away=2.05) {
  const ko=Date.parse('2026-09-27T08:00:00+08:00'),m={sid:'test',kickoff_utc:ko,league:'測試',home:'測試主隊',away:'測試客隊',snapshots:{},sublines:{}};
  for(const [stage,gap] of [['initial',240],['T30',30],['T5',5]]) for(const market of ['AH','OU'])
    m.snapshots[`${stage}_${market}`]={h:market==='AH'?'0':'2.25',home:home-1,away:away-1,captured_at:ko-gap*60000};
  return m;
}
for(const [price,side] of [[1.699,null],[1.70,'home'],[1.799,'home'],[1.80,null],[1.90,null],[1.999,null],[2.00,'home'],[2.099,'home'],[2.10,'away'],[2.11,'away'],[3,'away']]) {
  const hits=matchAll(fixture(price,1.60),[rule]);assert.equal(hits.length,side?1:0,`${price}`);
  if(side) {assert.equal(hits[0].match.pick,side);assert.equal(hits[0].rule.pick,side);assert.equal(hits[0].match.t5Dec,side==='home'?price:1.60);}
  checks++;
}
for(const mutate of [
  m=>m.snapshots.T30_AH.h='.25',m=>m.snapshots.T5_AH.h='.25',
  m=>m.snapshots.T5_OU.h='2.5',m=>delete m.snapshots.T30_AH,
  m=>m.snapshots.T5_AH.captured_at=rule.version_effective_at_ms-1,
  m=>m.snapshots.T5_OU.captured_at=rule.version_effective_at_ms-1,
  m=>m.snapshots.T5_AH.captured_at=m.kickoff_utc,
  m=>m.snapshots.T5_OU.captured_at=m.kickoff_utc,
  m=>m.snapshots.T5_AH.home=null,m=>m.snapshots.T5_AH.away=NaN,
  m=>m.snapshots.T5_OU.h=null,m=>m.kickoff_utc=NaN,
]) {const m=fixture();mutate(m);assert.equal(matchAll(m,[rule]).length,0);checks++;}
// Opt-in only: all prior rules/fixtures retain their behavior.
const oldAlow={...p.original_config.rules.find(r=>r.id==='ch-Alow'),enabled:true};
for(const m of p.regression_fixtures) {
  assert.deepEqual(matchAll(m,p.original_config.rules),oldMatch(m,p.original_config.rules));checks++;
  const base=oldMatch(m,[oldAlow]), actual=matchAll(m,[{...rule,require_t5_after_version:false}]);
  if(actual.length) {
    assert.equal(base.length,1,'new A+ must be subset of old A+');
    const h=base[0].match.t5Dec;
    assert.equal(actual[0].match.pick,h>=2.10?'away':'home');
  }
  checks++;
}
for(const [,c] of p.html.matchAll(/<script\b[^>]*>([\s\S]*?)<\/script>/g)) new vm.Script(c);
new vm.Script(`async function route(){${p.history_after}}`);
new vm.Script(`async function route(){${p.live_after}}`);
new vm.Script(p.notify_after);
new vm.Script(p.polling_after);
function formatter() {
  const box={flagFor:()=>'',formatKickoffHKT:()=>'',escapeHtml:s=>String(s).replaceAll('<','&lt;'),
    lookupCrownT5:()=>null,crownDirectLink:()=>null};
  vm.createContext(box);vm.runInContext(p.formatter_after,box);return box.formatHeavyMsg;
}
const previews=[];
for(const price of [1.75,2.05,2.15]) {
  const m=fixture(price,1.60),h=matchAll(m,[rule])[0], text=formatter()({...m,rules:[{...h.rule,match:h.match}]});
  assert(text.includes(`觸發主賠：${price.toFixed(2)}`));
  assert(text.includes(price>=2.1?'反向買客':'買主'));
  assert(text.includes(`平手 @ ${(price>=2.1?1.60:price).toFixed(2)}`));
  assert(!text.includes('73.9%'));previews.push(text);checks++;
}
// Run real history route using frozen notification price deliberately different from current snapshot.
const old={...fixture(2.15),sid:'old',home_score:0,away_score:1,status:'完'},
  home={...fixture(1.75),sid:'home',home_score:1,away_score:0,status:'完'},
  away={...fixture(1.75,1.95),sid:'away',home_score:0,away_score:1,status:'完'};
const fixtures=[old,home,away], receipts=new Map([
  ['home',{version:rule.version,side:'H',display:'買主',line:0,odds:1.75,trigger_home_dec:1.75}],
  ['away',{version:rule.version,side:'A',display:'買客',line:0,odds:1.60,trigger_home_dec:2.15}],
]);
let result;
const box={url:{pathname:'/api/strategy-2plus-overlap'},Date,Map,Set,console,S2T:{},
  fs:{readFileSync:()=>JSON.stringify({rules:[rule]})},
  db:{prepare:sql=>({all:(...args)=>{
    if(sql.includes('FROM matches m JOIN finished_matches')) return fixtures;
    if(sql.includes('FROM crown_snapshots')) return fixtures.flatMap(m=>Object.entries(m.snapshots).map(([k,v])=>({
      sid:m.sid,stage:k.slice(0,k.lastIndexOf('_')),market:k.slice(k.lastIndexOf('_')+1),
      handicap:v.h,home_odds:v.home,away_odds:v.away
    })));
    if(sql.includes('SELECT sid,notified_at FROM heavy_notified_rule')&&args[0]==='ch-Alow')
      return fixtures.map(m=>({sid:m.sid,notified_at:rule.version_effective_at_ms+(m.sid==='old'?-1:1)}));
    if(sql.includes('FROM alow_notification_receipts')) return [...receipts].map(([sid,r])=>({sid,version:rule.version,notified_at:rule.version_effective_at_ms+1,payload_json:JSON.stringify(r)}));
    return [];
  }})},
  res:{writeHead:s=>assert.equal(s,200),end:t=>{result=JSON.parse(t);}}};
vm.createContext(box);await vm.runInContext(`(async()=>{${p.history_after}})()`,box);
assert.equal(result.summary.by_channel['A+低盤'].n,2);
assert.equal(result.summary.by_channel['A+低盤（舊版已停用）'].n,1);
assert.equal(result.games.find(g=>g.sid==='away').bet.side,'A');
assert.equal(result.games.find(g=>g.sid==='away').crown.result,'W');
assert(Math.abs(result.games.find(g=>g.sid==='away').crown.pnl-600)<1e-8);
assert.equal(result.games.find(g=>g.sid==='old').crown.result,'L');checks+=6;
// Real notification pipeline, mocked Telegram only. Check grouping/dedupe/failed sends.
async function notifierTest({fail=false,late=false}={}) {
  const m=fixture(2.15,1.60);m.kickoff_utc=Date.now()+(late?-1:180000);
  for(const s of Object.values(m.snapshots))s.captured_at=m.kickoff_utc-120000;
  m.crown_snapshots=m.snapshots;
  const sent=[],ledger=new Set(),saved=[];
  const n2={...p.original_config.rules.find(r=>r.id==='ch-N2'),enabled:true,hour_hkt:null,hour_hkt_min:null,hour_hkt_max_exclusive:null,require_t5_after_version:false};
  const context={Date,Map,Set,console,HEAVY_NOTIFY_ENABLED:true,RULES_PATH:'rules',
    fs:{existsSync:()=>true,readFileSync:()=>JSON.stringify({rules:[rule,n2]})},
    matchAll,buildSignalsPayload:()=>[m],log:()=>{},formatHeavyMsg:formatter(),
    sendTelegram:async text=>{if(fail)throw new Error('mock failure');sent.push(text);return {ok:true,result:{message_id:123,date:Math.floor(Date.now()/1000)}};},
    db:{exec:()=>{},prepare:sql=>({
      get:(sid,id)=>ledger.has(sid+'::'+id)?{rule_id:id}:undefined,
      run:(...a)=>{
        if(sql.includes('INTO heavy_notified_rule'))ledger.add(a[0]+'::'+a[1]);
        if(sql.includes('INTO alow_notification_receipts'))saved.push(JSON.parse(a[3]));
      }
    })}};
  vm.createContext(context);vm.runInContext(p.notify_after,context);
  await context.notifyHeavyForSids(new Set([m.sid]));
  if(fail||late) {assert.equal(saved.length,0);assert.equal(ledger.size,0);assert.equal(sent.length,0);}
  else {
    assert.equal(sent.length,2);assert.equal(saved.length,1);assert.equal(saved[0].side,'A');
    assert.equal(saved[0].odds,1.6);assert.equal(saved[0].telegram_message_id,123);
    assert(sent.some(t=>t.includes('反向買客')));assert(sent.some(t=>t.includes('買大')));
    await context.notifyHeavyForSids(new Set([m.sid]));assert.equal(sent.length,2);
  }checks++;
}
await notifierTest();await notifierTest({fail:true});await notifierTest({late:true});
// A+ retry is independent of Pinnacle and of another channel's prior notice.
const pollCalls=[],pollBox={Date,Set,log:()=>{},notifyHeavyForSids:async(s,ids)=>pollCalls.push({s:[...s],ids}),
  db:{prepare:sql=>({all:()=>sql.includes('FROM crown_snapshots')?[{sid:'crown-only'}]:[]})}};
vm.createContext(pollBox);vm.runInContext(p.polling_after,pollBox);await pollBox.notifyOnlyTick();
assert.equal(pollCalls.length,1);assert.equal(pollCalls[0].s[0],'crown-only');
assert.equal(pollCalls[0].ids[0],'ch-Alow');checks++;
console.log(JSON.stringify({ok:true,checks,fixtures:p.regression_fixtures.length,message_previews:previews}));
