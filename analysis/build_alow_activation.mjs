import fs from 'node:fs';
import path from 'node:path';
import assert from 'node:assert/strict';
const root=process.argv[2], out=process.argv[3];
const e=JSON.parse(fs.readFileSync(path.join(root,'fresh/alow-n2-evidence.json'),'utf8'));
function replace(s,a,b) {assert.equal(s.split(a).length,2,`Anchor count: ${a.slice(0,100)}`); return s.replace(a,b);}
const originalConfig=JSON.parse(e.files['data/rules.json'].content);
let matcher=e.files['rule_matcher.js'].content;
const helper=`
// A+ v2: retain original base filters; select direction using HOME decimal price.
// The reverse branch uses actual AWAY odds, without a new 1.70 away-price floor.
function matchAlowPriceRule(m, rule) {
  const ah = m.snapshots?.T5_AH, ou = m.snapshots?.T5_OU;
  const ko = Number(m.kickoff_utc), from = Number(rule.version_effective_at_ms);
  if (!ah || !ou) return null;
  for (const s of [ah, ou]) {
    if ([s.h,s.home,s.away,s.captured_at].some(v => v == null || v === "" || !Number.isFinite(Number(v)))) return null;
    if (!(Number(s.home)>0 && Number(s.away)>0)) return null;
    if (rule.require_t5_after_version &&
        !(from > 0 && ko > from && Number(s.captured_at) >= from && Number(s.captured_at) < ko)) return null;
  }
  for (const stage of ["initial_AH", "T30_AH"]) {
    const s = m.snapshots?.[stage];
    if (!s || [s.h,s.home,s.away].some(v => v == null || v === "" || !Number.isFinite(Number(v)))) return null;
  }
  const base = matchAHRule(m, {...rule, side:"H", pick_label:"買主"});
  if (!base) return null;
  const homeDec = toDec(ah.home);
  let pick, band;
  if (homeDec >= 1.70 && homeDec < 1.80) {pick="home"; band="1.70≤主賠<1.80";}
  else if (homeDec >= 2.00 && homeDec < 2.10) {pick="home"; band="2.00≤主賠<2.10";}
  else if (homeDec >= 2.10) {pick="away"; band="主賠≥2.10";}
  else return null;
  const t5Dec = toDec(pick==="home" ? ah.home : ah.away);
  if (!(Number.isFinite(t5Dec) && t5Dec > 1)) return null;
  const initDec = toDec(pick==="home" ? m.snapshots.initial_AH.home : m.snapshots.initial_AH.away);
  return {...base, pick, pickLabel:pick==="home"?"買主":"買客", t5Dec, initDec,
    shrink:initDec-t5Dec, triggerHomeDec:homeDec, priceBand:band,
    direction:pick==="away"?"fade":"follow", ruleVersion:rule.version};
}

`;
matcher=replace(matcher,'function matchAll(m, rules) {',helper+'function matchAll(m, rules) {');
matcher=replace(matcher,'if (r.type === "ou_shift") match = matchOUShiftRule(m, r);',
  'if (r.id === "ch-Alow" && r.alow_price_direction_v2) match = matchAlowPriceRule(m, r);\n    else if (r.type === "ou_shift") match = matchOUShiftRule(m, r);');
matcher=replace(matcher,'if (match) hits.push({ rule: r, match });',`if (match) {
      const resolved = r.id === "ch-Alow" && r.alow_price_direction_v2
        ? {...r, side:match.pick==="home"?"H":"A", pick:match.pick,
           pick_label:match.pickLabel, direction:match.direction}
        : r;
      hits.push({rule:resolved, match});
    }`);

