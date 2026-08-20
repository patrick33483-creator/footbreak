/* 足破 · 皇冠賽事預測終端
 * 介面及預測流程沿用 HKJC 足破原版，只替換 HDC/HIL 盤源。
 */
'use strict';

const $  = (s, r = document) => r.querySelector(s);
const $$ = (s, r = document) => [...r.querySelectorAll(s)];
const numeric = (x) => (x == null || x === '' || !Number.isFinite(Number(x)) ? null : Number(x));
const pc = (x, d = 1) => {
  const n = numeric(x);
  return n == null ? '—' : (n * 100).toFixed(d) + '%';
};
const sg = (x, d = 2) => {
  const n = numeric(x);
  return n == null ? '—' : (n >= 0 ? '+' : '') + n.toFixed(d);
};
const f2 = (x) => {
  const n = numeric(x);
  return n == null ? '—' : n.toFixed(2);
};
const f3 = (x) => {
  const n = numeric(x);
  return n == null ? '—' : n.toFixed(3);
};
const money = (x) => {
  const n = numeric(x);
  return n == null ? '—' : '$' + Math.round(n).toLocaleString('en-US');
};
const esc = (s) => String(s ?? '').replace(/[&<>"']/g, (c) =>
  ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c]));
const DASHBOARD_FETCH_TIMEOUT_MS = Math.max(
  250,
  Math.min(15000, numeric(window.__CROWN_FETCH_TIMEOUT_MS__) || 6000),
);

const TAG = { 'T-5': 'tag-t5', 'T-30': 'tag-t30', '首預': 'tag-t60', '待入窗': 'tag-wait', '已開賽': 'tag-none' };
const STAGE_DESC = {
  '首預': '每晚 23:59 掃全板 · 參考初盤同開盤結構',
  'T-30': '開賽前 30 分鐘 · 陣容、傷患出咗,賠率漸定',
  'T-5': '開賽前約 10 分鐘起 · 唯一落注時點',
};
const VD_CLS = { '落注': 'v-go', '傾向': 'v-lean', '偏向': 'v-soft', '已預測': 'v-lean', '觀望': 'v-wait', '無傾向': 'v-none' };
const MKT = { HDC: '讓球', HIL: '入球大細', CHL: '角球大細', HAD: '主客和' };
const marketLabel = (value) => {
  const raw = String(value ?? '').trim();
  const code = raw.toUpperCase();
  if (MKT[code]) return MKT[code];
  if (raw === 'HKJC角球大細' || raw === '皇冠角球大細') return MKT.CHL;
  if (raw === '皇冠讓球') return MKT.HDC;
  return raw || '—';
};
// Historic artifacts may predate the Chinese-label contract.  Convert those
// stored labels at the display boundary without changing canonical codes.
const publicText = (value) => String(value ?? '')
  .replace(/\bHDC\b/g, '讓球')
  .replace(/\bHIL\b/g, '入球大細')
  .replace(/\bCHL\b/g, '角球大細')
  .replace(/\b[ABC](?:→[ABC])+\b/g, '方向變化');

function selectedMarketLine(prediction) {
  const code = String(prediction?.code || prediction?.market || '').toUpperCase();
  const side = String(prediction?.side || '').toUpperCase();
  const rawLine = prediction?.line ?? prediction?.condition;
  if (rawLine == null || String(rawLine).trim() === '') return null;
  const line = Number(rawLine);
  if (!Number.isFinite(line)) return null;
  return code === 'HDC' && side === 'A' ? -line : line;
}

function chinesePredictionLabel(prediction) {
  if (!prediction) return '無方向';
  const code = String(prediction.code || prediction.market || '').toUpperCase();
  const side = String(prediction.side || '').toUpperCase();
  const rawLine = prediction.line ?? prediction.condition;
  const line = selectedMarketLine(prediction);
  const lineText = Number.isFinite(line) ? historyQuarterLine(line, code === 'HDC') : String(rawLine || '').trim();
  if (code === 'HDC') return `讓球 ${side === 'H' ? '主隊' : side === 'A' ? '客隊' : '選擇'}${lineText ? ` ${lineText}` : ''}`;
  if (code === 'HIL') return `入球大細 ${side === 'H' ? '大' : side === 'L' ? '細' : '方向'}${lineText ? ` ${lineText}` : ''}`;
  if (code === 'CHL') return `角球大細 ${side === 'H' ? '大' : side === 'L' ? '細' : '方向'}${lineText ? ` ${lineText}` : ''}`;
  return String(prediction.label || `${rawLine || ''} ${side || ''}`).trim() || '無方向';
}
const ODDS_SOURCE_LABEL = {
  'titan007-crown-id-3': '皇冠盤（Titan007）',
  'hkjc-current-board': '馬會即時盤',
  hkjc: '馬會盤',
};
const oddsSourceLabel = (value) => ODDS_SOURCE_LABEL[value] || value || '未提供';

// DigitalOcean serves the authenticated dashboard and proxies this same-origin
// path to a local-only simulation settlement service.
const API_BASE = '/api';
let DATA = null, LIST = [], LED = null, SEL = null, STAGE = 'all', Q = '', VIEW = 'pred';
let HISTORY_STAGE = 'all';
const HISTORY_PAGE_SIZE = 50;
let HISTORY_VISIBLE = HISTORY_PAGE_SIZE;
let HISTORY = { state: 'idle', payload: null, error: '', version: null, source: null, promise: null };
let HISTORY_REQUEST_ID = 0;
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
  if (m <= 10) return 'T-5';
  if (m <= 36) return 'T-30';
  return '待入窗';
}
function stageSnapshotStatus(m, stage, nowMs, generatedAt) {
  // A card missing a stage is not proof that the scheduler missed it when the
  // public snapshot predates that stage's window.  Keep stale/unknown display
  // distinct from a post-window snapshot that can actually confirm a miss.
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

/* ══════════════════════ 啟動 ══════════════════════ */
async function fetchJsonWithTimeout(url, timeoutMs = DASHBOARD_FETCH_TIMEOUT_MS) {
  const controller = typeof AbortController === 'function' ? new AbortController() : null;
  let timer = null;
  const timeout = new Promise((_, reject) => {
    timer = setTimeout(() => {
      if (controller) controller.abort();
      reject(new Error('讀取逾時'));
    }, timeoutMs);
  });
  try {
    const response = await Promise.race([
      fetch(url, {
        cache: 'no-store',
        credentials: 'same-origin',
        ...(controller ? { signal: controller.signal } : {}),
      }),
      timeout,
    ]);
    if (!response.ok) throw new Error('HTTP ' + response.status);
    const raw = await Promise.race([response.json(), timeout]);
    if (!raw || typeof raw !== 'object' || !Array.isArray(raw.matches)) {
      throw new Error('資料格式不完整');
    }
    return raw;
  } catch (error) {
    if (
      controller && controller.signal.aborted
      || (error && ['AbortError', 'TimeoutError'].includes(error.name))
    ) {
      throw new Error('讀取逾時');
    }
    throw error;
  } finally {
    if (timer != null) clearTimeout(timer);
  }
}

async function fetchDashboardData() {
  // 先讀同一部署內嘅 JSON；本機 API 只作後備，兩者都由 Nginx
  // 同一個已認證來源提供。
  const sources = [`data.json?v=${Date.now()}`];
  if (API_BASE) sources.push(`${API_BASE}/data?v=${Date.now()}`);
  const failures = [];
  for (const url of sources) {
    try {
      return await fetchJsonWithTimeout(url);
    } catch (error) {
      failures.push(error && error.message ? error.message : '讀取失敗');
      // 靜態檔暫時不可用或吊住時，立即轉讀 API 後備。
    }
  }
  if (window.__CROWN_DATA__) return window.__CROWN_DATA__;
  throw new Error(`靜態資料同後備 API 都無法讀取（${failures.join(' / ')}）`);
}

function historyVersion(raw = DATA) {
  if (!raw || typeof raw !== 'object') return null;
  return raw.history_data_version || raw.history_generated_at || raw.generated_at || null;
}

function sanitizeHistory(history) {
  const source = history && typeof history === 'object' ? history : {};
  const rows = Array.isArray(source.rows) ? source.rows : [];
  return {
    ...source,
    rows: rows.flatMap((sourceRow) => {
      if (!sourceRow || typeof sourceRow !== 'object') return [];
      const row = { ...sourceRow };
      row.market_predictions = (sourceRow.market_predictions || []).filter((prediction) => {
        if (!prediction || !['HDC', 'HIL', 'CHL'].includes(prediction.code)) return false;
        if (!['H', 'A', 'L'].includes(prediction.side)) return false;
        const rawLine = prediction.line == null ? prediction.condition : prediction.line;
        const odds = Number(prediction.odds);
        return rawLine !== '' && Number.isFinite(Number(rawLine))
          && Number.isFinite(odds) && odds > 1;
      });
      return row.market_predictions.length ? [row] : [];
    }),
  };
}

function historyPayloadFromArtifact(raw) {
  if (!raw || typeof raw !== 'object') return null;
  if (raw.prediction_history && typeof raw.prediction_history === 'object') {
    return raw.prediction_history;
  }
  // Accept a direct history contract as well as the v1 sidecar wrapper.  This
  // keeps manually published and older dashboard artifacts viewable.
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
  } catch (_) {
    return null;
  }
}

