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
  const document = { querySelectorAll: () => [] };
  const factory = new Function(
    '$', 'numeric', 'pc', 'f3', 'esc', 'MKT', 'VIEW', 'fetch', 'document',
    `${block}\nreturn { renderChallenger, challengerValidate, challengerMarketCard, get CHAL() { return CHAL; }, set CHAL(v) { CHAL = v; }, get CHAL_FILTER() { return CHAL_FILTER; }, set CHAL_FILTER(v) { CHAL_FILTER = v; }, view: $('#viewChal') };`
  );
  return factory($, numeric, pc, f3, esc, MKT, 'chal', async () => { throw new Error('no network'); }, document);
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

  if (dashboard === 'crown/dashboard/app.js') {
    const v3 = mod.challengerMarketCard('HIL', evaluatedTest({
      prospective_v3: {
        status: 'prospective_shadow_collecting',
        freeze_cutoff: isoHoursAgo(8),
        selected_spec: { id: 'conservative_25' },
        minimum_prospective_fixtures: 30,
        prospective_fixtures: 12,
        prospective_rows: 31,
        remaining_fixtures: 18,
        auto_apply: false,
      },
    }));
    assert(v3.includes('section-challenger-hil-v3'), `${dashboard}: v3 prospective panel`);
    assert(v3.includes('前瞻影子樣本收集中'), `${dashboard}: v3 collecting status`);
    assert(v3.includes('12 / 30'), `${dashboard}: v3 unique fixture progress`);
    assert(v3.includes('自動套用:<b class="bad-txt">否</b>'), `${dashboard}: v3 stays isolated`);

    // 皇冠 CHL 前瞻凍結影子驗證(獨立於 HIL v3,亦獨立於資料健康)
    const chlCollecting = mod.challengerMarketCard('CHL', evaluatedTest({
      prospective_chl: {
        status: 'prospective_shadow_collecting',
        freeze_cutoff: isoHoursAgo(30),
        primary_unit: 'one_row_per_unique_fixture',
        primary_stage_rule: ['T-5', 'T-30', '首預'],
        selected_strategy: 'always_under',
        minimum_prospective_fixtures: 30,
        strong_sample_fixtures: 100,
        prospective_fixtures: 29,
        prospective_rows: 74,
        remaining_fixtures: 1,
        stage_diagnostics: [
          { stage: '首預', unique_fixtures: 29, rows: 29, champion: { hit_rate: 0.48, brier: 0.25 }, always_under: { hit_rate: 0.52 }, correlated_secondary_diagnostic: true },
          { stage: 'T-30', unique_fixtures: 27, rows: 27, champion: { hit_rate: 0.5, brier: 0.248 }, always_under: { hit_rate: 0.5 }, correlated_secondary_diagnostic: true },
          { stage: 'T-5', unique_fixtures: 18, rows: 18, champion: { hit_rate: 0.55, brier: 0.244 }, always_under: { hit_rate: 0.45 }, correlated_secondary_diagnostic: true },
        ],
        closing_reference: { available: true, coverage: 0.62, benchmark_only: true, metrics: { hit_rate: 0.55 } },
        auto_apply: false,
      },
    }));
    assert(chlCollecting.includes('section-challenger-chl-prospective'), `${dashboard}: CHL prospective panel`);
    assert(chlCollecting.includes('前瞻影子樣本收集中'), `${dashboard}: CHL collecting status`);
    assert(chlCollecting.includes('29 / 30'), `${dashboard}: CHL unique fixture progress`);
    assert(chlCollecting.includes('每場一行,唔係每階段一行'), `${dashboard}: CHL primary unit stated`);
    assert(chlCollecting.includes('T-5 &gt; T-30 &gt; 首預') || chlCollecting.includes('T-5 > T-30 > 首預'),
      `${dashboard}: frozen primary-stage rule shown`);
    assert(chlCollecting.includes('永遠買細(under)基準'), `${dashboard}: selected strategy shown`);
    assert(chlCollecting.includes('table-challenger-chl-stages'), `${dashboard}: CHL stage diagnostics shown`);
    assert(chlCollecting.includes('唔可以相加當獨立樣本'), `${dashboard}: stage rows are not independent`);
    assert(chlCollecting.includes('自動套用:<b class="bad-txt">否</b>'), `${dashboard}: CHL stays isolated`);
    assert(!chlCollecting.includes('section-challenger-hil-v3'), `${dashboard}: CHL card has no HIL v3 section`);
    assert(!v3.includes('section-challenger-chl-prospective'), `${dashboard}: HIL card has no CHL section`);

    const chlFeature = mod.challengerMarketCard('CHL', evaluatedTest({
      prospective_chl: {
        status: 'insufficient_feature_coverage',
        freeze_cutoff: isoHoursAgo(40),
        primary_stage_rule: ['T-5', 'T-30', '首預'],
        selected_strategy: 'team_corner_feature',
        minimum_prospective_fixtures: 30,
        prospective_fixtures: 41,
        prospective_rows: 110,
        rejection_reasons: ['insufficient_feature_coverage'],
        auto_apply: false,
      },
    }));
    assert(chlFeature.includes('flag-challenger-chl-feature-coverage'), `${dashboard}: CHL feature coverage flag`);
    assert(chlFeature.includes('唔會憑空填數'), `${dashboard}: CHL never invents features`);

    const chlPassed = mod.challengerMarketCard('CHL', evaluatedTest({
      prospective_chl: {
        status: 'candidate_passed_human_review_required',
        freeze_cutoff: isoHoursAgo(200),
        primary_stage_rule: ['T-5', 'T-30', '首預'],
        selected_strategy: 'always_under',
        minimum_prospective_fixtures: 30,
        strong_sample_fixtures: 100,
        prospective_fixtures: 44,
        prospective_rows: 121,
        remaining_fixtures: 0,
        sample_warning: 'below_strong_sample',
        champion: { metrics: { accuracy: 0.5, brier: 0.25, log_loss: 0.69, hit_rate: 0.5, hit_rate_ci95: [0.36, 0.64], unique_fixtures: 44 } },
        challenger: { metrics: { accuracy: 0.56, brier: 0.235, log_loss: 0.67, hit_rate: 0.56, unique_fixtures: 44 } },
        baselines: { always_under: { hit_rate: 0.56, unique_fixtures: 44 } },
        closing_reference: { available: false, status: 'unavailable_no_t5_snapshot', coverage: 0 },
        shadow_returns: {
          strategy: 'always_under', roi: 0.031, clv: null,
          reason: 'closing_odds_unavailable', rows: 44, aligned_rows: 44, direction_flips: 0,
        },
        delta: { accuracy: 0.06, brier: -0.015, log_loss: -0.02 },
        rejection_reasons: [],
        auto_apply: false,
      },
    }));
    assert(chlPassed.includes('banner-challenger-chl-review'), `${dashboard}: CHL passed banner`);
    assert(chlPassed.includes('未套用、亦唔會自動套用'), `${dashboard}: CHL passed never implies applied`);
    assert(chlPassed.includes('flag-challenger-chl-weak-sample'), `${dashboard}: CHL weak sample warning`);
    assert(chlPassed.includes('Wilson 95%'), `${dashboard}: CHL Wilson interval shown`);
    assert(chlPassed.includes('不可用'), `${dashboard}: CHL closing reference unavailable`);
    assert(chlPassed.includes('證明唔到正期望值'), `${dashboard}: CHL never claims +EV`);
    // An aligned shadow return still may never read as an edge.
    assert(chlPassed.includes('唔代表優勢,亦唔係 +EV'), `${dashboard}: CHL never implies +EV`);
    assert(chlPassed.includes('方向對齊 44/44 場'), `${dashboard}: CHL shows direction alignment`);
    assert(chlPassed.includes('影子回報 · 所揀方向:永遠買細(under)基準'),
      `${dashboard}: CHL shadow return names the strategy direction`);

    // A strategy that flips away from the priced side must show no ROI at all.
    const chlFlipped = mod.challengerMarketCard('CHL', evaluatedTest({
      prospective_chl: {
        status: 'prospective_tested_no_safe_upgrade',
        freeze_cutoff: isoHoursAgo(200),
        primary_stage_rule: ['T-5', 'T-30', '首預'],
        selected_strategy: 'always_under',
        minimum_prospective_fixtures: 30,
        prospective_fixtures: 41,
        prospective_rows: 110,
        remaining_fixtures: 0,
        champion: { metrics: { accuracy: 0.5, brier: 0.25, log_loss: 0.69, hit_rate: 0.5, unique_fixtures: 41 } },
        challenger: { metrics: { accuracy: 0.49, brier: 0.26, log_loss: 0.7, hit_rate: 0.49, unique_fixtures: 41 } },
        baselines: { always_under: { hit_rate: 0.49, unique_fixtures: 41 } },
        closing_reference: { available: false, status: 'unavailable_no_t5_snapshot', coverage: 0 },
        shadow_returns: {
          strategy: 'always_under', roi: null, clv: null,
          reason: 'opposite_side_price_unavailable',
          rows: 41, aligned_rows: 0, direction_flips: 41,
        },
        delta: { accuracy: -0.01, brier: 0.01, log_loss: 0.01 },
        rejection_reasons: ['meaningful_brier_improvement'],
        auto_apply: false,
      },
    }));
    assert(chlFlipped.includes('不可計算'), `${dashboard}: flipped shadow ROI is not computed`);
    assert(chlFlipped.includes('策略揀咗另一邊,但冇該方向嘅賽前實際賠率'),
      `${dashboard}: flipped shadow ROI states a precise reason`);
    assert(chlFlipped.includes('reason-challenger-chl-shadow'),
      `${dashboard}: unavailable shadow ROI exposes its reason node`);
    assert(chlFlipped.includes('反向 41 場'), `${dashboard}: direction flips are surfaced`);
    assert(!/影子回報[^<]*<b class="mono">[+-]?[0-9]/.test(chlFlipped),
      `${dashboard}: no numeric ROI may be rendered when direction is unaligned`);
  }

  if (dashboard === 'hkjc-dashboard/app.js') {
    // Footbreak must not pretend the Crown-only CHL model exists.
    const footbreakChl = mod.challengerMarketCard('CHL', evaluatedTest({
      prospective_chl: { status: 'candidate_passed_human_review_required', prospective_fixtures: 44 },
    }));
    assert(!footbreakChl.includes('section-challenger-chl-prospective'),
      'footbreak dashboard must not render the Crown CHL prospective section');
    assert(!footbreakChl.includes('前瞻凍結影子驗證'),
      'footbreak dashboard must not mention the Crown CHL prospective model');
  }

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
  assert(html().includes('button-challenger-filter-review'), `${dashboard}: review filter shown`);

  // 只睇已通過／待覆核
  mod.CHAL_FILTER = 'review';
  mod.renderChallenger();
  assert(html().includes('aria-pressed="true"'), `${dashboard}: review filter marked active`);
  assert(!html().includes('card-challenger-HDC'), `${dashboard}: rejected model hidden by review filter`);
  assert(html().includes('state-challenger-filter-empty'), `${dashboard}: empty review state shown`);

  mod.CHAL.payload.systems.footbreak.tests.HIL = evaluatedTest({
    status: 'candidate_passed_human_review_required',
    rejection_reasons: [],
  });
  mod.CHAL.payload.systems.crown.tests.HIL = evaluatedTest({
    status: 'candidate_passed_human_review_required',
    rejection_reasons: [],
  });
  mod.renderChallenger();
  assert(html().includes('card-challenger-HIL'), `${dashboard}: passed model shown by review filter`);
  assert(!html().includes('card-challenger-HDC'), `${dashboard}: non-passed model remains hidden`);
  assert(!html().includes('state-challenger-filter-empty'), `${dashboard}: empty state removed after pass`);
}

if (!process.exitCode) console.log('OK challenger dashboard UI smoke');
