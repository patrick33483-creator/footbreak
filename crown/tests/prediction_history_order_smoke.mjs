import assert from 'node:assert/strict';
import fs from 'node:fs';
import path from 'node:path';
import { fileURLToPath } from 'node:url';

const here = path.dirname(fileURLToPath(import.meta.url));
const app = fs.readFileSync(path.join(here, '..', 'dashboard', 'app.js'), 'utf8');
const start = app.indexOf("const HISTORY_STAGE_RANK =");
const end = app.indexOf("function renderHistory()", start);
assert.ok(start >= 0 && end > start, 'prediction-history ordering helpers must remain available');
const helpers = app.slice(start, end);
const { orderHistoryRows, historyFixtureIdentity } = new Function(
  `${helpers}; return { orderHistoryRows, historyFixtureIdentity };`,
)();

const row = (match_id, stage, kickoff, extra = {}) => ({
  match_id, stage, kickoff, home: `Home ${match_id || extra.home || ''}`,
  away: `Away ${match_id || extra.away || ''}`, league: 'L', ...extra,
});

const interleaved = [
  row('older', 'T-5', '2026-08-12T12:00:00+08:00'),
  row('newer', 'T-5', '2026-08-13T12:00:00+08:00'),
  row('older', '首預', '2026-08-12T12:00:00+08:00'),
  row('newer', 'T-30', '2026-08-13T12:00:00+08:00'),
  row('older', 'T-30', '2026-08-12T12:00:00+08:00'),
  row('newer', '首預', '2026-08-13T12:00:00+08:00'),
];
assert.deepEqual(
  orderHistoryRows(interleaved).map((item) => `${item.match_id}:${item.stage}`),
  ['newer:首預', 'newer:T-30', 'newer:T-5', 'older:首預', 'older:T-30', 'older:T-5'],
  'interleaved stages must become contiguous fixture groups in descending kickoff order',
);

const sameKickoff = [
  row('bravo', 'T-5', '2026-08-14T12:00:00+08:00'),
  row('alpha', '首預', '2026-08-14T12:00:00+08:00'),
  row('bravo', '首預', '2026-08-14T12:00:00+08:00'),
  row('alpha', 'T-5', '2026-08-14T12:00:00+08:00'),
];
assert.deepEqual(
  orderHistoryRows(sameKickoff).map((item) => `${item.match_id}:${item.stage}`),
  ['alpha:首預', 'alpha:T-5', 'bravo:首預', 'bravo:T-5'],
  'different fixtures at the same kickoff must not be merged',
);

const missingIds = [
  row('', 'T-5', '2026-08-15T12:00:00+08:00', { home: 'Alpha', away: 'Beta' }),
  row('', '首預', '2026-08-15T12:00:00+08:00', { home: 'Gamma', away: 'Delta' }),
  row('', '首預', '2026-08-15T12:00:00+08:00', { home: 'Alpha', away: 'Beta' }),
  row('', 'T-5', '2026-08-15T12:00:00+08:00', { home: 'Gamma', away: 'Delta' }),
];
assert.notEqual(
  historyFixtureIdentity(missingIds[0], 0),
  historyFixtureIdentity(missingIds[1], 1),
  'missing match ids must fall back to a full fixture identity, not kickoff alone',
);
assert.equal(
  historyFixtureIdentity({ match_id: 0 }, 0),
  'match:0',
  'a present numeric match id must not be treated as missing',
);
assert.deepEqual(
  orderHistoryRows(missingIds).map((item) => `${item.home}:${item.stage}`),
  ['Alpha:首預', 'Alpha:T-5', 'Gamma:首預', 'Gamma:T-5'],
  'fallback identities must keep stages contiguous without merging distinct fixtures',
);

const t5Only = orderHistoryRows(interleaved.filter((item) => item.stage === 'T-5'));
assert.deepEqual(
  t5Only.map((item) => item.match_id),
  ['newer', 'older'],
  'stage filtering must retain the same descending fixture-group ordering',
);

console.log('prediction history ordering smoke passed');
