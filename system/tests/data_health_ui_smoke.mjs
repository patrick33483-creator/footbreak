/* 資料健康面板嘅純邏輯冒煙測試。
 * 由 app.js 抽出資料健康區塊,注入最少量替身,檢查各種狀態同篩選嘅輸出。
 * 唔會載入預測、結算或落注邏輯,亦唔會發任何網絡請求。 */
import { readFileSync } from 'node:fs';
import { dirname, resolve } from 'node:path';
import { fileURLToPath } from 'node:url';

const ROOT = resolve(dirname(fileURLToPath(import.meta.url)), '..', '..');
const START = '/* ══════════════════════ 資料健康 · 完整率及錯誤分層 ══════════════════════ */';
const END = '/* ══════════════════════ 挑戰模型 · 隔離影子研究 ══════════════════════ */';

function assert(condition, message) {
  if (!condition) {
    console.error('FAIL ' + message);
    process.exitCode = 1;
  }
}

function loadHealthModule(appPath) {
  const source = readFileSync(appPath, 'utf-8');
  const start = source.indexOf(START);
  const end = source.indexOf(END, start);
  if (start < 0 || end < 0) throw new Error('data health block not found in ' + appPath);
  const block = source.slice(start, end);
  const view = { innerHTML: '' };
  const $ = (selector) => (selector === '#viewHealth' ? view : null);
  const numeric = (x) => (x == null || x === '' || !Number.isFinite(Number(x)) ? null : Number(x));
  const pc = (x, d = 1) => (numeric(x) == null ? '—' : (numeric(x) * 100).toFixed(d) + '%');
  const f3 = (x) => (numeric(x) == null ? '—' : numeric(x).toFixed(3));
  const esc = (s) => String(s ?? '').replace(/[&<>"']/g, (c) =>
    ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c]));
  const MKT = { HDC: '讓球', HIL: '入球大小', CHL: '總角球大小' };
  const document = { querySelectorAll: () => [] };
  const factory = new Function(
    '$', 'numeric', 'pc', 'f3', 'esc', 'MKT', 'VIEW', 'fetch', 'document',
    `${block}\nreturn { renderHealth, healthValidate, healthSliceVisible, healthSliceSource,` +
    ` get HEALTH() { return HEALTH; }, set HEALTH(v) { HEALTH = v; },` +
    ` get HEALTH_FILTER() { return HEALTH_FILTER; }, set HEALTH_FILTER(v) { HEALTH_FILTER = v; },` +
    ` view: $('#viewHealth') };`
  );
  return factory($, numeric, pc, f3, esc, MKT, 'health', async () => { throw new Error('no network'); }, document);
}

const isoHoursAgo = (hours) => new Date(Date.now() - hours * 3600000).toISOString();

function slice(dimension, key, overrides) {
  return {
    dimension, key, label: key,
    unique_fixtures: 60, unique_fixtures_all_states: 60, rows: 180,
    sample_status: 'sufficient', small_sample: false, minimum_unique_fixtures: 30,
    coverage_share: 0.5, graded_rows: 180, decided_rows: 170, hits: 90, pushes: 10,
    accuracy: 0.529412, accuracy_ci95: [0.454, 0.603],
    brier: 0.243, brier_rows: 180, log_loss: 0.681, log_loss_rows: 180,
    sample_basis: 'unique_fixtures', metric_unit: 'graded_prediction_rows',
    correlated_stage_rows: true,
    ...overrides,
  };
}

/* 主要診斷切面:每場每市場最新階段,所以冇同一場嘅重複階段列。 */
function primarySlice(dimension, key, overrides) {
  return slice(dimension, key, {
    rows: 60, graded_rows: 60, decided_rows: 57, hits: 30, pushes: 3,
    metric_unit: 'graded_prediction_rows_latest_stage_per_fixture_market',
    correlated_stage_rows: false,
    ...overrides,
  });
}

