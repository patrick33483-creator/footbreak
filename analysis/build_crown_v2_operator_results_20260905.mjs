#!/usr/bin/env node

import fs from "node:fs";

const [ledgerPath, oldOverlayPath, reportPath, outputPath] = process.argv.slice(2);
if (!ledgerPath || !oldOverlayPath || !reportPath || !outputPath) {
  throw new Error(
    "usage: node build_crown_v2_operator_results_20260905.mjs " +
      "<ledger> <old-overlay> <verification-report> <output>",
  );
}

const scoreByMatchId = new Map(
  Object.entries({
    "2999444": [2, 1],
    "2999445": [2, 1],
    "3073546": [0, 2],
    "3017137": [5, 1],
    "3017139": [2, 0],
    "3017135": [0, 0],
    "3074121": [0, 2],
    "3011378": [0, 3],
    "3072828": [0, 0],
    "3017504": [1, 1],
    "2952571": [5, 0],
    "3074031": [2, 0],
    "3046401": [3, 2],
    "3022530": [3, 1],
    "3074014": [1, 1],
    "3067704": [3, 0],
    "3040108": [2, 0],
    "3040109": [2, 0],
    "3044385": [2, 2],
    "3037897": [3, 3],
    "3040113": [2, 1],
    "3037794": [4, 0],
    "3021974": [1, 4],
    "3034775": [1, 2],
    "3062073": [0, 0],
    "2998236": [0, 2],
    "3026028": [0, 3],
    "3077486": [3, 3],
    "3062727": [1, 1],
    "3062632": [1, 1],
    "3000937": [1, 1],
    "2939044": [1, 1],
    "3027730": [2, 1],
    "2939043": [1, 1],
    "3019505": [0, 2],
    "3071701": [0, 3],
    "3083995": [1, 0],
    "2997393": [0, 0],
    "2957032": [1, 2],
  }),
);

const intentionallyPending = new Set([
  "3073570", // postponed; user chose to retain pending
  "3041547", // medium confidence; user chose not to fill
  "2920125", // medium confidence; user chose not to fill
  "3074431", // medium confidence; user chose not to fill
]);

const ledger = JSON.parse(fs.readFileSync(ledgerPath, "utf8"));
const oldOverlay = JSON.parse(fs.readFileSync(oldOverlayPath, "utf8"));
const report = fs.readFileSync(reportPath, "utf8");
const fixtures = ledger.fixtures ?? {};

const evidenceByMatchId = new Map();
for (const line of report.split("\n")) {
  const cells = line.split("|").map((cell) => cell.trim());
  if (cells.length < 8 || !/^\d+$/.test(cells[2] ?? "")) continue;
  const matchId = cells[2];
  const urls = [...line.matchAll(/\]\((https?:\/\/[^)]+)\)/g)].map(
    (match) => match[1],
  );
  if (urls.length) evidenceByMatchId.set(matchId, [...new Set(urls)]);
}

const oldRows = oldOverlay.results ?? [];
const oldIds = new Set(oldRows.map((row) => String(row.match_id)));
for (const matchId of scoreByMatchId.keys()) {
  if (oldIds.has(matchId)) {
    throw new Error(`new result already exists in old overlay: ${matchId}`);
  }
}

const newRows = [];
for (const [matchId, [homeScore, awayScore]] of scoreByMatchId) {
  const fixture = fixtures[matchId];
  if (!fixture) throw new Error(`fixture missing from live ledger: ${matchId}`);
  const evidenceUrls = evidenceByMatchId.get(matchId) ?? [];
  if (!evidenceUrls.length) {
    throw new Error(`verification evidence URL missing: ${matchId}`);
  }
  newRows.push({
    match_id: matchId,
    league: fixture.league,
    home: fixture.home,
    away: fixture.away,
    kickoff: fixture.kickoff_hkt,
    home_score: homeScore,
    away_score: awayScore,
    provider_event_id: evidenceUrls[0],
    provider_home: fixture.home,
    provider_away: fixture.away,
    provider_start: fixture.kickoff_utc,
    orientation: "direct",
    evidence_urls: evidenceUrls,
  });
}

if (newRows.length !== 39) throw new Error(`expected 39 new rows, got ${newRows.length}`);
if (oldRows.length !== 40) throw new Error(`expected 40 old rows, got ${oldRows.length}`);
if ([...scoreByMatchId.keys()].some((id) => intentionallyPending.has(id))) {
  throw new Error("an intentionally pending match was included");
}

const results = [...oldRows, ...newRows];
if (new Set(results.map((row) => String(row.match_id))).size !== 79) {
  throw new Error("merged overlay does not contain 79 unique match IDs");
}

const output = {
  schema_version: 1,
  batch_id: "crown-operator-result-overlay-20260905-v1",
  score_scope: "90_minutes_including_stoppage_time_excluding_extra_time",
  verified_at: "2026-09-05T18:06:00+08:00",
  results,
  excluded: [
    {
      match_id: "3073570",
      reason: "postponed; user chose to retain pending",
    },
    {
      match_id: "3041547",
      reason: "medium confidence; user chose not to fill",
    },
    {
      match_id: "2920125",
      reason: "medium confidence; user chose not to fill",
    },
    {
      match_id: "3074431",
      reason: "medium confidence; user chose not to fill",
    },
  ],
};

fs.writeFileSync(outputPath, `${JSON.stringify(output, null, 2)}\n`);
