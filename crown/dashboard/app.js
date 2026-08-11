/* 足破 · 皇冠賽事預測終端
 * 介面及預測流程沿用 HKJC 足破原版，只替換 HDC/HIL 盤源。
 */
'use strict';

const $  = (s, r = document) => r.querySelector(s);
const $$ = (s, r = document) => [...r.querySelectorAll(s)];
const pc = (x, d = 1) => (x == null ? '—' : (x * 100).toFixed(d) + '%');
const sg = (x, d = 2) => (x == null ? '—' : (x >= 0 ? '+' : '') + Number(x).toFixed(d));
const f2 = (x) => (x == null ? '—' : Number(x).toFixed(2));
const f3 = (x) => (x == null ? '—' : Number(x).toFixed(3));
const money = (x) => (x == null ? '—' : '$' + Math.round(x).toLocaleString('en-US'));
const esc = (s) => String(s ?? '').replace(/[&<>"']/g, (c) =>
  ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c]));

const TAG = { 'T-5': 'tag-t5', 'T-30': 'tag-t30', '首預': 'tag-t60', '待入窗': 'tag-wait', '已開賽': 'tag-none' };
const STAGE_DESC = {
  '首預': '每晚 23:59 掃全板 · 參考初盤同開盤結構',
  'T-30': '開賽前 30 分鐘 · 陣容、傷患出咗,賠率漸定',
  'T-5': '開賽前 5 分鐘 · 唯一落注時點',
};
const VD_CLS = { '落注': 'v-go', '傾向': 'v-lean', '偏向': 'v-soft', '已預測': 'v-lean', '觀望': 'v-wait', '無傾向': 'v-none' };
const MKT = { HDC: '讓球', HIL: '入球大小', CHL: '總角球大小', HAD: '主客和' };

// DigitalOcean serves the authenticated dashboard and proxies this same-origin
// path to a local-only simulation settlement service.
const API_BASE = '/api';
let DATA = null, LIST = [], LED = null, SEL = null, STAGE = 'all', Q = '', VIEW = 'pred';
let SETTLE_MESSAGE = '', SETTLE_BAD = false, SETTLING = false;
const FINISHED_MATCH_GRACE_MINUTES = 150;

const kt = (s) => new Date(String(s).replace(' ', 'T') + (/[Z+]/.test(s) ? '' : '+08:00'));
function hkClock(s) { return kt(s).toLocaleTimeString('zh-HK', { hour: '2-digit', minute: '2-digit', hour12: false, timeZone: 'Asia/Hong_Kong' }); }
function hkDay(s) { return kt(s).toLocaleDateString('zh-HK', { month: '2-digit', day: '2-digit', timeZone: 'Asia/Hong_Kong' }); }
function hkStamp(s) { return kt(s).toLocaleString('zh-HK', { month: '2-digit', day: '2-digit', hour: '2-digit', minute: '2-digit', hour12: false, timeZone: 'Asia/Hong_Kong' }); }
function minsLeft(s) { return (kt(s) - Date.now()) / 60000; }
function displayableMatches(matches) {
  // 主清單只服務即時追蹤。開賽後保留 150 分鐘以覆蓋補時／加時，
  // 之後視為已完場並移到預測紀錄，避免舊場阻住最新賽事。
  return (matches || []).filter((match) => {
    const kickoff = kt(match.kickoff_hkt);
    return Number.isFinite(kickoff.getTime())
      && (Date.now() - kickoff.getTime()) < FINISHED_MATCH_GRACE_MINUTES * 60000;
  });
}
function cdText(m) {
  if (m < 0) return '已開賽';
  if (m < 60) return Math.round(m) + '分';
  const h = Math.floor(m / 60);
  return h < 24 ? h + '時' + String(Math.round(m % 60)).padStart(2, '0')
                : Math.floor(h / 24) + '日' + (h % 24) + '時';
}
function stageOf(m) {
  if (m < 0) return '已開賽';
  if (m <= 9) return 'T-5';
  if (m <= 36) return 'T-30';
  return '待入窗';
}
function convClass(c) { return c >= 65 ? 'good' : c >= 58 ? 'amber' : c >= 50 ? '' : 'bad'; }
function heat(p, max) {
  if (max <= 0) return 'oklch(21% 0.01 258)';
  const t = Math.pow(p / max, 0.55);
  return `oklch(${(20 + t * 52).toFixed(1)}% ${(0.012 + t * 0.135).toFixed(3)} 74)`;
}

/* ══════════════════════ 啟動 ══════════════════════ */
async function fetchDashboardData() {
  // 先讀同一部署內嘅 JSON；本機 API 只作後備，兩者都由 Nginx
  // 同一個已認證來源提供。
  const sources = [`data.json?v=${Date.now()}`];
  if (API_BASE) sources.push(`${API_BASE}/data?v=${Date.now()}`);
  for (const url of sources) {
    try {
      const r = await fetch(url, { cache: 'no-store' });
      if (!r.ok) throw new Error('HTTP ' + r.status);
      return await r.json();
    } catch (_) {
      // 後端暫時不可用時，繼續讀靜態備份。
    }
  }
  if (window.__CROWN_DATA__) return window.__CROWN_DATA__;
  throw new Error('後端同靜態備份都無法讀取');
}

function applyData(raw) {
  DATA = raw;
  LED = raw.ledger || { bets: [], stats: {}, log: [] };
  LIST = displayableMatches(raw.matches).slice()
    .sort((a, b) => kt(a.kickoff_hkt) - kt(b.kickoff_hkt));
  $('#genAt').textContent = hkStamp(raw.generated_at) + ' HKT';
}

async function boot() {
  try {
    applyData(await fetchDashboardData());
  } catch (e) {
    $('#detail').innerHTML = `<div class="empty">資料載入失敗:${esc(e.message)}</div>`;
    return;
  }
  bindUI();
  render();
  setInterval(render, 30000);
  setInterval(() => refresh(true), 5 * 60000);   // 每 5 分鐘自動攞一次新資料
}

let BUSY = false;
async function refresh(silent) {
  if (BUSY) return;
  BUSY = true;
  const b = $('#refresh');
  if (b) { b.classList.add('spin'); b.disabled = true; }
  try {
    let raw;
    let settlementBusy = false;
    if (!silent && API_BASE) {
      const response = await fetch(`${API_BASE}/settle`, {
        method: 'POST',
        cache: 'no-store',
        headers: {
          'Content-Type': 'application/json',
          'X-Crown-Action': 'settle-simulation',
        },
        body: JSON.stringify({ confirm: 'simulation-only' }),
      });
      const result = await response.json().catch(() => ({}));
      settlementBusy = response.status === 409 && result.error === 'settlement_busy';
      if (!response.ok && !settlementBusy) {
        throw new Error(result.error || `結算 HTTP ${response.status}`);
      }
      raw = result.data;
    }
    if (!raw) raw = await fetchDashboardData();
    const changed = raw.generated_at !== (DATA && DATA.generated_at);
    applyData(raw);
    render();
    if (!silent) {
      flash(settlementBusy
        ? '結算程序運行中，已載入目前最新資料'
        : changed ? '賽果核對完成，已更新到最新資料' : '賽果核對完成，暫時冇新賽果');
    }
  } catch (e) {
    if (!silent) flash('更新失敗:' + e.message, true);
  } finally {
    BUSY = false;
    if (b) { b.classList.remove('spin'); b.disabled = false; }
  }
}

function flash(msg, bad) {
  let t = $('#toast');
  if (!t) { t = document.createElement('div'); t.id = 'toast'; document.body.appendChild(t); }
  t.className = 'toast show' + (bad ? ' bad' : '');
  t.textContent = msg;
  clearTimeout(flash._t);
  flash._t = setTimeout(() => { t.className = 'toast'; }, 2600);
}

function render() {
  $('#viewPred').hidden = VIEW !== 'pred';
  $('#viewLedger').hidden = VIEW !== 'ledger';
  $('#viewHistory').hidden = VIEW !== 'history';
  $$('#nav .navbtn').forEach((b) => b.classList.toggle('is-on', b.dataset.view === VIEW));
  if (VIEW === 'pred') {
    LIST = displayableMatches(LIST);
    renderKpis(); renderList();
    if (!SEL || !LIST.some((m) => m.match_id === SEL)) {
      const f = LIST.find((m) => m.pick) || LIST[0];
      SEL = f ? f.match_id : null;
    }
    if (SEL) renderDetail(SEL);
    else $('#detail').innerHTML = '<div class="empty">暫時冇未完場賽事</div>';
  } else if (VIEW === 'history') {
    renderHistory();
  } else {
    renderLedger();
  }
}

function bindUI() {
  const wx = $('#warnX'); if (wx) wx.onclick = () => { $('#warnbar').hidden = true; };
  $('#search').oninput = (e) => { Q = e.target.value.trim().toLowerCase(); renderList(); };
  $$('#stageChips .chip').forEach((b) => {
    b.onclick = () => {
      $$('#stageChips .chip').forEach((x) => x.classList.remove('is-on'));
      b.classList.add('is-on'); STAGE = b.dataset.stage; renderList();
    };
  });
  $$('#nav .navbtn').forEach((b) => { b.onclick = () => { VIEW = b.dataset.view; render(); }; });
  const rb = $('#refresh'); if (rb) rb.onclick = () => refresh(false);
}

/* ══════════════════════ KPI ══════════════════════ */
function renderKpis() {
  const has = (m, k) => (m.stages || []).some((x) => x.stage === k);
  const nT5 = LIST.filter((m) => has(m, 'T-5')).length;
  const s = LED.stats || {};
  const ledgerBets = (LED.bets || []).filter((b) => b.status !== 'VOIDED');
  const K = [
    ['追蹤賽事', LIST.length, ''],
    ['已首預', LIST.filter((m) => has(m, '首預')).length, ''],
    ['已 T-30', LIST.filter((m) => has(m, 'T-30')).length, ''],
    ['已 T-5', nT5, ''],
    ['模擬注', ledgerBets.length, ledgerBets.length ? 'good' : ''],
    ['模擬總注碼', money(s.turnover), 'amber'],
    ['待決注碼', money(s.open_stake), Number(s.open_stake || 0) > 0 ? 'amber' : ''],
    ['累計盈虧', money(s.pnl), Number(s.pnl || 0) > 0 ? 'good' : Number(s.pnl || 0) < 0 ? 'bad' : ''],
  ];
  $('#kpis').innerHTML = K.map(([l, v, c]) =>
    `<div class="kpi"><span class="kpi-lbl">${l}</span><span class="kpi-val ${c}">${v}</span></div>`).join('');
}

