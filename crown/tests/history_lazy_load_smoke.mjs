import assert from 'node:assert/strict';
import fs from 'node:fs';
import path from 'node:path';
import { fileURLToPath } from 'node:url';

const here = path.dirname(fileURLToPath(import.meta.url));
const source = fs.readFileSync(path.join(here, '..', 'dashboard', 'app.js'), 'utf8');

function makeNode() {
  return {
    hidden: false, innerHTML: '', textContent: '', disabled: false, onclick: null,
    classList: { add() {}, remove() {}, toggle() {} },
  };
}

function makeDashboard(mainPayload, historyPayload) {
  const nodes = new Map();
  const node = (id) => {
    if (!nodes.has(id)) nodes.set(id, makeNode());
    return nodes.get(id);
  };
  const document = {
    body: { appendChild() {} },
    documentElement: { scrollHeight: 0 },
    querySelector(selector) {
      return selector.startsWith('#') ? node(selector.slice(1)) : null;
    },
    querySelectorAll() { return []; },
    createElement() { return makeNode(); },
  };
  const window = {
    location: { href: 'http://crown.test/index.html', origin: 'http://crown.test' },
    addEventListener() {}, scrollTo() {}, scrollY: 0, innerHeight: 900,
  };
  const calls = [];
  const fetch = async (url) => {
    calls.push(String(url));
    const payload = String(url).includes('history.json') ? historyPayload : mainPayload;
    return { ok: true, json: async () => payload };
  };
  const instrumented = source.replace(
    /\nboot\(\);\s*$/,
    `
return {
  boot,
  showHistory() { VIEW = 'history'; render(); },
  showMore() { HISTORY_VISIBLE += HISTORY_PAGE_SIZE; renderHistory(); },
  filterHistory(stage) {
    HISTORY_STAGE = stage; HISTORY_VISIBLE = HISTORY_PAGE_SIZE; renderHistory();
  },
  historyState() { return HISTORY; },
  historyHtml() { return document.querySelector('#viewHistory').innerHTML; },
};`,
  );
  assert.notEqual(instrumented, source, 'test harness must suppress automatic boot');
  const app = new Function(
    'document', 'window', 'fetch', 'setInterval', 'clearInterval', 'requestAnimationFrame',
    instrumented,
  )(document, window, fetch, () => 0, () => {}, (callback) => callback());
  return { app, calls };
}

async function settle() {
  await new Promise((resolve) => setImmediate(resolve));
  await new Promise((resolve) => setImmediate(resolve));
}

const rows = Array.from({ length: 120 }, (_, index) => ({
  match_id: `m-${index}`,
  stage: index < 60 ? 'T-5' : 'T-30',
  kickoff: `2026-08-${String((index % 28) + 1).padStart(2, '0')}T12:00:00+08:00`,
  home: `Home ${index}`, away: `Away ${index}`, league: 'Test',
  market_predictions: [{ code: 'HDC', side: 'H', line: -0.5, odds: 1.91 }],
}));
const stats = { matches: 120, predictions: 120, by_stage: {}, by_market: {} };
const main = {
  schema_version: 'crown-dashboard-v2', generated_at: '2026-08-19T22:00:00+08:00',
  matches: [], ledger: { bets: [], stats: {}, log: [] }, stage_completeness: {},
  prediction_history: { stats }, history_data_url: 'history.json',
  history_data_version: 'history-v1',
};
const artifact = {
  schema_version: 'crown-history-v2', generated_at: main.generated_at,
  history_data_version: 'history-v1', prediction_history: { rows, stats },
};

const dashboard = makeDashboard(main, artifact);
await dashboard.app.boot();
assert.equal(dashboard.calls.length, 1, 'boot must fetch only the lightweight dashboard');
assert.ok(!dashboard.calls[0].includes('history.json'), 'boot must not request history');

dashboard.app.showHistory();
await settle();
assert.equal(
  dashboard.calls.filter((url) => url.includes('history.json')).length,
  1,
  'opening History must request the sidecar exactly once',
);
dashboard.app.showHistory();
await settle();
assert.equal(
  dashboard.calls.filter((url) => url.includes('history.json')).length,
  1,
  'an already loaded History view must use its in-memory cache',
);
assert.equal(
  (dashboard.app.historyHtml().match(/<td data-label="開賽"/g) || []).length,
  50,
  'initial history rendering must create only 50 history row elements',
);
assert.match(dashboard.app.historyHtml(), /顯示更多/, 'history pagination must expose 顯示更多');

dashboard.app.showMore();
assert.match(dashboard.app.historyHtml(), /100 \/ 120 筆/, '顯示更多 must add one page');
dashboard.app.filterHistory('T-5');
assert.match(
  dashboard.app.historyHtml(),
  /50 \/ 60 筆/,
  'filtering must reset pagination to the first 50 rows',
);
assert.equal(
  (dashboard.app.historyHtml().match(/<td data-label="開賽"/g) || []).length,
  50,
  'filtering must not leave prior pages in the DOM',
);

const legacy = {
  ...main,
  prediction_history: { rows: rows.slice(0, 2), stats },
  history_data_url: undefined,
  history_data_version: undefined,
};
const legacyDashboard = makeDashboard(legacy, artifact);
await legacyDashboard.app.boot();
legacyDashboard.app.showHistory();
await settle();
assert.equal(
  legacyDashboard.calls.filter((url) => url.includes('history.json')).length,
  0,
  'legacy inline history must remain compatible without requesting a sidecar',
);
assert.equal(legacyDashboard.app.historyState().source, 'inline');

console.log('Crown lazy history loading smoke passed');