const formatterBefore='function formatHeavyMsg(hit) {'+e.notification_code.split('function formatHeavyMsg(hit) {')[1].split('async function sendTelegram')[0];
let formatterAfter=formatterBefore.replaceAll('r.id === "ch-N2" ||','r.id === "ch-N2" || r.id === "ch-Alow" ||');
formatterAfter=replace(formatterAfter,'  for (const r of hit.rules) {',`  for (const r of hit.rules) {
    if (r.id === "ch-Alow" && r.match?.ruleVersion === "ALOW-PRICE-DIR-v2") {
      const a = r.match;
      lines.push(\`• <b>A+低盤 v2 · \${a.pick === "away" ? "反向買客" : "買主"}</b>\`);
      lines.push(\`  觸發主賠：\${a.triggerHomeDec.toFixed(2)}（\${escapeHtml(a.priceBand)}）\`);
      lines.push(\`  實際選擇：\${a.pickLabel} · \${escapeHtml(a.pick === "away" ? hit.away : hit.home)} · 平手 @ \${a.t5Dec.toFixed(2)}\`);
      lines.push("  基礎條件：平手盤；T30至T5讓球盤不變；大小盤≤2.25");
      lines.push("  重開後實績重新累積；非自動下注");
      continue;
    }`);
// Use frozen match price for Alow rather than re-reading mutable display data.
formatterAfter=replace(formatterAfter,
  'const crown = lookupCrownT5(hit.signal, rMarket, m.t5Line, m.pick);',
  'const crown = r0.id === "ch-Alow" && m.ruleVersion === "ALOW-PRICE-DIR-v2" ? {line:m.t5Line,dec:m.t5Dec} : lookupCrownT5(hit.signal, rMarket, m.t5Line, m.pick);');

const receiptHelper=`
// Immutable A+ notification receipts: actual selected side, price and Telegram ACK.
db.exec(\`CREATE TABLE IF NOT EXISTS alow_notification_receipts (
  sid TEXT PRIMARY KEY, version TEXT NOT NULL, notified_at INTEGER NOT NULL, payload_json TEXT NOT NULL
)\`);
function recordAlowReceipt(m, hits, acknowledgement) {
  const insert = db.prepare("INSERT OR IGNORE INTO alow_notification_receipts(sid,version,notified_at,payload_json) VALUES (?,?,?,?)");
  for (const h of hits) {
    if (h.rule.id !== "ch-Alow" || h.match.ruleVersion !== "ALOW-PRICE-DIR-v2") continue;
    const at = Date.now();
    const payload = {version:h.match.ruleVersion, kickoff_utc:m.kickoff_utc,
      pick:h.match.pick, side:h.rule.side, display:h.match.pickLabel,
      line:h.match.t5Line, odds:h.match.t5Dec, trigger_home_dec:h.match.triggerHomeDec,
      price_band:h.match.priceBand, notified_at:at,
      telegram_message_id:acknowledgement?.result?.message_id ?? null,
      telegram_date_ms:acknowledgement?.result?.date ? acknowledgement.result.date*1000 : null};
    insert.run(m.sid,h.match.ruleVersion,at,JSON.stringify(payload));
  }
}
`;
const notifyBefore=e.notification_code.slice(e.notification_code.indexOf('let heavyNotifyBusy = false;'),e.notification_code.indexOf('// ---------- schedulers ----------'));
let notifyAfter=receiptHelper+notifyBefore;
notifyAfter=replace(notifyAfter,'async function notifyHeavyForSids(sidsWithT5) {',
  'async function notifyHeavyForSids(sidsWithT5, onlyRuleIds = null) {');
notifyAfter=replace(notifyAfter,'const channelRules = rules.filter(r => r.type === "channel");',
  'const channelRules = rules.filter(r => r.type === "channel" && (!onlyRuleIds || onlyRuleIds.includes(r.id)));');
notifyAfter=replace(notifyAfter,'await sendTelegram(formatHeavyMsg(hitGrp), hitGrp.kickoff_utc);',
  'const ack = await sendTelegram(formatHeavyMsg(hitGrp), hitGrp.kickoff_utc);\n            recordAlowReceipt(m, group, ack);');