/* ══════════════════════ 賽事清單 ══════════════════════ */
function filtered() {
  return LIST.filter((m) => {
    if (STAGE === 'pick') { if (!m.pick) return false; }
    else if (STAGE === '首預') { if (!(m.stages || []).some((x) => x.stage === '首預')) return false; }
    else if (STAGE !== 'all' && stageOf(minsLeft(m.kickoff_hkt)) !== STAGE) return false;
    if (!Q) return true;
    return [m.home, m.away, m.league, m.home_en, m.away_en]
      .some((s) => (s || '').toLowerCase().includes(Q));
  });
}

function renderList() {
  const rows = filtered();
  $('#railCount').textContent = `顯示 ${rows.length} / ${LIST.length} 場`;
  $('#fixtures').innerHTML = rows.map((m) => {
    const mm = minsLeft(m.kickoff_hkt), st = stageOf(mm);
    return `<li class="fx ${m.match_id === SEL ? 'is-sel' : ''}" data-id="${esc(m.match_id)}" tabindex="0">
      <div class="fx-when">
        <span class="fx-clock">${hkClock(m.kickoff_hkt)}</span>
        <span class="fx-cd ${mm < 65 ? 'hot' : ''}">${cdText(mm)}</span>
      </div>
      <div class="fx-body">
        <div class="fx-teams">${esc(m.home)}<span class="fx-vs">vs</span>${esc(m.away)}</div>
        <div class="fx-meta">
          <span class="fx-tag ${TAG[st]}">${st}</span>
          <span class="conv-pill ${convClass(m.conviction)}">信念 ${m.conviction == null ? '—' : Number(m.conviction).toFixed(1)}</span>
          <span class="fx-lg">${esc(hkDay(m.kickoff_hkt))} · ${esc(m.league)}</span>
        </div>
        <div class="fx-foot">${dots(m)}
          ${m.pick
            ? `<span class="fx-pick">▶ ${esc(m.pick.label)} <b>@${f2(m.pick.odds)}</b> · ${money(m.pick.stake)}</span>`
            : `<span class="fx-pick wait">${(m.stages || []).some((x) => x.stage === 'T-5') ? '○ 唔買' : '○ 等 T-5'}</span>`}
        </div>
      </div></li>`;
  }).join('') || '<li class="empty">冇符合條件嘅賽事</li>';
  $$('#fixtures .fx').forEach((el) => {
    el.onclick = () => { SEL = el.dataset.id; renderList(); renderDetail(SEL); };
    el.onkeydown = (e) => { if (e.key === 'Enter' || e.key === ' ') { e.preventDefault(); el.onclick(); } };
  });
}

/* ══════════════════════ 賽事詳情 ══════════════════════ */
function renderDetail(id) {
  const m = LIST.find((x) => x.match_id === id);
  const D = $('#detail');
  if (!m) { D.innerHTML = '<div class="empty">請於左方揀一場賽事</div>'; return; }
  const mm = minsLeft(m.kickoff_hkt), st = stageOf(mm);
  let h = head(m, mm, st);
  h += oddsCompareCard(m);
  h += verdictCard(m);
  h += driftCard(m);
  h += runsCard(m);
  h += `<div class="grid g2">${stagesCard(m)}${adjCard(m)}</div>`;
  h += `<div class="grid g2">${wdlCard(m)}${ctxCard(m)}</div>`;
  if (m.dist) {
    h += `<div class="grid g2">${matrixCard(m)}${topsCard(m)}</div>`;
    h += `<div class="grid g2">${goalsDistCard(m)}${cornersCard(m)}</div>`;
  }
  h += candCard(m);
  D.innerHTML = h;
}

function head(m, mm, st) {
  const venue = m.venue ? `${esc(m.venue)}${m.venue_city ? ' · ' + esc(m.venue_city) : ''}` : '未提供';
  return `<div class="mhead">
    <div class="mhead-top">
      <span class="fx-tag ${TAG[st]}">${st}</span>
      <span class="mhead-lg">${esc(m.league)}</span>
    </div>
    <div class="mhead-teams">
      <div><div class="mt-name">${esc(m.home)}</div><div class="mt-en">${esc(m.home_en)}</div></div>
      <div class="mt-mid">
        <div class="mt-ko">${hkDay(m.kickoff_hkt)} ${hkClock(m.kickoff_hkt)}</div>
        <div class="mt-cd">${cdText(mm)}${mm >= 0 ? '後開賽' : ''}</div>
      </div>
      <div><div class="mt-name away">${esc(m.away)}</div><div class="mt-en away">${esc(m.away_en)}</div></div>
    </div>
    <div class="mhead-foot">
      <span>場地 <strong>${venue}</strong></span>
      ${m.neutral ? '<span style="color:var(--warn)">中立場</span>' : ''}
      <span>皇冠盤快照 <strong class="num">${m.source_snapshot_at ? hkStamp(m.source_snapshot_at) : '—'}</strong></span>
      <span>皇冠編號 <strong class="num">${esc(m.match_id)}</strong></span>
      ${m.book_odds?.hkjc ? '<span class="dual-badge">同場有 HKJC 盤</span>' : ''}
    </div></div>`;
}

function sourceFor(p) {
  return p && p.code === 'CHL' ? 'HKJC' : '皇冠';
}

function bookCond(p) {
  // 從 label 抽出已翻成選邊視角嘅盤口,抽唔到就退回原始 condition
  const m = String(p.label || '').match(/[（(]\s*(?:馬會|皇冠)盤\s*([^）)]*)[）)]/);
  return m ? m[1].trim() : (p.condition || '—');
}

function shortPick(p) {
  // 剝走盤源尾巴,避免窄格內斷行成一柱
  return String(p.label || '').replace(/[（(]\s*(?:馬會|皇冠)盤[^）)]*[）)]\s*$/, '').trim();
}

function oddsLines(lines, code) {
  if (!lines || !lines.length) return '<span class="dim">未開盤</span>';
  const names = code === 'HDC' ? { H: '主', A: '客' } : { H: '大', L: '細' };
  return lines.map((line) => {
    const prices = Object.entries(line.odds || {}).map(([side, price]) =>
      `<span>${names[side] || esc(side)} <b class="num">${f2(price)}</b></span>`).join('');
    return `<div class="odds-line"><strong>${esc(line.condition || '—')}</strong>${prices}</div>`;
  }).join('');
}

function oddsCompareCard(m) {
  const books = m.book_odds || {};
  if (!books.hkjc) return '';
  const market = (code, label) => `<div class="odds-market">
    <h3>${label}</h3>
    <div class="odds-books">
      <div><span class="book-name crown">皇冠</span>${oddsLines((books.crown || {})[code], code)}</div>
      <div><span class="book-name hkjc">HKJC</span>${oddsLines((books.hkjc || {})[code], code)}</div>
    </div>
  </div>`;
  return `<div class="card odds-compare">
    <h2 class="card-h">同場雙莊盤口 <span class="sub">預測仍以皇冠 HDC / HIL 為主；HKJC 只作並排比較，CHL 繼續用 HKJC</span></h2>
    <div class="odds-market-grid">${market('HDC', '全場讓球')}${market('HIL', '全場入球大小')}</div>
  </div>`;
}

function verdictCard(m) {
  const c = m.conviction;
  const bar = `<div class="conv-wrap">
      <div class="conv-track"><div class="conv-fill ${convClass(c)}" style="width:${Math.min(100, Math.max(0, c))}%"></div>
        <div class="conv-floor" style="left:${DATA.ledger.stats.conf_floor || 58}%"></div></div>
      <div class="conv-scale"><span>0</span><span>門檻 ${DATA.ledger.stats.conf_floor || 58}</span><span>100</span></div>
    </div>`;
  if (!m.pick) {
    const hasT5 = (m.stages || []).some((x) => x.stage === 'T-5');
    const mm = minsLeft(m.kickoff_hkt);
    const badge = hasT5 ? '觀望 · 唔買' : (mm > 0 ? '未到落注時點' : '無落注');
    const why = hasT5
      ? (m.no_bet_reason || '未達投注條件')
      : (mm > 0
        ? `最終投注決定統一喺<b>開賽前 5 分鐘</b>先出。依家係${esc(stageOf(mm))},只做預測記錄。距開賽 ${cdText(mm)}。`
        : '本場冇跑到 T-5,冇落注。');
    return `<div class="card verdict wait">
      <div class="vd-top"><span class="vd-badge wait">${badge}</span>
        <span class="vd-conv ${convClass(c)}">信念 ${f2(c)}</span></div>
      <p class="vd-why">${why}</p>
      ${bar}</div>`;
  }
  const p = m.pick;
  const G = p.confidence_only ? [
    ['市場', p.market], ['投注', shortPick(p)], [`${sourceFor(p)}賠率`, f2(p.odds)],
    ['模型勝率', pc(p.prob)], ['EV 參考', 'PinnAPI 無安全同場，不計 EV'],
    ['落注方式', '皇冠信念注'],
  ] : [
    ['市場', p.market], ['投注', shortPick(p)], [`${sourceFor(p)}賠率`, f2(p.odds)],
    ['我嘅公平價', f2(p.fair)], ['模型勝率', pc(p.prob)],
    ['走水機率', p.push > 1e-6 ? pc(p.push) : '—'],
    ['凱利(原始)', pc(p.kelly_raw)], ['凱利(採用)', pc(p.kelly_used)],
  ];
  if (p.code === 'HDC' && p.condition) G.splice(2, 0, ['皇冠盤口', bookCond(p)]);
  return `<div class="card verdict go">
    <div class="vd-top">
      <span class="vd-badge go">模擬落注</span>
      <span class="vd-main">${esc(p.label)} <b>@${f2(p.odds)}</b></span>
      <span class="vd-stake">${money(p.stake)}</span>
      <span class="vd-conv ${convClass(c)}">信念 ${f2(c)}</span>
    </div>
    <div class="vd-grid">${G.map(([l, v]) =>
      `<div class="par"><div class="par-l">${l}</div><div class="par-v">${esc(v)}</div></div>`).join('')}</div>
    ${bar}
    ${p.code === 'HDC' ? `<p class="vd-note">讓球讀法：上面嘅「讓 / 受讓」係按我揀嘅一邊寫。括弧入面嘅皇冠盤口已經同步翻成<b>我揀嗰邊嘅視角</b>，原始盤口係主隊視角，買客隊會反號。</p>` : ''}
    <p class="vd-note">${p.confidence_only
      ? `PinnAPI 無安全唯一同場時，不虛構 EV 或凱利值；只按即時完整皇冠盤及信念門檻建立 2% 本金模擬注。`
      : `注碼 = min(全凱利 × ${fracTxt()}${mktTxt(m)} × min(1, 信念/75), 單場上限 ${pc(DATA.ledger.stats.single_cap_pct, 0)}) × 本金 ${money(DATA.ledger.bankroll)},再受單日 100% / 在場 35% 組合上限約束。`}</p>
  </div>`;
}