async function loadHistory({ force = false } = {}) {
  if (HISTORY.source === 'inline') return HISTORY.payload;
  if (HISTORY.state === 'loading') return HISTORY.promise;
  if (HISTORY.state === 'ready' && !force) return HISTORY.payload;
  const url = historyRequestUrl();
  if (!url) {
    HISTORY = {
      state: 'error', payload: null, error: '歷史紀錄檔未提供，請重新載入儀表板後再試。',
      version: historyVersion(), source: null, promise: null,
    };
    if (VIEW === 'history') renderHistory();
    return null;
  }
  const expectedVersion = historyVersion();
  const requestId = ++HISTORY_REQUEST_ID;
  const request = (async () => {
    let loaded = null;
    try {
      const response = await fetch(url, { cache: 'no-store' });
      if (!response.ok) throw new Error(`HTTP ${response.status}`);
      const artifact = await response.json();
      const responseVersion = artifact && artifact.history_data_version;
      if (expectedVersion && responseVersion !== expectedVersion) {
        throw new Error('紀錄正在更新，請重新讀取。');
      }
      const payload = historyPayloadFromArtifact(artifact);
      if (!payload || !Array.isArray(payload.rows)) throw new Error('紀錄檔格式無效');
      // A main-data refresh may have launched a newer request while this
      // response was in flight.  Never let the older response overwrite it.
      if (requestId !== HISTORY_REQUEST_ID) return null;
      // Apply row sanitisation only after the History artifact has arrived.
      HISTORY = {
        state: 'ready', payload: sanitizeHistory(payload), error: '',
        version: expectedVersion || responseVersion || null, source: 'remote', promise: null,
      };
      loaded = HISTORY.payload;
    } catch (error) {
      if (requestId === HISTORY_REQUEST_ID) {
        HISTORY = {
          state: 'error', payload: null, error: error.message || '讀取失敗',
          version: expectedVersion, source: null, promise: null,
        };
      }
    }
    if (requestId === HISTORY_REQUEST_ID && VIEW === 'history') renderHistory();
    return loaded;
  })();
  HISTORY = {
    state: 'loading', payload: HISTORY.payload, error: '', version: expectedVersion,
    source: HISTORY.source, promise: request,
  };
  if (VIEW === 'history') renderHistory();
  return request;
}

function applyData(raw) {
  const history = raw && raw.prediction_history;
  const previousVersion = HISTORY.version;
  if (history && Array.isArray(history.rows)) {
    // Legacy inline-history snapshots remain supported without a sidecar.
    HISTORY = {
      state: 'ready', payload: sanitizeHistory(history), error: '',
      version: historyVersion(raw), source: 'inline', promise: null,
    };
  } else if (HISTORY.source === 'inline' || (
    previousVersion && historyVersion(raw) && previousVersion !== historyVersion(raw)
  )) {
    // A new main snapshot invalidates only an already-loaded remote sidecar.
    // Do not fetch here: loading remains strictly demand-driven by History.
    HISTORY_REQUEST_ID += 1;
    HISTORY = {
      state: 'idle', payload: null, error: '', version: historyVersion(raw),
      source: null, promise: null,
    };
  }
  DATA = raw;
  LED = raw.ledger || { bets: [], stats: {}, log: [] };
  LIST = displayableMatches(raw.matches).slice()
    .sort((a, b) => kt(a.kickoff_hkt) - kt(b.kickoff_hkt));
  $('#genAt').textContent = hkStamp(raw.generated_at) + ' HKT';
}

function renderBootState(state, message = '') {
  const loading = state === 'loading';
  const title = loading ? '正在載入皇冠賽事資料…' : '皇冠賽事資料暫時載入失敗';
  const detail = loading
    ? '正嘗試讀取最新資料；如果靜態檔案沒有回應，系統會自動轉用後備 API。'
    : `${esc(message || '未知錯誤')}。可以重新載入，預測原始資料不受呢個畫面故障影響。`;
  const kpis = $('#kpis');
  const count = $('#railCount');
  const fixtures = $('#fixtures');
  const panel = $('#detail');
  if (kpis) kpis.innerHTML = `<div class="kpi"><span class="kpi-lbl">皇冠資料</span><span class="kpi-val ${loading ? 'amber' : 'bad'}">${loading ? '載入中' : '失敗'}</span></div>`;
  if (count) count.textContent = loading ? '正在載入賽事…' : '未能讀取賽事';
  if (fixtures) fixtures.innerHTML = `<li class="empty">${title}</li>`;
  if (panel) {
    panel.innerHTML = `<div class="empty" data-testid="state-dashboard-${state}">
      <b>${title}</b><br>${detail}
      ${loading ? '' : '<br><button class="settle-btn" id="dashboardRetry" data-testid="button-dashboard-retry" type="button"><span>重新載入</span></button>'}
    </div>`;
  }
  const retry = $('#dashboardRetry');
  if (retry) retry.onclick = () => boot();
}

let UI_BOUND = false;
let RENDER_TIMER = null;
let REFRESH_TIMER = null;

