/* 足破 · 賽事預測終端 */
'use strict';

const $  = (s, r = document) => r.querySelector(s);
const $$ = (s, r = document) => [...r.querySelectorAll(s)];
const numeric = (x) => (x == null || x === '' || !Number.isFinite(Number(x)) ? null : Number(x));
const pc = (x, d = 1) => numeric(x) == null ? '—' : (numeric(x) * 100).toFixed(d) + '%';
const sg = (x, d = 2) => numeric(x) == null ? '—' : (numeric(x) >= 0 ? '+' : '') + numeric(x).toFixed(d);
const f2 = (x) => numeric(x) == null ? '—' : numeric(x).toFixed(2);
const f3 = (x) => numeric(x) == null ? '—' : numeric(x).toFixed(3);
const money = (x) => numeric(x) == null ? '—' : '$' + Math.round(numeric(x)).toLocaleString('en-US');
const esc = (s) => String(s ?? '').replace(/[&<>"']/g, (c) =>
  ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c]));

const TAG = { 'T-5': 'tag-t5', 'T-30': 'tag-t30', '首預': 'tag-t60', '待入窗': 'tag-wait', '已開賽': 'tag-none' };
const STAGE_DESC = {
  '首預': '每晚 23:59 掃全板 · 參考初盤同開盤結構',
  'T-30': '開賽前 30 分鐘 · 陣容、傷患出咗,賠率漸定',
  'T-5': '開賽前 5 分鐘 · 唯一落注時點',
};
const VD_CLS = { '落注': 'v-go', '傾向': 'v-lean', '偏向': 'v-soft', '觀望': 'v-wait', '無傾向': 'v-none' };
const MKT = { HDC: '讓球', HIL: '入球大細', CHL: '角球大細', HAD: '主客和' };
const leagueDisplay = (value) => (window.LeagueDisplay && window.LeagueDisplay.display ? window.LeagueDisplay.display(value) : String(value || ''));
const marketLabel = (value) => {
  const raw = String(value ?? '').trim();
  const code = raw.toUpperCase();
  if (MKT[code]) return MKT[code];
  if (raw === 'HKJC角球大細' || raw === '皇冠角球大細') return MKT.CHL;
  if (raw === '皇冠讓球') return MKT.HDC;
  return raw || '—';
};
// Accept old persisted labels without exposing canonical codes or abstract
// direction-path tokens in the public dashboard.
const publicText = (value) => String(value ?? '')
  .replace(/\bHDC\b/g, '讓球')
  .replace(/\bHIL\b/g, '入球大細')
  .replace(/\bCHL\b/g, '角球大細')
  .replace(/\b[ABC](?:→[ABC])+\b/g, '方向變化');
const ODDS_SOURCE_LABEL = {
  'titan007-crown-id-3': '皇冠盤（Titan007）',
  'hkjc-current-board': '馬會即時盤',
  hkjc: '馬會盤',
};
const oddsSourceLabel = (value) => ODDS_SOURCE_LABEL[value] || value || '未提供';

let DATA = null, LIST = [], LED = null, SEL = null, STAGE = 'all', Q = '', VIEW = 'pred';
let HISTORY_STAGE = 'all';
const HISTORY_PAGE_SIZE = 50;
let HISTORY_VISIBLE = HISTORY_PAGE_SIZE;
let HISTORY = { state: 'idle', payload: null, error: '', version: null, source: null, promise: null };
let HISTORY_REQUEST_ID = 0;

const kt = (s) => new Date(String(s).replace(' ', 'T') + (/[Z+]/.test(s) ? '' : '+08:00'));
function hkClock(s) { return kt(s).toLocaleTimeString('zh-HK', { hour: '2-digit', minute: '2-digit', hour12: false, timeZone: 'Asia/Hong_Kong' }); }
function hkDay(s) { return kt(s).toLocaleDateString('zh-HK', { month: '2-digit', day: '2-digit', timeZone: 'Asia/Hong_Kong' }); }
function hkStamp(s) { return kt(s).toLocaleString('zh-HK', { month: '2-digit', day: '2-digit', hour: '2-digit', minute: '2-digit', hour12: false, timeZone: 'Asia/Hong_Kong' }); }
function minsLeft(s) { return (kt(s) - Date.now()) / 60000; }
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
function stageSnapshotStatus(m, stage, nowMs, generatedAt) {
  // An absent stage in an old public JSON is not evidence of a scheduler miss.
  // Only a snapshot written after a stage's window closes may confirm one.
  const kickoff = kt(m && m.kickoff_hkt);
  const generated = kt(generatedAt);
  if (!Number.isFinite(kickoff.getTime()) || !Number.isFinite(generated.getTime())) return 'unknown';
  const windowStart = kickoff.getTime() - (stage === 'T-30' ? 40 : 10) * 60000;
  const windowEnd = stage === 'T-30' ? kickoff.getTime() - 20 * 60000 : kickoff.getTime();
  if (generated.getTime() < windowStart) return 'stale';
  if (nowMs < windowStart) return 'not_due';
  if (nowMs < windowEnd) return 'window_open';
  return generated.getTime() >= windowEnd ? 'confirmed_missing' : 'stale';
}
function missingT5Text(m, mins) {
  if (mins > 0) return '';
  return stageSnapshotStatus(m, 'T-5', Date.now(), DATA && DATA.generated_at) === 'confirmed_missing'
    ? '本場冇跑到 T-5，冇落注（已由開賽後快照確認）。'
    : '儀表板快照早於 T-5 完成時點或狀態未明，未能確認本場有冇跑到 T-5；等待最新同步，暫不判定冇落注。';
}
function convClass(c) { return c >= 65 ? 'good' : c >= 58 ? 'amber' : c >= 50 ? '' : 'bad'; }
function heat(p, max) {
  if (max <= 0) return 'oklch(21% 0.01 258)';
  const t = Math.pow(p / max, 0.55);
  return `oklch(${(20 + t * 52).toFixed(1)}% ${(0.012 + t * 0.135).toFixed(3)} 74)`;
}
function dashboardEmptyMessage() {
  const status = DATA && DATA.dashboard_status;
  if (status && status.state === 'not_yet_run') {
    return publicText(status.message || '系統尚未執行首次掃描；暫時未有賽事及預測紀錄。');
  }
  return '冇符合條件嘅賽事';
}

/* ══════════════════════ 啟動 ══════════════════════ */
const API_BASE = '/api';
function historyVersion(raw = DATA) {
  if (!raw || typeof raw !== 'object') return null;
  return raw.history_data_version || raw.history_generated_at || raw.generated_at || null;
}
function sanitizeHistory(history) {
  const source = history && typeof history === 'object' ? history : {};
  return {
    ...source,
    rows: (source.rows || []).flatMap((sourceRow) => {
      if (!sourceRow || typeof sourceRow !== 'object') return [];
      const row = { ...sourceRow };
      row.market_predictions = (sourceRow.market_predictions || []).filter((prediction) => {
        if (!prediction || !['HDC', 'HIL', 'CHL'].includes(prediction.code)) return false;
        if (!['H', 'A', 'L'].includes(prediction.side)) return false;
        const rawLine = prediction.line == null ? prediction.condition : prediction.line;
        const odds = Number(prediction.odds);
        return rawLine !== '' && Number.isFinite(Number(rawLine)) && Number.isFinite(odds) && odds > 1;
      });
      return row.market_predictions.length ? [row] : [];
    }),
  };
}
function historyPayloadFromArtifact(raw) {
  if (!raw || typeof raw !== 'object') return null;
  if (raw.prediction_history && typeof raw.prediction_history === 'object') return raw.prediction_history;
  return Array.isArray(raw.rows) || raw.stats ? raw : null;
}
function historyRequestUrl() {
  const rawUrl = DATA && DATA.history_data_url;
  if (typeof rawUrl !== 'string' || !rawUrl.trim()) return null;
  try {
    const url = new URL(rawUrl, window.location.href);
    if (url.origin !== window.location.origin) return null;
    const version = historyVersion();
    if (version) url.searchParams.set('v', version);
    return url.toString();
  } catch (_) { return null; }
}
async function loadHistory({ force = false } = {}) {
  if (HISTORY.source === 'inline') return HISTORY.payload;
  if (HISTORY.state === 'loading') return HISTORY.promise;
  if (HISTORY.state === 'ready' && !force) return HISTORY.payload;
  const url = historyRequestUrl();
  if (!url) {
    HISTORY = { state: 'error', payload: null, error: '歷史紀錄檔未提供，請重新載入儀表板後再試。', version: historyVersion(), source: null, promise: null };
    if (VIEW === 'fc') renderFc();
    return null;
  }
  const expectedVersion = historyVersion(), requestId = ++HISTORY_REQUEST_ID;
  const request = (async () => {
    let loaded = null;
    try {
      const response = await fetch(url, { cache: 'no-store' });
      if (!response.ok) throw new Error(`HTTP ${response.status}`);
      const artifact = await response.json();
      const responseVersion = artifact && artifact.history_data_version;
      if (expectedVersion && responseVersion !== expectedVersion) throw new Error('紀錄正在更新，請重新讀取。');
      const payload = historyPayloadFromArtifact(artifact);
      if (!payload || !Array.isArray(payload.rows)) throw new Error('紀錄檔格式無效');
      if (requestId !== HISTORY_REQUEST_ID) return null;
      HISTORY = { state: 'ready', payload: sanitizeHistory(payload), error: '', version: expectedVersion || responseVersion || null, source: 'remote', promise: null };
      loaded = HISTORY.payload;
    } catch (error) {
      if (requestId === HISTORY_REQUEST_ID) HISTORY = { state: 'error', payload: null, error: error.message || '讀取失敗', version: expectedVersion, source: null, promise: null };
    }
    if (requestId === HISTORY_REQUEST_ID && VIEW === 'fc') renderFc();
    return loaded;
  })();
  HISTORY = { state: 'loading', payload: HISTORY.payload, error: '', version: expectedVersion, source: HISTORY.source, promise: request };
  if (VIEW === 'fc') renderFc();
  return request;
}
function applyData(raw) {
  const history = raw && raw.prediction_history;
  const previousVersion = HISTORY.version;
  if (history && Array.isArray(history.rows)) {
    HISTORY = { state: 'ready', payload: sanitizeHistory(history), error: '', version: historyVersion(raw), source: 'inline', promise: null };
  } else if (HISTORY.source === 'inline' || (
    previousVersion && historyVersion(raw) && previousVersion !== historyVersion(raw)
  )) {
    HISTORY_REQUEST_ID += 1;
    HISTORY = { state: 'idle', payload: null, error: '', version: historyVersion(raw), source: null, promise: null };
  }
  DATA = raw;
  LED = raw.ledger || { bets: [], stats: {}, log: [] };
  LIST = (raw.matches || []).slice()
    .sort((a, b) => kt(a.kickoff_hkt) - kt(b.kickoff_hkt));
  $('#genAt').textContent = hkStamp(raw.generated_at) + ' HKT';
}
async function boot() {
  let raw;
  try {
    const r = await fetch('data.json?v=' + Date.now());
    if (!r.ok) throw new Error('HTTP ' + r.status);
    raw = await r.json();
  } catch (e) {
    $('#detail').innerHTML = `<div class="empty">資料載入失敗:${esc(e.message)}</div>`;
    return;
  }
  applyData(raw);
  bindUI();
  render();
  setInterval(render, 30000);
  setInterval(() => refresh(true), 60000);   // 每分鐘檢查伺服器最新資料
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
    if (!silent) {
      const settlement = await fetch(`${API_BASE}/settle`, {
        method: 'POST',
        cache: 'no-store',
        headers: {
          'Content-Type': 'application/json',
          'X-Footbreak-Action': 'settle-simulation',
        },
        body: JSON.stringify({ confirm: 'simulation-only' }),
      });
      const result = await settlement.json().catch(() => ({}));
      settlementBusy = settlement.status === 409 && result.error === 'settlement_busy';
      if (!settlement.ok && !settlementBusy) {
        throw new Error(result.error || `結算 HTTP ${settlement.status}`);
      }
      raw = result.data;
    }
    if (!raw) {
      const r = await fetch('data.json?v=' + Date.now(), { cache: 'no-store' });
      if (!r.ok) throw new Error('HTTP ' + r.status);
      raw = await r.json();
    }
    const oldHistoryVersion = historyVersion();
    const historyWasOpen = VIEW === 'fc';
    const changed = raw.generated_at !== (DATA && DATA.generated_at);
    applyData(raw);
    render();
    if (historyWasOpen && HISTORY.source !== 'inline' && (
      oldHistoryVersion !== historyVersion() || HISTORY.state === 'error'
    )) void loadHistory({ force: true });
    if (!silent) {
      flash(settlementBusy
        ? '結算程序運行中，已載入目前最新資料'
        : changed ? '賽果核對完成，已更新到最新資料' : '賽果核對完成，暫時冇新賽果');
    }
  } catch (e) {
    if (!silent) flash('更新失敗:' + e.message, true);
  } finally {
    // 挑戰模型報告獨立讀取:即使主資料或結算失敗,一樣重新攞一次(帶時間戳,不經快取)。
    if (VIEW === 'chal' || CHAL.state !== 'idle') void loadChallenger({ quiet: silent });
    // 資料健康報告同樣獨立讀取,唔會被主資料或結算失敗拖累。
    if (VIEW === 'health' || HEALTH.state !== 'idle') void loadHealth({ quiet: silent });
    if (VIEW === 'condition' || CONDITION.state !== 'idle') void loadCondition({ quiet: silent });
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
  $('#viewFc').hidden = VIEW !== 'fc';
  $('#viewLedger').hidden = VIEW !== 'ledger';
  $('#viewChal').hidden = VIEW !== 'chal';
  $('#viewHealth').hidden = VIEW !== 'health';
  $('#viewCondition').hidden = VIEW !== 'condition';
  $$('#nav .navbtn').forEach((b) => b.classList.toggle('is-on', b.dataset.view === VIEW));
  if (VIEW === 'pred') {
    renderKpis(); renderList();
    if (!SEL || !LIST.some((m) => m.match_id === SEL)) {
      const f = LIST.find((m) => m.pick) || LIST[0];
      SEL = f ? f.match_id : null;
    }
    renderDetail(SEL);
  } else if (VIEW === 'fc') {
    renderKpis(); renderFc();
  } else if (VIEW === 'chal') {
    renderChallenger();
    if (CHAL.state === 'idle') void loadChallenger({});
  } else if (VIEW === 'health') {
    renderHealth();
    if (HEALTH.state === 'idle') void loadHealth({});
} else if (VIEW === 'condition') {
    renderCondition();
    if (CONDITION.state === 'idle') void loadCondition({});
  } else {
    renderLedger();
  }
  requestAnimationFrame(updateScrollDock);
}

function updateScrollDock() {
  const dock = $('#scrollDock');
  if (!dock) return;
  dock.hidden = document.documentElement.scrollHeight <= window.innerHeight + 80;
}

function scrollToPageBottom() {
  let previousMax = -1;
  let attempts = 0;
  const advance = () => {
    const max = Math.max(0, document.documentElement.scrollHeight - window.innerHeight);
    if (Math.abs(window.scrollY - max) < 8 && max === previousMax) return;
    if (attempts >= 12) {
      window.scrollTo({ top: max, behavior: 'auto' });
      return;
    }
    previousMax = max;
    attempts += 1;
    window.scrollTo({ top: max, behavior: 'smooth' });
    if ('onscrollend' in window) {
      window.addEventListener('scrollend', advance, { once: true });
    } else {
      setTimeout(advance, 450);
    }
  };
  advance();
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
  const top = $('#scrollTop');
  const bottom = $('#scrollBottom');
  if (top) top.onclick = () => window.scrollTo({ top: 0, behavior: 'smooth' });
  if (bottom) bottom.onclick = scrollToPageBottom;
  window.addEventListener('resize', updateScrollDock, { passive: true });
}

/* ══════════════════════ KPI ══════════════════════ */
function renderKpis() {
  const picks = LIST.filter((m) => m.pick);
  const has = (m, k) => (m.stages || []).some((x) => x.stage === k);
  const nT5 = LIST.filter((m) => has(m, 'T-5')).length;
  const s = LED.stats || {};
  const K = [
    ['在板賽事', LIST.length, ''],
    ['已完結(隱藏)', DATA.n_hidden_ended || 0, ''],
    ['已 T-30', LIST.filter((m) => has(m, 'T-30')).length, ''],
    ['已 T-5', nT5, ''],
    ['已落注', picks.length, picks.length ? 'good' : ''],
    ['在場注碼', money(s.open_stake), 'amber'],
    ['佔本金', pc(s.open_pct, 1), (s.open_pct || 0) > 0.3 ? 'bad' : 'good'],
    ['累計盈虧', (s.pnl || 0) === 0 ? '—' : money(s.pnl), (s.pnl || 0) >= 0 ? 'good' : 'bad'],
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

function nextStageText(m, mins) {
  const stages = m.stages || [];
  const t30 = stages.some((x) => x.stage === 'T-30');
  const t5 = stages.some((x) => x.stage === 'T-5');
  if (t5) return '○ T-5 完成 · 唔買';
  if (t30) return '○ T-30 完成 · 等 T-5';
  if (mins > 40) return '○ 等 T-30';
  const t30State = stageSnapshotStatus(m, 'T-30', Date.now(), DATA && DATA.generated_at);
  if (mins >= 20) {
    return ['stale', 'unknown'].includes(t30State)
      ? '○ 儀表板快照過期 · 未能確認 T-30'
      : '○ T-30 窗口中 · 等待處理記錄';
  }
  if (mins > 0) {
    return t30State === 'confirmed_missing'
      ? '○ 已確認未記錄 T-30 · 等 T-5'
      : '○ 儀表板快照過期 · 未能確認 T-30';
  }
  return stageSnapshotStatus(m, 'T-5', Date.now(), DATA && DATA.generated_at) === 'confirmed_missing'
    ? '○ 已確認未記錄 T-5'
    : '○ 儀表板快照過期 · 未能確認 T-5';
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
          <span class="fx-lg">${esc(hkDay(m.kickoff_hkt))} · ${esc(leagueDisplay(m.league))}</span>
        </div>
        <div class="fx-foot">${dots(m)}
          ${m.pick
            ? `<span class="fx-pick">▶ ${esc(m.pick.label)} <b>@${f2(m.pick.odds)}</b> · ${money(m.pick.stake)}</span>`
            : `<span class="fx-pick wait">${nextStageText(m, mm)}</span>`}
        </div>
      </div></li>`;
  }).join('') || `<li class="empty">${esc(dashboardEmptyMessage())}</li>`;
  $$('#fixtures .fx').forEach((el) => {
    el.onclick = () => { SEL = el.dataset.id; renderList(); renderDetail(SEL); };
    el.onkeydown = (e) => { if (e.key === 'Enter' || e.key === ' ') { e.preventDefault(); el.onclick(); } };
  });
}

/* ══════════════════════ 賽事詳情 ══════════════════════ */
function renderDetail(id) {
  const m = LIST.find((x) => x.match_id === id);
  const D = $('#detail');
  if (!m) {
    D.innerHTML = `<div class="empty">${esc(
      LIST.length ? '請於左方揀一場賽事' : dashboardEmptyMessage(),
    )}</div>`;
    return;
  }
  const mm = minsLeft(m.kickoff_hkt), st = stageOf(mm);
  let h = head(m, mm, st);
  h += verdictCard(m);
  h += currentOddsCard(m);
  h += conditionMatchesCard(m);
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
      <span class="mhead-lg">${esc(leagueDisplay(m.league))}</span>
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
      <span>馬會盤開盤 <strong class="num">${m.hk_pool_opened ? hkStamp(m.hk_pool_opened) : '—'}</strong></span>
    </div></div>`;
}

function hkCond(p) {
  // 從 label 抽出已翻成選邊視角嘅盤口,抽唔到就退回原始 condition
  const m = String(p.label || '').match(/[（(]\s*馬會盤\s*([^）)]*)[）)]/);
  return m ? m[1].trim() : (p.condition || '—');
}

function shortPick(p) {
  // 剝走「(馬會盤 …)」尾巴,避免窄格內斷行成一柱
  return String(p.label || '').replace(/[（(]\s*馬會盤[^）)]*[）)]\s*$/, '').trim();
}

