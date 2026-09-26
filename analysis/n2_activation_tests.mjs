import assert from 'node:assert/strict';
import fs from 'node:fs';
import vm from 'node:vm';

const payload = JSON.parse(fs.readFileSync(process.argv[2], 'utf8'));
const imported = async code => import('data:text/javascript;base64,' + Buffer.from(code).toString('base64'));
const {matchAll} = await imported(payload.matcher);
const {matchAll: oldMatch} = await imported(payload.original_matcher);
const oldConfig = payload.original_config;
const rule = {...oldConfig.rules.find(r => r.id === 'ch-N2'), ...payload.rule_patch,
  version_effective_at_ms: Date.parse('2026-09-26T10:00:00Z')};
let checks = 0;
function fixture(time = '08:00:00', odds = .7, line = '2.25') {
  const ko = Date.parse(`2026-09-27T${time}+08:00`);
  const m = {sid:'test', kickoff_utc:ko, league:'TEST', snapshots:{}};
  for (const [stage,gap] of [['initial',240],['T30',30],['T5',5]]) {
    for (const market of ['AH','OU']) m.snapshots[`${stage}_${market}`] = {
      h:market === 'AH' ? '0' : line, home:odds, away:.9, captured_at:ko-gap*60000,
    };
  }
  return m;
}
function expect(m, yes, why) {
  assert.equal(matchAll(m,[rule]).length, Number(yes),why); checks++;
}
for (const [time,yes] of [['07:59:59',false],['08:00:00',true],['11:59:59',true],
  ['12:00:00',false],['04:00:00',false],['00:00:00',false],['23:59:59',false]]) expect(fixture(time),yes,time);
expect(fixture('08:00:00',.699),false,'1.699 fails');
expect(fixture('08:00:00',.7),true,'1.70 accepted');
expect(fixture('08:00:00',.799),true,'1.799 accepted');
expect(fixture('08:00:00',.8),true,'1.80 accepted');
expect(fixture('08:00:00',.7,'2.5'),false,'2.5 fails');
expect(fixture('08:00:00',.7,'2'),true,'2 accepted');
const stale=fixture(); stale.snapshots.T5_OU.captured_at=rule.version_effective_at_ms-1;
expect(stale,false,'preactivation T5 fails');
const late=fixture(); late.snapshots.T5_OU.captured_at=late.kickoff_utc;
expect(late,false,'postkickoff T5 fails');
const missing=fixture();delete missing.snapshots.T30_OU;
expect(missing,false,'missing T30 fails');
const bad=fixture();bad.kickoff_utc='garbage';expect(bad,false,'invalid time fails');
// Across every hour and legacy rule, new opt-in gates must not change old behavior.
for(let hour=0;hour<24;hour++) for(const odds of [.65,.7,.8,.95]) {
  const m=fixture(String(hour).padStart(2,'0')+':00:00',odds);
  assert.deepEqual(matchAll(m,oldConfig.rules),oldMatch(m,oldConfig.rules));checks++;
}
// Every historical fixture: unchanged rules remain bit-for-bit equivalent.
for (const m of payload.regression_fixtures || []) {
  assert.deepEqual(matchAll(m,oldConfig.rules),oldMatch(m,oldConfig.rules));checks++;
  const replayRule={...rule,require_t5_after_version:false};
  const oldN2={...oldConfig.rules.find(r=>r.id==='ch-N2'),enabled:true};
  const expected=oldMatch(m,[oldN2]).length && (new Date(m.kickoff_utc).getUTCHours()+8)%24>=8;
  assert.equal(matchAll(m,[replayRule]).length,Number(!!expected));checks++;
}
for (const [,code] of payload.html.matchAll(/<script\b[^>]*>([\s\S]*?)<\/script>/g)) new vm.Script(code);
new vm.Script(`async function route(){${payload.history_after}}`);
// Renderer must label new and old versions independently, including zero samples.
assert(payload.html.includes('新2 v2（重開後獨立）'));
assert(payload.history_after.includes('n2NotifiedAt'));
// Formatter tested offline; no Telegram send operation.
const formatterCode=payload.formatter_after;
const sandbox={flagFor:()=>'',formatKickoffHKT:()=>'',escapeHtml:s=>String(s),
  lookupCrownT5:()=>null,crownDirectLink:()=>null};
vm.createContext(sandbox);
vm.runInContext(formatterCode,sandbox);
const msg=sandbox.formatHeavyMsg({league:'離線測試',home:'測試主隊',away:'測試客隊',rules:[{...rule,match:{}}]});
assert(msg.includes('08:00至11:59'));assert(msg.includes('1.70'));
assert(!msg.includes('NaN'));checks++;
// Execute complete history endpoint using two synthetic notified fixtures:
// one before reactivation, one after. Never write to the real notification DB.
const n2old={...fixture(),sid:'old',home:'old',away:'away',home_score:3,away_score:0,status:'完'};
const n2new={...fixture(),sid:'new',home:'new',away:'away',home_score:3,away_score:0,status:'完'};
let result;
const historySandbox={
  url:{pathname:'/api/strategy-2plus-overlap'},Date,Map,Set,console,S2T:{},
  fs:{readFileSync:()=>JSON.stringify({rules:[rule]})},
  db:{prepare:sql=>({all:()=>{
    if(sql.includes('FROM matches m JOIN finished_matches')) return [n2old,n2new];
    if(sql.includes('FROM crown_snapshots')) return [n2old,n2new].flatMap(m=>Object.entries(m.snapshots).map(([k,v])=>{
      const i=k.lastIndexOf('_');
      return {sid:m.sid,stage:k.slice(0,i),market:k.slice(i+1),handicap:v.h,home_odds:v.home,away_odds:v.away};
    }));
    if(sql.includes('SELECT sid,notified_at FROM heavy_notified_rule')) return [
      {sid:'old',notified_at:rule.version_effective_at_ms-1},
      {sid:'new',notified_at:rule.version_effective_at_ms+1},
    ];
    return [];
  }})},
  res:{writeHead:status=>assert.equal(status,200),end:text=>{result=JSON.parse(text);}},
};
vm.createContext(historySandbox);
await vm.runInContext(`(async()=>{${payload.history_after}})()`,historySandbox);
assert.equal(result.summary.by_channel['新2'].n,1);
assert.equal(result.summary.by_channel['新2（舊版已停用）'].n,1);
assert.equal(result.games.find(g=>g.sid==='new').rule_version,'N2-AM8-v2');
assert.equal(result.games.find(g=>g.sid==='old').rule_version,'N2-v1');checks+=4;
console.log(JSON.stringify({ok:true,checks,message_preview:msg}));
