import assert from 'node:assert/strict';
import fs from 'node:fs';
import path from 'node:path';
import { fileURLToPath } from 'node:url';

const here = path.dirname(fileURLToPath(import.meta.url));
const source = fs.readFileSync(path.join(here, '..', '..', 'hkjc-dashboard', 'app.js'), 'utf8');

function node() {
  return {
    hidden: false, innerHTML: '', textContent: '', disabled: false, onclick: null,
    className: '', dataset: {},
    classList: { add() {}, remove() {}, toggle() {} },
  };
}

function makeDashboard(initialMain, initialHistory) {
  const nodes = new Map();
  const getNode = (id) => {
    if (!nodes.has(id)) nodes.set(id, node());
    return nodes.get(id);
  };
  const document = {
    body: { appendChild() {} }, documentElement: { scrollHeight: 0 },
    querySelector(selector) { return selector.startsWith('#') ? getNode(selector.slice(1)) : null; },
    querySelectorAll() { return []; }, createElement() { return node(); },
  };
  const window = {
    location: { href: 'http://footbreak.test/index.html', origin: 'http://footbreak.test' },
    addEventListener() {}, scrollTo() {}, scrollY: 0, innerHeight: 900,
  };
  let main = initialMain, history = initialHistory, historyStatus = 200;
  const calls = [];
  const fetch = async (url) => {
    calls.push(String(url));
    if (String(url).includes('history.json')) {
      return { ok: historyStatus >= 200 && historyStatus < 300, status: historyStatus, json: async () => history };
    }
    return { ok: true, status: 200, json: async () => main };
  };
  const instrumented = source.replace(/\nboot\(\);\s*$/, `
return {
  boot, refresh,
  showHistory() { VIEW = 'fc'; render(); },
  showMore() { HISTORY_VISIBLE += HISTORY_PAGE_SIZE; renderFc(); },
  filterHistory(stage) { HISTORY_STAGE = stage; HISTORY_VISIBLE = HISTORY_PAGE_SIZE; renderFc(); },
  retryHistory() { return loadHistory({ force: true }); },
  historyState() { return HISTORY; },
  historyHtml() { return document.querySelector('#viewFc').innerHTML; },
  predictionHtml() { return document.querySelector('#fixtures').innerHTML + document.querySelector('#detail').innerHTML; },
};`);
  assert.notEqual(instrumented, source, 'test harness must suppress automatic boot');
  const app = new Function(
    'document', 'window', 'fetch', 'setInterval', 'clearInterval', 'requestAnimationFrame',
    instrumented,
  )(document, window, fetch, () => 0, () => {}, (callback) => callback());
  return {
    app, calls,
    setMain(value) { main = value; }, setHistory(value) { history = value; },
    setHistoryStatus(value) { historyStatus = value; },
  };
}

async function settle() {
  await new Promise((resolve) => setImmediate(resolve));
  await new Promise((resolve) => setImmediate(resolve));
}

const rows = Array.from({ length: 120 }, (_, index) => ({
  match_id: `m-${index}`, stage: index < 60 ? 'T-5' : 'T-30',
  kickoff: `2026-08-${String((index % 28) + 1).padStart(2, '0')}T12:00:00+08:00`,
  home: `Home ${index}`, away: `Away ${index}`, league: 'Test',
  market_predictions: [{ code: 'HDC', side: 'H', line: -0.5, odds: 1.91 }],
}));
const stats = { matches: 120, predictions: 120, by_stage: {}, by_market: {} };
const main = {
  generated_at: '2026-08-19T22:00:00+08:00', matches: [], ledger: { bets: [], stats: {}, log: [] },
  prediction_history: { stats }, history_data_url: 'history.json', history_data_version: 'history-v1',
};
const artifact = {
  schema_version: 'footbreak-history-v1', generated_at: main.generated_at,
  history_data_version: 'history-v1', prediction_history: { rows, stats },
};

const dashboard = makeDashboard(main, artifact);
await dashboard.app.boot();
assert.equal(dashboard.calls.length, 1, 'boot must fetch only lightweight data.json');
assert.ok(!dashboard.calls[0].includes('history.json'), 'boot must not request history sidecar');

dashboard.app.showHistory(); await settle();
assert.equal(dashboard.calls.filter((url) => url.includes('history.json')).length, 1, 'opening history fetches once');
dashboard.app.showHistory(); await settle();
assert.equal(dashboard.calls.filter((url) => url.includes('history.json')).length, 1, 'loaded history uses session cache');
assert.equal((dashboard.app.historyHtml().match(/<td data-label="開賽"/g) || []).length, 50, 'initial render limits rows to 50');
assert.match(dashboard.app.historyHtml(), /顯示更多/, 'pagination control is visible');
dashboard.app.showMore();
assert.match(dashboard.app.historyHtml(), /100 \/ 120 筆/, 'show more renders one additional page');
dashboard.app.filterHistory('T-5');
assert.match(dashboard.app.historyHtml(), /50 \/ 60 筆/, 'stage filter resets page size');
assert.equal((dashboard.app.historyHtml().match(/<td data-label="開賽"/g) || []).length, 50, 'filter removes old pages');

const v2Main = { ...main, generated_at: '2026-08-19T22:01:00+08:00', history_data_version: 'history-v2' };
const v2Artifact = { ...artifact, generated_at: v2Main.generated_at, history_data_version: 'history-v2', prediction_history: { rows: rows.slice(0, 3), stats } };
dashboard.setMain(v2Main); dashboard.setHistory(v2Artifact);
await dashboard.app.refresh(true); await settle();
assert.equal(dashboard.calls.filter((url) => url.includes('history.json')).length, 2, 'open history refreshes only on sidecar version change');
assert.match(dashboard.app.historyHtml(), /已顯示全部 3 筆/, 'new sidecar replaces cached rows');

dashboard.setHistoryStatus(503);
await dashboard.app.retryHistory(); await settle();
assert.match(dashboard.app.historyHtml(), /預測紀錄讀取失敗/, 'failed history read is visible and retryable');
assert.match(dashboard.app.historyHtml(), /重新讀取/, 'error state includes retry control');
dashboard.setHistoryStatus(200); dashboard.setHistory(v2Artifact);
await dashboard.app.retryHistory(); await settle();
assert.equal(dashboard.app.historyState().state, 'ready', 'retry recovers a failed sidecar request');

const legacy = { ...main, prediction_history: { rows: rows.slice(0, 2), stats }, history_data_url: undefined, history_data_version: undefined };
const legacyDashboard = makeDashboard(legacy, artifact);
await legacyDashboard.app.boot(); legacyDashboard.app.showHistory(); await settle();
assert.equal(legacyDashboard.calls.filter((url) => url.includes('history.json')).length, 0, 'legacy inline history remains supported');
assert.equal(legacyDashboard.app.historyState().source, 'inline');

const bootstrap = {
  ...main,
  dashboard_status: {
    state: 'not_yet_run',
    message: '系統尚未執行首次掃描；暫時未有賽事及預測紀錄。',
  },
};
const bootstrapDashboard = makeDashboard(bootstrap, artifact);
await bootstrapDashboard.app.boot();
assert.match(
  bootstrapDashboard.app.predictionHtml(),
  /尚未執行首次掃描/,
  'offline first-install payload is an honest empty dashboard state',
);

console.log('Footbreak lazy history loading smoke passed');