function report(overrides) {
  return {
    schema_version: 1,
    report: 'data_health',
    system: 'PLACEHOLDER',
    generated_at: isoHoursAgo(2),
    status: 'degraded',
    policy: {
      read_only: true, auto_apply: false, retraining: false,
      primary_sample: 'unique_fixtures', stage_rows_are_reference_only: true,
      minimum_unique_fixtures: 30, settle_grace_minutes: 105, corner_retry_days: 7,
    },
    definitions: { unique_fixtures: '獨立賽事' },
    completeness: {
      overall: {
        unique_fixtures: 120, stage_rows: 340, prediction_rows: 690,
        graded_rows: 600, pending_rows: 60, excluded_rows: 30,
        duplicate_stage_keys: 3, quarantined_post_kickoff_rows: 2,
        result: { settle_due_fixtures: 118, fixtures_with_result: 110, coverage: 0.932, stale_unresolved_fixtures: 8, grace_minutes: 105 },
        corner_result: { corner_prediction_fixtures: 90, settle_due_fixtures: 88, fixtures_with_corner_result: 61, coverage: 0.693, missing_fixtures: 27, stale_beyond_retry_fixtures: 9, retry_days: 7 },
        missing_or_invalid: { probability: 4, line: 2, odds: 11, selection_side: 0, league: 3, stage: 0, source: 0, provider: 0, result: 8, corner_total: 27 },
      },
      by_market: {
        HDC: { unique_fixtures: 118, stage_rows: 330, prediction_rows: 330, graded_rows: 320, pending_rows: 6, excluded_rows: 4,
          result: { coverage: 0.94 }, corner_result: { coverage: null }, missing_or_invalid: { odds: 3 } },
        HIL: { unique_fixtures: 115, stage_rows: 320, prediction_rows: 320, graded_rows: 300, pending_rows: 12, excluded_rows: 8,
          result: { coverage: 0.93 }, corner_result: { coverage: null }, missing_or_invalid: { probability: 4, odds: 8 } },
        CHL: { unique_fixtures: 90, stage_rows: 250, prediction_rows: 250, graded_rows: 170, pending_rows: 42, excluded_rows: 38,
          result: { coverage: 0.93 }, corner_result: { coverage: 0.693 }, missing_or_invalid: { corner_total: 27 } },
      },
    },
    issues: [
      { code: 'stale_missing_corner_results', severity: 'high', scope: 'market:CHL', label: '超過重試期仍然缺角球賽果', count: 9, detail: '重試期 7 日' },
      { code: 'stale_unresolved_results', severity: 'high', scope: 'overall', label: '過了結算寬限期仍然冇賽果', count: 8, detail: '寬限期 105 分鐘' },
      { code: 'missing_odds', severity: 'warn', scope: 'market:HIL', label: '缺失賠率', count: 8, detail: '' },
    ],
    issue_counts: { high: 2, warn: 1, info: 0, total: 3 },
    metrics_policy: {
      sample_basis: 'unique_fixtures', metric_unit: 'graded_prediction_rows',
      correlated_stage_rows: true,
      primary_diagnostic_metric_unit: 'graded_prediction_rows_latest_stage_per_fixture_market',
      metrics_are_one_per_fixture: false,
      recommendation_evidence_unit: 'graded_prediction_rows_latest_stage_per_fixture_market',
    },
    baseline: {
      unique_fixtures: 120, rows: 690, graded_rows: 600, accuracy: 0.531,
      brier: 0.244, log_loss: 0.683, sample_status: 'sufficient',
      sample_basis: 'unique_fixtures', metric_unit: 'graded_prediction_rows',
      correlated_stage_rows: true,
    },
    error_slices: {
      market: [slice('market', 'HDC'), slice('market', 'HIL', { label: '入球大小' }), slice('market', 'CHL', { unique_fixtures: 12, sample_status: 'insufficient', small_sample: true })],
      stage: [slice('stage', '首預'), slice('stage', 'T-30'), slice('stage', 'T-5')],
      league: [slice('league', '英超'), slice('league', '小聯賽', { unique_fixtures: 7, sample_status: 'insufficient', small_sample: true, accuracy: 0.14 })],
      direction: [slice('direction', 'H'), slice('direction', 'A')],
      confidence: [slice('confidence', '58-64', { accuracy: 0.44 }), slice('confidence', '>=75')],
    },
    primary_diagnostic: {
      unit: 'graded_prediction_rows_latest_stage_per_fixture_market',
      sample_basis: 'unique_fixtures',
      stage_priority: ['T-5', 'T-30', '首預'],
      label: '主要診斷:每場每市場最新階段',
      baseline: {
        unique_fixtures: 120, rows: 230, graded_rows: 220, accuracy: 0.522,
        brier: 0.246, log_loss: 0.686, sample_status: 'sufficient',
        sample_basis: 'unique_fixtures',
        metric_unit: 'graded_prediction_rows_latest_stage_per_fixture_market',
        correlated_stage_rows: false,
      },
      error_slices: {
        market: [
          primarySlice('market', 'HDC'), primarySlice('market', 'HIL', { label: '入球大小' }),
          primarySlice('market', 'CHL', { unique_fixtures: 12, sample_status: 'insufficient', small_sample: true }),
        ],
        stage: [primarySlice('stage', 'T-5'), primarySlice('stage', 'T-30', { unique_fixtures: 9, sample_status: 'insufficient', small_sample: true })],
        league: [primarySlice('league', '英超'), primarySlice('league', '小聯賽', { unique_fixtures: 7, sample_status: 'insufficient', small_sample: true, accuracy: 0.14 })],
        direction: [primarySlice('direction', 'H'), primarySlice('direction', 'A')],
        confidence: [primarySlice('confidence', '58-64', { accuracy: 0.44 }), primarySlice('confidence', '>=75')],
      },
    },
    hil_v4_diagnostics: {
      scope: 'HIL', auto_apply: false, retraining: false, is_model: false, minimum_unique_fixtures: 30,
      evidence_unit: 'graded_prediction_rows_latest_stage_per_fixture_market',
      evidence_sample_basis: 'unique_fixtures',
      evidence_uses_repeated_stage_rows: false,
      feature_families: [
        { id: 'corner_independent_source', label: '角球獨立資料源', critical: false, rows: 320, present_rows: 0, coverage: 0 },
        { id: 'market_line_price', label: '盤口線及賠率', critical: true, rows: 320, present_rows: 318, coverage: 0.994 },
      ],
      missing_feature_families: ['corner_independent_source'],
      worst_stable_slices: [slice('confidence', '58-64', { accuracy: 0.44 })],
      recommendations: [
        { id: 'feature_family:corner_independent_source', kind: 'feature_coverage', priority: 'medium', title: 'HIL 缺少特徵族:角球獨立資料源', detail: '覆蓋率觀察,唔代表因果。' },
        { id: 'slice:confidence:58-64', kind: 'weak_slice', priority: 'medium', title: '表現最弱且樣本足夠的切面:58-64', detail: '只係關聯觀察,並唔代表因果。', evidence: slice('confidence', '58-64') },
        { id: 'slice:league:英超', kind: 'weak_slice', priority: 'medium', title: '表現最弱且樣本足夠的切面:英超', detail: '只係關聯觀察。', evidence: slice('league', '英超') },
      ],
      notes: ['本節只係診斷:唔會自動套用、唔會重訓。'],
    },
    ...overrides,
  };
}