/* ══════════════════════ 三段變化 ══════════════════════ */
const S3 = ['首預', 'T-30', 'T-5'];
const stg = (m, k) => (m.stages || []).find((x) => x.stage === k) || null;
const lean = (x) => {
  if (!x) return null;
  if (x.pick) return x.pick;
  return x.lead && (x.lead.ev || 0) > 0 ? x.lead : null;
};

function dots(m) {
  const done = S3.map((k) => !!stg(m, k));
  const flip = flipCount(m);
  return `<span class="dots" title="首預 / T-30 / T-5 完成情況">${
    S3.map((k, i) => `<i class="dot ${done[i] ? 'on' : ''} ${k === 'T-5' && done[i] ? 'fin' : ''}"></i>`).join('')
  }${flip ? `<b class="dflip" title="三段之間有 ${flip} 次方向轉變">⤳${flip}</b>` : ''}</span>`;
}

function flipCount(m) {
  const seq = S3.map((k) => stg(m, k)).filter(Boolean);
  let n = 0;
  for (let i = 1; i < seq.length; i++) {
    const a = (lean(seq[i - 1]) || {}).label || '無';
    const b = (lean(seq[i]) || {}).label || '無';
    if (a !== b) n++;
  }
  return n;
}

function driftCard(m) {
  const cols = S3.map((k) => stg(m, k));
  if (cols.filter(Boolean).length < 1) return '';
  const cell = (x, get, fmt) => {
    if (!x) return `<td class="na">—</td>`;
    const v = get(x);
    return `<td>${v == null ? '—' : fmt(v)}</td>`;
  };
  // 只有信念用好壞配色;其餘指標嘅升跌係方向,唔係好壞,用中性色
  const dcell = (i, get, fmt, judge) => {
    const a = cols[i - 1], b = cols[i];
    if (!a || !b) return `<td class="dcell na"></td>`;
    const va = get(a), vb = get(b);
    if (va == null || vb == null) return `<td class="dcell na"></td>`;
    const d = vb - va;
    if (Math.abs(d) < 5e-3) return `<td class="dcell dz">持平</td>`;
    const cls = judge ? (d > 0 ? 'dup' : 'ddn') : 'dnu';
    return `<td class="dcell ${cls}">${d > 0 ? '▲' : '▼'} ${fmt(Math.abs(d))}</td>`;
  };

  const rows = [
    ['信念', '信念', (x) => x.conviction, (v) => Number(v).toFixed(1), true],
    ['我終值 總入球', '終·入球', (x) => (x.final || {}).total, (v) => Number(v).toFixed(2), false],
    ['我終值 主客差', '終·主客差', (x) => (x.final || {}).supremacy, (v) => Number(v).toFixed(2), false],
    ['我終值 角球 μ', '終·角球', (x) => (x.final || {}).mu, (v) => Number(v).toFixed(2), false],
    ['銳利盤 總入球', '銳·入球', (x) => (x.now || {}).total, (v) => Number(v).toFixed(2), false],
    ['銳利盤 主客差', '銳·主客差', (x) => (x.now || {}).supremacy, (v) => Number(v).toFixed(2), false],
  ];

  const concl = S3.map((k, i) => {
    const x = cols[i];
    if (!x) return `<td class="na">未做</td>`;
    const L = lean(x);
    return `<td class="cc"><span class="vbadge ${VD_CLS[x.verdict] || ''}">${x.verdict}</span>
      <span class="cc-l">${L ? esc(L.label) : '無方向'}</span>
      ${L ? `<span class="cc-o">@${f2(L.odds)} · EV ${sg((L.ev || 0) * 100, 1)}%</span>` : ''}</td>`;
  });
  const cflip = [1, 2].map((i) => {
    const a = cols[i - 1], b = cols[i];
    if (!a || !b) return `<td class="dcell na"></td>`;
    const la = (lean(a) || {}).label || '無', lb = (lean(b) || {}).label || '無';
    return la === lb ? `<td class="dcell dz">一致</td>` : `<td class="dcell dflipc">轉軚</td>`;
  });

  const spark = convSpark(cols);
  const n = flipCount(m);

  return `<div class="card drift">
    <h2 class="card-h">三段變化 <span class="sub">首預 → T-30 → T-5 · 睇下我改咗啲乜</span></h2>
    <div class="tbl-wrap"><table class="t drifty">
      <tr><th>指標</th><th>首預</th><th class="dh">Δ</th><th>T-30</th><th class="dh">Δ</th><th>T-5</th></tr>
      <tr class="crow"><td class="lbl">結論</td>${concl[0]}${cflip[0]}${concl[1]}${cflip[1]}${concl[2]}</tr>
      ${rows.map(([l, sh, g, f, j]) => `<tr${j ? ' class="krow"' : ''}>
        <td class="lbl"><span class="lg">${l}</span><span class="ls">${sh}</span></td>
        ${cell(cols[0], g, f)}${dcell(1, g, f, j)}
        ${cell(cols[1], g, f)}${dcell(2, g, f, j)}
        ${cell(cols[2], g, f)}</tr>`).join('')}
    </table></div>
    ${spark}
    <div class="drift-sum">${n === 0
      ? `<b class="same">三段方向一致</b> — 由頭到尾冇改過睇法,信念變化見上表。`
      : `<b class="flip">三段之間轉軚 ${n} 次</b> — 新資訊(陣容、傷患、賠率移動、天氣)令我改咗睇法,細節見下面「三次預測」。`}</div>
  </div>`;
}

function convSpark(cols) {
  const pts = cols.map((x, i) => (x ? { i, c: x.conviction } : null)).filter(Boolean);
  if (pts.length < 2) return '';
  const W = 520, Hh = 104, PL = 34, PR = 14, PT = 20, PB = 24;
  const floor = (DATA.ledger.stats.conf_floor) || 58;
  const vals = pts.map((p) => p.c).concat([floor]);
  const lo = Math.min(...vals) - 4, hi = Math.max(...vals) + 4;
  const X = (i) => PL + (i / 2) * (W - PL - PR);
  const Y = (v) => PT + (1 - (v - lo) / (hi - lo)) * (Hh - PT - PB);
  const d = pts.map((p, k) => `${k ? 'L' : 'M'}${X(p.i).toFixed(1)},${Y(p.c).toFixed(1)}`).join(' ');
  return `<div class="spark-lbl">信念走勢 · 虛線係落注門檻 ${floor}</div>
  <svg class="spark" viewBox="0 0 ${W} ${Hh}" role="img" aria-label="信念走勢">
    <line class="sp-floor" x1="${PL}" x2="${W - PR}" y1="${Y(floor).toFixed(1)}" y2="${Y(floor).toFixed(1)}"/>
    <text class="sp-ft" x="${PL - 4}" y="${(Y(floor) + 3.5).toFixed(1)}">${floor}</text>
    <path class="sp-line" d="${d}"/>
    ${pts.map((p) => `<circle class="sp-pt ${p.c >= floor ? 'ok' : ''}" cx="${X(p.i).toFixed(1)}" cy="${Y(p.c).toFixed(1)}" r="4.5"/>
      <text class="sp-v" x="${X(p.i).toFixed(1)}" y="${(Y(p.c) - 9).toFixed(1)}">${Number(p.c).toFixed(1)}</text>`).join('')}
    ${S3.map((k, i) => `<text class="sp-x" x="${X(i).toFixed(1)}" y="${Hh - 5}">${k}</text>`).join('')}
  </svg>`;
}

/* ══════════════════════ 三次預測記錄 ══════════════════════ */
const RUN_ORDER = { '首預': 1, 'T-30': 2, 'T-5': 3 };

function runsCard(m) {
  const all = (m.stages || []).slice().sort((a, b) => RUN_ORDER[a.stage] - RUN_ORDER[b.stage]);
  const t5 = all.find((x) => x.stage === 'T-5');
  const prior = all.filter((x) => x.stage !== 'T-5');
  const mm = minsLeft(m.kickoff_hkt);

  const pending = ['首預', 'T-30', 'T-5'].filter((k) => !all.some((x) => x.stage === k));
  const pendHtml = pending.length
    ? `<div class="run-pend">未做:${pending.map((k) => `<span class="pchip">${k}</span>`).join('')}
         <span class="pdim">${mm > 0 ? '距開賽 ' + cdText(mm) + ',到時自動執行' : '賽事已開賽,唔會再補做'}</span></div>`
    : '';

  const body = all.length
    ? `${t5 ? runRow(t5, true, all) : `<div class="run-await">
          <span class="run-await-b">T-5 最終決定</span>
          <span>${mm > 0 ? '仲有 ' + cdText(mm) + ' 到開賽,最終投注決定會喺開賽前 5 分鐘先出' : '本場冇做到 T-5,無落注'}</span>
        </div>`}
       ${prior.length ? `<div class="run-prior-h">之前嘅預測</div>` : ''}
       ${prior.slice().reverse().map((x) => runRow(x, false, all)).join('')}`
    : `<div class="empty2">本場暫時未有任何預測記錄</div>`;

  return `<div class="card runs">
    <h2 class="card-h">三次預測 <span class="sub">首預 → T-30 → T-5 · 只有 T-5 會落注</span></h2>
    ${body}${pendHtml}</div>`;
}

