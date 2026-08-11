/* 挑戰模型面板嘅純邏輯冒煙測試。
 * 由 app.js 抽出挑戰模型區塊,注入最少量替身,檢查各種狀態嘅輸出。
 * 唔會載入預測、結算或落注邏輯。 */
import { readFileSync } from 'node:fs';
import { dirname, resolve } from 'node:path';
import { fileURLToPath } from 'node:url';

const ROOT = resolve(dirname(fileURLToPath(import.meta.url)), '..', '..');
const START = '/* ══════════════════════ 挑戰模型 · 隔離影子研究 ══════════════════════ */';
const END = '\nboot();';

function assert(condition, message) {
  if (!condition) {
    console.error('FAIL ' + message);
    process.exitCode = 1;
  }
}

function loadChallengerModule(appPath) {
  const source = readFileSync(appPath, 'utf-8');
  const start = source.indexOf(START);
  const end = source.indexOf(END, start);
  if (start < 0 || end < 0) throw new Error('challenger block not found in ' + appPath);
  const block = source.slice(start, end);
  const view = { innerHTML: '' };
  const $ = (selector) => (selector === '#viewChal' ? view : null);
  const numeric = (x) => (x == null || x === '' || !Number.isFinite(Number(x)) ? null : Number(x));
  const pc = (x, d = 1) => (numeric(x) == null ? '—' : (numeric(x) * 100).toFixed(d) + '%');
  const f3 = (x) => (numeric(x) == null ? '—' : numeric(x).toFixed(3));
  const esc = (s) => String(s ?? '').replace(/[&<>"']/g, (c) =>
    ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c]));
  const MKT = { HDC: '讓球', HIL: '入球大小', CHL: '總角球大小' };
  const factory = new Function(
    '$', 'numeric', 'pc', 'f3', 'esc', 'MKT', 'VIEW', 'fetch',
    `${block}\nreturn { renderChallenger, challengerValidate, challengerMarketCard, get CHAL() { return CHAL; }, set CHAL(v) { CHAL = v; }, view: $('#viewChal') };`
  );
  return factory($, numeric, pc, f3, esc, MKT, 'chal', async () => { throw new Error('no network'); });
}

const isoHoursAgo = (hours) => new Date(Date.now() - hours * 3600000).toISOString();

function evaluatedTest(overrides) {
  return {
    status: 'tested_no_safe_upgrade',
    eligible_fixtures: 142,
    eligible_rows: 380,
    train_fixtures: 99,
    holdout_fixtures: 43,
    champion: { metrics: { n: 120, accuracy: 0.55, brier: 0.243, log_loss: 0.681 } },
    challenger: { metrics: { n: 120, accuracy: 0.57, brier: 0.238, log_loss: 0.673 } },
    delta: { accuracy: 0.02, brier: -0.005, log_loss: -0.008 },
    checks: { meaningful_brier_improvement: false },
    rejection_reasons: ['meaningful_brier_improvement'],
    auto_apply: false,
    ...overrides,
  };
}