for (const [dashboard, system] of [
  ['hkjc-dashboard/app.js', 'footbreak'],
  ['crown/dashboard/app.js', 'crown'],
]) {
  const mod = loadHealthModule(resolve(ROOT, dashboard));
  const html = () => mod.view.innerHTML;
  const reset = () => {
    mod.HEALTH_FILTER = { market: 'all', stage: 'all', league: 'all', direction: 'all', sample: 'all', unit: 'all_stages' };
  };
  const ready = (overrides) => {
    mod.HEALTH = { state: 'ready', payload: report({ system, ...(overrides || {}) }), error: '', loadedAt: Date.now() };
  };

  // 載入中
  mod.HEALTH = { state: 'idle', payload: null, error: '', loadedAt: null };
  mod.renderHealth();
  assert(html().includes('state-health-loading'), `${dashboard}: loading state`);
  assert(html().includes('唔會改動任何預測'), `${dashboard}: loading keeps read-only note`);

  // 未生成 (404)
  mod.HEALTH = { state: 'missing', payload: null, error: '', loadedAt: Date.now() };
  mod.renderHealth();
  assert(html().includes('state-health-missing'), `${dashboard}: missing state`);

  // 讀取／格式錯誤
  mod.HEALTH = { state: 'error', payload: null, error: '報告唔係有效 JSON', loadedAt: Date.now() };
  mod.renderHealth();
  assert(html().includes('state-health-error'), `${dashboard}: error state`);
  assert(html().includes('報告唔係有效 JSON'), `${dashboard}: error message shown`);

  // 驗證器:錯系統／錯類型／唔係物件
  assert(mod.healthValidate(null) !== '', `${dashboard}: rejects non-object`);
  assert(mod.healthValidate({ report: 'other', system }) !== '', `${dashboard}: rejects wrong report`);
  assert(mod.healthValidate(report({ system: system === 'crown' ? 'footbreak' : 'crown' })) !== '',
    `${dashboard}: rejects the other system's artifact`);
  assert(mod.healthValidate(report({ system })) === '', `${dashboard}: accepts its own artifact`);

  // 資料源不可用
  mod.HEALTH = {
    state: 'ready',
    payload: {
      schema_version: 1, report: 'data_health', system, generated_at: isoHoursAgo(1),
      status: 'unavailable', status_reason: 'learning_database_missing',
      policy: { read_only: true }, completeness: { overall: {}, by_market: {} },
      issues: [], issue_counts: {}, baseline: {}, hil_v4_diagnostics: {},
    },
    error: '', loadedAt: Date.now(),
  };
  mod.renderHealth();
  assert(html().includes('state-health-unavailable'), `${dashboard}: unavailable state`);
  assert(html().includes('learning_database_missing'), `${dashboard}: unavailable reason shown`);

  // 過期報告
  reset();
  ready({ generated_at: isoHoursAgo(60) });
  mod.renderHealth();
  assert(html().includes('flag-health-stale'), `${dashboard}: stale report flagged`);

  // 樣本未夠
  ready({ status: 'insufficient_data' });
  mod.renderHealth();
  assert(html().includes('banner-health-insufficient'), `${dashboard}: insufficient banner`);

  // 正常密集內容
  reset();
  ready();
  mod.renderHealth();
  const dense = html();
  assert(dense.includes('kpis-health'), `${dashboard}: KPI row rendered`);
  assert(dense.includes('獨立賽事(主要樣本)'), `${dashboard}: unique fixtures is the primary sample`);
  assert(dense.includes('階段列(只作參考)'), `${dashboard}: stage rows are reference only`);
  assert(dense.includes('applied-health-filters'), `${dashboard}: applied filters visible`);
  assert(dense.includes('全部(未篩選)'), `${dashboard}: default applied filter text`);
  assert(dense.includes('filter-health'), `${dashboard}: filter controls rendered`);
  for (const dimension of ['market', 'stage', 'league', 'direction']) {
    assert(dense.includes(`select-health-${dimension}`), `${dashboard}: ${dimension} filter present`);
    assert(dense.includes(`card-health-slices-${dimension}`), `${dashboard}: ${dimension} slices rendered`);
  }
  assert(dense.includes('card-health-slices-confidence'), `${dashboard}: confidence slices rendered`);
  assert(dense.includes('card-health-issues'), `${dashboard}: completeness issues rendered`);
  assert(dense.includes('row-health-issue-stale_missing_corner_results'), `${dashboard}: corner issue rendered`);
  assert(dense.includes('card-health-market-CHL'), `${dashboard}: per-market completeness rendered`);
  assert(dense.includes('角球賽果 27'), `${dashboard}: missing corner chip rendered`);
  assert(dense.includes('note-health-hil-v4'), `${dashboard}: HIL v4 diagnostic note rendered`);
  assert(dense.includes('唔會自動套用、唔會重訓'), `${dashboard}: no auto-apply / no retraining`);
  assert(dense.includes('自動套用:<b class="bad-txt">否</b>'), `${dashboard}: auto apply explicitly no`);
  assert(dense.includes('關聯,並非因果') || dense.includes('關聯'), `${dashboard}: causation disclaimed`);
  assert(dense.includes('flag-health-small-sample'), `${dashboard}: small sample flagged`);
  assert(!/已上線|已自動套用|自動修復|自動調整咗/.test(dense), `${dashboard}: never implies an applied change`);
  // 樣本不足嘅切面唔會顯示命中率數字
  const smallRow = dense.split('row-health-slice-league-小聯賽')[1].split('</div>\n  </div>')[0];
  assert(!smallRow.includes('14.0%'), `${dashboard}: small-sample accuracy is suppressed`);
  assert(smallRow.includes('樣本不足'), `${dashboard}: small-sample label shown`);

  // 市場篩選
  mod.HEALTH_FILTER = { market: 'HIL', stage: 'all', league: 'all', direction: 'all', sample: 'all', unit: 'all_stages' };
  mod.renderHealth();
  const filtered = html();
  assert(filtered.includes('市場:HIL'), `${dashboard}: applied filter text updates`);
  assert(filtered.includes('row-health-slice-market-HIL'), `${dashboard}: selected market kept`);
  assert(!filtered.includes('row-health-slice-market-HDC'), `${dashboard}: other market hidden`);
  assert(filtered.includes('card-health-market-HIL'), `${dashboard}: market completeness filtered`);
  assert(!filtered.includes('card-health-market-CHL'), `${dashboard}: other market card hidden`);
  assert(!filtered.includes('row-health-issue-stale_missing_corner_results'),
    `${dashboard}: other market issue hidden`);
  assert(filtered.includes('row-health-issue-stale_unresolved_results'),
    `${dashboard}: overall issue always kept`);

  // 樣本足夠篩選
  mod.HEALTH_FILTER = { market: 'all', stage: 'all', league: 'all', direction: 'all', sample: 'sufficient', unit: 'all_stages' };
  mod.renderHealth();
  const sufficient = html();
  assert(sufficient.includes('樣本:≥30 場'), `${dashboard}: sample filter shown in applied filters`);
  assert(!sufficient.includes('row-health-slice-league-小聯賽'), `${dashboard}: small slice hidden`);
  assert(!sufficient.includes('row-health-slice-market-CHL'), `${dashboard}: small market slice hidden`);
  assert(sufficient.includes('row-health-slice-league-英超'), `${dashboard}: stable slice kept`);

  // 空篩選結果:全部切面都樣本不足,但用家只想睇樣本足夠
  const tiny = report({ system });
  for (const dimension of Object.keys(tiny.error_slices)) {
    tiny.error_slices[dimension] = tiny.error_slices[dimension].map((item) => ({
      ...item, unique_fixtures: 4, sample_status: 'insufficient', small_sample: true,
    }));
  }
  mod.HEALTH = { state: 'ready', payload: tiny, error: '', loadedAt: Date.now() };
  mod.HEALTH_FILTER = { market: 'all', stage: 'all', league: 'all', direction: 'all', sample: 'sufficient', unit: 'all_stages' };
  mod.renderHealth();
  assert(html().includes('state-health-slices-empty'), `${dashboard}: empty filter state shown`);
  assert(html().includes('清除篩選'), `${dashboard}: reset control offered`);

  // 指標單位:預設用主要診斷(每場每市場最新階段),避免相關階段列被當成獨立樣本
  ready();
  mod.HEALTH_FILTER = { market: 'all', stage: 'all', league: 'all', direction: 'all', sample: 'all', unit: 'primary' };
  mod.renderHealth();
  const primary = html();
  assert(primary.includes('note-health-metric-unit'), `${dashboard}: metric unit note rendered`);
  assert(primary.includes('指標單位:<b>已結算預測列</b>'), `${dashboard}: metric unit is graded rows`);
  assert(primary.includes('<b>唔係每場一行</b>'), `${dashboard}: never implies one row per fixture`);
  assert(primary.includes('data-metric-unit="graded_prediction_rows_latest_stage_per_fixture_market"'),
    `${dashboard}: primary metric unit exposed as data attribute`);
  assert(primary.includes('data-correlated-stage-rows="false"'),
    `${dashboard}: primary diagnostic is not correlated`);
  assert(primary.includes('單位:主要診斷:每場每市場最新階段'),
    `${dashboard}: applied filters state the unit`);
  assert(!primary.includes('row-health-slice-stage-首預'),
    `${dashboard}: primary diagnostic drops stages that were superseded`);
  assert(!primary.includes('flag-health-correlated'),
    `${dashboard}: primary rows are never flagged correlated`);
  assert(primary.includes('note-health-rec-unit'), `${dashboard}: recommendation evidence unit stated`);
  assert(primary.includes('證據一律取自「每場每市場最新階段」主要診斷'),
    `${dashboard}: recommendations name their evidence unit`);
  assert(primary.includes('由 600 條已結算預測列相加'),
    `${dashboard}: the KPI states how many graded rows the accuracy came from`);

  // 切換到全部階段列:必須明確標示相關、只作參考
  mod.HEALTH_FILTER = { market: 'all', stage: 'all', league: 'all', direction: 'all', sample: 'all', unit: 'all_stages' };
  mod.renderHealth();
  const allStages = html();
  assert(allStages.includes('data-metric-unit="graded_prediction_rows"'),
    `${dashboard}: all-stage metric unit exposed`);
  assert(allStages.includes('data-correlated-stage-rows="true"'),
    `${dashboard}: all-stage view is flagged correlated`);
  assert(allStages.includes('全部階段列(相關,只作參考)'), `${dashboard}: all-stage label is explicit`);
  assert(allStages.includes('唔可以當獨立樣本'), `${dashboard}: correlated rows are not independent`);
  assert(allStages.includes('row-health-slice-stage-首預'), `${dashboard}: all stages are shown`);
  assert(allStages.includes('flag-health-correlated'), `${dashboard}: correlated rows are flagged in the table`);

  // 報告未有主要診斷區塊時,要如實講返回退咗,唔可以扮成主要診斷
  const legacy = report({ system });
  delete legacy.primary_diagnostic;
  mod.HEALTH = { state: 'ready', payload: legacy, error: '', loadedAt: Date.now() };
  mod.HEALTH_FILTER = { market: 'all', stage: 'all', league: 'all', direction: 'all', sample: 'all', unit: 'primary' };
  mod.renderHealth();
  const fellBack = html();
  assert(fellBack.includes('報告未有主要診斷區塊,已回退到全部階段列'),
    `${dashboard}: fallback is disclosed, never disguised`);
  assert(fellBack.includes('data-metric-unit="graded_prediction_rows"'),
    `${dashboard}: fallback reports the honest unit`);
  assert(mod.healthSliceSource(legacy).unit === 'all_stages',
    `${dashboard}: source resolver reports the honest unit`);

  // 篩選器邏輯
  reset();
  assert(mod.healthSliceVisible('market', slice('market', 'HDC')), `${dashboard}: default shows all`);
  mod.HEALTH_FILTER = { market: 'all', stage: 'all', league: 'all', direction: 'all', sample: 'sufficient', unit: 'all_stages' };
  assert(!mod.healthSliceVisible('market', slice('market', 'HDC', { sample_status: 'insufficient' })),
    `${dashboard}: sample filter drops insufficient slices`);
}

if (!process.exitCode) console.log('OK data health dashboard UI smoke');