function runRow(x, isFinal, all) {
  const p = x.pick, ld = x.lead, v = x.verdict || '—';
  const forecasts = x.market_predictions || [];
  const info = x.info || {};
  const mv = x.movement || {};
  const chip = (val, u) => val == null ? '—' :
    `<b class="mv ${Math.abs(val) >= 0.15 ? 'big' : Math.abs(val) >= 0.06 ? 'mid' : 'sm'}">${sg(val)}${u || ''}</b>`;

  // 同上一段比較
  const idx = all.indexOf(x);
  const prev = idx > 0 ? all[idx - 1] : null;
  let delta = '';
  if (prev) {
    const pl = (x2) => {
      const q = x2.pick || (x2.lead && (x2.lead.ev || 0) > 0 ? x2.lead : null);
      return q ? q.label : ((x2.market_predictions || []).map((r) => r.label).join(' · ') || '無方向');
    };
    const a = pl(prev), b = pl(x);
    const dc = (x.conviction ?? 0) - (prev.conviction ?? 0);
    delta = `<div class="run-delta">對比 ${prev.stage}:${a === b
      ? `<b class="same">方向一致</b>` : `<b class="flip">由「${esc(a)}」轉為「${esc(b)}」</b>`}
      · 信念 ${sg(dc, 1)}</div>`;
  }

  const soft = ld && (ld.ev || 0) > 0;
  const main = p
    ? `<span class="run-pick">${esc(p.label)} <b>@${f2(p.odds)}</b></span>
       ${isFinal && p.stake ? `<span class="run-stake">${money(p.stake)}</span>` : ''}
       <span class="run-num">勝率 ${pc(p.prob)} · EV ${sg(p.ev * 100, 2)}%</span>`
    : soft
      ? `<span class="run-pick dimp">${esc(ld.label)}</span>
         <span class="run-num">勝率 ${pc(ld.prob)} · EV ${sg((ld.ev || 0) * 100, 2)}%</span>`
      : forecasts.length
        ? `<span class="run-pick dimp">${forecasts.map((r) => esc(r.label)).join(' · ')}</span>
           <span class="run-num">${forecasts.map((r) => `預測概率 ${pc(r.probability, 1)}`).join(' · ')} · 未有 Pinnacle 同路盤，未計 EV</span>`
      : `<span class="run-pick dimp">無明顯方向</span>
         ${ld ? `<span class="run-num">最佳候選 ${esc(ld.label)} · EV ${sg((ld.ev || 0) * 100, 2)}%,全部負值</span>` : ''}`;

  const facts = [];
  if (x.final) facts.push(`我終值 總入球 ${f2(x.final.total)} · 主客差 ${sg(x.final.supremacy)} · 角球 ${f2(x.final.mu)}`);
  if (mv.d_total != null) facts.push(`初盤→現價 入球 ${chip(mv.d_total, '球')} · 主客差 ${chip(mv.d_sup, '球')} · 角球 ${chip(mv.d_corners)}`);
  if (info.temp != null) facts.push(`天氣 ${esc(info.desc || '')} ${f2(info.temp)}°C`);
  facts.push(info.news ? `<b class="ok">有陣容 / 傷患資訊</b>` : `<b class="dim">未有陣容 / 傷患資訊</b>`);
  if (info.hk_lines != null) facts.push(`皇冠 ${info.hk_lines} 條線` +
    (info.hk_moved === true ? ` · <b class="amber">有變動</b>(最大 ${f2(info.hk_max_move_pct)}%)`
      : info.hk_moved === false ? ' · 無變動' : ''));

  return `<div class="run ${isFinal ? 'is-final' : ''}">
    <div class="run-l">
      <span class="run-tag ${TAG[x.stage] || ''}">${x.stage}</span>
      <span class="run-ts">${x.ts ? hkStamp(x.ts) : '—'}</span>
      <span class="run-min">${x.mins_to_ko != null ? '開賽前 ' + cdText(x.mins_to_ko) : ''}</span>
    </div>
    <div class="run-r">
      <div class="run-top">
        <span class="vbadge ${VD_CLS[v] || ''}">${v}</span>${main}
        <span class="run-conv ${convClass(x.conviction)}">信念 ${f2(x.conviction)}</span>
      </div>
      ${x.no_bet_reason ? `<div class="run-why">${esc(x.no_bet_reason)}</div>` : ''}
      ${delta}
      <div class="run-facts">${facts.map((t) => `<span>${t}</span>`).join('')}</div>
      <div class="run-desc">${STAGE_DESC[x.stage] || ''}</div>
    </div>
  </div>`;
}

function stagesCard(m) {
  const rows = [
    ['初盤', m.open, '莊家最初嘅估計'],
    ['現價', m.now, '銳利盤現時共識'],
    ['我終值', m.final, '加入我嘅調整之後'],
  ].filter((r) => r[1]);
  const mv = m.movement || {};
  const chip = (v, unit) => v == null ? '' :
    `<span class="mv ${Math.abs(v) >= 0.15 ? 'big' : Math.abs(v) >= 0.06 ? 'mid' : 'sm'}">${sg(v)}${unit || ''}</span>`;
  return `<div class="card"><h2 class="card-h">三段推演 <span class="sub">初盤 → 現價 → 我嘅判斷</span></h2>
    <div class="tbl-wrap"><table class="t stages">
      <tr><th>階段</th><th>總入球</th><th>主客差</th><th>角球 μ</th><th>λ 主</th><th>λ 客</th></tr>
      ${rows.map(([l, d, tip], i) => `<tr class="${i === 2 ? 'hi' : ''}" title="${esc(tip)}">
        <td class="lbl">${l}</td><td>${f2(d.total)}</td><td>${sg(d.supremacy)}</td>
        <td>${f2(d.mu)}</td><td>${f3(d.lh)}</td><td>${f3(d.la)}</td></tr>`).join('')}
    </table></div>
    <div class="mv-row">
      <span>初盤→現價 總入球 ${chip(mv.d_total, '球')} · 主客差 ${chip(mv.d_sup, '球')} · 角球 ${chip(mv.d_corners)}</span>
    </div>
    <div class="mv-row sub2">
      <span>皇冠盤自我上次快照 ${m.crown_moved_since_last === null || m.crown_moved_since_last === undefined
        ? '<b class="dim">未有對比基準(首次記錄)</b>'
        : m.crown_moved_since_last
          ? `<b class="amber">有變動</b> · ${m.crown_n_lines_moved} 條線,最大 ${f2(m.crown_max_move_pct)}%`
          : '<b class="dim">無變動</b>'} · 共 ${m.n_crown_lines} 條線</span>
    </div>
    <p class="mx-note">皇冠盤口變動以皇冠獨立狀態快照逐輪比對，唔會讀寫 HKJC 足破嘅快照或帳本。</p>
  </div>`;
}

function adjCard(m) {
  const A = m.adjustments || [];
  const mu = m.mults || {};
  if (!A.length) return `<div class="card"><h2 class="card-h">調整層</h2><div class="empty2">本場冇任何調整</div></div>`;
  return `<div class="card"><h2 class="card-h">調整層 <span class="sub">我相對市場嘅獨立判斷</span></h2>
    <div class="adjs">${A.map((a) => {
      const bits = [];
      if (a.goals && Math.abs(a.goals - 1) > 1e-9) bits.push(`入球 ×${f3(a.goals)}`);
      if (a.corners && Math.abs(a.corners - 1) > 1e-9) bits.push(`角球 ×${f3(a.corners)}`);
      if (a.supremacy) bits.push(`主客差 ${sg(a.supremacy)}`);
      if (a.confidence) bits.push(`信念 ${sg(a.confidence, 0)}`);
      const dir = (a.goals > 1.0001 || a.supremacy > 0) ? 'up' : (a.goals < 0.9999 || a.supremacy < 0) ? 'dn' : 'nu';
      return `<div class="adj ${dir}">
        <div class="adj-h"><span class="adj-tag">${esc(a.tag)}</span>
          <span class="adj-eff">${bits.length ? bits.map((b) => `<em>${esc(b)}</em>`).join('') : '<em class="nu">不調整</em>'}</span></div>
        <div class="adj-b">${esc(a.reason)}</div></div>`;
    }).join('')}</div>
    <div class="adj-sum">合計:入球 ×${f3(mu.goals_mult)} · 角球 ×${f3((mu.corners_direct_mult || 1) * (mu.corners_elasticity || 1))}
      <span class="dim">(其中彈性 ${f3(mu.corners_elasticity)})</span> · 主客差 ${sg(mu.sup_shift)}</div>
  </div>`;
}

function wdlCard(m) {
  const o = m.outcome || {};
  const S = [['h', o.home], ['d', o.draw], ['a', o.away]];
  return `<div class="card"><h2 class="card-h">賽果機率 <span class="sub">用我終值計 · 全場 90 分鐘</span></h2>
    <div class="wdl">
      <div class="wdl-bar">${S.map(([c, p]) =>
        `<div class="wdl-seg ${c}" style="width:${((p || 0) * 100).toFixed(2)}%">${p > .1 ? pc(p, 0) : ''}</div>`).join('')}</div>
      <div class="wdl-key"><span>主勝 <b>${pc(o.home)}</b></span><span>和 <b>${pc(o.draw)}</b></span><span>客勝 <b>${pc(o.away)}</b></span></div>
      <div class="wdl-key" style="color:var(--ink-4)"><span>${esc(m.home)}</span><span>${esc(m.away)}</span></div>
    </div></div>`;
}

function ctxCard(m) {
  const w = m.weather, f = m.fatigue || {}, ts = m.team_strength || {};
  const F = [];
  if (w) F.push(['ok', '天氣', `${w.desc || ''} 濕度 ${w.humidity}% · 風 ${f2(w.wind_kmh)}km/h(陣風 ${f2(w.gust_kmh)}) · 雨 ${f2(w.precip_mm_h)}mm/h`, f2(w.temp_c) + '°C']);
  else F.push(['bad', '天氣', '搵唔到場地座標', '不可用']);
  const rd = (x) => x && x.rest_days != null ? `${f2(x.rest_days)} 日` : '—';
  if (f.home || f.away) {
    F.push([(f.home?.rest_days ?? 9) < 3.2 || (f.away?.rest_days ?? 9) < 3.2 ? 'warn' : 'ok',
      '休息日 主 / 客',
      `上仗 ${f.home?.prev_date || '—'} / ${f.away?.prev_date || '—'} · 近 14 日 ${f.home?.games_14d ?? '—'} / ${f.away?.games_14d ?? '—'} 場`,
      `${rd(f.home)} / ${rd(f.away)}`]);
  } else F.push(['bad', '休息日', '該聯賽賽果數據不可用', '不可用']);
  if (ts.available) {
    F.push(['ok', '獨立近況實力',
      `近況 PPG 主 ${f2(ts.home?.ppg)} / 客 ${f2(ts.away?.ppg)} · 得失球差主 ${sg(ts.home?.gd_pg)} / 客 ${sg(ts.away?.gd_pg)}`,
      `已納入 · 可靠度 ${pc(ts.reliability, 0)}`]);
  } else {
    F.push(['bad', '獨立近況實力',
      ts.reason || '未有安全標準賽事對映，唔會硬配隊名',
      '未覆蓋']);
  }
  const news = (m.adjustments || []).filter((a) => !['大盤被推動', '讓球盤被推動', '大盤平穩', '角球盤移動', '氣溫偏高', '氣溫偏低', '氣溫中性', '其他天氣(不調整)', '休息日', '中立場', '獨立實力'].includes(a.tag));
  F.push([news.length ? 'ok' : 'bad', '陣容 / 傷患研究',
    news.length ? `已逐場上網搜尋,套用 ${news.length} 項調整` : '本場未做人手研究 — 資料源冇陣容同傷兵',
    news.length ? '已納入' : '未覆蓋']);
  F.push(['bad', '角球獨立輸入', '任何資料源都冇角球歷史統計,角球 100% 由銳利盤反推', '無']);
  return `<div class="card"><h2 class="card-h">情境輸入 <span class="sub">調整層嘅原始資料</span></h2>
    <div class="flags">${F.map(([c, l, s, v]) =>
      `<div class="flag ${c}"><div class="flag-l">${esc(l)}${s ? `<small>${esc(s)}</small>` : ''}</div>
        <div class="flag-v">${esc(v)}</div></div>`).join('')}</div></div>`;
}