for (const dashboard of ['hkjc-dashboard/app.js', 'crown/dashboard/app.js']) {
  const mod = loadChallengerModule(resolve(ROOT, dashboard));
  const html = () => mod.view.innerHTML;

  // 載入中
  mod.CHAL = { state: 'idle', payload: null, error: '', loadedAt: null };
  mod.renderChallenger();
  assert(html().includes('state-challenger-loading'), `${dashboard}: loading state`);
  assert(html().includes('永不自動套用'), `${dashboard}: loading keeps isolation note`);

  // 未生成 (404)
  mod.CHAL = { state: 'missing', payload: null, error: '', loadedAt: Date.now() };
  mod.renderChallenger();
  assert(html().includes('state-challenger-missing'), `${dashboard}: missing state`);
  assert(html().includes('12:20'), `${dashboard}: missing explains daily job`);

  // 格式錯誤
  mod.CHAL = { state: 'error', payload: null, error: '報告唔係有效 JSON', loadedAt: Date.now() };
  mod.renderChallenger();
  assert(html().includes('state-challenger-error'), `${dashboard}: error state`);
  assert(html().includes('報告唔係有效 JSON'), `${dashboard}: error message shown`);

  // 驗證器
  assert(mod.challengerValidate(null) !== '', `${dashboard}: rejects non-object`);
  assert(mod.challengerValidate({ policy: {}, systems: {} }) !== '', `${dashboard}: rejects missing system`);

  // 樣本不足
  const insufficient = mod.challengerMarketCard('CHL', {
    status: 'insufficient_data',
    eligible_fixtures: 37,
    required_fixtures: 100,
    remaining_fixtures: 63,
    rejection_reasons: ['minimum_eligible_fixtures'],
    auto_apply: false,
  });
  assert(insufficient.includes('37 / 100'), 'insufficient shows unique fixtures progress');
  assert(insufficient.includes('仲差 63 場'), 'insufficient shows remaining fixtures');
  assert(insufficient.includes('樣本未夠'), 'insufficient status is translated');
  assert(insufficient.includes('合資格賽事未夠 100 場'), 'rejection reason is translated');
  assert(!insufficient.includes('已套用'), 'insufficient never implies applied');

  // 已評估但未達門檻
  const tested = mod.challengerMarketCard('HDC', evaluatedTest({}));
  assert(tested.includes('已測試 · 未達升級門檻'), 'tested status translated');
  assert(tested.includes('訓練場次'), 'train fixtures shown');
  assert(tested.includes('驗證場次(holdout)'), 'holdout fixtures shown');
  assert(tested.includes('Brier') && tested.includes('對數損失') && tested.includes('準確率'),
    'all three metrics rendered');
  assert(tested.includes('0.243') && tested.includes('0.238'), 'champion and challenger brier shown');
  assert(tested.includes('-0.005'), 'brier delta shown');
  assert(tested.includes('Brier 改善未夠 0.01'), 'brier rejection translated');
  assert(tested.includes('自動套用:<b class="bad-txt">否</b>'), 'auto apply is explicitly no');

  // 通過門檻,等待人手覆核
  const review = mod.challengerMarketCard('HIL', evaluatedTest({
    status: 'candidate_passed_human_review_required',
    rejection_reasons: [],
  }));
  assert(review.includes('候選通過 · 等人手覆核'), 'review status translated');
  assert(review.includes('is-review'), 'review card is visually prominent');
  assert(review.includes('未套用、亦唔會自動套用'), 'review never implies applied');
  assert(!/已套用|已上線|已升級/.test(review), 'review avoids applied wording');

  // 過期報告
  mod.CHAL = {
    state: 'ready',
    payload: {
      generated_at: isoHoursAgo(50),
      policy: { auto_apply: false },
      systems: {
        footbreak: { review_required: false, tests: {} },
        crown: { review_required: false, tests: {} },
      },
    },
    error: '',
    loadedAt: Date.now(),
  };
  mod.renderChallenger();
  assert(html().includes('flag-challenger-stale'), `${dashboard}: stale report flagged`);

  // 新鮮報告 + 需要覆核
  mod.CHAL = {
    state: 'ready',
    payload: {
      generated_at: isoHoursAgo(2),
      policy: { auto_apply: false },
      systems: {
        footbreak: { review_required: true, tests: { HDC: evaluatedTest({}) } },
        crown: { review_required: true, tests: { HDC: evaluatedTest({}) } },
      },
    },
    error: '',
    loadedAt: Date.now(),
  };
  mod.renderChallenger();
  assert(!html().includes('flag-challenger-stale'), `${dashboard}: fresh report not flagged`);
  assert(html().includes('banner-challenger-review'), `${dashboard}: review banner shown`);
  assert(html().includes('仍然<strong>未套用</strong>'), `${dashboard}: banner says not applied`);
  assert(html().includes('card-challenger-HIL'), `${dashboard}: every market card rendered`);
}

if (!process.exitCode) console.log('OK challenger dashboard UI smoke');