async function boot() {
  renderBootState('loading');
  try {
    applyData(await fetchDashboardData());
  } catch (e) {
    renderBootState('error', e && e.message ? e.message : '未知錯誤');
    return;
  }
  try {
    if (!UI_BOUND) {
      bindUI();
      UI_BOUND = true;
    }
    render();
    if (RENDER_TIMER != null) clearInterval(RENDER_TIMER);
    if (REFRESH_TIMER != null) clearInterval(REFRESH_TIMER);
    RENDER_TIMER = setInterval(render, 30000);
    REFRESH_TIMER = setInterval(() => refresh(true), 5 * 60000);   // 每 5 分鐘自動攞一次新資料
  } catch (e) {
    renderBootState('error', `畫面渲染失敗：${e && e.message ? e.message : '未知錯誤'}`);
  }
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
        credentials: 'same-origin',
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
    const oldHistoryVersion = historyVersion();
    const historyWasOpen = VIEW === 'history';
    const changed = raw.generated_at !== (DATA && DATA.generated_at);
    applyData(raw);
    render();
    const newHistoryVersion = historyVersion();
    if (historyWasOpen && HISTORY.source !== 'inline') {
      if (oldHistoryVersion !== newHistoryVersion || HISTORY.state === 'error') {
        void loadHistory({ force: true });
      }
    }
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
  $('#viewLedger').hidden = VIEW !== 'ledger';
  $('#viewChal').hidden = VIEW !== 'chal';
  $('#viewHealth').hidden = VIEW !== 'health';
  $('#viewCondition').hidden = VIEW !== 'condition';
  $('#viewHistory').hidden = VIEW !== 'history';
  $$('#nav .navbtn').forEach((b) => b.classList.toggle('is-on', b.dataset.view === VIEW));
  if (VIEW === 'pred') {
    LIST = displayableMatches(LIST);
    renderKpis(); renderList();
    if (!SEL || !LIST.some((m) => m.match_id === SEL)) {
      const f = LIST[0];
      SEL = f ? f.match_id : null;
    }
    if (SEL) renderDetail(SEL);
    else $('#detail').innerHTML = '<div class="empty">暫時冇未完場賽事</div>';
  } else if (VIEW === 'history') {
    renderHistory();
    if (HISTORY.state === 'idle') void loadHistory();
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
    if (STAGE === 'pick') { return false; }
    else if (['首預', 'T-30', 'T-5'].includes(STAGE)) {
      // 階段按鈕代表「已完成並保存該階段」，唔係目前倒數時間窗。
      // 否則賽事一開波，明明已有 T-30/T-5 都會由相應清單消失。
      if (!(m.stages || []).some((x) => x.stage === STAGE)) return false;
    }
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
          <span class="fx-lg">${esc(hkDay(m.kickoff_hkt))} · ${esc(m.league)}</span>
        </div>
        <div class="fx-foot">${dots(m)}
          <span class="fx-pick wait">${nextStageText(m, mm)}</span>
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
      ${m.book_odds?.hkjc ? '<span class="dual-badge">同場有 HKJC 盤</span>' : ''}
    </div></div>`;
}

function sourceFor(p) {
  return p && p.code === 'CHL' ? 'HKJC' : '皇冠';
}

function bookCond(p) {
  // 從 label 抽出已翻成選邊視角嘅盤口,抽唔到就退回原始 condition
  const m = String(p.label || '').match(/[（(]\s*(?:馬會|皇冠)盤\s*([^）)]*)[）)]/);
  if (m) return m[1].trim();
  const line = selectedMarketLine(p);
  if (Number.isFinite(line)) {
    const team = p.side === 'A' ? '客隊' : p.side === 'H' ? '主隊' : '選擇';
    return `${team} ${historyQuarterLine(line, true)}`;
  }
  return p.condition || '—';
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
    const raw = line.condition == null || String(line.condition).trim() === ''
      ? NaN
      : Number(line.condition);
    const condition = code === 'HDC' && Number.isFinite(raw)
      ? `主隊 ${historyQuarterLine(raw, true)}`
      : (line.condition || '—');
    return `<div class="odds-line"><strong>${esc(condition)}</strong>${prices}</div>`;
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
    <h2 class="card-h">同場雙莊盤口 <span class="sub">預測仍以皇冠讓球／入球大細為主；HKJC 只作並排比較，角球大細繼續用 HKJC</span></h2>
    <div class="odds-market-grid">${market('HDC', '全場讓球')}${market('HIL', '全場入球大小')}</div>
  </div>`;
}

function wilsonMatchText(item) {
  const number = numeric(item.condition_number) == null ? '—' : String(Math.trunc(numeric(item.condition_number)));
  const direction = publicText(item.selected_role || '—');
  const line = numeric(item.selected_line) == null ? '—' : numeric(item.selected_line).toString();
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

function verdictCard(m) {
  const c = m.conviction;
  const bar = `<div class="conv-wrap">
      <div class="conv-track"><div class="conv-fill ${convClass(c)}" style="width:${Math.min(100, Math.max(0, c))}%"></div>
        <div class="conv-floor" style="left:${DATA.ledger.stats.conf_floor || 58}%"></div></div>
      <div class="conv-scale"><span>0</span><span>門檻 ${DATA.ledger.stats.conf_floor || 58}</span><span>100</span></div>
    </div>`;
  // Retired EV/Kelly `pick` fields may survive in historic state but cannot
  // create or display a simulation bet.  The condition portfolio is the only
  // source of a visible simulated position.
  const conditionBets = (LED.bets || []).filter((bet) =>
    bet.match_id === m.match_id && bet.portfolio === 'crown_wilson_test');
  const wilsonMatches = (m.wilson_matches || []).filter((item) => item && typeof item === 'object');
  if (wilsonMatches.length) {
    const hasBet = wilsonMatches.some((item) => item.bet_status === 'BET');
    return `<div class="card verdict ${hasBet ? 'go' : 'wait'}">
      <div class="vd-top"><span class="vd-badge ${hasBet ? 'go' : 'wait'}">${hasBet ? 'Wilson 模擬注' : 'Wilson 不投注'}</span></div>
      <div class="condition-match-list">${wilsonMatches.map(wilsonMatchText).join('')}</div>
      <p class="vd-note">${hasBet ? '已建立的市場為固定注碼模擬；每項以凍結 Wilson 原始入場算術為準。' : '已合符歷史 Wilson 條件；因賠率不足，不建立正式模擬注。'}</p>
    </div>`;
  }
  const historicalMatches = (m.condition_matches || []).filter((item) => item && typeof item === 'object');
  const hasT5 = (m.stages || []).some((x) => x.stage === 'T-5');
  if (hasT5 && historicalMatches.length) {
    return `<div class="card verdict wait">
      <div class="vd-top"><span class="vd-badge wait">Wilson 未建立模擬注</span></div>
      <div class="condition-match-list">${historicalMatches.map(historicalConditionMatchText).join('')}</div>
      <p class="vd-note">逐項列明條件、賠率門檻及不投注原因；正式模擬注仍以原生 T-5、來源證據及 Wilson 閘門為準。</p>
    </div>`;
  }
  if (!conditionBets.length) {
    const mm = minsLeft(m.kickoff_hkt);
    const missingT5 = missingT5Text(m, mm);
    const badge = hasT5 ? '觀望 · 唔買' : (mm > 0 ? '未到落注時點' : (
      missingT5.startsWith('本場') ? '無落注' : '狀態待同步'
    ));
    const why = hasT5
      ? (m.no_bet_reason || '未達條件模擬倉入場規則')
      : (mm > 0
        ? `最終投注決定統一喺<b>開賽前約 10 分鐘起</b>處理。依家係${esc(stageOf(mm))}，只做預測記錄。距開賽 ${cdText(mm)}。`
        : missingT5);
    return `<div class="card verdict wait">
      <div class="vd-top"><span class="vd-badge wait">${badge}</span>
        <span class="vd-conv ${convClass(c)}">信念 ${f2(c)}</span></div>
      <p class="vd-why">${why}</p>${bar}</div>`;
  }
  const p = conditionBets[0];
  const G = [
    ['市場', marketLabel(p.market || p.code)], ['投注', publicText(p.label)], ['選邊賠率', f2(p.odds)],
    ['固定注碼', money(p.stake)],
    ['歷史命中', `${pc(p.condition_accuracy, 1)} (${p.condition_hits || 0}/${p.condition_decided || 0})`],
    ['條件級別', p.condition_badge || '—'],
  ];
  return `<div class="card verdict go">
    <div class="vd-top">
      <span class="vd-badge go">條件模擬注</span>
      <span class="vd-main">${esc(publicText(p.label))} <b>@${f2(p.odds)}</b></span>
      <span class="vd-stake">${money(p.stake)}</span>
    </div>
    <div class="vd-grid">${G.map(([l, v]) =>
      `<div class="par"><div class="par-l">${l}</div><div class="par-v">${esc(v)}</div></div>`).join('')}</div>
    ${bar}
    <p class="vd-note">只於新保存 T-5，並且歷史已結算細緻條件嚴格高於 60%、至少 10 個已判定樣本時建立。每注固定 HK$1,000；不使用 EV 或凱利注碼。</p>
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
    const selectedLine = selectedMarketLine(row);
    const lineText = Number.isFinite(selectedLine)
      ? historyQuarterLine(selectedLine, row.code === 'HDC')
      : (row.line ?? '—');
    const selectionText = row.code === 'HDC'
      ? `${side(row)}隊 ${lineText}`
      : `${side(row)} ${lineText}`;
    const price = Number.isFinite(odds) && odds > 1
      ? `賠率 ${odds.toFixed(2)}`
      : `賠率缺失 · ${esc(reason[row.reason] || row.reason || '未有已保存現價')}`;
    return `<div class="current-odds-row"><b>${esc(MKT[row.code] || row.code || '—')}</b>
      <span>${esc(selectionText)}</span>
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
    <p class="current-odds-note">更新來源：${esc(oddsSourceLabel(m.current_odds_refresh_source || 'titan007-crown-id-3'))}${seen}</p>
  </div>`;
}

/* ══════════════════════ 三段變化 ══════════════════════ */
const S3 = ['首預', 'T-30', 'T-5'];
const stg = (m, k) => (m.stages || []).find((x) => x.stage === k) || null;
const lean = (x) => {
  if (!x) return null;
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
          <span>${mm > 0 ? '仲有 ' + cdText(mm) + ' 到開賽,最終投注決定會喺開賽前約 10 分鐘起處理' : missingT5Text(m, mm)}</span>
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
      return q ? chinesePredictionLabel(q) : ((x2.market_predictions || []).map(chinesePredictionLabel).join(' · ') || '無方向');
    };
    const a = pl(prev), b = pl(x);
    const dc = (x.conviction ?? 0) - (prev.conviction ?? 0);
    delta = `<div class="run-delta">對比 ${prev.stage}:${a === b
      ? `<b class="same">方向一致</b>` : `<b class="flip">由「${esc(a)}」轉為「${esc(b)}」</b>`}
      · 信念 ${sg(dc, 1)}</div>`;
  }

  const soft = ld && (ld.ev || 0) > 0;
  const main = p
    ? `<span class="run-pick">${esc(chinesePredictionLabel(p))} <b>賠率 ${f2(p.odds)}</b></span>
       ${isFinal && p.stake ? `<span class="run-stake">${money(p.stake)}</span>` : ''}
       <span class="run-num">勝率 ${pc(p.prob)} · 預期價值 ${sg(p.ev * 100, 2)}%</span>`
    : soft
      ? `<span class="run-pick dimp">${esc(chinesePredictionLabel(ld))}</span>
         <span class="run-num">勝率 ${pc(ld.prob)} · 預期價值 ${sg((ld.ev || 0) * 100, 2)}%</span>`
      : forecasts.length
        ? `<span class="run-pick dimp">${forecasts.map((r) => esc(chinesePredictionLabel(r))).join(' · ')}</span>
           <span class="run-num">${forecasts.map((r) => `預測概率 ${pc(r.probability, 1)}`).join(' · ')} · 未有平博同方向盤口，未計預期價值</span>`
      : `<span class="run-pick dimp">無明顯方向</span>
         ${ld ? `<span class="run-num">最佳候選 ${esc(chinesePredictionLabel(ld))} · 預期價值 ${sg((ld.ev || 0) * 100, 2)}%，全部負值</span>` : ''}`;

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
  const pk = null; // Retired EV/Kelly pick is research-only, never a simulation selection.
  const isPick = (c) => pk && c.code === pk.code && c.condition === pk.condition && c.side === pk.side;
  return `<div class="card"><h2 class="card-h">全部候選盤口 <span class="sub">按 EV 排序 · EV = 模型勝率 × 賠率 − 輸率</span></h2>
    <div class="tbl-wrap"><table class="t">
      <tr><th>市場</th><th>投注</th><th>莊家</th><th>賠率</th><th>公平價</th><th>勝率</th><th>走水</th><th>EV</th><th>凱利</th><th></th></tr>
      ${C.map((c) => `<tr class="${isPick(c) ? 'pickrow' : ''}">
        <td class="lbl">${esc(marketLabel(c.market || c.code))}</td><td>${esc(publicText(c.label))}</td>
        <td><span class="minitag">${sourceFor(c)}</span></td><td>${f2(c.odds)}</td><td class="dim">${f2(c.fair)}</td>
        <td>${pc(c.prob)}</td><td class="${c.push > 1e-6 ? 't-push' : 't-dim'}">${c.push > 1e-6 ? pc(c.push) : '—'}</td>
        <td class="${c.ev > 0 ? 'ev-p' : 'ev-n'}">${sg(c.ev * 100, 2)}%</td>
        <td>${c.kelly_raw > 0 ? pc(c.kelly_raw) : '—'}</td>
        <td>${c.is_main ? '<span class="minitag">主線</span>' : ''}${isPick(c) ? '<span class="minitag go">選中</span>' : ''}</td>
      </tr>`).join('')}
    </table></div>
    <p class="mx-note">主線 = 該資料源市場嘅主打盤口，流動性較好，排序時有 15% 加權。讓球／入球大細用皇冠，角球大細用嚴格對齊嘅 HKJC。</p></div>`;
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
function stk() { return null; }

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
  const s = LED.stats || {};
  const bets = (LED.bets || []).filter((bet) =>
    bet && bet.portfolio === 'crown_wilson_test' && bet.strategy === 'wilson-test-strategy-v1'
  );
  const audit = Array.isArray(LED.independent_validation?.audit) ? LED.independent_validation.audit.slice(-48).reverse() : [];
  const V = $('#viewLedger');
  const archive = LED.independent_validation?.historical_discovery_archive || {};
  const K = [
    ['啟用／切換', hkStamp(LED.independent_validation?.activation_at || DATA.generated_at), ''], ['起始本金', money(s.starting_bankroll || 50000), ''], ['每注', money(s.fixed_stake || 500), 'amber'], ['每場上限', money(s.fixture_stake_cap || 1500), 'amber'],
    ['待決', s.n_pending || 0, ''], ['已撤回', s.n_voided || 0, ''],
    ['已結算', s.n_settled || 0, ''], ['累計盈虧', s.n_settled ? money(s.pnl) : '—', (s.pnl || 0) >= 0 ? 'good' : 'bad'],
    ['前瞻回報率', s.roi == null ? '—' : pc(s.roi, 2), (s.roi || 0) >= 0 ? 'good' : 'bad'],
    ['前瞻命中／Wilson', s.n_decided ? `${pc(s.hit_rate, 1)} (${s.hits}/${s.n_decided}) · ${Array.isArray(s.wilson95) ? `${pc(s.wilson95[0], 1)}–${pc(s.wilson95[1], 1)}` : '—'}` : '—', ''],
    ['現金 / 權益', `${money(s.cash == null ? 50000 : s.cash)} / ${money(s.equity == null ? 50000 : s.equity)}`, (s.pnl || 0) >= 0 ? 'good' : 'bad'],
  ];
  let h = `<div class="ledger-head"><div class="ledger-title-row">
    <h1 class="pg-h">Wilson 測試攻略 <span class="sub">v1 已封存 · 起始 HK$50,000 · 每注 HK$500 · 每場 HK$1,500</span></h1>
    <button class="settle-btn" id="settleNow" data-testid="button-settle-simulation" type="button" ${SETTLING || !API_BASE ? 'disabled' : ''}><span>${SETTLING ? '結算中…' : '立即結算'}</span></button>
  </div>
  <p class="settle-status ${SETTLE_BAD ? 'bad' : SETTLE_MESSAGE ? 'good' : ''}" id="settleStatus" data-testid="status-settlement" aria-live="polite">${esc(SETTLE_MESSAGE || (API_BASE ? '只會結算已完場並有可靠賽果的獨立驗證注。' : '結算後端未連接，請重新開啟已部署的皇冠面板。'))}</p>
  <div class="shadow-note" role="note"><strong>入場規則</strong><span>只在首次持久化原生賽前 T-5 建立模擬注；凍結歷史證據至少 50 個唯一已判定 fixture-market，Wilson 95% 下限須 ≥ 實際損益平衡命中率 + 3 個百分點。每注 HK$500，每場最多三市場及 HK$1,500；基線永不受前瞻結果回寫。</span></div>
  <div class="kpis wide">${K.map(([l, v, c]) => `<div class="kpi"><span class="kpi-lbl">${l}</span><span class="kpi-val ${c}">${v}</span></div>`).join('')}</div></div>
  <div class="card history-note"><h2 class="card-h">已封存／退役 previous strategy（v1） <span class="sub">唯讀，保留歷史及待決結算，不混入 Wilson 前瞻盈虧、回報率、命中率、樣本或本金</span></h2>
    <p class="mx-note">摘要：已保留舊注單 ${numeric(archive.legacy_bet_count) == null ? '—' : archive.legacy_bet_count} 筆；舊帳本本金 ${archive.legacy_bankroll == null ? '—' : money(archive.legacy_bankroll)}。凍結條件會保留當時「歷史發現 x/y」，驗證結果不會回寫該基線。</p>
    ${Array.isArray(archive.legacy_bets) && archive.legacy_bets.length ? `<details><summary>查看已封存舊注單（唯讀）</summary><div class="tbl-wrap"><table class="t"><tr><th>賽事</th><th>市場</th><th>注碼</th><th>狀態</th><th>盈虧</th></tr>${archive.legacy_bets.map((b) => `<tr><td>${esc(b.home || '—')} vs ${esc(b.away || '—')}</td><td>${esc(marketLabel(b.market || b.code))}</td><td>${money(b.stake)}</td><td>${esc(b.status || '—')}</td><td>${b.pnl == null ? '—' : money(b.pnl)}</td></tr>`).join('')}</table></div></details>` : ''}</div>`;
  h += wilsonRolloverCard(LED.independent_validation || {});
  h += oddsTierCard(s);
  if (!bets.length) h += `<div class="card"><div class="empty2">暫時未有合資格 Wilson 模擬注。系統不會在 T-30、重跑或回補歷史時建倉。</div></div>`;
  else {
    if (s.n_settled) h += `<div class="grid g2">${equityCard(s)}${resultCard(s)}</div>`;
    if (s.n_settled) h += marketCard(s);
    h += `<div class="card"><h2 class="card-h">Wilson 模擬注單 <span class="sub">${bets.length} 筆 · 每筆 ${money(s.fixed_stake || 500)}</span></h2><div class="tbl-wrap condition-bets-wrap"><table class="t bets condition-bets"><tr><th></th><th>開賽</th><th>賽事</th><th>市場</th><th>投注</th><th>賠率</th><th>注碼</th><th>凍結歷史條件</th><th>Wilson 凍結算式／前瞻表現</th><th>狀態</th><th>結果</th><th>比分</th><th>前瞻盈虧</th></tr>${bets.map((b, i) => betRow(b, i, 'condition')).join('')}</table></div></div>`;
  }
  h += conditionAuditCard(audit);
  V.innerHTML = h; bindSettlementButton('settleNow', renderLedger); bindBetRows('#viewLedger');
}

function wilsonRolloverCard(validation) {
  const rollover = validation?.rollover || {};
  const rows = Object.values(rollover.conditions || {}).filter((row) => row && typeof row === 'object')
    .sort((a, b) => (numeric(a.condition_number) || 999999) - (numeric(b.condition_number) || 999999));
  if (!rows.length) return `<section class="card"><h2 class="card-h">Wilson 證據版本</h2><div class="empty2">尚未有已凍結條件；不會以舊驗證紀錄追溯補入。</div></section>`;
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

function conditionAuditReason(value) {
  const labels = {
    missing_new_t5_snapshot: '未有新保存的 T-5 快照',
    selected_market_missing_or_ambiguous: '選定市場缺失或有多個方向',
    selected_odds_invalid_or_missing: '選邊賠率缺失或不大於 1',
    selected_line_or_side_invalid: '選邊方向或盤口無效',
    selected_quote_not_provably_pre_kickoff: '未能證明選邊賠率早於開賽',
    selected_source_observation_invalid_or_missing: '選邊欠缺有效賠率來源觀測',
    no_historical_condition_above_60pct_with_20_decided: '沒有歷史條件同時高於 60% 並有至少 20 個已判定樣本',
    missing_fixture_context_for_public_condition_bet: '聯賽或主客隊資料不完整，安全跳過',
    fixture_two_market_cap: '同場已達兩個市場上限',
    fixture_stake_cap: '同場已達 HK$500 注碼上限',
    conflicting_condition_direction_or_line: '符合條件的機會在方向或線位衝突，已安全跳過',
    idempotent_existing_bet: '同一賽事、市場及 T-5 策略已有注單',
    historical_condition_eligible: '歷史已結算條件合資格',
  };
  return labels[value] || String(value || '—');
}

function conditionAuditSelection(item) {
  const selected = item || {};
  const conflicts = Array.isArray(selected.conflicting_selections) ? selected.conflicting_selections : [];
  if (conflicts.length) {
    return conflicts.map((row) => `${publicText(row.role || '方向')} ${row.line == null ? '' : row.line}`).join(' ／ ');
  }
  if (!selected.selected_label) return '—';
  const odds = numeric(selected.selected_odds);
  return `${publicText(selected.selected_label)}${odds == null ? '' : ` @${odds.toFixed(2)}`}`;
}

function conditionAuditCard(rows) {
  if (!rows.length) return '';
  return `<div class="card"><h2 class="card-h">T-5 條件審計 <span class="sub">最近 ${rows.length} 項；衝突一律不下注</span></h2>
    <div class="tbl-wrap"><table class="t"><tr><th>市場</th><th>結果</th><th>原因</th><th>選擇</th><th>凍結條件</th><th>歷史發現／獨立驗證</th></tr>
      ${rows.map((item) => `<tr><td class="lbl">${esc(marketLabel(item.market_label || item.market))}</td>
        <td>${item.status === 'CREATED' ? '<span class="stpill pending">已建立</span>' : '<span class="stpill voided">已跳過</span>'}</td>
        <td>${esc(conditionAuditReason(item.reason))}</td><td>${esc(conditionAuditSelection(item))}</td><td>${esc(publicText(item.condition_label || '—'))}</td>
        <td>${item.accuracy == null ? '—' : `${pc(item.accuracy, 1)} (${item.hits || 0}/${item.decided || 0})`}</td></tr>`).join('')}
    </table></div></div>`;
}

function bindBetRows(container) {
  $$(`${container} .bets tr.brow`).forEach((tr) => {
    tr.onclick = () => {
      const d = document.getElementById(tr.dataset.target);
      if (!d) return;
      d.classList.toggle('open');
      tr.classList.toggle('is-open');
    };
  });
}

function bindSettlementButton(buttonId, rerender) {
  const b = document.getElementById(buttonId);
  if (!b) return;
  b.onclick = async () => {
    if (SETTLING || !API_BASE) return;
    if (!window.confirm('只會核對已完場及有可靠賽果嘅注單。確認結算條件模擬倉？')) return;
    SETTLING = true;
    SETTLE_BAD = false;
    SETTLE_MESSAGE = '正在核對正式賽果及更新條件模擬倉…';
    rerender();
    try {
      const r = await fetch(`${API_BASE}/settle`, {
        method: 'POST',
        credentials: 'same-origin',
        cache: 'no-store',
        headers: {
          'Content-Type': 'application/json',
          'X-Crown-Action': 'settle-simulation',
        },
        body: JSON.stringify({ confirm: 'simulation-only' }),
      });
      const result = await r.json().catch(() => ({}));
      if (r.status === 401) {
        throw new Error('登入憑證已失效，請重新整理頁面並重新登入一次');
      }
      if (!r.ok || !result.ok) throw new Error(result.error || `HTTP ${r.status}`);
      applyData(result.data);
      const settled = result.settled_count || 0;
      const pending = result.pending_count || 0;
      const predictionSync = result.prediction_result_sync || {};
      const predictionGraded = predictionSync.graded_now || 0;
      const predictionUnresolved = predictionSync.unresolved || 0;
      SETTLE_BAD = !result.persisted;
      const syncNote = result.project_submitted === false
        ? ' 專案檔案同步暫緩，但本機模擬倉已保存。'
        : '';
      SETTLE_MESSAGE = settled
        ? `完成：新結算 ${settled} 注；預測紀錄新對到 ${predictionGraded} 場、仍待補 ${predictionUnresolved} 場；待決 ${pending}${result.persisted ? '，已保存。' : '；保存失敗，請稍後再試。'}${syncNote}`
        : `檢查完成：預測紀錄新對到 ${predictionGraded} 場、仍待補 ${predictionUnresolved} 場；未有新注可結算，待決 ${pending}。${syncNote}`;
    } catch (e) {
      SETTLE_BAD = true;
      SETTLE_MESSAGE = `結算失敗：${e.message}`;
    } finally {
      SETTLING = false;
      rerender();
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
  if (!Number.isFinite(q)) return '—';
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
  const line = selectedMarketLine(p);
  if (!Number.isFinite(line)) {
    if (p.code === 'HDC') {
      const team = p.side === 'A' ? r.away : r.home;
      return `${team || '選擇球隊'} · 盤口未提供`;
    }
    return '盤口未提供';
  }
  if (p.code === 'HDC') {
    const team = p.side === 'A' ? r.away : r.home;
    return `${team} ${historyQuarterLine(line, true)}`;
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
function historyRecoveryEvidence(p) {
  if (!p || !p.recovery_evidence_type) return '';
  const label = p.recovery_evidence_type === 'closing_substitution'
    ? '收市賠率替代'
    : p.recovery_evidence_type === 't5_exact'
      ? 'T-5 實際證據'
      : 'T-5 LOCF 證據';
  const observed = p.observed_at ? ` · 證據 ${hkStamp(p.observed_at)}` : '';
  const carried = p.prediction_stage_substitution_type === 'last_pre_t5_prediction_carry_forward';
  const prediction = carried ? '預測階段替代：承接最後 T-5 前方向（非真實 T-5）' : '已存 T-5 模型載荷';
  return `<span class="cell-sub recovery-evidence">POST-HOC／回補 · ${prediction} · ${label}${observed}</span>`;
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
        ${historyRecoveryEvidence(p)}
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
      <b>${esc(publicText(item.label || ''))}</b><div class="granular-rate">${pc(total.accuracy, 1)}</div>
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

const HISTORY_STAGE_RANK = { '首預': 1, 'T-30': 2, 'T-5': 3 };

function historyFixtureIdentity(row, index) {
  const matchId = row.match_id == null ? '' : String(row.match_id).trim();
  if (matchId) return `match:${matchId}`;
  // The fallback deliberately includes all available fixture identity fields.
  // It groups only stages that describe the same fixture, never just rows
  // sharing a kickoff time.  An entirely unidentified row remains distinct.
  const parts = [
    row.fixture_id, row.hkjc_match_id, row.titan_match_id,
    row.kickoff_hkt || row.kickoff, row.home, row.away, row.league,
  ].map((value) => value == null ? '' : String(value).trim());
  if (parts.some(Boolean)) return `fallback:${parts.join('\u241f')}`;
  return `unidentified:${index}`;
}

function historyKickoffMs(row) {
  const kickoff = Date.parse(row.kickoff_hkt || row.kickoff || '');
  return Number.isFinite(kickoff) ? kickoff : Number.NEGATIVE_INFINITY;
}

function orderHistoryRows(rows) {
  const groups = new Map();
  rows.forEach((row, index) => {
    const key = historyFixtureIdentity(row, index);
    const group = groups.get(key) || { key, kickoff: Number.NEGATIVE_INFINITY, rows: [] };
    group.kickoff = Math.max(group.kickoff, historyKickoffMs(row));
    group.rows.push({ row, index });
    groups.set(key, group);
  });
  return [...groups.values()]
    .sort((left, right) => right.kickoff - left.kickoff || left.key.localeCompare(right.key))
    .flatMap((group) => group.rows
      .sort((left, right) =>
        (HISTORY_STAGE_RANK[left.row.stage] || 99) - (HISTORY_STAGE_RANK[right.row.stage] || 99)
        || left.index - right.index)
      .map((item) => item.row));
}

function historyStageCompletenessCard(raw) {
  const summary = raw && typeof raw === 'object' ? raw : {};
  const stages = summary.stages && typeof summary.stages === 'object' ? summary.stages : {};
  const fixtureTotal = Number(summary.fixtures_total || 0);
  const overdueFixtures = Number(summary.fixtures_with_overdue_stage || 0);
  const cards = ['首預', 'T-30', 'T-5'].map((stage) => {
    const item = stages[stage] && typeof stages[stage] === 'object' ? stages[stage] : {};
    const recorded = Number(item.recorded || 0);
    const due = Number(item.due || 0);
    const missing = Number(item.missing_due || 0);
    const notDue = Number(item.not_due || 0);
    const rate = Number(item.completeness);
    const hasRate = item.completeness != null && Number.isFinite(rate);
    const state = missing > 0 ? 'bad' : hasRate ? 'good' : 'wait';
    const value = hasRate ? pc(rate, 1) : '未到期';
    return `<article class="stage-completeness-item ${state}" data-stage-completeness="${stage}">
      <div class="stage-completeness-head">
        <h3>${stage}</h3>
        <span class="stage-completeness-status">${missing > 0 ? `缺 ${missing}` : hasRate ? '完整' : '等待'}</span>
      </div>
      <strong class="stage-completeness-rate">${value}</strong>
      <div class="stage-completeness-meta">
        <span>已記錄 <b>${recorded}</b></span>
        <span>應完成 <b>${due}</b></span>
        <span>未到期 <b>${notDue}</b></span>
        <span>有賠率 <b>${Number(item.odds_available || 0)}</b></span>
        <span>缺賠率 <b>${Number(item.odds_missing || 0)}</b></span>
      </div>
    </article>`;
  }).join('');
  const healthClass = overdueFixtures > 0 ? 'bad' : 'good';
  const healthText = overdueFixtures > 0 ? `${overdueFixtures} 場有逾期缺失` : '冇逾期缺失';
  return `<section class="card stage-completeness" aria-labelledby="stageCompletenessTitle">
    <div class="stage-completeness-title">
      <div>
        <h2 id="stageCompletenessTitle">階段完整率監察</h2>
        <p>${fixtureTotal} 場獨立皇冠賽事 · 未到期唔扣完整率</p>
      </div>
      <span class="stage-completeness-health ${healthClass}">${healthText}</span>
    </div>
    <div class="stage-completeness-grid">${cards}</div>
    <p class="stage-completeness-note">首預對已進入賽程嘅場次即時檢查；T-30 喺寫入窗口結束後、T-5 喺開賽後仍未記錄先列作逾期。DATA_MISSING 會當未完成並等待重試。</p>
  </section>`;
}

function renderHistory() {
  const V = $('#viewHistory');
  const summaryPayload = DATA.prediction_history || { stats: {} };
  const payload = HISTORY.payload || summaryPayload;
  const rows = orderHistoryRows(
    (payload.rows || []).filter((row) => HISTORY_STAGE === 'all' || row.stage === HISTORY_STAGE),
  );
  const visibleRows = rows.slice(0, HISTORY_VISIBLE);
  const s = payload.stats || {};
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
      ${r.post_hoc_backfill ? `<div class="cell-sub recovery-audit-label">POST-HOC／BACKFILLED · ${((r.recovery || {}).recovery_kind === 'last_pre_t5_prediction_carry_forward') ? '最後 T-5 前預測承接（非真實 T-5）' : '已存 T-5 模型載荷回補（非原生 T-5）'} · 來源 ${esc((r.recovery || {}).source_stage || '已存階段')} · ${((r.recovery || {}).closing_odds_substitution) ? '含收市賠率替代 · ' : ''}不計主統計／不結算</div>` : ''}
      <div class="cell-sub mono">${r.predicted_at ? hkStamp(r.predicted_at) : '—'}</div></td>
    <td data-label="1X2 輔助"><b class="forecast-pick">${esc(r.forecast || '冇主客和預測')}</b>
      <div class="cell-sub">${r.probability == null ? '正式結果見市場欄' : `最高機率 ${pc(r.probability, 1)}`}${r.likely_score ? ` · 最可能 ${esc(r.likely_score)}` : ''}</div></td>
    <td data-label="各市場預測／結果">${historyMarkets(r)}<div class="market-summary">${historyMarketResult(r)}</div></td>
    <td data-label="信念" class="${convClass(r.conviction)}">${f2(r.conviction)}</td>
    <td data-label="模擬注">${r.simulated_bet
      ? `<span class="stpill pending">有模擬注</span><div class="cell-sub">${esc(publicText(r.bet_label || ''))}</div>`
      : `<span class="stpill voided">冇落注</span><div class="cell-sub hist-reason">${esc(publicText(r.no_bet_reason || '未達條件'))}</div>`}</td>
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
    ${[['all', '全部'], ['首預', '首預'], ['T-30', 'T-30'], ['T-5', 'T-5'], ['T-5（事後回補）', '回補稽核']].map(([value, label]) =>
      `<button type="button" class="history-stage-filter ${HISTORY_STAGE === value ? 'is-on' : ''}"
        data-history-stage="${value}" aria-pressed="${HISTORY_STAGE === value}">${label}</button>`).join('')}
  </div>`;
  const historyStatus = (() => {
    if (HISTORY.state === 'loading') {
      return `<div class="card history-load-state"><div class="empty2" data-testid="history-loading">正在讀取完整預測紀錄…</div></div>`;
    }
    if (HISTORY.state === 'error') {
      return `<div class="card history-load-state"><div class="empty2 bad-txt" data-testid="history-error">
        預測紀錄讀取失敗:${esc(HISTORY.error || '未知錯誤')}。
        <button type="button" class="history-refresh-btn" data-history-refresh>重新讀取紀錄</button>
      </div></div>`;
    }
    if (HISTORY.state !== 'ready') {
      return `<div class="card history-load-state"><div class="empty2" data-testid="history-not-loaded">開啟完整紀錄中…</div></div>`;
    }
    return '';
  })();
  const more = rows.length > visibleRows.length
    ? `<div class="history-more"><button type="button" class="history-more-btn" data-history-more>
        顯示更多
      </button><span>${visibleRows.length} / ${rows.length} 筆</span></div>`
    : rows.length
      ? `<div class="history-more"><span>已顯示全部 ${rows.length} 筆</span></div>`
      : '';

  V.innerHTML = `<div class="ledger-head">
    <h1 class="pg-h">預測紀錄 <span class="sub">有冇落注都照記 · 準確率與模擬倉分開</span></h1>
    <div class="kpis wide">${K.map(([l, v, c]) =>
      `<div class="kpi"><span class="kpi-lbl">${l}</span><span class="kpi-val ${c}">${v}</span></div>`).join('')}</div>
  </div>
  ${historyStageCompletenessCard(DATA.stage_completeness)}
  <div class="card history-note">
    <div class="history-summary-label">合併總覽</div>
    <div class="history-stage-summary">${stageSummary}</div>
    <div class="history-stage-summary">${marketSummary}</div>
    ${historyStageMarketMatrix(s)}
    ${historyConsensusCards(s)}
    <p class="mx-note">合併市場數字只計首預、T-30、T-5 原生獨立快照；事後回補只作稽核展示，絕不計入命中率、排名、學習、Telegram 或模擬注。下表按最新開賽時間優先排列。</p>
  </div>
  ${historyFilters}
  <div class="card"><h2 class="card-h">${HISTORY_STAGE === 'all' ? '全部紀錄' : `${HISTORY_STAGE} 紀錄`} <span class="sub">${rows.length} 筆 · 最新開賽時間優先</span></h2>
    ${historyStatus || historyTable(visibleRows, '暫時未有預測紀錄。')}
    ${historyStatus ? '' : more}
  </div>`;
  $$('#viewHistory [data-history-stage]').forEach((button) => {
    button.onclick = () => {
      HISTORY_STAGE = button.dataset.historyStage;
      HISTORY_VISIBLE = HISTORY_PAGE_SIZE;
      renderHistory();
    };
  });
  const retry = $('#viewHistory [data-history-refresh]');
  if (retry) retry.onclick = () => loadHistory({ force: true });
  const showMore = $('#viewHistory [data-history-more]');
  if (showMore) showMore.onclick = () => {
    HISTORY_VISIBLE += HISTORY_PAGE_SIZE;
    renderHistory();
  };
  if (HISTORY.state === 'idle') {
    void loadHistory();
  }
}

function dayStake(bets) {
  const today = new Date().toLocaleDateString('en-CA', { timeZone: 'Asia/Hong_Kong' });
  return bets.filter((b) => b.status === 'PENDING' && String(b.kickoff).slice(0, 10) === today)
             .reduce((a, b) => a + b.stake, 0);
}

function betRow(b, i, prefix = 'condition') {
  const H = b.history || [], target = `${prefix}-hist-${i}`;
  const frozen = LED.independent_validation?.conditions?.[b.frozen_condition_signature] || {};
  const prospective = frozen.prospective || {};
  const evidence = b.frozen_historical_evidence || frozen.historical_evidence || {};
  const arithmetic = b.wilson_admission || frozen.admission_arithmetic || {};
  const conditionNumber = numeric(b.condition_number) == null ? '—' : Math.trunc(numeric(b.condition_number));
  const condition = `條件 #${conditionNumber} · ${publicText((b.frozen_condition_definition || {}).path || evidence.label || '凍結歷史條件')} · 命中 ${evidence.hits || 0}/${evidence.decided || 0}`;
  const active = frozen.active_evidence || {};
  const validation = numeric(arithmetic.wilson95_lower_raw) == null
    ? '—'
    : `凍結命中率 ${pc(arithmetic.hit_rate_raw, 1)} · 入場版本 v${numeric(b.evidence_version) == null ? '—' : Math.trunc(numeric(b.evidence_version))}<div class="cell-sub">最低可接受賠率 ${f2(arithmetic.minimum_acceptable_odds_raw)}；目前 ${f2(arithmetic.actual_decimal_odds_raw)}</div><div class="cell-sub">有效 v${numeric(active.version) == null ? '—' : Math.trunc(numeric(active.version))} · ${active.cumulative_hits ?? 0}/${active.cumulative_decided ?? 0} · Wilson 下限 ${pc(active.wilson95_lower_raw, 1)} · 最低 ${numeric(active.minimum_acceptable_odds_display) == null ? f2(active.minimum_acceptable_odds_raw) : active.minimum_acceptable_odds_display}</div><div class="cell-sub">前瞻 ${pc(prospective.hit_rate, 1)} (${prospective.hits || 0}/${prospective.decided || 0}) · Wilson ${Array.isArray(prospective.wilson95) ? `${pc(prospective.wilson95[0], 1)}–${pc(prospective.wilson95[1], 1)}` : '—'} · ROI ${pc(prospective.roi, 2)}</div>`;
  return `<tr class="brow ${String(b.status || '').toLowerCase()}" data-i="${i}" data-target="${target}"><td class="exp">${H.length ? '▸' : ''}</td><td class="mono nowrap">${hkDay(b.kickoff)} ${hkClock(b.kickoff)}</td><td>${esc(b.home)} <span class="dim">v</span> ${esc(b.away)}<div class="cell-sub">${esc(b.league || '')}</div></td><td class="lbl">${esc(marketLabel(b.market || b.code))}</td><td><b>${esc(publicText(b.label || ''))}</b><div class="cell-sub">線位 ${esc(b.selected_line ?? b.line)}</div></td><td>${f2(b.odds)}</td><td class="stk">${money(b.stake)}</td><td>${esc(condition)}</td><td>${esc(validation)}</td><td><span class="stpill ${String(b.status || '').toLowerCase()}">${ST_LBL[b.status] || b.status || '—'}</span></td><td>${b.result ? `<span class="respill ${RES_CLS[b.result] || ''}">${RES_LBL[b.result] || b.result}</span>` : '<span class="dim">—</span>'}</td><td class="mono nowrap">${scoreCell(b)}</td><td class="${(b.pnl || 0) > 0 ? 'ev-p' : (b.pnl || 0) < 0 ? 'ev-n' : 'dim'}">${b.pnl == null ? '—' : money(b.pnl)}</td></tr><tr class="hrowwrap"><td colspan="13" class="histcell"><div class="hist-panel" id="${target}"><ol class="tl">${H.map((x) => `<li class="tl-i"><span class="tl-dot">·</span><div class="tl-b"><div class="tl-h"><b>${esc(x.action || '')}</b><span class="fx-tag ${TAG[x.stage] || 'tag-wait'}">${esc(x.stage || '')}</span><span class="tl-ts mono">${hkStamp(x.ts)}</span></div><div class="tl-d">${x.reason ? `<span class="tl-kv">${esc(publicText(x.reason))}</span>` : ''}${x.result ? `<span class="tl-kv">${esc(x.result)}</span>` : ''}</div></div></li>`).join('')}</ol></div></td></tr>`;
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
        <td class="lbl">${esc(marketLabel(k))}</td><td class="mono">${m.n}</td>
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
const HEALTH_SYSTEM = 'crown';
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

/* 純讀取 shadow-condition-report.json。此報告與既有挑戰模型、
 * 結算、注碼及通知完全分離；只呈現凍結後的前瞻條件診斷。 */
const CONDITION_FILE = 'shadow-condition-report.json';
const CONDITION_SYSTEM = 'crown';
const CONDITION_ID = 'crown_hdc_three_stage_exact';
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
  const head = `<div class="ledger-head"><div><h1>條件統計報告</h1><p class="dim">只作報告 / 不自動套用</p></div><button class="settle-btn" id="conditionReload" type="button">重新讀取</button></div>`;
  if (CONDITION.state === 'idle' || CONDITION.state === 'loading') {
    V.innerHTML = head + '<div class="card"><div class="empty2" data-testid="state-condition-loading">正在讀取條件統計報告…</div></div>';
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
const CHALLENGER_SYSTEM = 'crown';
const CHALLENGER_FILE = 'challenger-status.json';
const CHALLENGER_MARKETS = ['HDC', 'HIL', 'CHL'];
const CHALLENGER_REQUIRED_FIXTURES = 100;
const CHALLENGER_STALE_HOURS = 26;   // 每日 12:20 HKT 跑一次,超過一日即視為過期
const CHAL_STATUS = {
  insufficient_data: { text: '樣本未夠', cls: 'wait' },
  insufficient_chronological_partition: { text: '時序切分未夠', cls: 'wait' },
  tested_no_safe_upgrade: { text: '已測試 · 未達升級門檻', cls: 'hold' },
  prospective_shadow_collecting: { text: '前瞻影子樣本收集中', cls: 'wait' },
  prospective_tested_no_safe_upgrade: { text: '前瞻測試 · 未達升級門檻', cls: 'hold' },
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
  const reviewCount = CHALLENGER_MARKETS.filter((market) => {
    const test = tests[market] || {};
    return String(test.status || '') === 'candidate_passed_human_review_required' ||
      String((test.prospective_v3 || {}).status || '') === 'candidate_passed_human_review_required';
  }).length;
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

function challengerProspectiveV3(test) {
  if (!challengerIsPlainObject(test)) return '';
  const status = String(test.status || '');
  const label = challengerStatusLabel(status);
  const fixtures = numeric(test.prospective_fixtures) || 0;
  const required = numeric(test.minimum_prospective_fixtures) || 30;
  const remaining = test.remaining_fixtures == null ? Math.max(0, required - fixtures) : (numeric(test.remaining_fixtures) || 0);
  const pct = Math.max(0, Math.min(100, required > 0 ? fixtures / required * 100 : 0));
  const champion = (test.champion && test.champion.metrics) || {};
  const challenger = (test.challenger && test.challenger.metrics) || {};
  const delta = test.delta || {};
  const reviewed = status === 'candidate_passed_human_review_required';
  let body = `<div class="chal-progress">
      <div class="chal-progress-top"><span>前瞻獨立賽事(保留每個賽前階段)</span><b class="mono">${fixtures} / ${required}</b></div>
      <div class="chal-bar"><i style="width:${pct.toFixed(1)}%"></i></div>
      <div class="chal-progress-foot"><span class="dim">嚴格凍結後才計入</span>
        <span class="${remaining > 0 ? 'amber-txt' : 'good-txt'}">${remaining > 0 ? `仲差 ${remaining} 場先覆核` : '已夠 30 場前瞻測試'}</span></div>
      <div class="chal-rows dim">凍結截點 ${esc(challengerStamp(test.freeze_cutoff))} · 報告行數 ${numeric(test.prospective_rows) == null ? 0 : numeric(test.prospective_rows)}</div>
    </div>`;
  if (test.champion && test.challenger) {
    body += `<div class="chal-metrics" data-testid="metrics-challenger-hil-v3">
      <div class="chal-metric chal-metric-head"><span class="chal-metric-lbl">前瞻指標</span><span>現行冠軍</span><span>入球大細 v3</span><span>差距</span></div>
      ${challengerMetricRow('準確率', champion.accuracy, challenger.accuracy, delta.accuracy, false, (x) => pc(x, 1))}
      ${challengerMetricRow('Brier', champion.brier, challenger.brier, delta.brier, true, (x) => f3(x))}
      ${challengerMetricRow('對數損失', champion.log_loss, challenger.log_loss, delta.log_loss, true, (x) => f3(x))}
    </div>`;
  }
  if (Array.isArray(test.rejection_reasons) && test.rejection_reasons.length) {
    body += `<div class="chal-reasons"><span class="chal-reasons-lbl">未能升級原因</span>
      ${test.rejection_reasons.map((reason) => `<span class="chal-reason">${esc(challengerReasonLabel(reason))}</span>`).join('')}</div>`;
  } else if (reviewed) {
    body += `<div class="chal-review" data-testid="banner-challenger-hil-v3-review"><b>前瞻樣本通過全部安全門檻</b>
      <span>仍然<strong>未套用、亦唔會自動套用</strong>,只係通知人手覆核。</span></div>`;
  }
  return `<section class="chal-v3" data-testid="section-challenger-hil-v3">
    <h3>入球大細 v3 · 前瞻凍結影子驗證 <span class="chal-badge ${label.cls}" data-testid="status-challenger-hil-v3">${esc(label.text)}</span></h3>
    <p class="chal-hint dim">規格 ${esc((test.selected_spec || {}).id || '待凍結')}；不會重訓或改變，直至前瞻視窗完成。</p>
    ${body}<div class="chal-foot"><span>自動套用:<b class="bad-txt">否</b></span><span>只作隔離人手覆核</span></div>
  </section>`;
}

/* 皇冠 角球大細前瞻凍結影子驗證。與 入球大細 v3 完全分開,亦同「資料健康」無關。
 * 純顯示:唔會改任何預測、賽果、注碼或模擬倉。 */
const CHAL_CHL_STATUS = {
  prospective_shadow_collecting: { text: '前瞻影子樣本收集中', cls: 'wait' },
  insufficient_feature_coverage: { text: '特徵覆蓋不足 · 未評估', cls: 'wait' },
  prospective_tested_no_safe_upgrade: { text: '已測試 · 未達升級門檻', cls: 'hold' },
  candidate_passed_human_review_required: { text: '候選通過 · 等人手覆核', cls: 'review' },
};
const CHAL_CHL_STRATEGY = {
  market_favourite: '現行 HKJC 去水市場方向',
  always_under: '永遠買細(under)基準',
  closing_reference: 'T-5／收盤方向參考(只作基準)',
  team_corner_feature: '球隊角球特徵候選',
};
const CHAL_CHL_REASON = {
  minimum_prospective_fixtures: '前瞻獨立賽事未夠 30 場',
  identical_fixture_rows: '冠軍同候選樣本唔一致',
  candidate_differs_from_champion: '歷史揀返現行冠軍,冇可升級候選',
  meaningful_brier_improvement: 'Brier 改善未夠 0.01',
  log_loss_improved: '對數損失冇改善',
  accuracy_not_materially_worse: '準確率跌幅超過 2%',
  insufficient_feature_coverage: '缺乏賽前球隊角球特徵,唔會憑空填數',
  unscorable_rows: '有列無法評分',
  no_prospective_rows: '未有前瞻場次',
  direction_not_resolvable: '分唔到大細方向,唔會亂估',
  unscorable_settlement_target: '結算結果唔完整',
  selected_side_price_unavailable: '所選方向本身冇賽前賠率',
  opposite_side_price_unavailable: '策略揀咗另一邊,但冇該方向嘅賽前實際賠率',
  model_probability_unavailable: '模型未有機率,分唔到方向',
  aligned_price_unavailable_for_every_row: '未能逐場對齊方向同賠率',
  closing_odds_unavailable: '冇收盤價,CLV 無法計算',
};

function challengerChlStatus(status) {
  return CHAL_CHL_STATUS[status] || { text: status ? String(status) : '未知狀態', cls: 'hold' };
}
function challengerChlStrategy(id) {
  return id ? (CHAL_CHL_STRATEGY[id] || String(id)) : '待凍結';
}
function challengerChlReason(reason) {
  return CHAL_CHL_REASON[reason] || challengerReasonLabel(reason);
}
function challengerChlCell(label, value) {
  return `<span class="chal-chl-cell" data-label="${esc(label)}">${value}</span>`;
}
function challengerChlStageRows(test) {
  const stages = Array.isArray(test.stage_diagnostics) ? test.stage_diagnostics : [];
  if (!stages.length) return '';
  const rows = stages.map((item) => {
    const champion = item.champion || {};
    const under = item.always_under || {};
    return `<div class="chal-chl-row" data-testid="row-challenger-chl-stage-${esc(item.stage)}">
      ${challengerChlCell('階段', `<b>${esc(item.stage)}</b>`)}
      ${challengerChlCell('獨立賽事', numeric(item.unique_fixtures) || 0)}
      ${challengerChlCell('命中率', pc(champion.hit_rate, 1))}
      ${challengerChlCell('Brier', f3(champion.brier))}
      ${challengerChlCell('永遠買細 命中率', pc(under.hit_rate, 1))}
    </div>`;
  }).join('');
  return `<div class="chal-chl-table" data-testid="table-challenger-chl-stages">
    <div class="chal-chl-row chal-chl-head" aria-hidden="true">
      <span class="chal-chl-cell">階段</span><span class="chal-chl-cell">獨立賽事</span>
      <span class="chal-chl-cell">命中率</span><span class="chal-chl-cell">Brier</span>
      <span class="chal-chl-cell">永遠買細 命中率</span>
    </div>${rows}</div>
    <p class="chal-hint dim">階段指標只作<strong>相關性次要診斷</strong>,同一場出現多次,<b>唔可以相加當獨立樣本</b>。</p>`;
}
function challengerProspectiveCHL(test) {
  if (!challengerIsPlainObject(test)) return '';
  const status = String(test.status || '');
  const label = challengerChlStatus(status);
  const fixtures = numeric(test.prospective_fixtures) || 0;
  const required = numeric(test.minimum_prospective_fixtures) || 30;
  const strong = numeric(test.strong_sample_fixtures) || 100;
  const remaining = test.remaining_fixtures == null
    ? Math.max(0, required - fixtures) : (numeric(test.remaining_fixtures) || 0);
  const pct = Math.max(0, Math.min(100, required > 0 ? fixtures / required * 100 : 0));
  const champion = (test.champion && test.champion.metrics) || {};
  const challenger = (test.challenger && test.challenger.metrics) || {};
  const under = ((test.baselines || {}).always_under) || {};
  const closing = test.closing_reference || {};
  const delta = test.delta || {};
  const reviewed = status === 'candidate_passed_human_review_required';
  const rule = Array.isArray(test.primary_stage_rule) ? test.primary_stage_rule.join(' > ') : 'T-5 > T-30 > 首預';

  let body = `<div class="chal-chl-meta" data-testid="meta-challenger-chl">
      <div><span class="chal-split-lbl">凍結截點</span><b class="mono">${esc(challengerStamp(test.freeze_cutoff))}</b></div>
      <div><span class="chal-split-lbl">主樣本階段規則(已凍結)</span><b>${esc(rule)}</b></div>
      <div><span class="chal-split-lbl">選定策略</span><b>${esc(challengerChlStrategy(test.selected_strategy))}</b></div>
    </div>
    <div class="chal-progress">
      <div class="chal-progress-top"><span>前瞻獨立賽事(每場一行,唔係每階段一行)</span>
        <b class="mono">${fixtures} / ${required}</b></div>
      <div class="chal-bar"><i style="width:${pct.toFixed(1)}%"></i></div>
      <div class="chal-progress-foot"><span class="dim">嚴格凍結後開賽先計入</span>
        <span class="${remaining > 0 ? 'amber-txt' : 'good-txt'}">${remaining > 0 ? `仲差 ${remaining} 場先覆核` : `已夠 ${required} 場前瞻測試`}</span></div>
      <div class="chal-rows dim">對應階段列 ${numeric(test.prospective_rows) == null ? 0 : numeric(test.prospective_rows)} 行(只作參考)</div>
    </div>`;

  if (test.sample_warning === 'below_strong_sample') {
    body += `<div class="chal-chl-warn" data-testid="flag-challenger-chl-weak-sample">
      <b>樣本仍然偏細</b><span>少於 ${strong} 場獨立賽事,結論唔穩定,只可以當觀察。</span></div>`;
  }
  if (status === 'insufficient_feature_coverage') {
    body += `<div class="chal-chl-warn" data-testid="flag-challenger-chl-feature-coverage">
      <b>缺乏賽前球隊角球特徵</b>
      <span>報告<strong>唔會憑空填數,亦唔會用賽後資料回填</strong>,直接標示覆蓋不足。</span></div>`;
  }
  if (test.champion && test.challenger) {
    body += `<div class="chal-metrics" data-testid="metrics-challenger-chl">
      <div class="chal-metric chal-metric-head"><span class="chal-metric-lbl">前瞻指標</span>
        <span>現行冠軍</span><span>候選策略</span><span>差距</span></div>
      ${challengerMetricRow('準確率', champion.accuracy, challenger.accuracy, delta.accuracy, false, (x) => pc(x, 1))}
      ${challengerMetricRow('Brier', champion.brier, challenger.brier, delta.brier, true, (x) => f3(x))}
      ${challengerMetricRow('對數損失', champion.log_loss, challenger.log_loss, delta.log_loss, true, (x) => f3(x))}
    </div>
    <div class="chal-chl-base" data-testid="baselines-challenger-chl">
      <div><span class="chal-split-lbl">冠軍命中率</span><b class="mono">${pc(champion.hit_rate, 1)}</b>
        <span class="dim">${Array.isArray(champion.hit_rate_ci95) ? `Wilson 95% ${pc(champion.hit_rate_ci95[0], 0)}–${pc(champion.hit_rate_ci95[1], 0)}` : '區間不適用'}</span></div>
      <div><span class="chal-split-lbl">永遠買細 命中率</span><b class="mono">${pc(under.hit_rate, 1)}</b>
        <span class="dim">${numeric(under.unique_fixtures) || 0} 場</span></div>
      <div><span class="chal-split-lbl">T-5／收盤參考</span><b class="mono">${closing.available ? pc((closing.metrics || {}).hit_rate, 1) : '不可用'}</b>
        <span class="dim">${closing.available ? `覆蓋 ${pc(closing.coverage, 0)} · 只作基準` : '冇 T-5 快照'}</span></div>
    </div>`;
  }
  body += challengerChlStageRows(test);
  if (test.shadow_returns) {
    const shadow = test.shadow_returns;
    // ROI 只有喺每一場所揀方向都有「該方向」嘅賽前實際賠率先計得出;
    // 否則一定顯示不可計算,絕對唔會攞另一邊嘅賠率頂上。
    const roiAvailable = shadow.roi != null;
    const flips = numeric(shadow.direction_flips) || 0;
    body += `<p class="chal-hint dim" data-testid="note-challenger-chl-shadow">
      影子回報 · 所揀方向:${esc(challengerChlStrategy(shadow.strategy || test.selected_strategy))}
      <b class="mono">${roiAvailable ? pc(shadow.roi, 2) : '不可計算'}</b>
      ${roiAvailable ? '' : `<span data-testid="reason-challenger-chl-shadow">(${esc(challengerChlReason(shadow.reason))})</span>`}
      · CLV <b class="mono">${shadow.clv == null ? '不可用' : f3(shadow.clv)}</b>${shadow.clv == null ? '(冇收盤價)' : ''}
      · 方向對齊 ${numeric(shadow.aligned_rows) || 0}/${numeric(shadow.rows) || 0} 場${flips ? ` · 反向 ${flips} 場` : ''} ——
      <strong>唔代表優勢,亦唔係 +EV:用 HKJC 賠率預測 HKJC 賽果證明唔到正期望值;
      只有所揀方向本身有實際賽前賠率先會顯示數字,否則一律留空。</strong></p>`;
  }
  if (Array.isArray(test.rejection_reasons) && test.rejection_reasons.length) {
    body += `<div class="chal-reasons"><span class="chal-reasons-lbl">未能升級原因</span>
      ${test.rejection_reasons.map((reason) => `<span class="chal-reason">${esc(challengerChlReason(reason))}</span>`).join('')}</div>`;
  } else if (reviewed) {
    body += `<div class="chal-review" data-testid="banner-challenger-chl-review"><b>前瞻樣本通過全部安全門檻</b>
      <span>仍然<strong>未套用、亦唔會自動套用</strong>,只係通知人手覆核。</span></div>`;
  }
  return `<section class="chal-v3 chal-chl" data-testid="section-challenger-chl-prospective">
    <h3>角球大細前瞻凍結影子驗證 <span class="chal-badge ${label.cls}" data-testid="status-challenger-chl">${esc(label.text)}</span></h3>
    <p class="chal-hint dim">凍結後不會重訓或改變,直至前瞻視窗完成;策略、階段規則同截點喺第一次生產執行時已經定死。</p>
    ${body}<div class="chal-foot"><span>自動套用:<b class="bad-txt">否</b></span><span>只作隔離人手覆核</span></div>
  </section>`;
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
    <span>影子研究,唔影響現行預測</span></div>`;
  if (market === 'HIL') body += challengerProspectiveV3(test.prospective_v3);
  if (market === 'CHL') body += challengerProspectiveCHL(test.prospective_chl);

  return `<div class="card chal-card ${reviewing ? 'is-review' : ''}" data-testid="card-challenger-${market}">
    <h2 class="card-h">${name}
      <span class="chal-badge ${label.cls}" data-testid="status-challenger-${market}">${esc(label.text)}</span></h2>
    ${body}</div>`;
}

function renderChallenger() {
  const V = $('#viewChal');
  // The standalone Challenger UI smoke test evaluates this renderer without
  // bootstrapping the dashboard data object.  v2 is additive UI context only;
  // it must never prevent the legacy challenger report from rendering.
  const v2 = (typeof DATA !== 'undefined' && DATA && DATA.v2_challenger) || {};
  const v2Cutover = v2.cutover_at ? `政策截點 ${esc(v2.cutover_at)}` : '固定政策截點';
  const v2Activation = v2.activation_at ? `啟用界線 ${esc(v2.activation_at)}` : '未見啟用界線';
  const v2Markets = (((v2.stats || {}).by_market) || {});
  const v2Rows = Object.keys(v2Markets).map((market) => {
    const item = v2Markets[market] || {}, model = ((item.league_shrunk || {}).probability_metrics) || {};
    const base = item.market_no_vig_baseline || {}, clv = item.clv || {};
    return `<div class="condition-kpi"><span>${esc(marketLabel(market))} · 分層收縮</span><b class="mono">${numeric(model.unique_fixtures) == null ? '未有證據' : model.unique_fixtures} 場</b><small>ROI ${pc(model.roi, 2)} · Brier ${f3(model.brier)} · Log Loss ${f3(model.log_loss)} · 校準 ${pc(model.calibration, 1)} · 無水基準 ${base.available ? f3(base.brier) : '未有證據'} · CLV 覆蓋 ${pc(clv.coverage, 0)}</small></div>`;
  }).join('');
  const v2Banner = `<div class="shadow-note condition-note" data-testid="note-crown-v2-challenger"><strong>v2挑戰者研究中／非正式推介</strong><span>只收晚於 ${v2Cutover} 與 ${v2Activation} 的首次原生賽前 T-5。每市場按 unique fixture＋market 計；同市場雙邊、同盤口、同觀測時間及同來源先可得無水機率，否則顯示「未有證據」。v1 歷史失敗基準按 v2 啟用時唯讀封存，v2 不發 actionable Telegram、不用 Kelly、不會自動升格。</span>${v2Rows ? `<div class="condition-grid">${v2Rows}</div>` : '<span>前瞻樣本／CLV 尚未有證據，不會用 0 或賽後資料代替。</span>'}</div>`;
  if (!V) return;
  if (CHAL.state === 'idle' || (CHAL.state === 'loading' && !CHAL.payload)) {
    V.innerHTML = challengerHead(v2Banner) +
      `<div class="card"><div class="empty2" data-testid="state-challenger-loading">正在讀取挑戰模型報告…</div></div>`;
    challengerBind();
    return;
  }
  if (CHAL.state === 'missing') {
    V.innerHTML = challengerHead(v2Banner) +
      `<div class="card"><div class="empty2" data-testid="state-challenger-missing">
        報告未生成。挑戰模型每日 12:20 HKT 先評估一次,第一次評估之前唔會有檔案。</div></div>`;
    challengerBind();
    return;
  }
  if (CHAL.state === 'error') {
    V.innerHTML = challengerHead(v2Banner) +
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
  const visibleMarkets = CHALLENGER_MARKETS.filter((market) => {
    const test = tests[market] || {};
    return CHAL_FILTER === 'all' ||
      String(test.status || '') === 'candidate_passed_human_review_required' ||
      String((test.prospective_v3 || {}).status || '') === 'candidate_passed_human_review_required';
  });
  const cards = visibleMarkets.length
    ? `<div class="chal-grid">${visibleMarkets.map((market) => challengerMarketCard(market, tests[market])).join('')}</div>`
    : `<div class="card chal-filter-empty" data-testid="state-challenger-filter-empty">
        <div class="empty2"><b>暫時冇模型等待覆核</b><span>新候選通過全部安全門檻後,會自動出現喺呢個篩選。</span></div>
      </div>`;
  V.innerHTML = challengerHead(banner + v2Banner) +
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