function wilsonMatchText(item) {
  const number = numeric(item.condition_number) == null ? '—' : String(Math.trunc(numeric(item.condition_number)));
  const direction = publicText(item.selected_role || '—');
  const line = numeric(item.selected_line) == null ? '—' : numeric(item.selected_line).toString();
  // Producer persists this alongside the raw admission value. Do not derive
  // it from the UI quote or re-run Wilson arithmetic in the browser.
  const minimum = numeric(item.minimum_required_odds_display) == null
    ? f2(item.minimum_required_odds) : String(item.minimum_required_odds_display);
  const isBet = item.bet_status === 'BET';
  return `<div class="condition-match"><b>合符條件 #${esc(number)}</b>
    <span>${esc(marketLabel(item.market_label || item.market))} · ${esc(direction)} ${esc(line)} · 現時賠率 ${f2(item.odds)} · 最低賠率要求 ${esc(minimum)}</span>
    <span class="${isBet ? 'good-txt' : 'bad-txt'}">${isBet ? '模擬投注' : '因賠率不足，不投注'}</span></div>`;
}

function historicalConditionMatchText(item) {
  const number = numeric(item.condition_rank) == null ? '—' : String(Math.trunc(numeric(item.condition_rank)));
  const total = item.total || {};
  const decided = numeric(total.decided) || 0;
  const interval = Array.isArray(total.wilson95) ? total.wilson95 : [];
  const lower = numeric(interval[0]);
  const minimum = lower != null && lower > 0.03 ? 1 / (lower - 0.03) : null;
  const actual = numeric(item.selected_odds);
  const sampleReady = decided >= 50;
  const lowOdds = sampleReady && minimum != null && actual != null && actual < minimum;
  const isT5 = String(item.decision_stage || item.stage || '') === 'T-5';
  let status = '只作條件觀察；T-5 先作正式投注決定';
  let tone = '';
  if (isT5 && !sampleReady) {
    status = `合符條件 #${number}，但樣本不足 50 場，不投注`;
    tone = 'bad-txt';
  } else if (isT5 && lowOdds) {
    status = `合符條件 #${number}，但賠率不足，不投注`;
    tone = 'bad-txt';
  } else if (isT5 && sampleReady && minimum != null && actual != null) {
    status = `合符條件 #${number}，賠率達標；仍須通過正式 Wilson 證據閘門`;
    tone = 'good-txt';
  }
  return `<div class="condition-match"><b>條件 #${esc(number)} · ${esc(publicText(item.label || ''))}</b>
    <span>${pc(total.accuracy, 1)} (${total.hits || 0}/${total.decided || 0}) · 現時賠率 ${actual == null ? '—' : f2(actual)} · 最低賠率要求 ${minimum == null ? '未能計算' : f2(minimum)}</span>
    <span class="${tone}">${esc(status)}</span></div>`;
}

function wilsonVerdictCard(m) {
  const matches = (m.wilson_matches || []).filter((item) => item && typeof item === 'object');
  if (!matches.length) return '';
  const bets = matches.filter((item) => item.bet_status === 'BET');
  const lowOdds = matches.filter((item) => item.bet_status !== 'BET');
  return `<div class="card verdict ${bets.length ? 'go' : 'wait'}">
    <div class="vd-top"><span class="vd-badge ${bets.length ? 'go' : 'wait'}">${bets.length ? 'Wilson 模擬注' : 'Wilson 不投注'}</span></div>
    <div class="condition-match-list">${matches.map(wilsonMatchText).join('')}</div>
    <p class="vd-note">${lowOdds.length ? '已合符歷史 Wilson 條件；低於凍結最低可接受賠率的市場不會建立正式模擬注。' : '所有顯示市場均以凍結 Wilson 條件及原始入場算術建立模擬注。'}</p>
  </div>`;
}

function verdictCard(m) {
  const c = m.conviction;
  const bar = `<div class="conv-wrap">
      <div class="conv-track"><div class="conv-fill ${convClass(c)}" style="width:${Math.min(100, Math.max(0, c))}%"></div>
        <div class="conv-floor" style="left:${DATA.ledger.stats.conf_floor || 58}%"></div></div>
      <div class="conv-scale"><span>0</span><span>門檻 ${DATA.ledger.stats.conf_floor || 58}</span><span>100</span></div>
    </div>`;
  const wilsonVerdict = wilsonVerdictCard(m);
  if (wilsonVerdict) return wilsonVerdict;
  const historicalMatches = (m.condition_matches || []).filter((item) => item && typeof item === 'object');
  const hasT5 = (m.stages || []).some((x) => x.stage === 'T-5');
  if (hasT5 && historicalMatches.length) {
    return `<div class="card verdict wait">
      <div class="vd-top"><span class="vd-badge wait">Wilson 未建立模擬注</span></div>
      <div class="condition-match-list">${historicalMatches.map(historicalConditionMatchText).join('')}</div>
      <p class="vd-note">逐項列明條件、賠率門檻及不投注原因；正式模擬注仍以原生 T-5、來源證據及 Wilson 閘門為準。</p>
    </div>`;
  }
  if (!m.pick) {
    const mm = minsLeft(m.kickoff_hkt);
    const missingT5 = missingT5Text(m, mm);
    const badge = hasT5 ? '觀望 · 唔買' : (mm > 0 ? '未到落注時點' : (
      missingT5.startsWith('本場') ? '無落注' : '狀態待同步'
    ));
    const why = hasT5
      ? (m.no_bet_reason || '未達投注條件')
      : (mm > 0
        ? `最終投注決定統一喺<b>開賽前 5 分鐘</b>先出。依家係${esc(stageOf(mm))},只做預測記錄。距開賽 ${cdText(mm)}。`
        : missingT5);
    return `<div class="card verdict wait">
      <div class="vd-top"><span class="vd-badge wait">${badge}</span>
        <span class="vd-conv ${convClass(c)}">信念 ${f2(c)}</span></div>
      <p class="vd-why">${why}</p>
      ${bar}</div>`;
  }
  const p = m.pick;
  const G = [
    ['市場', p.market], ['投注', shortPick(p)], ['馬會賠率', f2(p.odds)],
    ['我嘅公平價', f2(p.fair)], ['模型勝率', pc(p.prob)],
    ['走水機率', p.push > 1e-6 ? pc(p.push) : '—'],
    ['凱利(原始)', pc(p.kelly_raw)], ['凱利(採用)', pc(p.kelly_used)],
  ];
  if (p.code === 'HDC' && p.condition) G.splice(2, 0, ['馬會盤口', hkCond(p)]);
  return `<div class="card verdict go">
    <div class="vd-top">
      <span class="vd-badge go">建議投注</span>
      <span class="vd-main">${esc(p.label)} <b>@${f2(p.odds)}</b></span>
      <span class="vd-stake">${money(p.stake)}</span>
      <span class="vd-conv ${convClass(c)}">信念 ${f2(c)}</span>
    </div>
    <div class="vd-grid">${G.map(([l, v]) =>
      `<div class="par"><div class="par-l">${l}</div><div class="par-v">${esc(v)}</div></div>`).join('')}</div>
    ${bar}
    ${p.code === 'HDC' ? `<p class="vd-note">讓球讀法 —— 上面嘅「讓 / 受讓」係按我揀嘅一邊寫。括弧入面嘅馬會盤口已經同步翻成<b>我揀嗰邊嘅視角</b>(馬會原始盤口係主隊視角,買客隊會反號)。</p>` : ''}
    <p class="vd-note">注碼 = min(全凱利 × ${fracTxt()}${mktTxt(m)} × min(1, 信念/75), 單場上限 ${pc(DATA.ledger.stats.single_cap_pct, 0)} × min(1, b/0.80)) × 本金 ${money(DATA.ledger.bankroll)},組合層不設單日 / 在場曝險上限(用戶指定)。已取消硬性最低賠率門檻 —— 改用賠率遞減單場上限(b = 賠率 − 1;b &lt; 0.80 時上限按 b/0.80 縮細),因為注碼 = EV / b,b 越細同樣的模型誤差對對數增長的殺傷力越大。</p>
  </div>`;
}

function currentOddsCard(m) {
  const rows = m.current_selected_odds_journal || [];
  if (!rows.length && !m.current_odds_status) return '';
  const side = (row) => row.code === 'HDC'
    ? ({ H: '主', A: '客' }[row.side] || row.side || '—')
    : ({ H: '大', L: '細' }[row.side] || row.side || '—');
  const reason = {
    current_exact_quote_unavailable: '目前未有相同盤口選邊賠率',
    one_or_more_current_selected_quotes_unavailable: '部分已選現價不可用',
    no_current_selected_quote: '未有已選市場現價',
  };
  const seen = m.current_odds_refreshed_at
    ? ` · 觀察 ${esc(hkStamp(m.current_odds_refreshed_at))}`
    : '';
  const items = rows.map((row) => {
    const odds = Number(row.odds);
    const observedAt = row.observed_board_at || row.observed_at;
    const price = Number.isFinite(odds) && odds > 1
      ? `賠率 ${odds.toFixed(2)}`
      : `賠率缺失 · ${esc(reason[row.reason] || row.reason || '未有已保存現價')}`;
    return `<div class="current-odds-row"><b>${esc(MKT[row.code] || row.code || '—')}</b>
      <span>${esc(row.line ?? '—')} · ${esc(side(row))}</span>
      <span class="${Number.isFinite(odds) && odds > 1 ? '' : 'missing'}">${price}</span>
      <small>資料來源：${esc(oddsSourceLabel(row.source))} · 記錄時間：${observedAt ? esc(hkStamp(observedAt)) : '未提供'}</small>
    </div>`;
  }).join('');
  const empty = m.current_odds_status === 'missing' && !rows.length
    ? `<div class="current-odds-row missing">賠率缺失 · ${esc(reason[m.current_odds_reason] || m.current_odds_reason || '未有已選市場現價')}</div>`
    : '';
  return `<div class="card current-odds">
    <h2 class="card-h">目前已選賠率 <span class="sub">只供未開賽卡片參考；不會改寫首預／T-30／T-5 歷史</span></h2>
    <div class="current-odds-list">${items || empty}</div>
    <p class="current-odds-note">更新來源：${esc(oddsSourceLabel(m.current_odds_refresh_source))}${seen}</p>
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
          <span>${mm > 0 ? '仲有 ' + cdText(mm) + ' 到開賽,最終投注決定會喺開賽前 5 分鐘先出' : missingT5Text(m, mm)}</span>
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
      return q ? q.label : '無方向';
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
      : `<span class="run-pick dimp">無明顯方向</span>
         ${ld ? `<span class="run-num">最佳候選 ${esc(ld.label)} · EV ${sg((ld.ev || 0) * 100, 2)}%,全部負值</span>` : ''}`;

  const facts = [];
  if (x.final) facts.push(`我終值 總入球 ${f2(x.final.total)} · 主客差 ${sg(x.final.supremacy)} · 角球 ${f2(x.final.mu)}`);
  if (mv.d_total != null) facts.push(`初盤→現價 入球 ${chip(mv.d_total, '球')} · 主客差 ${chip(mv.d_sup, '球')} · 角球 ${chip(mv.d_corners)}`);
  if (info.temp != null) facts.push(`天氣 ${esc(info.desc || '')} ${f2(info.temp)}°C`);
  facts.push(info.news ? `<b class="ok">有陣容 / 傷患資訊</b>` : `<b class="dim">未有陣容 / 傷患資訊</b>`);
  if (info.hk_lines != null) facts.push(`馬會 ${info.hk_lines} 條線` +
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
      <span>馬會盤自我上次快照 ${m.hk_moved_since_last === null || m.hk_moved_since_last === undefined
        ? '<b class="dim">未有對比基準(首次記錄)</b>'
        : m.hk_moved_since_last
          ? `<b class="amber">有變動</b> · ${m.hk_n_lines_moved} 條線,最大 ${f2(m.hk_max_move_pct)}%`
          : '<b class="dim">無變動</b>'} · 共 ${m.n_hk_lines} 條線</span>
    </div>
    <p class="mx-note">馬會 feed 冇「最後改價時間」呢個欄位(<span class="mono">updateAt</span> 其實係開盤時間),所以盤口變動只能靠自存快照逐輪比對。</p>
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
  const w = m.weather, f = m.fatigue || {};
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
  const news = (m.adjustments || []).filter((a) => !['大盤被推動', '讓球盤被推動', '大盤平穩', '角球盤移動', '氣溫偏高', '氣溫偏低', '氣溫中性', '其他天氣(不調整)', '休息日', '中立場'].includes(a.tag));
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
      <tr><th>市場</th><th>投注</th><th>馬會</th><th>公平價</th><th>勝率</th><th>走水</th><th>EV</th><th>凱利</th><th></th></tr>
      ${C.map((c) => `<tr class="${isPick(c) ? 'pickrow' : ''}">
        <td class="lbl">${esc(marketLabel(c.market || c.code))}</td><td>${esc(publicText(c.label))}</td>
        <td>${f2(c.odds)}</td><td class="dim">${f2(c.fair)}</td>
        <td>${pc(c.prob)}</td><td class="${c.push > 1e-6 ? 't-push' : 't-dim'}">${c.push > 1e-6 ? pc(c.push) : '—'}</td>
        <td class="${c.ev > 0 ? 'ev-p' : 'ev-n'}">${sg(c.ev * 100, 2)}%</td>
        <td>${c.kelly_raw > 0 ? pc(c.kelly_raw) : '—'}</td>
        <td>${c.is_main ? '<span class="minitag">主線</span>' : ''}${isPick(c) ? '<span class="minitag go">選中</span>' : ''}</td>
      </tr>`).join('')}
    </table></div>
    <p class="mx-note">主線 = 馬會該市場嘅主打盤口,流動性較好,排序時有 15% 加權。</p></div>`;
}

/* ══════════════════════ 純預測 ══════════════════════
   唔理 EV、唔理有冇注 —— 淨係講模型估呢場係咩賽果。
   資料由 gen_app_data.forecast() 出,每場都有(包括 archived)。 */

let FCSORT = 'conv', FCQ = '', FCHI = false;
const FCSORTS = [
  ['conv', '信念'], ['ko', '開賽時間'], ['total', '總入球'],
  ['sup', '主客差'], ['edge', '單邊最強'],
];

function distBar(dist, opts) {
  const o = opts || {};
  if (!dist || !dist.length) return '<div class="fcd-na">冇分佈</div>';
  const lo = o.lo == null ? 0 : o.lo;
  const hi = o.hi == null ? dist.length - 1 : Math.min(o.hi, dist.length - 1);
  const seg = [];
  for (let i = lo; i <= hi; i++) seg.push([i, dist[i] || 0]);
  const mx = Math.max(...seg.map((s) => s[1])) || 1;
  const med = o.med;
  const lastPlus = hi === dist.length - 1;
  return `<div class="fcd">${seg.map(([i, p]) => {
    const h = Math.max(2, Math.round((p / mx) * 100));
    const on = (o.band && i >= o.band[0] && i <= o.band[1]);
    const isMed = med === i;
    return `<div class="fcd-c${on ? ' in' : ''}${isMed ? ' med' : ''}" title="${i}${lastPlus && i === hi ? '+' : ''} 球:${pc(p, 1)}">
      <div class="fcd-b" style="height:${h}%"></div>
      <span class="fcd-x">${i}${lastPlus && i === hi ? '+' : ''}</span>
    </div>`;
  }).join('')}</div>`;
}

function wdlBar(p, home, away) {
  const [h, d, a] = p;
  const seg = [['h', h, home], ['d', d, '和'], ['a', a, away]];
  return `<div class="fcw">${seg.map(([k, v]) =>
    `<div class="fcw-s ${k}" style="flex:${(v * 1000).toFixed(0)}" title="${esc(k === 'd' ? '和局' : k === 'h' ? home : away)} ${pc(v, 1)}">
      <span>${v >= 0.16 ? pc(v, 0) : ''}</span></div>`).join('')}</div>
  <div class="fcw-l"><span class="h">${esc(home)} ${pc(h, 1)}</span><span class="d">和 ${pc(d, 1)}</span><span class="a">${esc(away)} ${pc(a, 1)}</span></div>`;
}

