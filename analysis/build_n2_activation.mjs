import fs from 'node:fs';
import path from 'node:path';
const root = process.argv[2];
const out = process.argv[3];
const evidence = JSON.parse(fs.readFileSync(path.join(root,'fresh/alow-n2-evidence.json'),'utf8'));
const formatter = evidence.notification_code.split('function formatHeavyMsg(hit) {')[1].split('async function sendTelegram')[0];
const formatterBefore = 'function formatHeavyMsg(hit) {' + formatter;
const formatterAfter = formatterBefore.replaceAll('r.id === "ch-BI" || String(r.id)', 'r.id === "ch-BI" || r.id === "ch-N2" || String(r.id)');
const liveBefore = evidence.server_strategy_sections[1].code;
const liveAfter = liveBefore.replace(
  'const fires = [];',
  'const n2NotifiedSet = new Set(db.prepare("SELECT sid FROM heavy_notified_rule WHERE rule_id=?").all("ch-N2").map(r => r.sid));\n        const fires = [];'
).replace(
  'tg_sent: rule.id === "ch-U075-v2" ? newNotifiedSet.has(mm.sid) : notifiedSet.has(mm.sid),',
  'tg_sent: rule.id === "ch-N2" ? n2NotifiedSet.has(mm.sid) : rule.id === "ch-U075-v2" ? newNotifiedSet.has(mm.sid) : notifiedSet.has(mm.sid),'
);
const config = JSON.parse(evidence.files['data/rules.json'].content);
const db = evidence.databases['/opt/crown-radar-v2/data/crown.db'].rows;
const snapshots={};
for(const r of db.crown_snapshots) (snapshots[r.sid]??={})[`${r.stage}_${r.market}`]={
  h:r.handicap,home:r.home_odds,away:r.away_odds,captured_at:r.captured_at,
};
const payload={
  expected_sha256:{
    'server.js':evidence.server_sha256,
    ...Object.fromEntries(['rule_matcher.js','strategy.html','data/rules.json'].map(n=>[n,evidence.files[n].sha256])),
  },
  original_config:config,
  original_matcher:evidence.files['rule_matcher.js'].content,
  matcher:fs.readFileSync(path.join(root,'rule_matcher.js'),'utf8'),
  html:fs.readFileSync(path.join(root,'strategy.html'),'utf8'),
  history_before:evidence.server_strategy_sections[0].code,
  history_after:fs.readFileSync(path.join(root,'history.txt'),'utf8'),
  formatter_before:formatterBefore,
  formatter_after:formatterAfter,
  live_before:liveBefore,live_after:liveAfter,
  rule_patch:{
    enabled:true, hour_hkt_min:8,hour_hkt_max_exclusive:12,t5_dec_min:1.70,
    require_t5_after_version:true,version:'N2-AM8-v2',
    version_note:'2026-09-26 用戶批准重開新2；香港時間08:00至11:59開賽，大賠維持≥1.70，其他原条件不變。',
    label:'聲道 新2 v2 · 香港時間08:00至11:59開賽 · 皇冠T-5大小盤≤2.25 · 大賠≥1.70 · 買大',
    hit_display:'重開後新版實績獨立累積；56注事後研究不是未來命中率保證',
    detail:'原新2只收窄開賽時間，由04:00至11:59改成08:00至11:59；價格維持十進制1.70或以上，非1.80。保留旧通知帳本，啟用前快照及已開賽不補發。',
    hit:0,sample:0,hit_rate:null,ci_low:null,ci_high:null,
    min_odds_conservative:null,min_odds_point:null,
  },
  regression_fixtures:db.matches.map(m=>({...m,snapshots:snapshots[m.sid]||{},sublines:{}})),
};
fs.writeFileSync(out,JSON.stringify(payload));
console.log('Generated payload with regression fixtures:',payload.regression_fixtures.length);