function matrixCard(m) {
  const M = m.dist.matrix, max = Math.max(...M.flat());
  const tops = new Set(m.dist.top_scores.slice(0, 4).map((s) => s.s));
  let rows = '';
  for (let i = 0; i < M.length; i++) {
    rows += `<tr><th>${i}</th>` + M[i].map((p, j) =>
      `<td class="${tops.has(i + '-' + j) ? 'top' : ''}" style="background:${heat(p, max)}" title="${i}-${j} · ${pc(p, 2)}">${(p * 100).toFixed(1)}</td>`
    ).join('') + '</tr>';
  }
  return `<div class="card"><h2 class="card-h">比分機率矩陣 <span class="sub">單位 %,豎=主隊,橫=客隊</span></h2>
    <div class="mx-wrap"><table class="mx">
      <tr><th class="cor">主↓客→</th>${M[0].map((_, j) => `<th>${j}</th>`).join('')}</tr>${rows}</table></div>
    <p class="mx-note">金框 = 四個最可能比分。主隊為 ${esc(m.home)}。</p></div>`;
}

function topsCard(m) {
  const T = m.dist.top_scores, max = T[0].p;
  return `<div class="card"><h2 class="card-h">最可能比分 <span class="sub">前 8 位</span></h2>
    <div class="tops">${T.map((s) => `<div class="top-row">
      <span class="top-sc">${s.s.replace('-', ' – ')}</span>
      <span class="top-track"><span class="top-fill" style="width:${(s.p / max * 100).toFixed(1)}%"></span></span>
      <span class="top-p">${pc(s.p)}</span></div>`).join('')}</div></div>`;
}

function goalsDistCard(m) {
  const d = m.dist.goals_dist, max = Math.max(...d);
  return `<div class="card"><h2 class="card-h">總入球分佈 <span class="sub">期望 ${f2(m.final.total)} 球</span></h2>
    <div class="hist">${d.map((p, k) => `<div class="hb">
      <span class="hb-p">${(p * 100).toFixed(0)}</span>
      <span class="hb-fill" style="height:${(p / max * 118).toFixed(0)}px"></span>
      <span class="hb-k">${k === d.length - 1 ? k + '+' : k}</span></div>`).join('')}</div></div>`;
}

function cornersCard(m) {
  const d = m.dist.corners_dist;
  if (!d) return `<div class="card"><h2 class="card-h">總角球分佈</h2>
    <div class="flag bad"><div class="flag-l">銳利盤未開角球市場<small>無角球盤即無任何角球輸入。</small></div>
    <div class="flag-v">不可用</div></div></div>`;
  const max = Math.max(...d);
  return `<div class="card"><h2 class="card-h">總角球分佈 <span class="sub">負二項 μ=${f2(m.final.mu)} φ=${f2(m.dist.phi)}</span></h2>
    <div class="hist">${d.map((p, k) => `<div class="hb">
      <span class="hb-p">${p > .02 ? (p * 100).toFixed(0) : ''}</span>
      <span class="hb-fill cn" style="height:${(p / max * 118).toFixed(0)}px"></span>
      <span class="hb-k">${k % 4 === 0 ? (k === d.length - 1 ? k + '+' : k) : ''}</span></div>`).join('')}</div></div>`;
}

function candCard(m) {
  const C = m.candidates || [];
  const pk = m.pick;
  const isPick = (c) => pk && c.code === pk.code && c.condition === pk.condition && c.side === pk.side;
  return `<div class="card"><h2 class="card-h">全部候選盤口 <span class="sub">按 EV 排序 · EV = 模型勝率 × 賠率 − 輸率</span></h2>
    <div class="tbl-wrap"><table class="t">
      <tr><th>市場</th><th>投注</th><th>莊家</th><th>賠率</th><th>公平價</th><th>勝率</th><th>走水</th><th>EV</th><th>凱利</th><th></th></tr>
      ${C.map((c) => `<tr class="${isPick(c) ? 'pickrow' : ''}">
        <td class="lbl">${esc(c.market)}</td><td>${esc(c.label)}</td>
        <td><span class="minitag">${sourceFor(c)}</span></td><td>${f2(c.odds)}</td><td class="dim">${f2(c.fair)}</td>
        <td>${pc(c.prob)}</td><td class="${c.push > 1e-6 ? 't-push' : 't-dim'}">${c.push > 1e-6 ? pc(c.push) : '—'}</td>
        <td class="${c.ev > 0 ? 'ev-p' : 'ev-n'}">${sg(c.ev * 100, 2)}%</td>
        <td>${c.kelly_raw > 0 ? pc(c.kelly_raw) : '—'}</td>
        <td>${c.is_main ? '<span class="minitag">主線</span>' : ''}${isPick(c) ? '<span class="minitag go">選中</span>' : ''}</td>
      </tr>`).join('')}
    </table></div>
    <p class="mx-note">主線 = 該資料源市場嘅主打盤口，流動性較好，排序時有 15% 加權。HDC / HIL 用皇冠，CHL 用嚴格對齊嘅 HKJC。</p></div>`;
}

/* ══════════════════════ 模擬倉 ══════════════════════ */
const ST_LBL = { PENDING: '待決', SETTLED: '已結算', VOIDED: '已撤回' };
const ACT_ICO = { '首次推介': '＋', '加注': '↑', '改盤口': '⇄', '維持': '=', '轉觀望': '✕', '結算': '⚑' };
const RES_LBL = {
  'Won': '全贏', 'Half Won': '半贏', 'Refunded': '走水退本',
  'Half Lost': '半輸', 'Lost': '全輸',
};
const RES_CLS = {
  'Won': 'r-w', 'Half Won': 'r-hw', 'Refunded': 'r-p',
  'Half Lost': 'r-hl', 'Lost': 'r-l',
};

/* ── 注碼階段 ── */
function stk() { return (DATA.ledger.stats || {}).staking || null; }

function fracTxt() {
  const k = stk(); if (!k) return '1/3';
  const f = k.fraction;
  if (Math.abs(f - 1 / 3) < 0.01) return '1/3';
  if (Math.abs(f - 0.5) < 0.01) return '1/2';
  if (Math.abs(f - 2 / 3) < 0.01) return '2/3';
  return f.toFixed(2);
}

function mktTxt(m) {
  const k = stk(); if (!k || !k.market_mult) return '';
  const code = ((m.pick || {}).code) || null;
  const mm = code ? k.market_mult[code] : null;
  return mm && mm !== 1 ? ` × ${mm}(${code === 'CHL' ? '角球折讓' : '市場折讓'})` : '';
}

function stkLabel(s) {
  const k = (s || {}).staking; if (!k) return '分數凱利';
  return `${fracTxt()} 凱利`;
}

function stakeStageCard() {
  const k = stk(); if (!k) return '';
  const LAD = [
    [1, '1/3 凱利 · 上限 4%', '0–80 注'],
    [2, '1/2 凱利 · 上限 5%', '≥80 注 且 校準斜率 > 0.6'],
    [3, '2/3 凱利 · 上限 6%', '≥200 注 且 實際 ROI > 預測 ROI 的 60%'],
  ];
  const sl = k.slope;
  const slTxt = sl == null ? '樣本未足' : sl.toFixed(2);
  const slCls = sl == null ? '' : (sl > 0.6 ? 'good' : 'bad');
  const need2 = Math.max(0, 80 - (k.n_settled || 0));

  const bk = k.buckets || [];
  let cal = `<p class="mx-note">校準曲線需要至少 3 個機率分桶。而家已結算 ${k.n_settled || 0} 注,${need2 > 0 ? `仲差 ${need2} 注到階段二嘅樣本門檻` : '樣本已達門檻'}。</p>`;
  if (bk.length >= 2) {
    cal = `<div class="tbl-wrap"><table class="calib">
      <thead><tr><th>模型機率區間</th><th>注數</th><th>模型預測</th><th>實際命中</th><th>落差</th></tr></thead>
      <tbody>${bk.map(b => {
        const d = b.actual - b.pred;
        const rng = b.hi > 1 ? `${(b.lo * 100).toFixed(0)}% 以上`
          : `${(b.lo * 100).toFixed(0)}–${(b.hi * 100).toFixed(0)}%`;
        return `<tr><td class="lbl">${rng}</td>
          <td class="mono">${b.n}</td><td class="mono">${pc(b.pred, 1)}</td>
          <td class="mono">${pc(b.actual, 1)}</td>
          <td class="mono ${Math.abs(d) < 0.02 ? 'dz' : d > 0 ? 'dup' : 'ddn'}">${d >= 0 ? '+' : ''}${pc(d, 1)}</td></tr>`;
      }).join('')}</tbody></table></div>`;
  }

  return `<div class="card stk">
    <h2 class="card-h">注碼階段 <span class="card-sub">凱利分數由已結算樣本嘅校準表現決定</span></h2>
    <div class="stk-now">
      <span class="stk-badge">${k.label}</span>
      <span class="stk-m"><b>${fracTxt()}</b> 凱利</span>
      <span class="stk-m">單場上限 <b>${pc(k.cap, 0)}</b></span>
      <span class="stk-m">角球 <b>×${(k.market_mult || {}).CHL != null ? k.market_mult.CHL : 1}</b></span>
      <span class="stk-m">校準斜率 <b class="${slCls}">${slTxt}</b></span>
      <span class="stk-m">已結算 <b>${k.n_settled || 0}</b> 注</span>
      ${k.demoted ? '<span class="stk-dem">已降級 — 實際命中率低於模型預測 8 個百分點以上</span>' : ''}
    </div>
    <div class="ladder">${LAD.map(([lv, w, cond]) => `
      <div class="lad ${lv === k.level ? 'on' : lv < k.level ? 'past' : ''}">
        <span class="lad-n">${lv}</span>
        <span class="lad-w">${w}</span>
        <span class="lad-c">${cond}</span>
      </div>`).join('')}</div>
    <h3 class="sub-h">校準曲線</h3>
    ${cal}
    <p class="mx-note">校準斜率 = 把注單按模型機率分桶,實際命中率對模型機率做加權迴歸嘅斜率。1.0 = 完美校準;0.5 = 實際 edge 只有模型自稱嘅一半。呢個係唯一能夠證明應唔應該加大注碼嘅證據。角球冇任何獨立資料源(μ 100% 由盤口反推),所以一律再乘 0.5。</p>
  </div>`;
}