function fcCard(m) {
  const f = m.fc;
  const mins = minsLeft(m.kickoff_hkt);
  const st = (m.stages || []).length ? m.stages[m.stages.length - 1].stage : stageOf(mins);
  const conv = m.conviction;
  const [ph, pd, pa] = f.p;
  const lean = Math.max(ph, pd, pa);
  const leanTxt = lean === ph ? m.home : lean === pa ? m.away : '和局';
  const g = f.gband || [null, null, null];
  const c = f.cband || [null, null, null];
  const bet = m.pick ? `<span class="fc-bet">★ 已落注 ${esc(m.pick.label)} @${f2(m.pick.odds)} ${money(m.pick.stake)}</span>` : '';
  const why = m.no_bet_reason && !m.pick
    ? `<div class="fc-why">唔買原因:${esc(m.no_bet_reason)}</div>` : '';

  return `<article class="fcc" data-id="${esc(m.match_id)}">
    <header class="fcc-h">
      <div class="fcc-t">
        <span class="fcc-clk">${hkDay(m.kickoff_hkt)} ${hkClock(m.kickoff_hkt)}</span>
        <span class="tag ${TAG[st] || 'tag-none'}">${esc(st)}</span>
        <span class="fcc-cd">${cdText(mins)}</span>
      </div>
      <div class="fcc-m">
        <b>${esc(m.home)}</b><span class="vs">vs</span><b>${esc(m.away)}</b>
      </div>
      <div class="fcc-lg">${esc(leagueDisplay(m.league || ''))}</div>
      <div class="fcc-cv ${convClass(conv)}">信念 ${conv == null ? '—' : Number(conv).toFixed(1)}</div>
    </header>

    <div class="fcc-lead">
      我估 <b>${esc(leanTxt)}</b> 較大機會(${pc(lean, 1)})· 預期入球
      <b class="mono">${f2(f.lh)} - ${f2(f.la)}</b>(合共 ${f2(f.total)},主客差 ${sg(f.sup, 2)})
      ${bet}
    </div>

    ${wdlBar(f.p, m.home, m.away)}

    <div class="fcc-sc">
      <span class="fcc-lbl">最可能比分</span>
      ${f.tops.map((t, i) => `<span class="scchip${i ? '' : ' top'}">${t.s} <i>${pc(t.p, 1)}</i></span>`).join('')}
    </div>

    <div class="fcc-grid">
      <section class="fcc-d">
        <div class="fcc-dh"><span class="fcc-lbl">總入球分佈</span>
          <span class="fcc-dn">中位 <b>${g[0]}</b> · 八成 ${g[1]}–${g[2]}</span></div>
        ${distBar(f.goals, { med: g[0], band: [g[1], g[2]] })}
        <div class="fcc-ou">
          <span>大1.5 <b>${pc(f.o15, 0)}</b></span>
          <span>大2.5 <b>${pc(f.o25, 0)}</b></span>
          <span>大3.5 <b>${pc(f.o35, 0)}</b></span>
          <span>兩隊入球 <b>${pc(f.btts, 0)}</b></span>
        </div>
      </section>
      <section class="fcc-d">
        <div class="fcc-dh"><span class="fcc-lbl">總角球分佈</span>
          <span class="fcc-dn">${f.corners ? `期望 <b>${f2(f.mu)}</b> · 中位 <b>${c[0]}</b> · 八成 ${c[1]}–${c[2]}` : '冇角球盤'}</span></div>
        ${f.corners ? distBar(f.corners, { lo: 2, med: c[0], band: [c[1], c[2]] }) : '<div class="fcd-na">馬會冇開角球盤,反推唔到</div>'}
        <div class="fcc-ou">
          <span>大8.5 <b>${pc(f.c85, 0)}</b></span>
          <span>大9.5 <b>${pc(f.c95, 0)}</b></span>
          <span>大10.5 <b>${pc(f.c105, 0)}</b></span>
        </div>
      </section>
    </div>
    ${why}
  </article>`;
}

/* ── 準繩度記分板 ── */
function accPanel() {
  const a = DATA.accuracy;
  if (!a || !a.overall) {
    return `<div class="acc-empty">還未有已完場而又有預測嘅賽事 —— 記分板會自動累積。</div>`;
  }
  const o = a.overall;
  const cell = (lbl, rt, br, note) => {
    if (!rt) return `<div class="acm dim"><span class="acm-l">${lbl}</span><b>—</b></div>`;
    return `<div class="acm"><span class="acm-l">${lbl}</span>
      <b>${rt.pct}%</b>
      <span class="acm-n">${rt.hit}/${rt.n}${br != null ? ` · Brier ${br.toFixed(3)}` : ''}${note ? ` · ${note}` : ''}</span></div>`;
  };
  const cal = (a.calibration || []).map((c) => {
    const w = Math.max(2, Math.round(c.act));
    const pw = Math.max(2, Math.round(c.pred));
    const off = c.act - c.pred;
    return `<div class="cal-r">
      <span class="cal-x">${c.lbl}</span>
      <div class="cal-t"><div class="cal-p" style="width:${pw}%"></div>
        <div class="cal-a" style="width:${w}%"></div></div>
      <span class="cal-v">預 ${c.pred}% → 實 ${c.act}%
        <i class="${Math.abs(off) <= 5 ? 'ok' : off > 0 ? 'up' : 'dn'}">${sg(off, 1)}</i>
        <em>n=${c.n}</em></span></div>`;
  }).join('');

  const conf = (a.by_conf || []).map((v) => `<tr>
      <td>${v.lbl}</td><td class="num">${v.n}</td>
      <td class="num">${v.wdl ? v.wdl.pct + '%' : '—'}</td>
      <td class="num">${v.wdl_brier == null ? '—' : v.wdl_brier.toFixed(3)}</td>
      <td class="num">${v.o25 ? v.o25.pct + '%' : '—'}</td></tr>`).join('');
  const stg = Object.entries(a.by_stage || {}).map(([s, v]) => `<tr>
      <td>${esc(s)}</td><td class="num">${v.n}</td>
      <td class="num">${v.wdl ? v.wdl.pct + '%' : '—'}</td>
      <td class="num">${v.wdl_brier == null ? '—' : v.wdl_brier.toFixed(3)}</td>
      <td class="num">${v.o25 ? v.o25.pct + '%' : '—'}</td></tr>`).join('');

  return `<details class="acc" open>
    <summary><b>我估得準唔準</b>
      <span class="acc-sub">${a.n_matches} 場已完場 · ${a.n_preds} 次階段預測 · 同盈虧完全分開</span></summary>
    <div class="acc-b">
      <div class="acc-m">
        ${cell('1X2 命中', o.wdl, o.wdl_brier, `log loss ${o.wdl_ll}`)}
        ${cell('大細2.5', o.o25, o.o25_brier)}
        ${cell('兩隊入球', o.btts, o.btts_brier)}
        ${cell('角球9.5', o.c95, o.c95_brier)}
        ${cell('準確比分', o.score1, null)}
        ${cell('頭五比分', o.score5, null)}
      </div>
      <div class="acc-m">
        <div class="acm"><span class="acm-l">入球誤差</span><b>${o.goals_mae}</b>
          <span class="acm-n">平均差 ${o.goals_mae} 球 · 八成區間覆蓋 ${o.goals_cover ? o.goals_cover.pct + '%' : '—'}</span></div>
        <div class="acm"><span class="acm-l">角球誤差</span><b>${o.corners_mae == null ? '—' : o.corners_mae}</b>
          <span class="acm-n">平均差 ${o.corners_mae == null ? '—' : o.corners_mae} 個 · 八成區間覆蓋 ${o.corners_cover ? o.corners_cover.pct + '%' : '—'}</span></div>
      </div>

      <div class="acc-cols">
        <div>
          <div class="fcc-lbl">1X2 校準 —— 講 X% 嘅事,實際發生率係?</div>
          <div class="cal">${cal || '<div class="acc-empty">樣本唔夠</div>'}</div>
          <div class="cal-lg"><span class="k p"></span>預測<span class="k a"></span>實際</div>
        </div>
        <div>
          <div class="fcc-lbl">按信念 —— 信念高係咪真係準啲?</div>
          <div class="tbl-wrap"><table class="acc-t">
            <tr><th>信念</th><th>n</th><th>1X2</th><th>Brier</th><th>大細</th></tr>${conf}</table></div>
          <div class="fcc-lbl" style="margin-top:12px">按階段 —— 越近開賽係咪越準?</div>
          <div class="tbl-wrap"><table class="acc-t">
            <tr><th>階段</th><th>n</th><th>1X2</th><th>Brier</th><th>大細</th></tr>${stg}</table></div>
        </div>
      </div>
      <p class="acc-f">Brier 越低越好(三類 1X2 亂猜約 0.67,市場水準約 0.55–0.60)。
      命中率淨係睇「最高機率嗰邊有冇發生」,樣本少嘅時候好易跳 —— 校準同 Brier 可靠啲。
      更新於 ${esc(a.generated_at || '')}。</p>
    </div>
  </details>`;
}

const HIST_MARKET_LABEL = { HDC: '讓球', HIL: '入球大細', CHL: '角球大細' };
const HIST_SETTLEMENT_LABEL = {
  Won: '全贏', 'Half Won': '半贏', Refunded: '走水',
  'Half Lost': '半輸', Lost: '全輸',
};
function historyQuarterLine(raw, signed = true) {
  const q = Math.round(Number(raw) * 4);
  if (!Number.isFinite(q)) return '—';
  const values = q % 2 === 0
    ? [q / 4]
    : q > 0
      ? [(q - 1) / 4, (q + 1) / 4]
      : [(q + 1) / 4, (q - 1) / 4];
  return values.map((value) => signed && value > 0 ? `+${value}` : String(value)).join('/');
}
function historyPredictionLabel(r, p) {
  const line = Number(p.line ?? p.condition);
  if (!Number.isFinite(line)) {
    const team = p.code === 'HDC' ? (p.side === 'A' ? r.away : r.home) : '';
    return `${team}${team ? ' · ' : ''}盤口未提供`;
  }
  if (p.code === 'HDC') {
    const team = p.side === 'A' ? r.away : r.home;
    const selectedLine = p.side === 'A' ? -line : line;
    return `${team} ${historyQuarterLine(selectedLine, true)}`;
  }
  if (p.code === 'HIL') return `${p.side === 'H' ? '大' : '細'} ${historyQuarterLine(line, false)} 球`;
  if (p.code === 'CHL') return `${p.side === 'H' ? '大' : '細'} ${historyQuarterLine(line, false)} 角球`;
  return p.label || `${p.condition} ${p.side}`;
}
function historyOdds(p) {
  const odds = Number(p.odds);
  if (Number.isFinite(odds) && odds > 1) {
    return `<span class="history-market-odds">賠率 ${odds.toFixed(2)}</span>`;
  }
  return '';
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
      <span class="history-market-pick"><b>${marketLabel(HIST_MARKET_LABEL[p.code] || p.code)}</b>
        ${esc(historyPredictionLabel(r, p))}
        <span class="cell-sub history-market-meta">${pc(p.probability, 1)} ${historyOdds(p)}</span>
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
  if (!(r.market_predictions || []).length) return '<span class="dim">冇市場預測</span>';
  if (!grades.length) return '<span class="stpill pending">市場待賽果</span>';
  return `<span class="market-total ${decided.length && hits === decided.length ? 'all-hit' : hits ? 'some-hit' : 'none-hit'}">
      市場命中 ${hits}/${decided.length}</span>
    ${pushes ? `<div class="cell-sub">走水 ${pushes} 項</div>` : ''}`;
}

function historyStageMarketMatrix(stats) {
  const stages = ['首預', 'T-30', 'T-5'];
  const codes = ['HDC', 'HIL', 'CHL'];
  const matrix = stats.by_stage_market || {};
  const cell = (stage, code) => {
    const x = (matrix[stage] || {})[code] || {};
    const graded = Number(x.graded || 0);
    const decided = Number(x.decided || 0);
    const pushes = Number(x.pushes || 0);
    const groups = x.odds_groups || {};
    const low = groups.below_1_70 || {};
    const direction = (label, selected) => {
      const high = selected || {};
      const selectedLow = (high.odds_groups || {}).below_1_70 || {};
      const highText = high.decided
        ? `${pc(high.accuracy, 1)} (${high.hits || 0}/${high.decided || 0})`
        : `待累積 (0/0)`;
      return `<span class="stage-market-direction"><b>${label}</b> ≥1.70 ${highText} · &lt;1.70 ${selectedLow.hits || 0}/${selectedLow.decided || 0}</span>`;
    };
    const cornerBreakdown = code === 'CHL'
      ? `<div class="stage-market-directions">
          ${direction('角球大', (x.by_selection || {}).H)}
          ${direction('角球細', (x.by_selection || {}).L)}
        </div>`
      : '';
    const summary = x.accuracy == null
      ? `<span class="stage-market-empty">≥1.70 待累積</span><small>已評分 ${graded}${pushes ? ` · 走水 ${pushes}` : ''}</small>`
      : `<strong>≥1.70 ${pc(x.accuracy, 1)}</strong>
         <small>命中 ${x.hits}/${decided}</small>
         <small>已評分 ${graded}${pushes ? ` · 走水 ${pushes}` : ''}</small>
         <small>&lt;1.70 ${low.hits || 0}/${low.decided || 0}</small>`;
    return summary + cornerBreakdown;
  };
  return `<div class="stage-market-block">
    <div class="stage-market-title">分階段市場命中率 <span>只計有有效賠率紀錄；主統計為選邊賠率 ≥1.70；角球另拆大／細</span></div>
    <table class="stage-market-table" aria-label="首預、T-30及T-5各市場命中率（選邊賠率大於等於1.70）">
      <thead><tr><th>階段</th>${codes.map((code) => `<th>${HIST_MARKET_LABEL[code]}</th>`).join('')}</tr></thead>
      <tbody>${stages.map((stage) => `<tr><th>${stage}</th>${codes.map((code) => `<td>${cell(stage, code)}</td>`).join('')}</tr>`).join('')}</tbody>
    </table>
  </div>`;
}

function legacyHistoryConsensusCards(stats) {
  const report = stats.three_stage_consensus || {};
  const markets = report.markets || {};
  const ranking = report.ranking || {};
  const rankCards = (ranking.top || []).map((item, index) => {
    const qualified = item.sample_qualified === true;
    const audit = item.odds_bias || {};
    const low = audit.low_odds || {};
    const kept = audit.at_or_above_threshold || {};
    const oddsLine = audit.priced_decided
      ? `<div class="consensus-odds-audit">
          <span class="eligible">≥1.70 主統計 ${kept.hits || 0}/${kept.decided || 0} (${kept.accuracy == null ? '—' : pc(kept.accuracy, 1)})</span>
          <span>平均 ${f2(kept.average_odds)}</span>
          <span class="low">&lt;1.70 獨立 ${low.hits || 0}/${low.decided || 0} (${low.accuracy == null ? '—' : pc(low.accuracy, 1)})</span>
          <span>佔有賠率 ${pc(low.share, 1)} · 平均 ${f2(low.average_odds)}</span>
        </div>`
      : '<div class="consensus-odds-audit unavailable">賠率資料不足，未能檢查熱門盤偏差</div>';
    return `<article class="consensus-rank-card">
      <div class="consensus-rank-head">
        <span class="consensus-rank-number">#${index + 1}</span>
        <span class="consensus-sample ${qualified ? 'enough' : ''}">${qualified ? '樣本達標' : '只作觀察'}</span>
      </div>
      <b>${esc(marketLabel(item.market_label || item.market))} · ${esc(publicText(item.condition_label || ''))}</b>
      <div class="consensus-rank-rate">${pc(item.accuracy, 1)}</div>
      <small>≥1.70 命中 ${item.hits || 0}/${item.decided || 0} · 合資格 ${item.fixtures || 0} 場</small>
      ${oddsLine}
    </article>`;
  }).join('');
  const codes = ['HDC', 'HIL', 'CHL'];
  const card = (code) => {
    const market = markets[code] || {};
    const stable = market.same_direction || {};
    const primary = stable.primary || {};
    const exactGroup = market.same_direction_and_line || {};
    const exact = exactGroup.primary || {};
    const stableOdds = stable.odds_segments || {};
    const stableLow = stableOdds.low_odds || {};
    const exactOdds = exactGroup.odds_segments || {};
    const exactLow = exactOdds.low_odds || {};
    const breakdown = exactGroup.breakdown || [];
    const enough = Number(primary.decided || 0) >= 30;
    const split = breakdown.map((item) => {
      const decided = Number(item.decided || 0);
      const low = (item.odds_bias || {}).low_odds || {};
      const result = item.accuracy == null
        ? '待累積'
        : `${pc(item.accuracy, 1)} (${item.hits || 0}/${decided})`;
      const lowResult = low.accuracy == null
        ? '待累積'
        : `${pc(low.accuracy, 1)} (${low.hits || 0}/${low.decided || 0})`;
      return `<div class="consensus-split-row">
        <b>${esc(publicText(item.label || item.key || '未分類'))}</b>
        <span>≥1.70 ${result}<br>&lt;1.70 ${lowResult}</span>
      </div>`;
    }).join('');
    return `<article class="consensus-card">
      <div class="consensus-card-head">
        <b>${HIST_MARKET_LABEL[code]}</b>
        <span class="consensus-sample ${enough ? 'enough' : ''}">${enough ? '樣本達標' : '樣本不足'}</span>
      </div>
      <div class="consensus-rate ${primary.accuracy == null ? 'empty' : ''}">${
        primary.accuracy == null ? '待累積' : pc(primary.accuracy, 1)
      }</div>
      <div class="consensus-meta">≥1.70 命中 ${primary.hits || 0}/${primary.decided || 0} · 合資格 ${primary.fixtures || 0} 場</div>
      <div class="consensus-detail">&lt;1.70 獨立 ${
        stableLow.accuracy == null ? '待累積' : `${pc(stableLow.accuracy, 1)} (${stableLow.hits || 0}/${stableLow.decided || 0})`
      } · 盤口曾變 ${stable.line_changed_fixtures || 0} 場</div>
      <div class="consensus-exact">方向＋盤口完全一致 ≥1.70 ${
        exact.accuracy == null ? '待累積' : `${pc(exact.accuracy, 1)} (${exact.hits || 0}/${exact.decided || 0})`
      }</div>
      <div class="consensus-detail">方向＋盤口完全一致 &lt;1.70 ${
        exactLow.accuracy == null ? '待累積' : `${pc(exactLow.accuracy, 1)} (${exactLow.hits || 0}/${exactLow.decided || 0})`
      }</div>
      <div class="consensus-split" aria-label="${HIST_MARKET_LABEL[code]}方向及盤口完全一致拆分">
        <div class="consensus-split-title">完全一致拆分</div>
        ${split}
      </div>
    </article>`;
  };
  const transitionReport = stats.three_stage_transitions || {};
  const transitionConditions = transitionReport.conditions || {};
  const transitionTier = (tier) => {
    const item = tier || {};
    const decided = Number(item.decided || 0);
    const pushes = Number(item.pushes || 0);
    const result = item.accuracy == null
      ? '待累積'
      : `${pc(item.accuracy, 1)} (${item.hits || 0}/${decided})`;
    return `${result}${pushes ? ` · 走水 ${pushes}` : ''}`;
  };
  const transitionSections = [
    ['same_direction_line_moved', '同向改盤'],
    ['first_missing_then_stable', '首預缺向後定'],
    ['flip_then_stable', 'T-30 反向後定'],
  ].map(([key, fallback]) => {
    const condition = transitionConditions[key] || {};
    const transitionCards = codes.map((code) => {
      const market = (condition.markets || {})[code] || {};
      const tiers = ((market.aggregate || {}).tiers) || {};
      const split = (market.breakdown || []).map((item) => {
        const itemTiers = item.tiers || {};
        return `<div class="consensus-split-row">
          <b>${esc(publicText(item.label || item.key || '未分類'))}</b>
          <span>≥1.70 ${transitionTier(itemTiers.at_or_above_1_70)}<br>&lt;1.70 ${transitionTier(itemTiers.below_1_70)}</span>
        </div>`;
      }).join('');
      return `<article class="consensus-card transition-card">
        <div class="consensus-card-head"><b>${HIST_MARKET_LABEL[code]}</b></div>
        <div class="transition-aggregate">
          <span>整體 ≥1.70 ${transitionTier(tiers.at_or_above_1_70)}</span>
          <span>整體 &lt;1.70 ${transitionTier(tiers.below_1_70)}</span>
        </div>
        <div class="consensus-split" aria-label="${HIST_MARKET_LABEL[code]}${esc(publicText(condition.label || fallback))}拆分">
          <div class="consensus-split-title">分類拆分</div>${split}
        </div>
      </article>`;
    }).join('');
    return `<section class="transition-condition" aria-label="${esc(publicText(condition.label || fallback))}">
      <div class="transition-condition-head">
        <b>${esc(publicText(condition.label || fallback))}</b>
        <span>${esc(publicText(condition.definition || ''))}</span>
      </div>
      <div class="consensus-grid transition-grid">${transitionCards}</div>
    </section>`;
  }).join('');
  return `<section class="consensus-block" aria-label="首預、T-30及T-5方向一致命中率">
    <div class="consensus-ranking-block" aria-label="最高命中條件自動排名">
      <div class="stage-market-title">最高命中條件自動排名 <span>只計 T-5 賠率 ≥1.70；樣本多於 ${ranking.minimum_decided || 30} 場優先</span></div>
      <div class="consensus-ranking-grid">${rankCards || '<div class="consensus-ranking-empty">暫時未有已結算條件可排名</div>'}</div>
      <p class="consensus-ranking-note">主排名只使用有 T-5 有效賠率嘅場次；低賠結果獨立列出，不會推高主統計。命中率排名唔等於預期價值，仍要配合回報率同收市價值驗證。</p>
    </div>
    <div class="stage-market-title">三階段一致命中率 <span>首預、T-30、T-5 同方向 · 每場只計一次，以 T-5 盤口結算</span></div>
    <div class="consensus-grid">${codes.map(card).join('')}</div>
    <p class="consensus-note">主統計只計 T-5 賠率 ≥1.70；低於 1.70 獨立顯示，走水及未能評分紀錄不計入分母。</p>
    <div class="transition-block" aria-label="三階段轉向統計">
      <div class="stage-market-title">三階段轉向統計 <span>每場每市場只計一次；只計已結算、有有效 T-5 賠率嘅紀錄</span></div>
      ${transitionSections}
      <p class="consensus-note">分類顯示 ≥1.70 及 &lt;1.70；T-5 走水會保留作審計，但唔會計入命中率分母。</p>
    </div>
  </section>`;
}

