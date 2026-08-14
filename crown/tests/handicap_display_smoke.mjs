import assert from 'node:assert/strict';
import fs from 'node:fs';
import path from 'node:path';
import { fileURLToPath } from 'node:url';

const here = path.dirname(fileURLToPath(import.meta.url));
const app = fs.readFileSync(path.join(here, '..', 'dashboard', 'app.js'), 'utf8');

function extractFunction(name) {
  const start = app.indexOf(`function ${name}(`);
  assert.ok(start >= 0, `${name} must exist`);
  const bodyStart = app.indexOf('{', start);
  let depth = 0;
  for (let i = bodyStart; i < app.length; i += 1) {
    if (app[i] === '{') depth += 1;
    if (app[i] === '}') depth -= 1;
    if (depth === 0) return app.slice(start, i + 1);
  }
  throw new Error(`${name} body is incomplete`);
}

const helpers = [
  extractFunction('historyQuarterLine'),
  extractFunction('selectedMarketLine'),
  extractFunction('chinesePredictionLabel'),
].join('\n');
const { selectedMarketLine, chinesePredictionLabel } = new Function(
  `${helpers}; return { selectedMarketLine, chinesePredictionLabel };`,
)();

assert.equal(selectedMarketLine({ code: 'HDC', side: 'H', line: -0.5 }), -0.5);
assert.equal(selectedMarketLine({ code: 'HDC', side: 'A', line: -0.5 }), 0.5);
assert.equal(selectedMarketLine({ code: 'HDC', side: 'A', line: 1 }), -1);
assert.equal(selectedMarketLine({ code: 'HDC', side: 'A', line: null }), null);
assert.equal(chinesePredictionLabel({ code: 'HDC', side: 'H', line: -0.75 }), '讓球 主隊 -0.5/-1');
assert.equal(chinesePredictionLabel({ code: 'HDC', side: 'A', line: -0.5 }), '讓球 客隊 +0.5');
assert.equal(chinesePredictionLabel({ code: 'HDC', side: 'A', line: 1 }), '讓球 客隊 -1');
assert.equal(chinesePredictionLabel({ code: 'HIL', side: 'H', line: 3.25 }), '入球大 3/3.5');

console.log('handicap display smoke passed');