function renderLedger() {
  const s = LED.stats || {}, bets = LED.bets || [];
  const V = $('#viewLedger');
  const K = [
    ['本金', money(LED.bankroll), ''],
    ['模擬注碼', money(s.open_stake), 'amber'],
    ['佔本金', pc(s.open_pct, 1), (s.open_pct || 0) > 0.3 ? 'bad' : 'good'],
    ['待決', s.n_pending, ''],
    ['已撤回', s.n_voided, ''],
    ['已結算', s.n_settled, ''],
    ['累計盈虧', s.n_settled ? money(s.pnl) : '—', (s.pnl || 0) >= 0 ? 'good' : 'bad'],
    ['ROI', s.roi == null ? '—' : pc(s.roi, 2), (s.roi || 0) >= 0 ? 'good' : 'bad'],
    ['命中率', s.n_decided ? `${pc(s.hit_rate, 1)} (${s.hits}/${s.n_decided})` : '—',
      s.n_decided ? ((s.hit_rate || 0) >= 0.5 ? 'good' : 'bad') : ''],
    ['戶口結餘', money(s.equity != null ? s.equity : LED.bankroll),
      (s.pnl || 0) >= 0 ? 'good' : 'bad'],
  ];

  const capBar = (lbl, used, cap) => {
    const t = cap ? Math.min(1, used / cap) : 0;
    return `<div class="cap">
      <div class="cap-h"><span>${lbl}</span><span class="mono">${money(used)} / ${money(cap)}</span></div>
      <div class="cap-track"><div class="cap-fill ${t > .9 ? 'bad' : t > .7 ? 'warn' : ''}" style="width:${(t * 100).toFixed(1)}%"></div></div>
    </div>`;
  };

  let h = `<div class="ledger-head">
    <div class="ledger-title-row">
      <h1 class="pg-h">模擬倉 <span class="sub">${stkLabel(s)} · 單場上限 ${pc(s.single_cap_pct, 0)} · 信念門檻 ${s.conf_floor} · 最低賠率 ${f2(DATA.signal_policy?.minimum_odds || 1.5)}</span></h1>
      <button class="settle-btn" id="settleNow" data-testid="button-settle-simulation" type="button" ${SETTLING || !API_BASE ? 'disabled' : ''}>
        <svg viewBox="0 0 24 24" aria-hidden="true"><path d="M20 7v5h-5M4 17v-5h5M6.1 9a7 7 0 0 1 11.2-2.3L20 9M4 15l2.7 2.3A7 7 0 0 0 17.9 15"/></svg>
        <span>${SETTLING ? '結算中…' : '立即結算'}</span>
      </button>
    </div>
    <p class="settle-status ${SETTLE_BAD ? 'bad' : SETTLE_MESSAGE ? 'good' : ''}" id="settleStatus" data-testid="status-settlement" aria-live="polite">
      ${esc(SETTLE_MESSAGE || (API_BASE
        ? '手動操作，只會結算已完場並有正式賽果嘅皇冠模擬注。'
        : '結算後端未連接，請重新開啟已部署嘅皇冠面板。'))}
    </p>
    <div class="kpis wide">${K.map(([l, v, c]) =>
      `<div class="kpi"><span class="kpi-lbl">${l}</span><span class="kpi-val ${c}">${v}</span></div>`).join('')}</div>
  </div>`;

  h += `<div class="grid g2">
    <div class="card"><h2 class="card-h">組合上限使用率</h2>
      ${capBar('在場總曝險(上限 35%)', s.open_stake, s.open_cap)}
      ${capBar('今日曝險(上限 100%)', dayStake(bets), s.daily_cap)}
      <p class="mx-note">凱利只在機率完全校準時才最優。本模型基礎層係盤口翻譯器,EV 系統性高估風險高,所以用分數凱利起步,再加單場同組合上限控制單日爆倉風險。</p>
    </div>
    ${logCard()}
  </div>`;

  h += stakeStageCard();
  h += notifyCard();

  if (!bets.length) {
    h += `<div class="card"><div class="empty2">仲未有任何推介記錄</div></div>`;
    V.innerHTML = h; bindSettlementButton(); return;
  }

  if (s.n_settled) h += `<div class="grid g2">${equityCard(s)}${resultCard(s)}</div>`;
  if (s.n_settled) h += marketCard(s);

  h += `<div class="card"><h2 class="card-h">注單 <span class="sub">${bets.length} 筆 · 撳一下睇三階段變化</span></h2>
    <div class="tbl-wrap"><table class="t bets">
      <tr><th></th><th>開賽</th><th>賽事</th><th>市場</th><th>投注</th><th>賠率</th><th>注碼</th>
          <th>勝率</th><th>EV</th><th>信念</th><th>最新</th><th>狀態</th><th>結果</th><th>比分</th><th>盈虧</th></tr>
      ${bets.map((b, i) => betRow(b, i)).join('')}
    </table></div></div>`;

  V.innerHTML = h;
  bindSettlementButton();
  $$('#viewLedger .bets tr.brow').forEach((tr) => {
    tr.onclick = () => {
      const d = $(`#hist-${tr.dataset.i}`);
      d.classList.toggle('open');
      tr.classList.toggle('is-open');
    };
  });
}

function bindSettlementButton() {
  const b = $('#settleNow');
  if (!b) return;
  b.onclick = async () => {
    if (SETTLING || !API_BASE) return;
    if (!window.confirm('只會結算已完場、有正式賽果嘅皇冠模擬注。確認立即檢查賽果並結算？')) return;
    SETTLING = true;
    SETTLE_BAD = false;
    SETTLE_MESSAGE = '正在核對正式賽果及更新模擬倉…';
    renderLedger();
    try {
      const r = await fetch(`${API_BASE}/settle`, {
        method: 'POST',
        headers: {
          'Content-Type': 'application/json',
          'X-Crown-Action': 'settle-simulation',
        },
        body: JSON.stringify({ confirm: 'simulation-only' }),
      });
      const result = await r.json().catch(() => ({}));
      if (!r.ok || !result.ok) throw new Error(result.error || `HTTP ${r.status}`);
      applyData(result.data);
      const settled = result.settled_count || 0;
      const pending = result.pending_count || 0;
      SETTLE_BAD = !result.persisted;
      const syncNote = result.project_submitted === false
        ? ' 專案檔案同步暫緩，但本機模擬倉已保存。'
        : '';
      SETTLE_MESSAGE = settled
        ? `完成：新結算 ${settled} 注${pending ? `，另有 ${pending} 注待正式賽果` : ''}${result.persisted ? '，已保存。' : '；保存失敗，請稍後再試。'}${syncNote}`
        : `檢查完成：未有新注可結算${pending ? `，${pending} 注仍待正式賽果` : ''}。${syncNote}`;
    } catch (e) {
      SETTLE_BAD = true;
      SETTLE_MESSAGE = `結算失敗：${e.message}`;
    } finally {
      SETTLING = false;
      renderLedger();
    }
  };
}

const HIST_MARKET_LABEL = { HDC: '讓球', HIL: '入球大細', CHL: '角球大細' };
const HIST_SETTLEMENT_LABEL = {
  Won: '全贏', 'Half Won': '半贏', Refunded: '走水',
  'Half Lost': '半輸', Lost: '全輸',
};
function historyQuarterLine(raw, signed = true) {
  const q = Math.round(Number(raw) * 4);
  if (!Number.isFinite(q)) return String(raw ?? '—');
  const values = q % 2 === 0
    ? [q / 4]
    : q > 0
      ? [(q - 1) / 4, (q + 1) / 4]
      : [(q + 1) / 4, (q - 1) / 4];
  return values.map((value) => {
    const text = Number.isInteger(value) ? String(value) : String(value);
    return signed && value > 0 ? `+${text}` : text;
  }).join('/');
}
function historyPredictionLabel(r, p) {
  const line = Number(p.line ?? p.condition);
  if (p.code === 'HDC') {
    const team = p.side === 'A' ? r.away : r.home;
    const selectedLine = p.side === 'A' ? -line : line;
    return `${team} ${historyQuarterLine(selectedLine, true)}`;
  }
  if (p.code === 'HIL') return `${p.side === 'H' ? '大' : '細'} ${historyQuarterLine(line, false)} 球`;
  if (p.code === 'CHL') return `${p.side === 'H' ? '大' : '細'} ${historyQuarterLine(line, false)} 角球`;
  return p.label || `${p.condition} ${p.side}`;
}
function historyCornerResult(r, p) {
  if (p.code !== 'CHL') return '';
  const raw = (r.result_detail || {}).corners_total ?? r.corners_total ?? r.corners;
  const total = Number(raw);
  if (!Number.isFinite(total) || total < 0) return '';
  return `<span class="market-actual">賽果 <b>${Math.trunc(total)}</b> 角</span>`;
}
function historyMarkets(r) {
  const grades = Object.fromEntries((r.market_grades || []).map((g) => [g.code, g]));
  return (r.market_predictions || []).map((p) => {
    const g = grades[p.code] || {};
    const settlement = HIST_SETTLEMENT_LABEL[g.settlement] || g.settlement || '';
    const badge = g.grade_status === 'GRADED'
      ? g.hit === true
        ? `<span class="market-hit hit"><b>命中</b> · ${esc(settlement)}</span>`
        : g.hit === false
          ? `<span class="market-hit miss"><b>落空</b> · ${esc(settlement)}</span>`
          : `<span class="market-hit push"><b>走水</b></span>`
      : g.reason === 'corners_result_missing'
        ? '<span class="market-hit pending">角球賽果同步中</span>'
        : '<span class="market-hit pending">待賽果</span>';
    const actual = historyCornerResult(r, p);
    const result = actual
      ? `<span class="history-market-outcome">${badge}${actual}</span>`
      : badge;
    return `<div class="history-market-row">
      <span class="history-market-pick"><b>${HIST_MARKET_LABEL[p.code] || esc(p.code)}</b>
        ${esc(historyPredictionLabel(r, p))}
        <span class="cell-sub">${pc(p.probability, 1)}</span>
      </span>
      ${result}
    </div>`;
  }).join('') || '<span class="dim">未有可評分市場</span>';
}

function historyMarketResult(r) {
  const grades = (r.market_grades || []).filter((g) => g.grade_status === 'GRADED');
  const decided = grades.filter((g) => g.hit === true || g.hit === false);
  const hits = decided.filter((g) => g.hit === true).length;
  const pushes = grades.filter((g) => g.hit == null).length;
  const pendingCorners = (r.market_grades || []).filter((g) => g.reason === 'corners_result_missing').length;
  if (!(r.market_predictions || []).length) return '<span class="dim">冇市場預測</span>';
  if (!grades.length) return '<span class="stpill pending">市場待賽果</span>';
  return `<span class="market-total ${decided.length && hits === decided.length ? 'all-hit' : hits ? 'some-hit' : 'none-hit'}">
      市場命中 ${hits}/${decided.length}</span>
    ${pushes ? `<div class="cell-sub">走水 ${pushes} 項</div>` : ''}
    ${pendingCorners ? `<div class="cell-sub">角球待賽果 ${pendingCorners} 項</div>` : ''}`;
}