notifyAfter=replace(notifyAfter,'await sendTelegram(formatHeavyMsg(hit), hit.kickoff_utc);',
  'const ack = await sendTelegram(formatHeavyMsg(hit), hit.kickoff_utc);\n        recordAlowReceipt(m, ruleHits, ack);');
const pollingBefore=e.notification_code.slice(e.notification_code.indexOf('async function notifyOnlyTick() {'),
  e.notification_code.indexOf('setInterval(() => { void checkpointTick();'));
let pollingAfter=replace(pollingBefore,'    if (!rows.length) return;',`    // A+ is Crown-based. Its own retry pool must not depend on Pinnacle
    // snapshots or be blocked by a different channel's whole-match notification.
    const alowRows = db.prepare(\`
      SELECT m.sid FROM matches m
      WHERE m.has_crown=1 AND m.kickoff_utc > ? AND m.kickoff_utc <= ?
        AND NOT EXISTS (SELECT 1 FROM heavy_notified_rule n WHERE n.sid=m.sid AND n.rule_id='ch-Alow')
        AND (SELECT COUNT(DISTINCT market) FROM crown_snapshots WHERE sid=m.sid AND stage='T5')=2
    \`).all(now, now + 30 * 60_000);
    if (alowRows.length) await notifyHeavyForSids(new Set(alowRows.map(r=>r.sid)), ["ch-Alow"]);
    if (!rows.length) return;`);

const historyBefore=e.server_strategy_sections.find(x=>x.code.includes('"/api/strategy-2plus-overlap"')).code;
let historyAfter=historyBefore;
historyAfter=replace(historyAfter,'        CHANNELS.push({',`        CHANNELS.push({
          id:"A+低盤（舊版已停用）", label:"A+低盤 v1（重開前通知紀錄）",
          archived:true, market:"AH", side:"H", display:"買主",
        });
        CHANNELS.push({`);
historyAfter=replace(historyAfter,'"新2（舊版已停用）": "ch-N2" };','"新2（舊版已停用）": "ch-N2", "A+低盤（舊版已停用）":"ch-Alow" };');
historyAfter=replace(historyAfter,'        let n2VersionAt = Infinity;','        let n2VersionAt = Infinity;\n        let alowVersionAt = Infinity;');
historyAfter=replace(historyAfter,'            if (r.enabled === false) disabledRuleIds.add(r.id);',`            if (r.enabled === false) disabledRuleIds.add(r.id);
            if (r.id === "ch-Alow" && r.alow_price_direction_v2 &&
                Number(r.version_effective_at_ms) > 0) alowVersionAt = Number(r.version_effective_at_ms);`);
historyAfter=replace(historyAfter,'        const n2NotifiedAt = new Map();',`        const n2NotifiedAt = new Map();
        const alowNotifiedAt = new Map(), alowReceipts = new Map();
        for (const r of db.prepare("SELECT sid,notified_at FROM heavy_notified_rule WHERE rule_id=?").all("ch-Alow")) {
          notifiedSet.add(\`\${r.sid}::ch-Alow\`);
          alowNotifiedAt.set(r.sid, Number(r.notified_at));
        }
        for (const r of db.prepare("SELECT sid,version,notified_at,payload_json FROM alow_notification_receipts").all()) {
          if (r.version !== "ALOW-PRICE-DIR-v2" || Number(r.notified_at) < alowVersionAt) continue;
          try { alowReceipts.set(r.sid, JSON.parse(r.payload_json)); } catch {}
        }`);
