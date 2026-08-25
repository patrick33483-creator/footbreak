/* Pure-render smoke tests for the collapsed Footbreak Wilson condition funnel. */
import { readFileSync } from 'node:fs';
import { dirname, resolve } from 'node:path';
import { fileURLToPath } from 'node:url';

const ROOT = resolve(dirname(fileURLToPath(import.meta.url)), '..', '..');
const source = readFileSync(resolve(ROOT, 'hkjc-dashboard', 'app.js'), 'utf8');
const start = source.indexOf('function wilsonRolloverCard(validation) {');
const end = source.indexOf('\nfunction probabilityResearchCard(', start);
if (start < 0 || end < 0) throw new Error('Wilson funnel renderer not found');

const numeric = (value) =>
  value == null || value === '' || !Number.isFinite(Number(value)) ? null : Number(value);
const esc = (value) => String(value ?? '').replace(/[&<>"']/g, (character) =>
  ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' })[character]);
const publicText = (value) => String(value ?? '');
const wilsonConditionLabel = (value) => `條件 ${value}`;
const factory = new Function(
  'numeric', 'esc', 'publicText', 'wilsonConditionLabel',
  `${source.slice(start, end)}\nreturn wilsonRolloverCard;`,
);
const render = factory(numeric, esc, publicText, wilsonConditionLabel);

function assert(condition, message) {
  if (!condition) throw new Error(`FAIL ${message}`);
}

const signature = 'a'.repeat(24);
const html = render({
  condition_funnel: {
    schema_version: 1,
    read_only: true,
    conditions: [{
      condition_number: 4,
      condition_signature: signature,
      condition_version: 'granular-condition-v1',
      definition: {
        market: 'HDC', stage: 'T-5', path: '首預→T-30→T-5',
        role: '主讓', line_bucket: '-0.75~-0.25', odds_tier: '1.70–1.89',
      },
      active_evidence: {
        version: 3, evidence_hash: 'e'.repeat(64),
        cumulative_hits: 53, cumulative_decided: 80,
      },
      stages: {
        eligible_post_activation_t5_observations: { available: false },
        exact_condition_matches: { available: true, availability: 'bounded', count: 9 },
        recorded_formal_evidence: {
          available: true, count: 8, formal_bets: 5, formal_observations: 3,
        },
        settled_valid_evidence: { available: true, count: 7, hits: 5 },
        current_rollover_progress: { available: true, display: '7/20' },
      },
      rejections: {
        items: [{
          code: 'wilson_gate_not_passed',
          label: '完全相同條件吻合，但賠率未通過 Wilson 門檻',
          count: 3,
          source: 'retained_condition_audit',
        }],
        omitted_reason_kinds: 0,
      },
    }],
  },
});

assert(html.includes('data-testid="wilson-condition-funnel"'), 'funnel test id');
assert(html.includes('data-testid="wilson-condition-4"'), 'condition test id');
assert(html.includes('<details class="wilson-condition"'), 'semantic collapsed disclosure');
assert(!html.includes('<details class="wilson-condition" open'), 'conditions default collapsed');
assert(html.includes('證據 v3 · 53/80'), 'active evidence version and cumulative evidence');
assert(html.includes('7/20'), 'persisted current x/20');
assert(html.includes(signature), 'full immutable signature');
assert(html.includes('granular-condition-v1'), 'condition definition version');
assert(html.includes('未能可靠重建'), 'unavailable upstream count');
assert(html.includes('9（保留窗口）'), 'bounded exact-match count');
assert(html.includes('模擬注 5 · 觀察 3'), 'formal evidence split');
assert(html.includes('完全相同條件吻合，但賠率未通過 Wilson 門檻'), 'supported rejection');
assert(!html.includes('provider'), 'no provider payload rendering');

const stale = render({ rollover: { conditions: { old: { pending_progress: { display: '2/20' } } } } });
assert(stale.includes('漏斗資料未可用；不會由舊摘要或即時資料推算。'), 'stale payload fails closed');
assert(!stale.includes('2/20'), 'stale rollover summary is not inferred into funnel');

console.log('Wilson condition funnel UI smoke checks passed');