function historyStageMarketMatrix(stats) {
  const stages = ['首預', 'T-30', 'T-5'];
  const codes = ['HDC', 'HIL', 'CHL'];
  const matrix = stats.by_stage_market || {};
  const cell = (stage, code) => {
    const x = (matrix[stage] || {})[code] || {};
    return x.accuracy == null
      ? `<span class="stage-market-empty">待累積</span><small>${x.graded || 0} 個已評分</small>`
      : `<strong>${pc(x.accuracy, 1)}</strong><small>${x.hits}/${x.decided}</small>`;
  };
  return `<div class="stage-market-block">
    <div class="stage-market-title">分階段市場命中率 <span>每格獨立計算</span></div>
    <table class="stage-market-table" aria-label="首預、T-30及T-5各市場命中率">
      <thead><tr><th>階段</th>${codes.map((code) => `<th>${HIST_MARKET_LABEL[code]}</th>`).join('')}</tr></thead>
      <tbody>${stages.map((stage) => `<tr><th>${stage}</th>${codes.map((code) => `<td>${cell(stage, code)}</td>`).join('')}</tr>`).join('')}</tbody>
    </table>
  </div>`;
}

function renderHistory() {
  const V = $('#viewHistory');
  const payload = DATA.prediction_history || { rows: [], stats: {} };
  const historyTime = (row) => {
    const kickoff = Date.parse(row.kickoff || '');
    const predicted = Date.parse(row.predicted_at || '');
    return [
      Number.isFinite(kickoff) ? kickoff : Number.NEGATIVE_INFINITY,
      Number.isFinite(predicted) ? predicted : Number.NEGATIVE_INFINITY,
    ];
  };
  const rows = [...(payload.rows || [])].sort((left, right) => {
    const a = historyTime(left), b = historyTime(right);
    return b[0] - a[0] || b[1] - a[1];
  });
  const gradedRows = rows.filter((r) => r.result_status === '已核對');
  const pendingRows = rows.filter((r) => r.result_status === '待賽果');
  const excludedRows = rows.filter((r) => r.result_status === '不計');
  const s = payload.stats || {};
  const accuracy = s.accuracy == null ? '待賽果' : pc(s.accuracy, 1);
  const K = [
    ['記錄賽事', s.matches || 0, ''],
    ['階段預測', s.predictions || 0, 'amber'],
    ['已核對', s.graded || 0, ''],
    ['待賽果', s.pending || 0, ''],
    ['命中', s.hits || 0, 'good'],
    ['命中率', accuracy, s.accuracy == null ? '' : s.accuracy >= .5 ? 'good' : 'bad'],
  ];
  const stageSummary = ['首預', 'T-30', 'T-5'].map((stage) => {
    const x = (s.by_stage || {})[stage] || {};
    return `<span class="hist-stage"><b>${stage}</b> ${
      x.accuracy == null ? '待累積' : `${pc(x.accuracy, 1)} (${x.hits}/${x.graded})`
    }</span>`;
  }).join('');
  const marketSummary = ['HDC', 'HIL', 'CHL'].map((code) => {
    const x = (s.by_market || {})[code] || {};
    return `<span class="hist-stage"><b>${HIST_MARKET_LABEL[code]}</b> ${
      x.accuracy == null ? `待累積 (${x.graded || 0})` : `${pc(x.accuracy, 1)} (${x.hits}/${x.decided})`
    }</span>`;
  }).join('');
  const historyRows = (items) => items.map((r) => `<tr>
    <td data-label="開賽" class="mono nowrap">${r.kickoff ? `${hkDay(r.kickoff)} ${hkClock(r.kickoff)}` : '—'}</td>
    <td data-label="賽事">${esc(r.home)} <span class="dim">v</span> ${esc(r.away)}
      <div class="cell-sub">${esc(r.league || '')}</div></td>
    <td data-label="階段"><span class="fx-tag ${TAG[r.stage] || 'tag-wait'}">${esc(r.stage || '—')}</span>
      <div class="cell-sub mono">${r.predicted_at ? hkStamp(r.predicted_at) : '—'}</div></td>
    <td data-label="1X2 輔助"><b class="forecast-pick">${esc(r.forecast || '冇主客和預測')}</b>
      <div class="cell-sub">${r.probability == null ? '正式結果見市場欄' : `最高機率 ${pc(r.probability, 1)}`}${r.likely_score ? ` · 最可能 ${esc(r.likely_score)}` : ''}</div></td>
    <td data-label="各市場預測／結果">${historyMarkets(r)}<div class="market-summary">${historyMarketResult(r)}</div></td>
    <td data-label="信念" class="${convClass(r.conviction)}">${f2(r.conviction)}</td>
    <td data-label="模擬注">${r.simulated_bet
      ? `<span class="stpill pending">有模擬注</span><div class="cell-sub">${esc(r.bet_label || '')}</div>`
      : `<span class="stpill voided">冇落注</span><div class="cell-sub hist-reason">${esc(r.no_bet_reason || '未達條件')}</div>`}</td>
    <td data-label="整場賽果" class="history-result-cell">${r.actual
      ? r.correct == null
        ? `<span class="stpill voided">冇主客和預測</span>
           <div class="hist-result"><b>${esc(r.score || '—')}</b> · ${esc(r.actual)}</div>
           <div class="cell-sub">${esc(String(r.result_source || '').startsWith('hkjc_official') ? '馬會官方賽果' : r.result_source || '已核對賽果')}</div>`
        : `<span class="respill ${r.correct ? 'r-w' : 'r-l'}">主客和${r.correct ? '命中' : '落空'}</span>
         <div class="hist-result"><b>${esc(r.score || '—')}</b> · ${esc(r.actual)}</div>
         <div class="cell-sub">${esc(String(r.result_source || '').startsWith('hkjc_official') ? '馬會官方賽果' : r.result_source || '已核對賽果')}</div>`
      : r.result_status === '不計'
        ? `<span class="stpill voided">不計</span>
           <div class="cell-sub">${esc(r.excluded_reason || '延期／取消／腰斬')}</div>`
        : '<span class="stpill pending">待賽果</span>'}</td>
  </tr>`).join('');
  const historyTable = (items, empty) => `<div class="tbl-wrap"><table class="t history-table">
      <tr><th>開賽</th><th>賽事</th><th>階段</th><th>1X2 輔助</th><th>各市場預測／結果</th><th>信念</th><th>模擬注</th><th>整場賽果</th></tr>
      ${historyRows(items) || `<tr class="history-empty-row"><td colspan="8" class="empty2">${empty}</td></tr>`}
    </table></div>`;

  V.innerHTML = `<div class="ledger-head">
    <h1 class="pg-h">預測紀錄 <span class="sub">有冇落注都照記 · 準確率與模擬倉分開</span></h1>
    <div class="kpis wide">${K.map(([l, v, c]) =>
      `<div class="kpi"><span class="kpi-lbl">${l}</span><span class="kpi-val ${c}">${v}</span></div>`).join('')}</div>
  </div>
  <div class="card history-note">
    <div class="history-summary-label">合併總覽</div>
    <div class="history-stage-summary">${stageSummary}</div>
    <div class="history-stage-summary">${marketSummary}</div>
    ${historyStageMarketMatrix(s)}
    <p class="mx-note">合併市場數字會計首預、T-30、T-5 每個獨立快照；下表則按階段分開。正式學習樣本為當時主線，走水不計入命中率分母；未完場或未取到賽果唔當輸。</p>
  </div>
  <div class="card"><h2 class="card-h">已核對賽果 <span class="sub">${gradedRows.length} 筆 · 命中 ${s.hits || 0}</span></h2>
    ${historyTable(gradedRows, '暫時未有已核對賽果。')}
  </div>
  <div class="card"><h2 class="card-h">待賽果 <span class="sub">${pendingRows.length} 筆</span></h2>
    ${historyTable(pendingRows, '目前冇待核對紀錄。')}
  </div>
  <div class="card"><h2 class="card-h">不計入準確率 <span class="sub">${excludedRows.length} 筆</span></h2>
    ${historyTable(excludedRows, '目前冇延期、取消或腰斬紀錄。')}
  </div>`;
}

function dayStake(bets) {
  const today = new Date().toLocaleDateString('en-CA', { timeZone: 'Asia/Hong_Kong' });
  return bets.filter((b) => b.status === 'PENDING' && String(b.kickoff).slice(0, 10) === today)
             .reduce((a, b) => a + b.stake, 0);
}