historyAfter=replace(historyAfter,'          for (const ch of CHANNELS) {','          for (let ch of CHANNELS) {');
historyAfter=replace(historyAfter,'            if (ruleId === "ch-N2") {',`            let alowReceipt = null;
            if (ruleId === "ch-Alow") {
              const currentVersion = (alowNotifiedAt.get(m.sid) || 0) >= alowVersionAt;
              if (currentVersion === !!ch.archived) continue;
              if (currentVersion) {
                alowReceipt = alowReceipts.get(m.sid);
                if (!alowReceipt || !["H","A"].includes(alowReceipt.side) ||
                    !(alowReceipt.odds > 1) || alowReceipt.line !== 0) continue;
                ch = {...ch, side:alowReceipt.side, display:alowReceipt.display,
                  label:"A+低盤 v2（主賠分段選方向）"};
              }
            }
            if (ruleId === "ch-N2") {`);
historyAfter=replace(historyAfter,'const crownLine = crownSrc.handicap;','const crownLine = alowReceipt ? alowReceipt.line : crownSrc.handicap;');
historyAfter=replace(historyAfter,'const crownOdds = ch.side === "H" || ch.side === "O" ? toDec(crownSrc.home_odds) : toDec(crownSrc.away_odds);',
  'const crownOdds = alowReceipt ? alowReceipt.odds : ch.side === "H" || ch.side === "O" ? toDec(crownSrc.home_odds) : toDec(crownSrc.away_odds);');
historyAfter=replace(historyAfter,'              ...(ruleId === "ch-N2" ? {',`              ...(ruleId === "ch-Alow" ? {
                rule_version:ch.archived ? "ALOW-v1" : "ALOW-PRICE-DIR-v2",
                notified_at:alowNotifiedAt.get(m.sid) || null,
                trigger_home_dec:alowReceipt?.trigger_home_dec ?? null,
                price_band:alowReceipt?.price_band ?? null,
              } : {}),
              ...(ruleId === "ch-N2" ? {`);
historyAfter=replace(historyAfter,'"受讓075買細（舊版已停用）": chStat(), "新2（舊版已停用）": chStat() },',
  '"受讓075買細（舊版已停用）": chStat(), "新2（舊版已停用）": chStat(), "A+低盤（舊版已停用）":chStat() },');
const liveBefore=e.server_strategy_sections.find(x=>x.code.includes('"/api/four-channels-live"')).code;
let liveAfter=liveBefore;
liveAfter=replace(liveAfter,'        const fires = [];',`        const alowNotifiedSet = new Set(db.prepare("SELECT sid FROM heavy_notified_rule WHERE rule_id=?").all("ch-Alow").map(r=>r.sid));
        const fires = [];`);
liveAfter=replace(liveAfter,'tg_sent: rule.id === "ch-N2" ?', 'tg_sent: rule.id === "ch-Alow" ? alowNotifiedSet.has(mm.sid) : rule.id === "ch-N2" ?');
liveAfter=replace(liveAfter,'              rule_label: rule.label || rule.id,',`              rule_label: rule.label || rule.id,
              ...(rule.id === "ch-Alow" ? {trigger_home_dec:h.match.triggerHomeDec, price_band:h.match.priceBand,
                  rule_version:h.match.ruleVersion} : {}),`);

let html=e.files['strategy.html'].content;
html=replace(html,'A+低盤仍然停用；舊版紀錄保留並獨立標示。','A+低盤 v2 已重開；舊版紀錄保留並獨立標示。');
html=replace(html,'<span class="ch-tag ch-A低盤">A+低盤（停用）</span> = 平手 + AH穩 + OU低盤 (line ≤ 2.25) + 皇冠 T-5 <code>買主 ≥ 1.70</code>',
  '<span class="ch-tag ch-A低盤">A+低盤 v2</span> = 平手 + T30至T5讓球盤不變 + 大小盤≤2.25；按皇冠 T-5 <b>主隊十進制賠率</b>：<code>1.70≤主賠&lt;1.80 或 2.00≤主賠&lt;2.10 → 買主</code>；<code>主賠≥2.10 → 反向買客</code>。其餘主賠不通知；買客用實際客賠，沒有另加客賠≥1.70。');