function historyConsensusCards(stats) {
  const report = stats.granular_conditions || {};
  const items = report.ranking || [];
  const cards = items.map((item, index) => {
    const total = item.total || {};
    const active = item.active_evidence || {};
    const progress = item.validation_progress || item.pending_progress || {};
    const lastBatch = item.last_merged_batch || {};
    const ci = total.wilson95 || [];
    const lower = numeric(ci[0]);
    const minimumOdds = lower != null && lower > 0.03
      ? f2(1 / (lower - 0.03)) : '—';
    const conditionNumber = Number.isInteger(Number(item.condition_number))
      ? Number(item.condition_number) : index + 1;
    const pending = progress.display || `${Number(progress.pending_decided || 0)}/${Number(progress.required || 20)}`;
    const batchText = lastBatch.version
      ? (lastBatch.initial_migration_full_cohort
        ? `初始完整驗證已合併 ${lastBatch.batch_hits || 0}/${lastBatch.batch_decided || 0}`
        : `最近合併 ${lastBatch.batch_hits || 0}/${lastBatch.batch_decided || 0}（v${lastBatch.version}）`)
      : '尚未有新批次合併';
    return `<article class="granular-rank-card">
      <div class="granular-rank-head"><span>#${conditionNumber}</span><span class="granular-badge">${esc(item.badge || '觀察')}</span></div>
      <b>${esc(publicText(item.label || ''))}</b>
      <div class="granular-rate">${pc(total.accuracy, 1)}</div>
      <small>命中 ${total.hits || 0}/${total.decided || 0} · Wilson 95% ${ci.length ? `${pc(ci[0], 1)}–${pc(ci[1], 1)}` : '—'}</small>
      <small>Wilson 最低要求賠率 ${minimumOdds}</small>
      <small>活躍證據 v${active.version || '—'} · ${batchText}</small>
      <small>新前瞻待合併 ${esc(String(pending))}（已判定結果數，非命中率）</small>
      <small>歷史賠率層 ${esc(item.odds_tier || '—')} · 只使用活躍版本作日後 T-5 Wilson 閘門</small>
    </article>`;
  }).join('');
  return `<section class="granular-block" aria-label="細緻條件排名">
    <div class="stage-market-title">細緻條件排名 <span>只顯示命中率嚴格高於 60%、已判定最少 10 場</span></div>
    <div class="granular-grid">${cards || '<div class="granular-empty">暫時未有符合條件</div>'}</div>
    <p class="granular-note">走水保留審計但不計分母；10–29 場標示樣本不足。只作數據觀察，並非自動投注或下注建議。</p>
  </section>`;
}

function conditionMatchesCard(m) {
  const matches = m.condition_matches || [];
  const wilsonMatches = m.wilson_matches || [];
  if (!matches.length && !wilsonMatches.length) return '';
  return `<section class="card condition-match-card"><h2 class="card-h">條件觀察 <span class="sub">已保存 ${esc(m.stage || '')} 資料</span></h2>
    ${wilsonMatches.length ? `<div class="condition-match-list">${wilsonMatches.map(wilsonMatchText).join('')}</div>` : ''}
    ${matches.length ? `<div class="condition-match-list">${matches.map((item) => {
      return historicalConditionMatchText(item);
    }).join('')}</div>` : ''}</section>`;
}

