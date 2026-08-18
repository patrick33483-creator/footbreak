import assert from 'node:assert/strict';
import fs from 'node:fs';
import path from 'node:path';
import { fileURLToPath } from 'node:url';

const here = path.dirname(fileURLToPath(import.meta.url));
const roots = [
  path.join(here, '..', 'dashboard', 'app.js'),
  path.join(here, '..', '..', 'hkjc-dashboard', 'app.js'),
];

function extractFunction(source, name) {
  const start = source.indexOf(`function ${name}(`);
  assert.ok(start >= 0, `${name} must exist`);
  const bodyStart = source.indexOf('{', start);
  let depth = 0;
  for (let i = bodyStart; i < source.length; i += 1) {
    if (source[i] === '{') depth += 1;
    if (source[i] === '}') depth -= 1;
    if (depth === 0) return source.slice(start, i + 1);
  }
  throw new Error(`${name} body is incomplete`);
}

for (const file of roots) {
  const app = fs.readFileSync(file, 'utf8');
  const kt = "const kt = (s) => new Date(String(s).replace(' ', 'T') + (/[Z+]/.test(s) ? '' : '+08:00'));";
  const stageSnapshotStatus = new Function(
    `${kt}\n${extractFunction(app, 'stageSnapshotStatus')}; return stageSnapshotStatus;`,
  )();
  const fixture = { kickoff_hkt: '2026-08-19T00:15:00+08:00' };
  const now = Date.parse('2026-08-19T00:03:00+08:00');

  assert.equal(
    stageSnapshotStatus(fixture, 'T-30', now, '2026-08-18T23:21:00+08:00'),
    'stale',
    `${path.basename(file)} must not call an old T-30 snapshot a missed stage`,
  );
  assert.equal(
    stageSnapshotStatus(fixture, 'T-30', now, '2026-08-19T00:02:00+08:00'),
    'confirmed_missing',
    `${path.basename(file)} must retain a diagnosable confirmed T-30 miss`,
  );

  const later = { kickoff_hkt: '2026-08-19T00:30:00+08:00' };
  assert.equal(
    stageSnapshotStatus(later, 'T-30', now, '2026-08-18T23:21:00+08:00'),
    'stale',
    `${path.basename(file)} must distinguish a stale in-window T-30 card`,
  );
  assert.equal(
    stageSnapshotStatus(later, 'T-30', now, '2026-08-19T00:02:00+08:00'),
    'window_open',
    `${path.basename(file)} may show a live T-30 window only from a current snapshot`,
  );

  const started = { kickoff_hkt: '2026-08-19T00:00:00+08:00' };
  assert.equal(
    stageSnapshotStatus(started, 'T-5', now, '2026-08-18T23:21:00+08:00'),
    'stale',
    `${path.basename(file)} must not claim a stale pre-T5 card skipped T-5`,
  );
  assert.equal(
    stageSnapshotStatus(started, 'T-5', now, '2026-08-19T00:01:00+08:00'),
    'confirmed_missing',
    `${path.basename(file)} must retain a diagnosable truly missing T-5`,
  );
}

console.log('dashboard freshness smoke passed');
