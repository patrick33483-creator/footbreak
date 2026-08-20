import assert from 'node:assert/strict';
import fs from 'node:fs';
import path from 'node:path';
import { fileURLToPath } from 'node:url';

const here = path.dirname(fileURLToPath(import.meta.url));
const source = fs.readFileSync(path.join(here, '..', 'dashboard', 'app.js'), 'utf8');

function makeNode() {
  return {
    hidden: false, innerHTML: '', textContent: '', disabled: false, onclick: null, oninput: null,
    dataset: {}, classList: { add() {}, remove() {}, toggle() {} },
  };
}

function makeDashboard(fetchImpl) {
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
    __CROWN_FETCH_TIMEOUT_MS__: 1,
    location: { href: 'http://crown.test/index.html', origin: 'http://crown.test' },
    addEventListener() {}, scrollTo() {}, scrollY: 0, innerHeight: 900,
  };
  const instrumented = source.replace(
    /\nboot\(\);\s*$/,
    `
return {
  boot,
  detailHtml() { return document.querySelector('#detail').innerHTML; },
  fixtureHtml() { return document.querySelector('#fixtures').innerHTML; },
  railCount() { return document.querySelector('#railCount').textContent; },
};`,
  );
  assert.notEqual(instrumented, source, 'test harness must suppress automatic boot');
  return new Function(
    'document', 'window', 'fetch', 'setInterval', 'clearInterval', 'requestAnimationFrame',
    instrumented,
  )(document, window, fetchImpl, () => 1, () => {}, (callback) => callback());
}

const payload = {
  schema_version: 'crown-dashboard-v2',
  generated_at: '2026-08-20T22:00:00+08:00',
  matches: [],
  ledger: { bets: [], stats: {}, log: [] },
  stage_completeness: {},
  prediction_history: { stats: {} },
};

{
  const calls = [];
  const app = makeDashboard(async (url) => {
    calls.push(String(url));
    if (calls.length === 1) return new Promise(() => {});
    return { ok: true, json: async () => payload };
  });
  await app.boot();
  assert.equal(calls.length, 2, 'a hung static payload must fall back to the API');
  assert.match(calls[0], /data\.json/);
  assert.match(calls[1], /\/api\/data/);
  assert.match(app.railCount(), /顯示 0 \/ 0 場/);
  assert.doesNotMatch(app.detailHtml(), /載入失敗/);
}

{
  const app = makeDashboard(async () => new Promise(() => {}));
  await app.boot();
  assert.match(app.detailHtml(), /皇冠賽事資料暫時載入失敗/);
  assert.match(app.detailHtml(), /重新載入/);
  assert.match(app.fixtureHtml(), /載入失敗/);
  assert.equal(app.railCount(), '未能讀取賽事');
}

{
  const app = makeDashboard(async () => ({
    ok: true,
    json: async () => ({ generated_at: payload.generated_at, ledger: {} }),
  }));
  await app.boot();
  assert.match(app.detailHtml(), /資料格式不完整/);
}

console.log('Crown dashboard boot resilience smoke passed');