function renderFc() {
  const V = $('#viewFc');
  const summaryPayload = DATA.prediction_history || { stats: {} };
  const payload = HISTORY.payload || summaryPayload;
  const historyTime = (row) => {
    const kickoff = Date.parse(row.kickoff || '');
    const predicted = Date.parse(row.predicted_at || '');
    return [
      Number.isFinite(kickoff) ? kickoff : Number.NEGATIVE_INFINITY,
      Number.isFinite(predicted) ? predicted : Number.NEGATIVE_INFINITY,
    ];
  };
  const rows = [...(payload.rows || [])]
    .filter((row) => HISTORY_STAGE === 'all' || row.stage === HISTORY_STAGE)
    .sort((left, right) => {
    const a = historyTime(left), b = historyTime(right);
    return b[0] - a[0] || b[1] - a[1];
    });
  const s = payload.stats || {};
  const visibleRows = rows.slice(0, HISTORY_VISIBLE);
  const accuracy = s.wdl_accuracy == null ? '待賽果' : pc(s.wdl_accuracy, 1);
  const K = [
    ['記錄賽事', s.matches || 0, ''],
    ['階段預測', s.predictions || 0, 'amber'],
    ['1X2 已評分', s.wdl_graded || 0, ''],
    ['待賽果', s.pending || 0, ''],
    ['1X2 命中', s.wdl_hits || 0, 'good'],
    ['1X2 命中率', accuracy, s.wdl_accuracy == null ? '' : s.wdl_accuracy >= .5 ? 'good' : 'bad'],
  ];
  const stageSummary = ['首預', 'T-30', 'T-5'].map((stage) => {
    const x = (s.by_stage || {})[stage] || {};
    return `<span class="hist-stage"><b>${stage}</b> ${
      x.accuracy == null ? '待累積' : `${pc(x.accuracy, 1)} (${x.hits}/${x.graded})`
    }</span>`;
  }).join('');
  const marketSummary = ['HDC', 'HIL', 'CHL'].map((code) => {
    const x = (s.by_market || {})[code] || {};
    const pushes = Number(x.pushes || 0);
    const groups = x.odds_groups || {};
    const low = groups.below_1_70 || {};
    return `<span class="hist-stage"><b>${HIST_MARKET_LABEL[code]}</b> ${
      x.accuracy == null
        ? `≥1.70 待累積 (已評分 ${x.graded || 0})`
        : `≥1.70 ${pc(x.accuracy, 1)} (命中 ${x.hits}/${x.decided} · 已評分 ${x.graded || 0}${pushes ? ` · 走水 ${pushes}` : ''})`
    }<small>＜1.70 ${low.hits || 0}/${low.decided || 0}</small></span>`;
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
  const historyFilters = `<div class="history-stage-filters" role="group" aria-label="按預測階段篩選紀錄">
    ${[['all', '全部'], ['首預', '首預'], ['T-30', 'T-30'], ['T-5', 'T-5']].map(([value, label]) =>
      `<button type="button" class="history-stage-filter ${HISTORY_STAGE === value ? 'is-on' : ''}"
        data-history-stage="${value}" aria-pressed="${HISTORY_STAGE === value}">${label}</button>`).join('')}
  </div>`;
  const historyStatus = (() => {
    if (HISTORY.state === 'loading') return '<div class="empty2" data-testid="history-loading">正在讀取完整預測紀錄…</div>';
    if (HISTORY.state === 'error') return `<div class="empty2 bad-txt" data-testid="history-error">預測紀錄讀取失敗：${esc(HISTORY.error || '未知錯誤')}。 <button type="button" class="history-refresh-btn" data-history-refresh>重新讀取</button></div>`;
    if (HISTORY.state !== 'ready') return '<div class="empty2" data-testid="history-not-loaded">開啟完整紀錄中…</div>';
    return '';
  })();
  const more = rows.length > visibleRows.length
    ? `<div class="history-more"><button type="button" class="history-more-btn" data-history-more>顯示更多</button><span>${visibleRows.length} / ${rows.length} 筆</span></div>`
    : rows.length ? `<div class="history-more"><span>已顯示全部 ${rows.length} 筆</span></div>` : '';

  V.innerHTML = `<div class="ledger-head">
    <h1 class="pg-h">純預測紀錄 <span class="sub">有冇落注都照記 · 準確率與模擬倉分開</span></h1>
    <div class="kpis wide">${K.map(([l, v, c]) =>
      `<div class="kpi"><span class="kpi-lbl">${l}</span><span class="kpi-val ${c}">${v}</span></div>`).join('')}</div>
  </div>
  <div class="card history-note">
    <div class="history-summary-label">合併總覽</div>
    <div class="history-stage-summary">${stageSummary}</div>
    <div class="history-stage-summary">${marketSummary}</div>
    ${historyStageMarketMatrix(s)}
    ${historyConsensusCards(s)}
    <p class="mx-note">合併市場數字會計首預、T-30、T-5 每個獨立快照；下表則按階段分開。正式學習樣本為當時主線，走水不計入命中率分母；未完場或未取到賽果唔當輸。</p>
  </div>
  ${historyFilters}
  <div class="card"><h2 class="card-h">${HISTORY_STAGE === 'all' ? '全部紀錄' : `${HISTORY_STAGE} 紀錄`} <span class="sub">${rows.length} 筆 · 最新開賽時間優先</span></h2>
    ${historyStatus || historyTable(visibleRows, '暫時未有預測紀錄。')}
    ${historyStatus ? '' : more}
  </div>`;
  $$('#viewFc [data-history-stage]').forEach((button) => {
    button.onclick = () => { HISTORY_STAGE = button.dataset.historyStage; HISTORY_VISIBLE = HISTORY_PAGE_SIZE; renderFc(); };
  });
  const retry = $('#viewFc [data-history-refresh]');
  if (retry) retry.onclick = () => loadHistory({ force: true });
  const showMore = $('#viewFc [data-history-more]');
  if (showMore) showMore.onclick = () => { HISTORY_VISIBLE += HISTORY_PAGE_SIZE; renderFc(); };
  if (HISTORY.state === 'idle') void loadHistory();
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
  const s = LED.stats || {}, bets = (LED.bets || []).filter((bet) =>
    bet && bet.portfolio === 'footbreak_wilson_test' && bet.strategy === 'wilson-test-strategy-v1'
  );
  const validation = LED.independent_validation || {};
  const archive = validation.historical_discovery_archive || {};
  const V = $('#viewLedger');
  const K = [
    ['啟用／切換', validation.activation_at ? hkStamp(validation.activation_at) : '—', ''],
    ['起始本金', money(s.starting_bankroll ?? LED.bankroll), ''],
    ['每注', money(s.fixed_stake), ''],
    ['每場上限', money(s.fixture_stake_cap), ''],
    ['待決', s.n_pending || 0, ''],
    ['已結算', s.n_settled || 0, ''],
    ['累計盈虧', s.n_settled ? money(s.pnl) : '—', (s.pnl || 0) >= 0 ? 'good' : 'bad'],
    ['前瞻回報率', s.roi == null ? '—' : pc(s.roi, 2), (s.roi || 0) >= 0 ? 'good' : 'bad'],
    ['命中率', s.n_decided ? `${pc(s.hit_rate, 1)} (${s.hits}/${s.n_decided})` : '—',
      s.n_decided ? ((s.hit_rate || 0) >= 0.5 ? 'good' : 'bad') : ''],
    ['前瞻 Wilson 95%', s.wilson95 ? `${pc(s.wilson95[0], 1)}–${pc(s.wilson95[1], 1)}` : '—', ''],
    ['現金 / 權益', `${money(s.cash ?? LED.bankroll)} / ${money(s.equity ?? LED.bankroll)}`, (s.pnl || 0) >= 0 ? 'good' : 'bad'],
  ];
  const rule = s.rules || {};
  let h = `<div class="ledger-head">
    <h1 class="pg-h">Wilson 測試攻略 <span class="sub">啟用後首次原生 T-5 · 只作模擬記錄</span></h1>
    <div class="kpis wide">${K.map(([l, v, c]) =>
      `<div class="kpi"><span class="kpi-lbl">${l}</span><span class="kpi-val ${c}">${v}</span></div>`).join('')}</div>
  </div>`;
  h += `<div class="card"><h2 class="card-h">建立規則</h2>
    <div class="rule-grid">
      <div><b>建立時點</b><span>只限新保存的 T-5 預測；重跑、T-30 與歷史回填不會建立注單。</span></div>
      <div><b>凍結歷史證據</b><span>最少 50 個唯一已判定 fixture-market；Wilson 95% 下限必須不少於實際賠率損益平衡率加 3 個百分點。</span></div>
      <div><b>市場與防呆</b><span>每注 HK$500；每場最多三個市場及 HK$1,500；同市場只可一注，絕不容許相反方向。</span></div>
      <div><b>資料證據</b><span>必須有有效方向、有限盤口、賠率大於 1，以及可證明在開賽前觀測的賠率。</span></div>
    </div>
  </div>`;
  h += wilsonRolloverCard(validation);
  h += crownExecutionTestCard(LED.crown_execution_test || {});
  h += `<div class="card history-note"><h2 class="card-h">已封存／退役 previous strategy（v1） <span class="sub">唯讀；保留歷史、盈虧及待決結算，不混入 Wilson 前瞻成績</span></h2>
    <p class="mx-note">摘要：已保留舊注單 ${numeric(archive.legacy_bet_count) == null ? '—' : archive.legacy_bet_count} 筆；舊帳本本金 ${archive.legacy_bankroll == null ? '—' : money(archive.legacy_bankroll)}。凍結條件會顯示當時的「歷史發現 x/y」，其後的驗證結果不會回寫該基線。</p>
    ${Array.isArray(archive.legacy_bets) && archive.legacy_bets.length ? `<details><summary>查看已封存舊注單（唯讀）</summary><div class="tbl-wrap"><table class="t"><tr><th>賽事</th><th>市場</th><th>注碼</th><th>狀態</th><th>盈虧</th></tr>${archive.legacy_bets.map((b) => `<tr><td>${esc(b.home || '—')} vs ${esc(b.away || '—')}</td><td>${esc(marketLabel(b.market || b.code))}</td><td>${money(b.stake)}</td><td>${esc(b.status || '—')}</td><td>${b.pnl == null ? '—' : money(b.pnl)}</td></tr>`).join('')}</table></div></details>` : ''}</div>`;
  const diagnostics = validation.diagnostics || {}, diagnosticLabels = diagnostics.labels || {}, diagnosticCounts = diagnostics.counts || {};
  const diagnosticRows = Object.keys(diagnosticLabels).map((code) => ({
    label: diagnosticLabels[code], count: numeric(diagnosticCounts[code]) || 0,
  })).filter((row) => row.count > 0);
  h += `<div class="card"><h2 class="card-h">建立診斷 <span class="sub">最近 ${numeric(diagnostics.window_limit) || 0} 個原生 T-5 市場評估；只顯示彙總，不含供應商原始資料</span></h2>${
    diagnosticRows.length
      ? `<div class="rule-grid">${diagnosticRows.map((row) => `<div><b>${esc(row.label)}</b><span>${row.count} 次</span></div>`).join('')}</div>`
      : '<div class="empty2">暫未有可顯示的原生 T-5 市場評估診斷。</div>'
  }</div>`;
  if (s.n_settled) h += `<div class="grid g2">${equityCard(s)}${resultCard(s)}</div>${marketCard(s)}`;
  h += oddsTierCard(s);
  h += probabilityResearchCard(LED.probability_research || {});
  if (!bets.length) {
    h += `<div class="card"><div class="empty2">尚未有符合條件的 Wilson 模擬注。系統只在首次保存的原生 T-5 評估。</div></div>`;
  } else {
    h += `<div class="card"><h2 class="card-h">Wilson 模擬注單 <span class="sub">${bets.length} 筆 · 每注 ${money(s.fixed_stake || 500)}</span></h2>
      <div class="tbl-wrap"><table class="t bets condition-bets"><thead><tr>
        <th>開賽</th><th>對賽 / 聯賽</th><th>市場</th><th>方向</th><th>盤口</th><th>賠率</th><th>歷史發現 / 獨立驗證</th><th>注碼</th><th>狀態</th><th>結果</th><th>盈虧</th>
      </tr></thead><tbody>${bets.map(conditionBetRow).join('')}</tbody></table></div></div>`;
  }
  V.innerHTML = h;
}

function crownExecutionTestCard(portfolio) {
  const s = portfolio.stats || {}, rows = (portfolio.bets || []).filter((b) =>
    b && b.portfolio === 'footbreak_crown_execution_test'
  );
  const labels = {
    crown_local_evidence_unavailable: '本機皇冠報價證據未可用',
    crown_fixture_identity_missing_or_ambiguous: '跨書賽事身份缺失或不明確',
    crown_fixture_kickoff_identity_mismatch: '跨書開賽時間身份不一致',
    crown_exact_market_side_line_missing_or_ambiguous: '皇冠相同市場／方向／盤口未有唯一報價',
    crown_execution_quote_stale_at_t5: '皇冠報價在 T-5 已過時',
    crown_wilson_gate_not_passed: '皇冠執行賠率未達 Wilson 最低要求',
    active_wilson_condition_unavailable: 'Wilson 凍結條件未可用',
    fixture_cap_reached: '每場模擬注碼上限已達',
  };
  const diagnostics = Object.entries(portfolio.rejections || {}).filter(([, n]) => numeric(n) > 0)
    .map(([reason, count]) => `<div><b>${esc(labels[reason] || '其他安全拒絕')}</b><span>${numeric(count) || 0} 次</span></div>`).join('');
  const summary = [
    ['本金／權益', `${money(s.starting_bankroll ?? 50000)} / ${money(s.equity ?? s.starting_bankroll ?? 50000)}`],
    ['盈虧／ROI', `${money(s.pnl || 0)} / ${s.roi == null ? '—' : pc(s.roi, 2)}`],
    ['待決／已結算', `${numeric(s.n_pending) || 0} / ${numeric(s.n_settled) || 0}`],
    ['命中／走水', `${numeric(s.hits) || 0}/${numeric(s.n_decided) || 0} / ${numeric(s.pushes) || 0}`],
  ];
  const result = (b) => b.result ? `<span class="respill ${RES_CLS[b.result] || ''}">${RES_LBL[b.result] || esc(b.result)}</span>` : '—';
  const status = (b) => `<span class="stpill ${String(b.status || '').toLowerCase()}">${ST_LBL[b.status] || esc(b.status || '—')}</span>`;
  return `<section class="card" data-testid="footbreak-crown-execution-test">
    <h2 class="card-h">足破×皇冠執行測試倉（模擬） <span class="sub">獨立帳本；不混入足破 Wilson、皇冠 Wilson 或 Radar</span></h2>
    <p class="mx-note">「訊號賠率層」只用馬會原生 T-5 歷史條件分層；「執行最低賠率」只以同一市場、方向及亞洲盤口的皇冠實際執行賠率判定。例：馬會 &lt;1.70 而皇冠最低要求 1.80，必須皇冠報價 ≥1.80 才會建立模擬注。</p>
    <div class="kpis wide">${summary.map(([l, v]) => `<div class="kpi"><span class="kpi-lbl">${l}</span><span class="kpi-val">${v}</span></div>`).join('')}</div>
    ${rows.length ? `<div class="tbl-wrap"><table class="t bets"><thead><tr><th>開賽／對賽</th><th>市場／方向／盤口</th><th>訊號賠率層（馬會）</th><th>皇冠執行賠率</th><th>執行最低賠率</th><th>條件</th><th>注碼</th><th>狀態／結果／盈虧</th></tr></thead><tbody>${rows.map((b) => {
      const admission = b.wilson_admission || {};
      return `<tr><td class="mono nowrap">${hkDay(b.kickoff)} ${hkClock(b.kickoff)}<div class="cell-sub">${esc(b.home || '—')} vs ${esc(b.away || '—')} · ${esc(leagueDisplay(b.league || '—'))}</div></td>
        <td>${esc(marketLabel(b.market_label || b.market))}<div class="cell-sub">${esc(publicText(b.selected_role || '—'))} ${numeric(b.selected_line) == null ? '—' : numeric(b.selected_line)}</div></td>
        <td class="mono">${f2(b.hkjc_signal_odds)}</td><td class="mono">${f2(b.crown_execution_odds)}</td>
        <td class="mono">${f2(admission.minimum_acceptable_odds_raw)}</td><td>條件 #${numeric(b.condition_number) == null ? '—' : Math.trunc(numeric(b.condition_number))}</td>
        <td>${money(b.stake)}</td><td>${status(b)}<div class="cell-sub">${result(b)} · ${b.pnl == null ? '—' : money(b.pnl)}</div></td></tr>`;
    }).join('')}</tbody></table></div>` : '<div class="empty2">尚未有已提交的足破×皇冠模擬注；缺少、新鮮度不足或身份不明確的本機證據會安全拒絕。</div>'}
    <h3 class="sub-h">安全拒絕診斷（彙總）</h3>${diagnostics ? `<div class="rule-grid">${diagnostics}</div>` : '<div class="empty2">暫未有拒絕紀錄。</div>'}
  </section>`;
}

function wilsonRolloverCard(validation) {
  const rollover = validation?.rollover || {};
  const rows = Object.values(rollover.conditions || {}).filter((row) => row && typeof row === 'object')
    .sort((a, b) => (numeric(a.condition_number) || 999999) - (numeric(b.condition_number) || 999999));
  if (!rows.length) return `<section class="card"><h2 class="card-h">Wilson 證據版本</h2><div class="empty2">尚未有已凍結條件；新結果不會以舊驗證紀錄追溯補入。</div></section>`;
  return `<section class="card" data-testid="wilson-evidence-rollover"><h2 class="card-h">Wilson 證據版本 <span class="sub">每個完全相同條件獨立累積；20 個新已判定結果才建立新版本</span></h2>
    <div class="tbl-wrap"><table class="t"><thead><tr><th>條件</th><th>有效證據</th><th>Wilson / 最低賠率</th><th>最近合併</th><th>下一批進度</th></tr></thead><tbody>${rows.map((row) => {
      const active = row.active_evidence || {}, last = row.last_merged_batch || {}, pending = row.pending_progress || {};
      const version = numeric(active.version) == null ? '—' : `v${Math.trunc(numeric(active.version))}`;
      const total = `${numeric(active.cumulative_hits) || 0}/${numeric(active.cumulative_decided) || 0}`;
      const lower = pc(active.wilson95_lower_raw, 1);
      const minimum = numeric(active.minimum_acceptable_odds_display) == null ? f2(active.minimum_acceptable_odds_raw) : String(active.minimum_acceptable_odds_display);
      const batch = numeric(last.batch_decided) ? `${numeric(last.batch_hits) || 0}/${numeric(last.batch_decided)}${last.initial_migration_full_cohort ? '（初始完整驗證 cohort）' : ''}` : '—';
      const progress = pending.display || `${numeric(pending.eligible_decided) || 0}/${numeric(pending.required) || 20}`;
      return `<tr><td>條件 #${numeric(row.condition_number) == null ? '—' : Math.trunc(numeric(row.condition_number))}</td><td>${esc(version)} · ${esc(total)}</td><td>下限 ${esc(lower)} · 最低 ${esc(minimum)}</td><td>${esc(batch)}</td><td><b>${esc(progress)}</b></td></tr>`;
    }).join('')}</tbody></table></div>
    <p class="mx-note">「命中 x/y」是已判定命中率，不是批次進度；批次只看最右欄 x/20。公開面板只顯示不可逆 fixture-market 摘要，不顯示供應商或賽事 ID。</p></section>`;
}

function probabilityResearchCard(research) {
  const report = research.stats || {}, markets = report.markets || {}, artifact = research.evidence_artifact || {};
  const coverage = artifact.coverage || {}, excluded = coverage.excluded || {};
  const artifactStamp = artifact.generated_at ? hkStamp(artifact.generated_at) : '未有證據';
  const exclusionText = Object.keys(excluded).length
    ? Object.keys(excluded).map((key) => `${esc(key)} ${numeric(excluded[key]) || 0}`).join(' · ')
    : '暫未有排除紀錄';
  const cell = (m, variant) => {
    const row = (m || {})[variant] || {}, gate = (m || {}).promotion || {};
    const unavailable = row.available === false;
    return `<tr><td>${variant === 'exact_only' ? '只用完全相同條件' : '分層收縮'}</td>
      <td>${numeric(row.unique_fixtures) == null ? '未有證據' : row.unique_fixtures}</td>
      <td>${pc(row.roi, 2)}</td><td>${row.wilson95 ? `${pc(row.wilson95[0], 1)}–${pc(row.wilson95[1], 1)}` : '未有證據'}</td>
      <td>${pc(row.weighted_break_even, 1)}</td><td>${f3(row.brier)}</td><td>${f3(row.log_loss)}</td>
      <td>${unavailable ? '未有證據' : (row.calibration == null ? '未有證據' : pc(row.calibration, 1))}</td>
      <td>${unavailable ? '未有證據' : 'CLV 尚未有證據'}</td>
      <td>${gate.blocked ? '已阻擋（需人手覆核）' : '只可人手覆核'}</td></tr>`;
  };
  const bodies = Object.keys(markets).map((market) => {
    const m = markets[market];
    return `<h3 class="sub-h">${esc(marketLabel(market))}</h3><div class="tbl-wrap"><table class="t"><thead><tr><th>變體</th><th>獨立賽事</th><th>ROI</th><th>Wilson 95%</th><th>加權損益兩平</th><th>Brier</th><th>Log Loss</th><th>校準</th><th>CLV 覆蓋</th><th>晉級</th></tr></thead><tbody>${cell(m,'exact_only')}${cell(m,'hierarchical_shrunk')}</tbody></table></div>`;
  }).join('');
  return `<section class="card history-note" data-testid="footbreak-probability-research">
    <h2 class="card-h">機率驗證研究 <span class="sub">研究中／非正式推介</span></h2>
    <p class="mx-note">只收啟用界線後首次原生賽前 T-5，按同一 fixture＋市場計樣本。exact-only 與分層經驗貝葉斯收縮共用同批賽事作前瞻 ablation；缺少凍結賽前證據一律顯示「未有證據」，不是 0。固定每注 HK$250、每場最多 HK$500；不用 Kelly、不發 Telegram、不自動升級，亦不會改寫獨立驗證倉、舊注單或盈虧。</p>
    <p class="mx-note">保守晉級門檻：每市場至少 100 場（200 較佳）、ROI 正、Wilson 下限高於加權損益兩平 3 個百分點、Brier／Log Loss 不差過市場基準、CLV 覆蓋達門檻且平均 CLV 非負；任何必要指標未有證據即阻擋。</p>
    <div class="rule-grid" data-testid="footbreak-probability-evidence">
      <div><b>Evidence artifact</b><span>${artifact.available ? `已驗證 · ${esc(artifactStamp)} · 接納 ${numeric(coverage.accepted_rows) || 0} 行` : `未有證據：${esc(artifact.reason || 'artifact 不可用')}`}</span></div>
      <div><b>來源／邊界</b><span>${artifact.available ? `${esc(artifact.source || '—')}；source boundary ${esc(hkStamp(artifact.source_boundary_at))}` : '不會用 ranking aggregate、舊 validation 或回補資料代替。'}</span></div>
      <div><b>按市場 coverage</b><span>${artifact.available ? Object.keys(coverage.by_market || {}).map((k) => `${esc(marketLabel(k))} ${numeric(coverage.by_market[k]) || 0}`).join(' · ') || '未有證據' : '未有證據'}</span></div>
      <div><b>排除原因（aggregate）</b><span>${exclusionText}</span></div>
    </div>
    ${bodies || '<div class="empty2">尚未有前瞻研究列；不會以舊驗證倉或回補資料代替。</div>'}
  </section>`;
}

function conditionBetRow(b) {
  const result = b.result ? `<span class="respill ${RES_CLS[b.result] || ''}">${RES_LBL[b.result] || b.result}</span>` : '<span class="dim">—</span>';
  const frozen = (LED.independent_validation?.conditions || {})[b.frozen_condition_signature] || {};
  const prospective = frozen.prospective || {};
  const evidence = b.frozen_historical_evidence || frozen.historical_evidence || {};
  const a = b.wilson_admission || frozen.admission_arithmetic || {};
  const active = frozen.active_evidence || {};
  const history = numeric(a.wilson95_lower_raw) == null ? '—' : `凍結歷史 ${evidence.hits ?? 0}/${evidence.decided ?? 0} · ${pc(a.hit_rate_raw, 1)}<div class="cell-sub">入場版本 v${numeric(b.evidence_version) == null ? '—' : Math.trunc(numeric(b.evidence_version))}；最低可接受賠率 ${f2(a.minimum_acceptable_odds_raw)} · 目前 ${f2(a.actual_decimal_odds_raw)}</div><div class="cell-sub">有效 v${numeric(active.version) == null ? '—' : Math.trunc(numeric(active.version))} · ${active.cumulative_hits ?? 0}/${active.cumulative_decided ?? 0} · Wilson 下限 ${pc(active.wilson95_lower_raw, 1)} · 最低 ${numeric(active.minimum_acceptable_odds_display) == null ? f2(active.minimum_acceptable_odds_raw) : active.minimum_acceptable_odds_display}</div><div class="cell-sub">前瞻 ${pc(prospective.hit_rate, 1)} (${prospective.hits ?? 0}/${prospective.decided ?? 0}) · Wilson ${prospective.wilson95 ? `${pc(prospective.wilson95[0], 1)}–${pc(prospective.wilson95[1], 1)}` : '—'} · ROI ${pc(prospective.roi, 2)}</div>`;
  const status = `<span class="stpill ${String(b.status || '').toLowerCase()}">${ST_LBL[b.status] || b.status || '—'}</span>`;
  const pnl = b.pnl == null ? '—' : money(b.pnl);
  const tone = (b.pnl || 0) > 0 ? 'ev-p' : (b.pnl || 0) < 0 ? 'ev-n' : 'dim';
  return `<tr class="brow ${String(b.status || '').toLowerCase()}">
    <td class="mono nowrap">${hkDay(b.kickoff)} ${hkClock(b.kickoff)}</td>
    <td><b>${esc(b.home || '—')} <span class="dim">vs</span> ${esc(b.away || '—')}</b><div class="cell-sub">${esc(leagueDisplay(b.league || '—'))}</div></td>
    <td class="lbl">${esc(marketLabel(b.market_label))}<div class="cell-sub">條件 #${numeric(b.condition_number) == null ? '—' : Math.trunc(numeric(b.condition_number))}</div></td>
    <td><b>${esc(publicText(b.selected_role || '—'))}</b></td>
    <td class="mono">${numeric(b.selected_line) == null ? '—' : numeric(b.selected_line).toString()}</td>
    <td class="mono">${f2(b.odds)}</td>
    <td class="mono">${history}</td>
    <td class="stk">${money(b.stake)}</td><td>${status}</td><td>${result}</td>
    <td class="${tone}">${pnl}</td>
  </tr>`;
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


/* ── 獨立驗證賠率分層 ── */
function oddsTierCard(s) {
  const report = s.odds_tiers || {};
  const received = Array.isArray(report.tiers) ? report.tiers : [];
  const expected = [
    ['1.70-1.79', '1.70–1.79'], ['1.80-1.89', '1.80–1.89'],
    ['1.90-1.99', '1.90–1.99'], ['2.00-plus', '≥2.00'],
  ];
  const byKey = Object.fromEntries(received.map((row) => [row.key, row]));
  const tiers = expected.map(([key, label]) => byKey[key] || {
    key, label, n_bets: 0, n_settled: 0, n_decided: 0, hits: 0,
    pushes: 0, pnl: 0, roi: null, hit_rate: null, wilson95: null, by_market: [],
  });
  const diagnostics = report.excluded_diagnostics || {};
  const split = (tier) => {
    const markets = Array.isArray(tier.by_market) ? tier.by_market : [];
    if (!markets.length) return '';
    return `<tr class="odds-tier-split"><td colspan="6"><b>${esc(tier.label || '—')} · 按市場</b>
      <div class="tbl-wrap"><table class="t odds-tier-market-table"><tr><th>市場</th><th>注數／已決定</th><th>命中率</th><th>實際盈虧</th><th>ROI</th><th>Wilson 95%</th></tr>
      ${markets.map((m) => `<tr><td class="lbl">${esc(marketLabel(m.market))}</td>
        <td class="mono">${m.n_bets || 0}／${m.n_decided || 0}</td>
        <td class="mono">${m.n_decided ? `${pc(m.hit_rate, 1)} (${m.hits || 0}/${m.n_decided})` : '—'}</td>
        <td class="mono ${(m.pnl || 0) > 0 ? 'ev-p' : (m.pnl || 0) < 0 ? 'ev-n' : 'dim'}">${money(m.pnl)}</td>
        <td class="mono ${m.roi == null ? 'dim' : m.roi >= 0 ? 'ev-p' : 'ev-n'}">${pc(m.roi, 2)}</td>
        <td class="mono">${Array.isArray(m.wilson95) ? `${pc(m.wilson95[0], 1)}–${pc(m.wilson95[1], 1)}` : '—'}</td></tr>`).join('')}
      </table></div></td></tr>`;
  };
  const excluded = [
    ['低於 1.70', diagnostics.below_1_70],
    ['無效／缺失賠率', diagnostics.invalid_or_missing_odds],
  ].filter(([, count]) => Number(count) > 0)
    .map(([label, count]) => `${label} ${count} 注`).join('；');
  return `<div class="card odds-tier-card"><h2 class="card-h">賠率分層統計
      <span class="sub">只計前瞻獨立驗證倉有效注單／賽果；走水不計入命中率分母</span></h2>
    <div class="tbl-wrap"><table class="t odds-tier-table" aria-label="獨立驗證倉賠率分層統計">
      <tr><th>賠率層</th><th>注數／已決定</th><th>命中率</th><th>實際盈虧</th><th>ROI</th><th>Wilson 95%</th></tr>
      ${tiers.map((tier) => `<tr><td class="lbl">${esc(tier.label || '—')}</td>
        <td class="mono">${tier.n_bets || 0}／${tier.n_decided || 0}</td>
        <td class="mono">${tier.n_decided ? `${pc(tier.hit_rate, 1)} (${tier.hits || 0}/${tier.n_decided})` : '—'}</td>
        <td class="mono ${(tier.pnl || 0) > 0 ? 'ev-p' : (tier.pnl || 0) < 0 ? 'ev-n' : 'dim'}">${money(tier.pnl)}</td>
        <td class="mono ${tier.roi == null ? 'dim' : tier.roi >= 0 ? 'ev-p' : 'ev-n'}">${pc(tier.roi, 2)}</td>
        <td class="mono">${Array.isArray(tier.wilson95) ? `${pc(tier.wilson95[0], 1)}–${pc(tier.wilson95[1], 1)}` : '—'}</td></tr>${split(tier)}`).join('')}
    </table></div>
    <p class="mx-note">「注數」包括待決及已結算有效注；「已決定」只包括非走水的已結算注。實際盈虧及 ROI 以已結算注的實際亞洲盤盈虧／投注額計算。${excluded ? ` ${esc(excluded)}只作內部排除診斷，絕不混入上述四層。` : ''}</p>
  </div>`;
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

/* ══════════════════════ 資料健康 · 完整率及錯誤分層 ══════════════════════ */
/* 純讀取 data-health.json 嘅唯讀診斷面板。呢度唔會改任何預測、結算、落注、
 * 注碼或通知;報告本身亦係唯讀生成,唔會重訓、唔會自動套用。
 * 主要樣本永遠係「獨立賽事」;階段列只作參考,唔可以當獨立樣本。 */
const HEALTH_SYSTEM = 'footbreak';
const HEALTH_FILE = 'data-health.json';
const HEALTH_STALE_HOURS = 26;
const HEALTH_MIN_FIXTURES = 30;
const HEALTH_MARKETS = ['HDC', 'HIL', 'CHL'];
const HEALTH_DIMENSIONS = [
  { id: 'market', label: '市場' },
  { id: 'stage', label: '階段' },
  { id: 'league', label: '聯賽' },
  { id: 'direction', label: '方向' },
  { id: 'confidence', label: '信念' },
];
const HEALTH_FILTER_DIMENSIONS = ['market', 'stage', 'league', 'direction'];
const HEALTH_STATUS = {
  ok: { text: '資料健康', cls: 'review' },
  watch: { text: '有待留意', cls: 'hold' },
  degraded: { text: '資料有缺口', cls: 'bad' },
  insufficient_data: { text: '樣本未夠', cls: 'wait' },
  no_data: { text: '未有資料', cls: 'wait' },
  unavailable: { text: '資料源不可用', cls: 'bad' },
};
const HEALTH_SEVERITY = { high: '嚴重', warn: '注意', info: '參考' };
const HEALTH_ISSUE_LABEL = {
  post_kickoff_quarantined_rows: '開賽後才寫入的預測(已隔離)',
  malformed_payload_rows: '無法解析的預測內容',
  nonfinite_prediction_values: 'NaN／無限值欄位',
  duplicate_market_keys_in_stage: '同一階段重複市場鍵',
  invalid_stage_rows: '階段值不合法',
  malformed_market_prediction_rows: '格式錯誤的市場預測列',
  stage_rows_without_market_predictions: '冇市場預測的階段列',
  unsupported_market_rows: '非讓球／入球大細／角球大細市場列',
  stale_unresolved_results: '過寬限期仍然冇賽果',
  stale_missing_corner_results: '超過重試期仍然缺角球賽果',
  missing_corner_results: '缺角球賽果(仍在重試期)',
  missing_probability: '缺失或不合法機率',
  missing_line: '缺失盤口線',
  missing_odds: '缺失賠率',
  missing_selection_side: '缺失選擇方向',
  missing_source: '缺失資料來源',
  missing_provider: '缺失供應商',
  missing_league: '缺失聯賽',
  missing_stage: '缺失階段',
};
const HEALTH_MISSING_LABEL = {
  probability: '機率', line: '盤口線', odds: '賠率', selection_side: '方向',
  league: '聯賽', stage: '階段', source: '來源', provider: '供應商',
  result: '賽果', corner_total: '角球賽果',
};
let HEALTH = { state: 'idle', payload: null, error: '', loadedAt: null };
/* unit:'primary' = 每場每市場最新階段(消除同一場嘅重複階段列);
 * unit:'all_stages' = 全部階段列(彼此相關,只作參考)。
 * 兩者嘅指標單位都係「已結算預測列」,獨立賽事只係樣本量基礎。 */
let HEALTH_FILTER = {
  market: 'all', stage: 'all', league: 'all', direction: 'all',
  sample: 'all', unit: 'primary',
};
const HEALTH_UNIT_LABEL = {
  primary: '主要診斷:每場每市場最新階段',
  all_stages: '全部階段列(相關,只作參考)',
};
const HEALTH_UNIT_NOTE = {
  primary: '每場每市場每個方向只取最新賽前階段(T-5 > T-30 > 首預),'
    + '所以冇同一場嘅重複階段列。指標單位仍然係已結算預測列。',
  all_stages: '包含同一場嘅首預／T-30／T-5,呢啲係高度相關嘅重複量度,'
    + '唔可以當獨立樣本;只作參考。',
};

/* 按目前單位揀返對應嘅切面同基準。primary 缺失時安全回退到全部階段列,
 * 但標籤會照實講返用咗邊個單位,唔會扮成 primary。 */
function healthSliceSource(payload) {
  const primary = healthIsPlainObject(payload.primary_diagnostic)
    ? payload.primary_diagnostic : null;
  const wantsPrimary = HEALTH_FILTER.unit !== 'all_stages';
  if (wantsPrimary && primary && healthIsPlainObject(primary.error_slices)) {
    return {
      unit: 'primary',
      slices: primary.error_slices,
      baseline: healthIsPlainObject(primary.baseline) ? primary.baseline : {},
      available: true,
    };
  }
  return {
    unit: 'all_stages',
    slices: healthIsPlainObject(payload.error_slices) ? payload.error_slices : {},
    baseline: healthIsPlainObject(payload.baseline) ? payload.baseline : {},
    available: !wantsPrimary || !primary,
    fellBack: wantsPrimary && !primary,
  };
}

/* 一句講清指標單位,放喺每個分層表上面,避免有人當佢係每場一行。 */
function healthUnitCaption(source) {
  const baseline = source.baseline || {};
  const correlated = baseline.correlated_stage_rows === true
    || (source.unit === 'all_stages');
  return `<p class="health-unit-note" data-testid="note-health-metric-unit"
      data-metric-unit="${source.unit === 'primary'
        ? 'graded_prediction_rows_latest_stage_per_fixture_market'
        : 'graded_prediction_rows'}"
      data-correlated-stage-rows="${correlated}">
    指標單位:<b>已結算預測列</b>(命中率／Brier／對數損失都係逐行相加),
    <b>唔係每場一行</b>;獨立賽事只係樣本量基礎同 ≥${HEALTH_MIN_FIXTURES} 場門檻依據。
    目前單位:<b>${esc(HEALTH_UNIT_LABEL[source.unit])}</b>——${esc(HEALTH_UNIT_NOTE[source.unit])}
    ${correlated ? '<b class="bad-txt">同一場有多個相關階段列,唔可以當獨立樣本。</b>' : ''}
    ${source.fellBack ? '<b class="bad-txt">報告未有主要診斷區塊,已回退到全部階段列。</b>' : ''}
  </p>`;
}

function healthIsPlainObject(value) {
  return !!value && typeof value === 'object' && !Array.isArray(value);
}
function healthInt(value) {
  const parsed = numeric(value);
  return parsed == null ? 0 : Math.round(parsed);
}
function healthPct(value) {
  return numeric(value) == null ? '—' : pc(value, 1);
}
function healthStatusLabel(status) {
  return HEALTH_STATUS[status] || { text: status ? String(status) : '未知狀態', cls: 'hold' };
}
function healthIssueLabel(issue) {
  return issue.label || HEALTH_ISSUE_LABEL[issue.code] || String(issue.code || '未知問題');
}
function healthValidate(payload) {
  if (!healthIsPlainObject(payload)) return '報告格式不符(唔係物件)';
  if (payload.report !== 'data_health') return '報告類型不符';
  if (payload.system !== HEALTH_SYSTEM) return `報告唔係 ${HEALTH_SYSTEM} 系統`;
  if (!healthIsPlainObject(payload.policy)) return '報告缺少 policy 欄位';
  if (!healthIsPlainObject(payload.completeness)) return '報告缺少完整率欄位';
  if (payload.status !== 'unavailable' && !healthIsPlainObject(payload.error_slices)) {
    return '報告缺少錯誤分層欄位';
  }
  return '';
}
function healthAgeHours(payload) {
  const stamp = payload && payload.generated_at;
  if (!stamp) return null;
  const at = new Date(stamp);
  if (!Number.isFinite(at.getTime())) return null;
  return (Date.now() - at.getTime()) / 3600000;
}
function healthStamp(value) {
  const at = new Date(value);
  if (!Number.isFinite(at.getTime())) return '—';
  return at.toLocaleString('zh-HK', {
    month: '2-digit', day: '2-digit', hour: '2-digit', minute: '2-digit',
    hour12: false, timeZone: 'Asia/Hong_Kong',
  });
}

async function loadHealth(options) {
  const opts = options || {};
  if (HEALTH.state === 'loading') return HEALTH.state;
  const previous = HEALTH;
  HEALTH = { state: 'loading', payload: previous.payload, error: '', loadedAt: previous.loadedAt };
  if (!opts.quiet && VIEW === 'health') renderHealth();
  try {
    // 每次都加時間戳 + no-store,「重新讀取」一定攞到最新一份報告。
    const response = await fetch(`${HEALTH_FILE}?v=${Date.now()}`, { cache: 'no-store' });
    if (response.status === 404) {
      HEALTH = { state: 'missing', payload: null, error: '', loadedAt: Date.now() };
    } else if (!response.ok) {
      HEALTH = { state: 'error', payload: null, error: `HTTP ${response.status}`, loadedAt: Date.now() };
    } else {
      let payload = null;
      try {
        payload = await response.json();
      } catch (parseError) {
        HEALTH = { state: 'error', payload: null, error: '報告唔係有效 JSON', loadedAt: Date.now() };
        if (VIEW === 'health') renderHealth();
        return HEALTH.state;
      }
      const invalid = healthValidate(payload);
      HEALTH = invalid
        ? { state: 'error', payload: null, error: invalid, loadedAt: Date.now() }
        : { state: 'ready', payload, error: '', loadedAt: Date.now() };
    }
  } catch (e) {
    HEALTH = { state: 'error', payload: null, error: e.message || '讀取失敗', loadedAt: Date.now() };
  }
  if (VIEW === 'health') renderHealth();
  return HEALTH.state;
}

function healthReadOnlyNote() {
  return `<div class="shadow-note health-note" role="note" data-testid="note-health-readonly">
    <strong>唯讀診斷</strong>
    <span>資料健康報告只做讀取同統計:<b>唔會改動任何預測、賽果、結算、注碼、模擬倉或通知</b>,
    亦<b>唔會重訓、唔會自動套用</b>。主要樣本係<b>獨立賽事(按場計)</b>;
    階段列(首預／T-30／T-5)只作參考,同一場<b>唔可以當三場</b>。</span></div>`;
}

function healthHead(extra) {
  return `<div class="ledger-head"><div class="ledger-title-row">
      <h1 class="pg-h">資料健康 <span class="sub">完整率及錯誤分層 · 唯讀診斷</span></h1>
      <button class="settle-btn" id="healthReload" type="button">重新讀取</button>
    </div>
    ${healthReadOnlyNote()}${extra || ''}</div>`;
}

function healthKpi(label, value, sub, tone) {
  return `<div class="health-kpi ${tone || ''}">
    <span class="health-kpi-lbl">${esc(label)}</span>
    <b class="health-kpi-val mono">${value}</b>
    <span class="health-kpi-sub">${sub == null ? '' : esc(sub)}</span>
  </div>`;
}

function healthKpiRow(payload) {
  const overall = ((payload.completeness || {}).overall) || {};
  const result = overall.result || {};
  const corner = overall.corner_result || {};
  const baseline = payload.baseline || {};
  const counts = payload.issue_counts || {};
  const resultTone = numeric(result.coverage) != null && numeric(result.coverage) < 0.98 ? 'bad' : '';
  const cornerTone = numeric(corner.coverage) != null && numeric(corner.coverage) < 0.9 ? 'bad' : '';
  return `<div class="health-kpis" data-testid="kpis-health">
    ${healthKpi('獨立賽事(主要樣本)', healthInt(overall.unique_fixtures), '同一場只算一場')}
    ${healthKpi('階段列(只作參考)', healthInt(overall.stage_rows), '首預／T-30／T-5')}
    ${healthKpi('市場預測列(只作參考)', healthInt(overall.prediction_rows), '每場每階段每方向')}
    ${healthKpi('賽果覆蓋率', healthPct(result.coverage), `過寬限期 ${healthInt(result.settle_due_fixtures)} 場`, resultTone)}
    ${healthKpi('角球賽果覆蓋率', healthPct(corner.coverage), `角球大細場次 ${healthInt(corner.corner_prediction_fixtures)}`, cornerTone)}
    ${healthKpi('已評估獨立賽事(樣本量)', healthInt(baseline.unique_fixtures),
      `整體命中率 ${healthPct(baseline.accuracy)} · 由 ${healthInt(baseline.graded_rows)} 條已結算預測列相加`)}
    ${healthKpi('資料問題', healthInt(counts.total), `嚴重 ${healthInt(counts.high)} · 注意 ${healthInt(counts.warn)}`,
      healthInt(counts.high) ? 'bad' : '')}
  </div>`;
}

function healthOptions(values, selected) {
  return values.map((item) =>
    `<option value="${esc(item.value)}"${item.value === selected ? ' selected' : ''}>${esc(item.text)}</option>`
  ).join('');
}

function healthFilterControls(payload) {
  const source = healthSliceSource(payload);
  const slices = source.slices || {};
  const build = (dimension) => {
    const items = Array.isArray(slices[dimension]) ? slices[dimension] : [];
    return [{ value: 'all', text: '全部' }].concat(items.map((item) => ({
      value: String(item.key),
      text: `${item.label || item.key}(${healthInt(item.unique_fixtures)} 場)`,
    })));
  };
  const labels = { market: '市場', stage: '階段', league: '聯賽', direction: '方向' };
  const selects = HEALTH_FILTER_DIMENSIONS.map((dimension) => `
    <label class="health-filter-field">
      <span class="health-filter-lbl">${labels[dimension]}</span>
      <select class="health-select" data-health-filter="${dimension}"
        data-testid="select-health-${dimension}">${healthOptions(build(dimension), HEALTH_FILTER[dimension])}</select>
    </label>`).join('');
  return `<div class="health-filter" role="group" aria-label="資料健康篩選" data-testid="filter-health">
    <div class="health-filter-field health-unit-field">
      <span class="health-filter-lbl">指標單位</span>
      <div class="health-sample-btns">
        <button type="button" class="chal-filter-btn ${source.unit === 'primary' ? 'active' : ''}"
          data-health-unit="primary" data-testid="button-health-unit-primary"
          aria-pressed="${source.unit === 'primary'}">每場每市場最新階段</button>
        <button type="button" class="chal-filter-btn ${source.unit === 'all_stages' ? 'active' : ''}"
          data-health-unit="all_stages" data-testid="button-health-unit-all-stages"
          aria-pressed="${source.unit === 'all_stages'}">全部階段列(參考)</button>
      </div>
    </div>
    ${selects}
    <div class="health-filter-field">
      <span class="health-filter-lbl">樣本</span>
      <div class="health-sample-btns">
        <button type="button" class="chal-filter-btn ${HEALTH_FILTER.sample === 'all' ? 'active' : ''}"
          data-health-sample="all" data-testid="button-health-sample-all"
          aria-pressed="${HEALTH_FILTER.sample === 'all'}">全部</button>
        <button type="button" class="chal-filter-btn ${HEALTH_FILTER.sample === 'sufficient' ? 'active' : ''}"
          data-health-sample="sufficient" data-testid="button-health-sample-sufficient"
          aria-pressed="${HEALTH_FILTER.sample === 'sufficient'}">只睇樣本足夠(≥${HEALTH_MIN_FIXTURES} 場)</button>
      </div>
    </div>
    <button type="button" class="chal-filter-btn health-reset" data-health-reset="1"
      data-testid="button-health-reset">清除篩選</button>
  </div>`;
}

function healthAppliedFilters() {
  const labels = { market: '市場', stage: '階段', league: '聯賽', direction: '方向' };
  const parts = HEALTH_FILTER_DIMENSIONS
    .filter((dimension) => HEALTH_FILTER[dimension] !== 'all')
    .map((dimension) => `${labels[dimension]}:${HEALTH_FILTER[dimension]}`);
  if (HEALTH_FILTER.sample === 'sufficient') parts.push(`樣本:≥${HEALTH_MIN_FIXTURES} 場`);
  const unit = HEALTH_FILTER.unit === 'all_stages' ? 'all_stages' : 'primary';
  return `<div class="health-applied" data-testid="applied-health-filters">
    <span class="health-applied-lbl">生效篩選</span>
    <span class="health-applied-val">${parts.length ? esc(parts.join(' · ')) : '全部(未篩選)'}</span>
    <span class="health-applied-unit" data-testid="applied-health-unit">單位:${esc(HEALTH_UNIT_LABEL[unit])}</span>
    <span class="health-applied-note">切面係單維度彙總,篩選只影響顯示,唔會交叉相乘。</span>
  </div>`;
}

function healthSliceVisible(dimension, item) {
  if (HEALTH_FILTER.sample === 'sufficient' && item.sample_status !== 'sufficient') return false;
  const selected = HEALTH_FILTER[dimension];
  if (!selected || selected === 'all') return true;
  return String(item.key) === String(selected);
}

function healthCell(label, value, extraClass) {
  return `<span class="health-cell ${extraClass || ''}" data-label="${esc(label)}">${value}</span>`;
}

function healthPublicText(value) {
  return String(value ?? '')
    .replace(/\bHDC\b/g, '讓球')
    .replace(/\bHIL\b/g, '入球大細')
    .replace(/\bCHL\b/g, '角球大細')
    .replace(/\b[ABC](?:→[ABC])+\b/g, '方向變化');
}

function healthSliceRow(item) {
  const insufficient = item.sample_status !== 'sufficient';
  const ci = Array.isArray(item.accuracy_ci95)
    ? `${pc(item.accuracy_ci95[0], 0)}–${pc(item.accuracy_ci95[1], 0)}`
    : '—';
  return `<div class="health-row ${insufficient ? 'is-small' : ''}"
      data-testid="row-health-slice-${esc(item.dimension)}-${esc(item.key)}">
    ${healthCell('切面', `<b>${esc(healthPublicText(item.label || item.key))}</b>${insufficient
      ? '<span class="health-flag" data-testid="flag-health-small-sample">樣本不足</span>' : ''}`, 'health-cell-key')}
    ${healthCell('獨立賽事(樣本量)', healthInt(item.unique_fixtures))}
    ${healthCell('預測列(指標單位)', `${healthInt(item.graded_rows == null ? item.rows : item.graded_rows)}${
      item.correlated_stage_rows === true
        ? '<span class="health-flag" data-testid="flag-health-correlated">相關階段列</span>' : ''}`)}
    ${healthCell('已判定列', healthInt(item.decided_rows))}
    ${healthCell('命中', healthInt(item.hits))}
    ${healthCell('和局退款', healthInt(item.pushes))}
    ${healthCell('命中率', insufficient ? '<span class="dim">樣本不足</span>' : healthPct(item.accuracy))}
    ${healthCell('Wilson 95%', insufficient ? '—' : ci)}
    ${healthCell('Brier', insufficient ? '—' : f3(item.brier))}
    ${healthCell('對數損失', insufficient ? '—' : f3(item.log_loss))}
  </div>`;
}

function healthSliceHead() {
  return `<div class="health-row health-row-head" aria-hidden="true">
    <span class="health-cell health-cell-key">切面</span>
    <span class="health-cell">獨立賽事(樣本量)</span>
    <span class="health-cell">預測列(指標單位)</span>
    <span class="health-cell">已判定列</span>
    <span class="health-cell">命中</span>
    <span class="health-cell">和局退款</span>
    <span class="health-cell">命中率</span>
    <span class="health-cell">Wilson 95%</span>
    <span class="health-cell">Brier</span>
    <span class="health-cell">對數損失</span>
  </div>`;
}

function healthSlicesSection(payload) {
  const source = healthSliceSource(payload);
  const slices = source.slices || {};
  const blocks = HEALTH_DIMENSIONS.map((dimension) => {
    const items = (Array.isArray(slices[dimension.id]) ? slices[dimension.id] : [])
      .filter((item) => healthSliceVisible(dimension.id, item));
    if (!items.length) return '';
    return `<div class="card health-card" data-testid="card-health-slices-${dimension.id}">
      <h2 class="card-h">${dimension.label}分層 <span class="sub">${items.length} 個切面</span></h2>
      <div class="health-table">${healthSliceHead()}${items.map(healthSliceRow).join('')}</div>
    </div>`;
  }).filter(Boolean).join('');
  const caption = healthUnitCaption(source);
  if (!blocks) {
    return `<h2 class="health-section-h">錯誤分層 <span class="sub">獨立賽事係樣本量基礎,指標單位係已結算預測列</span></h2>
      ${caption}<div class="card" data-testid="state-health-slices-empty"><div class="empty2">
      <b>今次篩選冇任何切面</b><span>可以㩒「清除篩選」睇返全部切面。</span></div></div>`;
  }
  return `<h2 class="health-section-h">錯誤分層 <span class="sub">獨立賽事係樣本量基礎,指標單位係已結算預測列</span></h2>
    ${caption}${blocks}`;
}

function healthIssuesSection(payload) {
  // This renderer is also exercised as a standalone module in the dashboard
  // smoke test, so keep the user-facing market mapping local rather than
  // relying on the page-level helper.
  const healthMarketLabel = (market) => ({
    HDC: '讓球', HIL: '入球大細', CHL: '角球大細',
  })[market] || market;
  const marketFilter = HEALTH_FILTER.market;
  const issues = (Array.isArray(payload.issues) ? payload.issues : []).filter((issue) => {
    if (marketFilter === 'all') return true;
    const scope = String(issue.scope || '');
    return scope === 'overall' || scope === `market:${marketFilter}`;
  });
  const overall = ((payload.completeness || {}).overall) || {};
  const byMarket = ((payload.completeness || {}).by_market) || {};
  const markets = HEALTH_MARKETS.filter((market) =>
    (marketFilter === 'all' || marketFilter === market) && byMarket[market]);
  const marketCards = markets.map((market) => {
    const item = byMarket[market] || {};
    const missing = item.missing_or_invalid || {};
    const chips = Object.keys(HEALTH_MISSING_LABEL)
      .filter((field) => healthInt(missing[field]) > 0)
      .map((field) =>
        `<span class="health-chip bad">${HEALTH_MISSING_LABEL[field]} ${healthInt(missing[field])}</span>`)
      .join('') || '<span class="health-chip good">冇缺失欄位</span>';
    return `<div class="card health-card" data-testid="card-health-market-${market}">
      <h2 class="card-h">${healthMarketLabel(market)}</h2>
      <div class="health-grid">
        <div><span class="health-grid-lbl">獨立賽事</span><b class="mono">${healthInt(item.unique_fixtures)}</b></div>
        <div><span class="health-grid-lbl">階段列(參考)</span><b class="mono">${healthInt(item.stage_rows)}</b></div>
        <div><span class="health-grid-lbl">預測列(參考)</span><b class="mono">${healthInt(item.prediction_rows)}</b></div>
        <div><span class="health-grid-lbl">已結算</span><b class="mono">${healthInt(item.graded_rows)}</b></div>
        <div><span class="health-grid-lbl">待結算</span><b class="mono">${healthInt(item.pending_rows)}</b></div>
        <div><span class="health-grid-lbl">不適用</span><b class="mono">${healthInt(item.excluded_rows)}</b></div>
        <div><span class="health-grid-lbl">賽果覆蓋</span><b class="mono">${healthPct((item.result || {}).coverage)}</b></div>
        <div><span class="health-grid-lbl">角球覆蓋</span><b class="mono">${healthPct((item.corner_result || {}).coverage)}</b></div>
      </div>
      <div class="health-chips">${chips}</div>
    </div>`;
  }).join('');
  const issueRows = issues.length
    ? issues.map((issue) => `<div class="health-row health-issue sev-${esc(issue.severity)}"
        data-testid="row-health-issue-${esc(issue.code)}">
      ${healthCell('嚴重度', `<span class="health-sev ${esc(issue.severity)}">${HEALTH_SEVERITY[issue.severity] || esc(issue.severity)}</span>`, 'health-cell-key')}
      ${healthCell('問題', esc(healthIssueLabel(issue)))}
      ${healthCell('範圍', esc(issue.scope === 'overall' ? '整體' : String(issue.scope || '—')))}
      ${healthCell('數量', healthInt(issue.count))}
      ${healthCell('說明', esc(issue.detail || '—'))}
    </div>`).join('')
    : `<div class="empty2" data-testid="state-health-no-issues">呢個篩選冇偵測到完整率問題。</div>`;
  const dup = healthInt(overall.duplicate_stage_keys);
  const quarantined = healthInt(overall.quarantined_post_kickoff_rows);
  return `<h2 class="health-section-h">完整率問題 <span class="sub">重複階段鍵 ${dup} · 開賽後隔離列 ${quarantined}</span></h2>
    <div class="card health-card" data-testid="card-health-issues">
      <h2 class="card-h">偵測到的問題 <span class="sub">${issues.length} 項</span></h2>
      <div class="health-table">${issueRows}</div>
    </div>${marketCards}`;
}

function healthRecommendationsSection(payload) {
  const diagnostics = payload.hil_v4_diagnostics || {};
  const all = Array.isArray(diagnostics.recommendations) ? diagnostics.recommendations : [];
  const items = all.filter((item) => {
    if (item.kind !== 'weak_slice') return true;
    const evidence = item.evidence || {};
    const dimension = String(evidence.dimension || '');
    if (!HEALTH_FILTER_DIMENSIONS.includes(dimension)) return true;
    return healthSliceVisible(dimension, evidence);
  });
  const families = Array.isArray(diagnostics.feature_families) ? diagnostics.feature_families : [];
  const familyRows = families.map((family) => `<div class="health-row"
      data-testid="row-health-family-${esc(family.id)}">
    ${healthCell('特徵族', `<b>${esc(family.label || family.id)}</b>${family.critical ? '<span class="health-flag">關鍵</span>' : ''}`, 'health-cell-key')}
    ${healthCell('覆蓋率', healthPct(family.coverage))}
    ${healthCell('有值列', `${healthInt(family.present_rows)} / ${healthInt(family.rows)}`)}
  </div>`).join('');
  const cards = items.length
    ? items.map((item) => `<div class="card health-card health-rec prio-${esc(item.priority)}"
        data-testid="card-health-rec-${esc(item.id)}">
      <h2 class="card-h">${esc(item.title || item.id)}
        <span class="chal-badge ${item.priority === 'high' ? 'hold' : 'wait'}">${item.priority === 'high' ? '優先' : '次要'}</span></h2>
      <p class="health-rec-detail">${esc(item.detail || '')}</p>
    </div>`).join('')
    : `<div class="card" data-testid="state-health-no-rec"><div class="empty2">
        <b>暫時冇合資格建議</b><span>樣本少於 ${HEALTH_MIN_FIXTURES} 場獨立賽事的切面唔會用嚟做任何建議。</span></div></div>`;
  return `<h2 class="health-section-h">入球大細 v4 診斷建議 <span class="sub">只係診斷,唔係模型</span></h2>
    <div class="card health-card health-rec-note" data-testid="note-health-hil-v4">
      <b>唔會自動套用、唔會重訓</b>
      <span>呢節只指出缺失特徵族同穩定表現最弱嘅切面,供人手判斷。
      所有觀察只係<strong>關聯,並非因果</strong>;樣本不足嘅切面永遠唔會產生建議。
      <b data-testid="note-health-rec-unit">證據一律取自「每場每市場最新階段」主要診斷</b>——
      同一場嘅重複階段列彼此相關,<strong>絕對唔會當作獨立證據</strong>;
      ≥${HEALTH_MIN_FIXTURES} 場門檻仍然以獨立賽事數計算。
      自動套用:<b class="bad-txt">否</b>。</span>
    </div>
    <div class="card health-card" data-testid="card-health-families">
      <h2 class="card-h">入球大細特徵族覆蓋率 <span class="sub">低覆蓋 = 資料缺口,唔等於原因</span></h2>
      <div class="health-table">${familyRows || '<div class="empty2">冇特徵族資料。</div>'}</div>
    </div>
    ${cards}`;
}

function renderHealth() {
  const V = $('#viewHealth');
  if (!V) return;
  if (HEALTH.state === 'idle' || (HEALTH.state === 'loading' && !HEALTH.payload)) {
    V.innerHTML = healthHead() +
      `<div class="card"><div class="empty2" data-testid="state-health-loading">正在讀取資料健康報告…</div></div>`;
    healthBind();
    return;
  }
  if (HEALTH.state === 'missing') {
    V.innerHTML = healthHead() +
      `<div class="card"><div class="empty2" data-testid="state-health-missing">
        報告未生成。伺服器每日至少重寫一次(結算週期亦會重生),第一次生成之前唔會有檔案。</div></div>`;
    healthBind();
    return;
  }
  if (HEALTH.state === 'error') {
    V.innerHTML = healthHead() +
      `<div class="card"><div class="empty2 bad-txt" data-testid="state-health-error">
        報告讀取失敗:${esc(HEALTH.error || '未知錯誤')}。可以㩒「重新讀取」再試。</div></div>`;
    healthBind();
    return;
  }
  const payload = HEALTH.payload || {};
  const ageHours = healthAgeHours(payload);
  const stale = ageHours != null && ageHours > HEALTH_STALE_HOURS;
  const label = healthStatusLabel(payload.status);
  let banner = `<div class="chal-stamp ${stale ? 'is-stale' : ''}" data-testid="stamp-health">
      <span class="chal-badge ${label.cls}" data-testid="status-health">${esc(label.text)}</span>
      <span>報告時間 <b class="mono">${healthStamp(payload.generated_at)} HKT</b></span>
      <span>${ageHours == null ? '時間不明' : `距今 ${ageHours < 1 ? '不足 1' : Math.floor(ageHours)} 小時`}</span>
      ${stale ? '<span class="chal-stale-flag" data-testid="flag-health-stale">報告過期,未見最新一次伺服器重生</span>' : ''}
    </div>`;
  if (payload.status === 'unavailable') {
    V.innerHTML = healthHead(banner) +
      `<div class="card"><div class="empty2 bad-txt" data-testid="state-health-unavailable">
        資料源不可用(${esc(payload.status_reason || '未知原因')})。報告唔會憑空推算,亦唔會顯示舊數。</div></div>`;
    healthBind();
    return;
  }
  if (payload.status === 'insufficient_data' || payload.status === 'no_data') {
    banner += `<div class="chal-review chal-review-top health-warn" data-testid="banner-health-insufficient">
      <b>整體樣本未夠</b>
      <span>已評估獨立賽事少於 ${HEALTH_MIN_FIXTURES} 場,所有命中率／Brier 只作觀察,<strong>唔應該用嚟落任何結論</strong>。</span></div>`;
  }
  V.innerHTML = healthHead(banner) +
    healthKpiRow(payload) +
    healthFilterControls(payload) +
    healthAppliedFilters() +
    healthIssuesSection(payload) +
    healthSlicesSection(payload) +
    healthRecommendationsSection(payload);
  healthBind();
}

function healthBind() {
  const button = $('#healthReload');
  if (button) button.onclick = () => loadHealth({});
  document.querySelectorAll('[data-health-filter]').forEach((select) => {
    select.onchange = () => {
      HEALTH_FILTER[select.dataset.healthFilter] = select.value;
      renderHealth();
    };
  });
  document.querySelectorAll('[data-health-unit]').forEach((element) => {
    element.onclick = () => {
      HEALTH_FILTER.unit = element.dataset.healthUnit === 'all_stages' ? 'all_stages' : 'primary';
      renderHealth();
    };
  });
  document.querySelectorAll('[data-health-sample]').forEach((element) => {
    element.onclick = () => {
      HEALTH_FILTER.sample = element.dataset.healthSample === 'sufficient' ? 'sufficient' : 'all';
      renderHealth();
    };
  });
  document.querySelectorAll('[data-health-reset]').forEach((element) => {
    element.onclick = () => {
      HEALTH_FILTER = {
        market: 'all', stage: 'all', league: 'all', direction: 'all',
        sample: 'all', unit: 'primary',
      };
      renderHealth();
    };
  });
}

/* ══════════════════════ 挑戰模型 · 隔離影子研究 ══════════════════════ */

/* 純讀取條件研究資料。此報告與既有挑戰模型、模擬倉、
 * 結算、注碼及通知完全分離；只呈現凍結後的前瞻條件診斷。 */
const CONDITION_FILE = 'shadow-condition-report.json';
const CONDITION_SYSTEM = 'footbreak';
const CONDITION_ID = 'footbreak_hil_t5_under';
const CONDITION_STALE_HOURS = 1;
let CONDITION = { state: 'idle', payload: null, error: '' };
function conditionObject(value) { return value && typeof value === 'object' && !Array.isArray(value); }
function conditionValidate(payload) {
  if (!conditionObject(payload) || payload.report !== 'shadow_conditions') return '報告格式不符';
  if (payload.system !== CONDITION_SYSTEM || payload.condition_id !== CONDITION_ID) return '報告系統或條件不符';
  if (!conditionObject(payload.condition) || !conditionObject(payload.condition.metrics)) return '報告缺少條件指標';
  return '';
}
function conditionAge(value) {
  const date = new Date(value || '');
  return Number.isFinite(date.getTime()) ? (Date.now() - date.getTime()) / 3600000 : null;
}
async function loadCondition(options) {
  const opts = options || {};
  CONDITION = { ...CONDITION, state: 'loading', error: '' };
  if (!opts.quiet && VIEW === 'condition') renderCondition();
  try {
    const response = await fetch(`${CONDITION_FILE}?v=${Date.now()}`, { cache: 'no-store' });
    if (response.status === 404) { CONDITION = { state: 'missing', payload: null, error: '' }; return; }
    if (!response.ok) throw new Error(`HTTP ${response.status}`);
    const payload = await response.json();
    const invalid = conditionValidate(payload);
    CONDITION = invalid ? { state: 'error', payload: null, error: invalid } : { state: 'ready', payload, error: '' };
  } catch (error) {
    CONDITION = { state: 'error', payload: null, error: error.message || '讀取失敗' };
  } finally {
    if (VIEW === 'condition') renderCondition();
  }
}
function conditionKpi(label, value, sub) {
  return `<div class="condition-kpi"><span>${esc(label)}</span><b class="mono">${value}</b><small>${esc(sub || '')}</small></div>`;
}
function renderCondition() {
  const V = $('#viewCondition'); if (!V) return;
  const head = `<div class="ledger-head"><div><h1>條件研究報告</h1><p class="dim">只作報告 / 不自動套用</p></div><button class="settle-btn" id="conditionReload" type="button">重新讀取</button></div>`;
  if (CONDITION.state === 'idle' || CONDITION.state === 'loading') {
    V.innerHTML = head + '<div class="card"><div class="empty2" data-testid="state-condition-loading">正在讀取條件研究報告…</div></div>';
  } else if (CONDITION.state === 'missing') {
    V.innerHTML = head + '<div class="card"><div class="empty2" data-testid="state-condition-missing">報告尚未生成；不會回填凍結前歷史。</div></div>';
  } else if (CONDITION.state === 'error') {
    V.innerHTML = head + `<div class="card"><div class="empty2 bad-txt" data-testid="state-condition-error">${esc(CONDITION.error)}</div></div>`;
  } else {
    const payload = CONDITION.payload, report = payload.condition, metrics = report.metrics || {}, counts = metrics.counts || {}, progress = report.progress || {};
    const age = conditionAge(payload.generated_at), stale = age != null && age > CONDITION_STALE_HOURS;
    const qualified = numeric(progress.decided_unique_fixtures) || 0, required = numeric(progress.required_unique_decided_fixtures) || 100;
    const pct = Math.min(100, qualified * 100 / required);
    V.innerHTML = head + `<div class="shadow-note condition-note" data-testid="note-condition-isolation"><strong>只作報告 / 不自動套用</strong><span>完全隔離：唔會改機率、推介、模擬倉、注碼、Telegram 或模型升級。</span></div>
      <section class="card condition-card" data-testid="card-shadow-condition-${CONDITION_SYSTEM}">
        <h2 class="card-h">${esc(report.condition || '')} <span class="chal-badge ${report.status === 'human_review_ready' ? 'go' : 'wait'}" data-testid="status-shadow-condition">${report.status === 'human_review_ready' ? '可供人手覆核' : '收集中／樣本不足'}</span></h2>
        <p class="condition-copy">${esc(report.qualification || '')}；每場只算一次，並且只取凍結截點後開賽的不可變賽前列。已合資格 ${numeric(progress.qualified_unique_fixtures) || 0} 場；進度只計已判定場。</p>
        <div class="chal-progress"><div class="chal-progress-top"><span>前瞻獨立賽事進度</span><b class="mono">${qualified} / ${required}</b></div><div class="chal-bar"><i style="width:${pct.toFixed(1)}%"></i></div><div class="chal-progress-foot"><span>凍結截點 ${esc(hkStamp(report.freeze_cutoff))} HKT</span><span>${stale ? '報告過期' : '報告最新'}</span></div></div>
        <div class="condition-grid">
          ${conditionKpi('命中率', pc(metrics.hit_rate, 1), `命中 ${numeric(counts.hits) || 0} / 已判定 ${numeric(counts.decided) || 0}`)}
          ${conditionKpi('ROI', pc(metrics.roi, 2), metrics.roi_reason || '使用實際選邊賽前賠率')}
          ${conditionKpi('CLV', sg(metrics.clv, 3), metrics.clv_reason || '同市場／方向／盤口收盤價')}
          ${conditionKpi('Brier', f3(metrics.brier), metrics.brier_reason || '賽前機率對不可變結算目標')}
        </div>
        <div class="condition-counts"><span>走水／退款 ${numeric(counts.pushes_refunds) || 0}</span><span>半贏 ${numeric(counts.half_won) || 0}</span><span>半輸 ${numeric(counts.half_lost) || 0}</span><span>賽果不可用 ${numeric(counts.outcome_unavailable) || 0}</span><span>選邊賠率缺失 ${numeric(counts.missing_selected_direction_odds) || 0}</span><span>同向收盤價缺失 ${numeric(counts.missing_same_direction_closing_quote) || 0}</span></div>
        <p class="condition-copy"><b>指標定義：</b>命中率排除退款／走水及未有賽果；ROI 只在所有已判定場有實際所選方向的賽前賠率才顯示；CLV 只在每場保存同市場、同方向、同盤口收盤價才顯示；Brier 用保存的賽前機率及結算目標（半贏 .75、退款 .5、半輸 .25）。</p>
      </section>`;
  }
  const reload = $('#conditionReload'); if (reload) reload.onclick = () => loadCondition({});
}

/* 純讀取 challenger-status.json。呢度唔會改任何預測、結算、落注、
 * 訓練或帳目邏輯；候選模型永遠唔會自動套用。 */
const CHALLENGER_SYSTEM = 'footbreak';
const CHALLENGER_FILE = 'challenger-status.json';
const CHALLENGER_MARKETS = ['HDC', 'HIL', 'CHL'];
const CHALLENGER_REQUIRED_FIXTURES = 100;
const CHALLENGER_STALE_HOURS = 26;   // 每日 12:20 HKT 跑一次,超過一日即視為過期
const CHAL_STATUS = {
  insufficient_data: { text: '樣本未夠', cls: 'wait' },
  insufficient_chronological_partition: { text: '時序切分未夠', cls: 'wait' },
  tested_no_safe_upgrade: { text: '已測試 · 未達升級門檻', cls: 'hold' },
  candidate_passed_human_review_required: { text: '候選通過 · 等人手覆核', cls: 'review' },
};
const CHAL_REASON = {
  minimum_eligible_fixtures: '合資格賽事未夠 100 場',
  minimum_train_or_holdout_fixtures: '訓練／驗證場次未夠(需 70／30)',
  minimum_holdout_fixtures: '驗證場次未夠 30 場',
  identical_holdout_rows: '冠軍同挑戰者驗證樣本唔一致',
  meaningful_brier_improvement: 'Brier 改善未夠 0.01',
  log_loss_improved: '對數損失冇改善',
  accuracy_not_materially_worse: '準確率跌幅超過 2%',
};
let CHAL = { state: 'idle', payload: null, error: '', loadedAt: null };
let CHAL_FILTER = 'all';

function challengerStatusLabel(status) {
  return CHAL_STATUS[status] || { text: status ? String(status) : '未知狀態', cls: 'hold' };
}
function challengerReasonLabel(reason) {
  return CHAL_REASON[reason] || String(reason);
}
function challengerIsPlainObject(value) {
  return !!value && typeof value === 'object' && !Array.isArray(value);
}
function challengerValidate(payload) {
  if (!challengerIsPlainObject(payload)) return '報告格式不符(唔係物件)';
  if (!challengerIsPlainObject(payload.policy)) return '報告缺少 policy 欄位';
  const system = challengerIsPlainObject(payload.systems) ? payload.systems[CHALLENGER_SYSTEM] : null;
  if (!challengerIsPlainObject(system)) return `報告缺少 ${CHALLENGER_SYSTEM} 系統結果`;
  if (!challengerIsPlainObject(system.tests)) return '報告缺少市場測試結果';
  return '';
}
function challengerAgeHours(payload) {
  const stamp = payload && payload.generated_at;
  if (!stamp) return null;
  const at = new Date(stamp);
  if (!Number.isFinite(at.getTime())) return null;
  return (Date.now() - at.getTime()) / 3600000;
}
function challengerStamp(value) {
  const at = new Date(value);
  if (!Number.isFinite(at.getTime())) return '—';
  return at.toLocaleString('zh-HK', {
    month: '2-digit', day: '2-digit', hour: '2-digit', minute: '2-digit',
    hour12: false, timeZone: 'Asia/Hong_Kong',
  });
}

async function loadChallenger(options) {
  const opts = options || {};
  if (CHAL.state === 'loading') return CHAL.state;
  const previous = CHAL;
  CHAL = { state: 'loading', payload: previous.payload, error: '', loadedAt: previous.loadedAt };
  if (!opts.quiet && VIEW === 'chal') renderChallenger();
  try {
    // 每次都加時間戳 + no-store,「更新」掣一定攞到最新一份報告。
    const response = await fetch(`${CHALLENGER_FILE}?v=${Date.now()}`, { cache: 'no-store' });
    if (response.status === 404) {
      CHAL = { state: 'missing', payload: null, error: '', loadedAt: Date.now() };
    } else if (!response.ok) {
      CHAL = { state: 'error', payload: null, error: `HTTP ${response.status}`, loadedAt: Date.now() };
    } else {
      let payload = null;
      try {
        payload = await response.json();
      } catch (parseError) {
        CHAL = { state: 'error', payload: null, error: '報告唔係有效 JSON', loadedAt: Date.now() };
        if (VIEW === 'chal') renderChallenger();
        return CHAL.state;
      }
      const invalid = challengerValidate(payload);
      CHAL = invalid
        ? { state: 'error', payload: null, error: invalid, loadedAt: Date.now() }
        : { state: 'ready', payload, error: '', loadedAt: Date.now() };
    }
  } catch (e) {
    CHAL = { state: 'error', payload: null, error: e.message || '讀取失敗', loadedAt: Date.now() };
  }
  if (VIEW === 'chal') renderChallenger();
  return CHAL.state;
}

function challengerIsolationNote() {
  return `<div class="shadow-note chal-note" role="note" data-testid="note-challenger-isolation">
    <strong>隔離研究</strong>
    <span>挑戰模型完全隔離,只做離線訓練同回溯評估:<b>永不自動套用</b>,唔會改動任何預測機率、選項、注碼、模擬倉或通知。
    就算通過全部安全門檻,亦<b>只係等人手覆核</b>,未套用。</span></div>`;
}

function challengerHead(extra) {
  return `<div class="ledger-head"><div class="ledger-title-row">
      <h1 class="pg-h">挑戰模型 <span class="sub">Challenger · 每日 12:20 HKT 離線評估</span></h1>
      <button class="settle-btn" id="challengerReload" type="button">重新讀取</button>
    </div>
    ${challengerIsolationNote()}${extra || ''}</div>`;
}

function challengerMetricRow(label, championValue, challengerValue, deltaValue, lowerIsBetter, formatter) {
  const delta = numeric(deltaValue);
  let tone = '';
  if (delta != null && Math.abs(delta) > 1e-9) {
    const better = lowerIsBetter ? delta < 0 : delta > 0;
    tone = better ? 'good' : 'bad';
  }
  const deltaText = delta == null ? '—' : (delta > 0 ? '+' : '') + formatter(delta);
  return `<div class="chal-metric">
    <span class="chal-metric-lbl">${label}</span>
    <span class="chal-metric-val" data-role="champion">${formatter(championValue)}</span>
    <span class="chal-metric-val" data-role="challenger">${formatter(challengerValue)}</span>
    <span class="chal-metric-val chal-delta ${tone}">${deltaText}</span>
  </div>`;
}

function challengerFilterControls(tests) {
  const reviewCount = CHALLENGER_MARKETS.filter((market) =>
    String((tests[market] || {}).status || '') === 'candidate_passed_human_review_required'
  ).length;
  return `<div class="chal-filter" role="group" aria-label="挑戰模型篩選" data-testid="filter-challenger">
    <span class="chal-filter-lbl">顯示</span>
    <button type="button" class="chal-filter-btn ${CHAL_FILTER === 'all' ? 'active' : ''}"
      data-chal-filter="all" data-testid="button-challenger-filter-all"
      aria-pressed="${CHAL_FILTER === 'all'}">全部 <b>${CHALLENGER_MARKETS.length}</b></button>
    <button type="button" class="chal-filter-btn ${CHAL_FILTER === 'review' ? 'active' : ''}"
      data-chal-filter="review" data-testid="button-challenger-filter-review"
      aria-pressed="${CHAL_FILTER === 'review'}">已通過／待覆核 <b>${reviewCount}</b></button>
  </div>`;
}

function challengerMarketCard(market, test) {
  const name = MKT[market] || market;
  if (!challengerIsPlainObject(test)) {
    return `<div class="card chal-card" data-testid="card-challenger-${market}">
      <h2 class="card-h">${name}</h2>
      <div class="empty2">今次報告冇呢個市場嘅結果。</div></div>`;
  }
  const status = String(test.status || '');
  const label = challengerStatusLabel(status);
  const eligible = numeric(test.eligible_fixtures) || 0;
  const required = numeric(test.required_fixtures) || CHALLENGER_REQUIRED_FIXTURES;
  const remaining = test.remaining_fixtures == null
    ? Math.max(0, required - eligible)
    : (numeric(test.remaining_fixtures) || 0);
  const pctDone = Math.max(0, Math.min(100, required > 0 ? (eligible / required) * 100 : 0));
  const evaluated = test.champion || test.challenger || test.holdout_fixtures != null;
  const reasons = Array.isArray(test.rejection_reasons) ? test.rejection_reasons : [];
  const champion = (test.champion && test.champion.metrics) || {};
  const challenger = (test.challenger && test.challenger.metrics) || {};
  const delta = test.delta || {};
  const reviewing = status === 'candidate_passed_human_review_required';

  let body = `<div class="chal-progress" data-testid="progress-challenger-${market}">
      <div class="chal-progress-top">
        <span>合資格獨立賽事(按場計,唔係按紀錄行數)</span>
        <b class="mono">${eligible} / ${required}</b>
      </div>
      <div class="chal-bar"><i style="width:${pctDone.toFixed(1)}%"></i></div>
      <div class="chal-progress-foot">
        <span class="dim">已達 ${pctDone.toFixed(0)}%</span>
        <span class="${remaining > 0 ? 'amber-txt' : 'good-txt'}">${remaining > 0 ? `仲差 ${remaining} 場先夠評估` : '已夠場次評估'}</span>
      </div>
      ${numeric(test.eligible_rows) == null ? '' : `<div class="chal-rows dim">對應紀錄行數 ${numeric(test.eligible_rows)} 行(只作參考)</div>`}
    </div>`;

  if (evaluated) {
    body += `<div class="chal-split">
        <div><span class="chal-split-lbl">訓練場次</span><b class="mono">${numeric(test.train_fixtures) == null ? '—' : numeric(test.train_fixtures)}</b></div>
        <div><span class="chal-split-lbl">驗證場次(holdout)</span><b class="mono">${numeric(test.holdout_fixtures) == null ? '—' : numeric(test.holdout_fixtures)}</b></div>
        <div><span class="chal-split-lbl">驗證樣本</span><b class="mono">${numeric(challenger.n) == null ? '—' : numeric(challenger.n)}</b></div>
      </div>
      <div class="chal-metrics" data-testid="metrics-challenger-${market}">
        <div class="chal-metric chal-metric-head">
          <span class="chal-metric-lbl">指標</span><span>現行冠軍</span><span>挑戰者</span><span>差距</span>
        </div>
        ${challengerMetricRow('準確率', champion.accuracy, challenger.accuracy, delta.accuracy, false, (x) => pc(x, 1))}
        ${challengerMetricRow('Brier', champion.brier, challenger.brier, delta.brier, true, (x) => f3(x))}
        ${challengerMetricRow('對數損失', champion.log_loss, challenger.log_loss, delta.log_loss, true, (x) => f3(x))}
      </div>
      <p class="chal-hint dim">Brier 同對數損失越低越好,準確率越高越好;差距 = 挑戰者 − 冠軍。</p>`;
  }

  if (reasons.length) {
    body += `<div class="chal-reasons"><span class="chal-reasons-lbl">未能升級原因</span>
      ${reasons.map((reason) => `<span class="chal-reason">${esc(challengerReasonLabel(reason))}</span>`).join('')}</div>`;
  } else if (reviewing) {
    body += `<div class="chal-review" data-testid="banner-challenger-review-${market}">
      <b>通過全部安全門檻</b>
      <span>此刻<strong>未套用、亦唔會自動套用</strong>,只係等人手覆核決定。</span></div>`;
  }

  body += `<div class="chal-foot"><span>自動套用:<b class="bad-txt">否</b></span>
    <span>獨立研究,唔影響現行預測</span></div>`;

  return `<div class="card chal-card ${reviewing ? 'is-review' : ''}" data-testid="card-challenger-${market}">
    <h2 class="card-h">${name}
      <span class="chal-badge ${label.cls}" data-testid="status-challenger-${market}">${esc(label.text)}</span></h2>
    ${body}</div>`;
}

function renderChallenger() {
  const V = $('#viewChal');
  if (!V) return;
  if (CHAL.state === 'idle' || (CHAL.state === 'loading' && !CHAL.payload)) {
    V.innerHTML = challengerHead() +
      `<div class="card"><div class="empty2" data-testid="state-challenger-loading">正在讀取挑戰模型報告…</div></div>`;
    challengerBind();
    return;
  }
  if (CHAL.state === 'missing') {
    V.innerHTML = challengerHead() +
      `<div class="card"><div class="empty2" data-testid="state-challenger-missing">
        報告未生成。挑戰模型每日 12:20 HKT 先評估一次,第一次評估之前唔會有檔案。</div></div>`;
    challengerBind();
    return;
  }
  if (CHAL.state === 'error') {
    V.innerHTML = challengerHead() +
      `<div class="card"><div class="empty2 bad-txt" data-testid="state-challenger-error">
        報告讀取失敗:${esc(CHAL.error || '未知錯誤')}。可以㩒「重新讀取」再試。</div></div>`;
    challengerBind();
    return;
  }
  const payload = CHAL.payload || {};
  const system = (payload.systems || {})[CHALLENGER_SYSTEM] || {};
  const tests = system.tests || {};
  const ageHours = challengerAgeHours(payload);
  const stale = ageHours != null && ageHours > CHALLENGER_STALE_HOURS;
  const reviewRequired = system.review_required === true;
  let banner = `<div class="chal-stamp ${stale ? 'is-stale' : ''}" data-testid="stamp-challenger">
      <span>報告時間 <b class="mono">${challengerStamp(payload.generated_at)} HKT</b></span>
      <span>${ageHours == null ? '時間不明' : `距今 ${ageHours < 1 ? '不足 1' : Math.floor(ageHours)} 小時`}</span>
      ${stale ? '<span class="chal-stale-flag" data-testid="flag-challenger-stale">報告過期,未見最新一次每日評估</span>' : ''}
    </div>`;
  if (reviewRequired) {
    banner += `<div class="chal-review chal-review-top" data-testid="banner-challenger-review">
      <b>有候選模型通過安全門檻</b>
      <span>仍然<strong>未套用</strong>,亦唔會自動套用;只係等人手覆核決定。</span></div>`;
  }
  const visibleMarkets = CHALLENGER_MARKETS.filter((market) =>
    CHAL_FILTER === 'all' ||
    String((tests[market] || {}).status || '') === 'candidate_passed_human_review_required'
  );
  const cards = visibleMarkets.length
    ? `<div class="chal-grid">${visibleMarkets.map((market) => challengerMarketCard(market, tests[market])).join('')}</div>`
    : `<div class="card chal-filter-empty" data-testid="state-challenger-filter-empty">
        <div class="empty2"><b>暫時冇模型等待覆核</b><span>新候選通過全部安全門檻後,會自動出現喺呢個篩選。</span></div>
      </div>`;
  V.innerHTML = challengerHead(banner) +
    challengerFilterControls(tests) + cards;
  challengerBind();
}

function challengerBind() {
  const button = $('#challengerReload');
  if (button) button.onclick = () => loadChallenger({});
  document.querySelectorAll('[data-chal-filter]').forEach((filterButton) => {
    filterButton.onclick = () => {
      CHAL_FILTER = filterButton.dataset.chalFilter === 'review' ? 'review' : 'all';
      renderChallenger();
    };
  });
}

boot();