function betRow(b, i) {
  const H = b.history || [];
  const chg = H.filter((x) => x.action !== '維持').length;
  const main = `<tr class="brow ${b.status.toLowerCase()}" data-i="${i}">
    <td class="exp">${H.length ? '▸' : ''}</td>
    <td class="mono nowrap">${hkDay(b.kickoff)} ${hkClock(b.kickoff)}</td>
    <td>${esc(b.home)} <span class="dim">v</span> ${esc(b.away)}<div class="cell-sub">${esc(b.league)}</div></td>
    <td class="lbl">${esc(b.market)}</td>
    <td><b>${esc(b.label.replace(b.market, '').trim())}</b></td>
    <td>${f2(b.odds)}</td>
    <td class="stk">${money(b.stake)}</td>
    <td>${pc(b.model_prob)}</td>
    <td class="${b.ev == null ? 'dim' : b.ev > 0 ? 'ev-p' : 'ev-n'}">${b.ev == null ? '無 EV 參考' : `${sg(b.ev * 100, 2)}%`}</td>
    <td class="${convClass(b.conviction)}">${f2(b.conviction)}</td>
    <td>${esc(b.stage)}${chg > 1 ? `<span class="minitag">${chg} 次變動</span>` : ''}</td>
    <td><span class="stpill ${b.status.toLowerCase()}">${ST_LBL[b.status] || b.status}</span></td>
    <td>${b.result ? `<span class="respill ${RES_CLS[b.result] || ''}">${RES_LBL[b.result] || b.result}</span>` : '<span class="dim">—</span>'}</td>
    <td class="mono nowrap">${scoreCell(b)}</td>
    <td class="${(b.pnl || 0) > 0 ? 'ev-p' : (b.pnl || 0) < 0 ? 'ev-n' : 'dim'}">${b.pnl == null ? '—' : money(b.pnl)}</td>
  </tr>`;
  const hist = `<tr class="hrowwrap"><td colspan="14" class="histcell">
    <div class="hist-panel" id="hist-${i}">
      ${b.void_reason ? `<div class="void-note">撤回原因:${esc(b.void_reason)}</div>` : ''}
      <ol class="tl">${H.map((x) => `<li class="tl-i ${x.action === '轉觀望' ? 'x' : x.action === '維持' ? 'keep' : ''}">
        <span class="tl-dot">${ACT_ICO[x.action] || '·'}</span>
        <div class="tl-b">
          <div class="tl-h"><b>${esc(x.action)}</b>
            <span class="fx-tag ${TAG[x.stage] || 'tag-wait'}">${esc(x.stage)}</span>
            <span class="tl-ts mono">${hkStamp(x.ts)}</span></div>
          <div class="tl-d">
            ${x.from ? `<span class="tl-kv">由 <b>${esc(x.from)}</b> → <b>${esc(x.to)}</b></span>` : ''}
            ${x.label && !x.from ? `<span class="tl-kv">${esc(x.label)}</span>` : ''}
            ${x.odds ? `<span class="tl-kv">賠率 <b>${f2(x.odds)}</b></span>` : ''}
            ${x.add ? `<span class="tl-kv">加 <b>${money(x.add)}</b> → ${money(x.stake)}</span>`
                    : x.stake ? `<span class="tl-kv">注碼 <b>${money(x.stake)}</b></span>` : ''}
            ${x.ev != null ? `<span class="tl-kv">EV <b class="${x.ev > 0 ? 'ev-p' : 'ev-n'}">${sg(x.ev * 100, 2)}%</b></span>` : ''}
            ${x.conviction != null ? `<span class="tl-kv">信念 <b class="${convClass(x.conviction)}">${f2(x.conviction)}</b></span>` : ''}
            ${x.reason ? `<span class="tl-kv">${esc(x.reason)}</span>` : ''}
            ${x.result ? `<span class="tl-kv">判定 <b class="${RES_CLS[x.result] || ''}">${RES_LBL[x.result] || x.result}</b></span>` : ''}
            ${x.pnl != null ? `<span class="tl-kv">盈虧 <b class="${x.pnl > 0 ? 'ev-p' : x.pnl < 0 ? 'ev-n' : 'dim'}">${money(x.pnl)}</b></span>` : ''}
            ${x.score ? `<span class="tl-kv">比分 <b class="mono">${esc(x.score.goals)}</b>${x.score.corners ? ` · 角球 <b class="mono">${esc(x.score.corners)}</b>(${x.score.corners_total})` : ''}</span>` : ''}
          </div>
          ${x.final ? `<div class="tl-f mono">當時終值 · 總入球 ${f2(x.final.total)} · 主客差 ${sg(x.final.supremacy)} · 角球 ${f2(x.final.mu)}</div>` : ''}
        </div></li>`).join('')}</ol>
    </div></td></tr>`;
  return main + hist;
}

function scoreCell(b) {
  if (!b.score) return '<span class="dim">—</span>';
  const g = b.score.goals || '—';
  if (b.code === 'CHL') {
    return `${b.score.corners || '—'} <span class="dim">角</span>` +
           (b.score.corners_total != null ? ` <b>${b.score.corners_total}</b>` : '') +
           `<div class="cell-sub">入球 ${g}</div>`;
  }
  return `${g}` + (b.score.goals_total != null ? ` <span class="dim">共</span> <b>${b.score.goals_total}</b>` : '');
}

/* ── 資金曲線 ── */
function equityCard(s) {
  const c = s.curve || [];
  const bank = LED.bankroll || 50000;
  const pts = [{ equity: bank, label: '起始', pnl: 0, ts: null }].concat(c);
  const vals = pts.map((p) => p.equity);
  const lo = Math.min(...vals), hi = Math.max(...vals);
  const pad = Math.max((hi - lo) * 0.18, bank * 0.004);
  const y0 = lo - pad, y1 = hi + pad;
  const W = 560, H = 170, PADL = 8, PADR = 8, PADT = 10, PADB = 8;
  const X = (i) => PADL + (pts.length < 2 ? 0 : i * (W - PADL - PADR) / (pts.length - 1));
  const Y = (v) => PADT + (y1 - v) / (y1 - y0) * (H - PADT - PADB);
  const line = pts.map((p, i) => `${i ? 'L' : 'M'}${X(i).toFixed(1)},${Y(p.equity).toFixed(1)}`).join(' ');
  const area = `${line} L${X(pts.length - 1).toFixed(1)},${H - PADB} L${X(0).toFixed(1)},${H - PADB} Z`;
  const up = (s.pnl || 0) >= 0;
  const base = Y(bank).toFixed(1);
  return `<div class="card"><h2 class="card-h">資金曲線
      <span class="sub">起始 ${money(bank)} → 現時 ${money(s.equity)}</span></h2>
    <svg class="eq" viewBox="0 0 ${W} ${H}" preserveAspectRatio="none" role="img"
         aria-label="模擬倉資金曲線,由 ${money(bank)} 到 ${money(s.equity)}">
      <defs><linearGradient id="eqg" x1="0" y1="0" x2="0" y2="1">
        <stop offset="0%" stop-color="${up ? 'var(--good)' : 'var(--bad)'}" stop-opacity=".28"/>
        <stop offset="100%" stop-color="${up ? 'var(--good)' : 'var(--bad)'}" stop-opacity="0"/>
      </linearGradient></defs>
      <line class="eq-base" x1="0" y1="${base}" x2="${W}" y2="${base}"/>
      <path d="${area}" fill="url(#eqg)"/>
      <path d="${line}" fill="none" stroke="${up ? 'var(--good)' : 'var(--bad)'}" stroke-width="2"/>
      ${pts.map((p, i) => `<circle cx="${X(i).toFixed(1)}" cy="${Y(p.equity).toFixed(1)}" r="${i ? 3 : 2.5}"
        fill="${i === 0 ? 'var(--dim)' : (p.pnl > 0 ? 'var(--good)' : p.pnl < 0 ? 'var(--bad)' : 'var(--dim)')}"/>`).join('')}
    </svg>
    <div class="eq-legend"><span class="dim">虛線 = 起始本金</span>
      <span>已投注 ${money(s.turnover)} · 回報 <b class="${up ? 'ev-p' : 'ev-n'}">${money(s.pnl)}</b></span></div>
    <p class="mx-note">曲線按結算時間順序累加,只計已結算注單;走水退本唔會改變結餘。</p>
  </div>`;
}

/* ── 判定分布 ── */
function resultCard(s) {
  const R = s.res_counts || {};
  const order = ['Won', 'Half Won', 'Refunded', 'Half Lost', 'Lost'];
  const tot = order.reduce((a, k) => a + (R[k] || 0), 0) || 1;
  return `<div class="card"><h2 class="card-h">判定分布
      <span class="sub">${s.n_settled} 注已結算 · 走水不計入命中率</span></h2>
    <div class="resbar">${order.map((k) => (R[k] ? `<span class="rb ${RES_CLS[k]}"
      style="width:${(R[k] / tot * 100).toFixed(1)}%" title="${RES_LBL[k]} ${R[k]}"></span>` : '')).join('')}</div>
    <div class="reslist">${order.map((k) => `<div class="rl">
      <span class="rl-d ${RES_CLS[k]}"></span><span class="rl-n">${RES_LBL[k]}</span>
      <span class="rl-v mono">${R[k] || 0}</span></div>`).join('')}</div>
    <p class="mx-note">半贏 / 半輸來自 0.25 與 0.75 盤(斜線盤),兩條半盤各佔一半注碼獨立結算。</p>
  </div>`;
}

/* ── 分市場表現 ── */
function marketCard(s) {
  const M = s.by_market || {};
  const keys = Object.keys(M);
  if (!keys.length) return '';
  return `<div class="card"><h2 class="card-h">分市場表現 <span class="sub">樣本仍小,只作方向參考</span></h2>
    <div class="tbl-wrap"><table class="t">
      <tr><th>市場</th><th>注數</th><th>投注額</th><th>盈虧</th><th>ROI</th><th>命中率</th></tr>
      ${keys.map((k) => { const m = M[k]; return `<tr>
        <td class="lbl">${esc(k)}</td><td class="mono">${m.n}</td>
        <td class="mono">${money(m.stake)}</td>
        <td class="mono ${m.pnl > 0 ? 'ev-p' : m.pnl < 0 ? 'ev-n' : 'dim'}">${money(m.pnl)}</td>
        <td class="mono ${m.roi >= 0 ? 'ev-p' : 'ev-n'}">${pc(m.roi, 2)}</td>
        <td class="mono">${m.dec ? `${pc(m.hit_rate, 1)} (${m.hit}/${m.dec})` : '—'}</td></tr>`; }).join('')}
    </table></div></div>`;
}

function notifyCard() {
  const n = (LED.stats || {}).notify;
  if (!n) return '';
  const on = !!n.last_sent;
  const ch = [
    ['落注', '真正建立注單先發', n.n_bets],
    ['結算', '賽果拉到後自動送', n.n_settled],
    ['佇列', '時點補位 / 移除', n.n_queue ? '運作中' : '待命'],
    ['總結', n.last_sweep ? ('最近 ' + n.last_sweep) : '每晚 23:59', n.n_sweeps],
  ];
  return `<div class="card"><h2 class="card-h">Telegram 通知 <span class="sub">四條通道 · 全部冪等,唔會重複發</span></h2>
    <div class="stk-chips">
      <span class="chip"><b class="${on ? 'dz' : ''}">${on ? '運作中' : '待命'}</b> 狀態</span>
      <span class="chip"><b>${on ? hkStamp(n.last_sent) : '\u2014'}</b> 最後推送</span>
    </div>
    <table class="calib"><thead><tr><th>通道</th><th>觸發</th><th class="r">已推送</th></tr></thead>
      <tbody>${ch.map((c) => `<tr><td><b>${c[0]}</b></td><td class="dim">${esc(String(c[1]))}</td><td class="r mono">${esc(String(c[2]))}</td></tr>`).join('')}</tbody></table>
    <p class="mx-note">同一注、同一日總結唔會重複發。落注通知只喺 T-5 真正建立注單時送出;佇列通知只喺時點有增減時送出。</p>
  </div>
`;
}

function logCard() {
  const L = (LED.log || []).slice().reverse();
  if (!L.length) return `<div class="card"><h2 class="card-h">同步紀錄</h2><div class="empty2">未有紀錄</div></div>`;
  return `<div class="card"><h2 class="card-h">同步紀錄 <span class="sub">每次跑預測嘅倉位變化</span></h2>
    <div class="logs">${L.slice(0, 12).map((e) => `<div class="log">
      <div class="log-h"><span class="mono">${hkStamp(e.ts)}</span>
        <span class="minitag ${(e.n_changes || (e.changes||[]).length) ? 'go' : ''}">${e.kind === '結算' ? '結算' : ((e.n_changes != null ? e.n_changes : (e.changes||[]).length) + ' 項變動')}</span></div>
      ${(e.changes || []).map((c) => `<div class="log-l">${esc(c)}</div>`).join('') || '<div class="log-l dim">無變化</div>'}
    </div>`).join('')}</div></div>`;
}

boot();