html=replace(html,'  <p id="n2-version-note"',`  <p id="alow-version-note" style="color:var(--ch2);font-size:12px">A+低盤 v2 只收重開後的新T-5快照並於開賽前通知，每場只發一次。TG與結算按實際主／客方向及通知時賠率；新版從零累積，舊159場不當成新版實績。兩組獨立前瞻已停止，舊資料保留。沒有自動下注。</p>
  <p id="n2-version-note"`);
html=replace(html,'<button data-filter="ch:A+低盤">A+低盤</button>',
  '<button data-filter="ch:A+低盤">A+低盤 v2</button>\n  <button data-filter="ch:A+低盤（舊版已停用）">A+低盤舊版紀錄</button>');
html=replace(html,'<button data-filter="ah">買主 (AH)</button>','<button data-filter="ah">讓球（買主／買客）</button>');
html=replace(html,'const CH_ORDER = ["B∩I", "A+低盤",','const CH_ORDER = ["B∩I", "A+低盤", "A+低盤（舊版已停用）",');
html=replace(html,'"A+低盤": "ch2",','"A+低盤": "ch2", "A+低盤（舊版已停用）":"ch2",');
html=replace(html,'"A+低盤": "ch-A低盤",','"A+低盤": "ch-A低盤", "A+低盤（舊版已停用）":"ch-A低盤",');
html=replace(html,'"ch-Alow": "A+低盤",','"ch-Alow": "A+低盤 v2",');
html=replace(html,': chId === "新2" ? "新2 v2（重開後獨立）" : chId',
  ': chId === "新2" ? "新2 v2（重開後獨立）" : chId === "A+低盤" ? "A+低盤 v2（重開後）" : chId');
const db=e.databases['/opt/crown-radar-v2/data/crown.db'].rows, snapshots={};
for(const r of db.crown_snapshots) (snapshots[r.sid]??={})[`${r.stage}_${r.market}`]={h:r.handicap,home:r.home_odds,away:r.away_odds,captured_at:r.captured_at};
const payload={
  expected_sha256:{'server.js':e.server_sha256,...Object.fromEntries(['rule_matcher.js','strategy.html','data/rules.json'].map(n=>[n,e.files[n].sha256]))},
  original_config:originalConfig,original_matcher:e.files['rule_matcher.js'].content,
  matcher,html,history_before:historyBefore,history_after:historyAfter,
  live_before:liveBefore,live_after:liveAfter,formatter_before:formatterBefore,formatter_after:formatterAfter,
  notify_before:notifyBefore,notify_after:notifyAfter,
  polling_before:pollingBefore,polling_after:pollingAfter,
  rule_patch:{enabled:true,alow_price_direction_v2:true,require_t5_after_version:true,
    version:'ALOW-PRICE-DIR-v2',side:'H',pick:'home',pick_label:'按主賠選主／客',
    label:'A+低盤 v2 · 平手／T30至T5盤不變／大小盤≤2.25 · 主賠1.70至低於1.80或2.00至低於2.10買主；主賠≥2.10買客',
    detail:'兩個主賠區間順向買主；主賠≥2.10反向買客，客賠不另加1.70底線。其他原條件保留。啟用前快照及已開賽不補發。新版與舊通知分開統計。',
    hit_display:'重開後新版自然通知重新累積；舊研究不是新版實績或未來保證',
    hit:0,sample:0,hit_rate:null,ci_low:null,ci_high:null,min_odds_conservative:null,min_odds_point:null},
  regression_fixtures:db.matches.map(m=>({...m,snapshots:snapshots[m.sid]||{},sublines:{}})),
};
fs.writeFileSync(out,JSON.stringify(payload));
fs.writeFileSync(path.join(root,'strategy.html'),html);
fs.writeFileSync(path.join(root,'rule_matcher.js'),matcher);
console.log(JSON.stringify({fixtures:payload.regression_fixtures.length,bytes:fs.statSync(out).size}));
