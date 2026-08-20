# Granular-ranking Wilson rollover follow-up

## Root cause fixed

The visible condition cards were not rendered from
`wilson_validation.conditions`.  They were rebuilt directly from
`analysis.granular_conditions.mine()`:

* Footbreak: `hkjc-dashboard/app.js` `historyConsensusCards()`.
* Crown: `crown/dashboard/app.js` `historyConsensusCards()`.

That pure miner recalculated `total` and `holdout` each history pass, while the
first rollout only versioned separately frozen Wilson conditions.  Therefore
the screenshot card continued to show its old discovery values and an
admission could still start from the same old ranking total.

## Added mechanism

`analysis.wilson_validation` now owns an additive
`sync_granular_ranking_evidence()` / `project_granular_ranking_evidence()`
adapter.

1. It derives a signature from every exact granular key (system, market,
   path, decision stage, odds tier, direction, role, bucket, movement and
   tier path), rather than a display position.
2. At the one-time granular migration only, it persists every visible valid
   ranking condition in the stable current card order.
3. For each such condition it records the original discovery total and the
   complete old holdout inside immutable ledger evidence, then makes exactly
   one aggregate version merge.  This is not split into 20-row batches.
4. It sets the post-migration activation boundary to the migration time and
   resets the fresh prospective counter to `0/20`.
5. Later regenerated rankings cannot overwrite the condition definition,
   historical evidence, migration cohort, versions, condition number, or
   pending counter.  A global migration latch means newly discovered
   conditions never replay a later historical holdout as prospective data.
6. Ordinary settled native T-5 Wilson bets continue through the existing
   provenance, uniqueness, binary-outcome and strict-boundary checks.  Their
   next 20 rows append immutable versions; 26 produces one 20-row version and
   six pending.

The migration is persisted atomically from Footbreak dashboard generation and
from Crown dashboard generation, and is also applied before each system's
Wilson T-5 matcher.  The latter consumes the projected active evidence, so
the formal `Wilson95 lower >= 1/current_odds + 0.03` gate uses the same
numbers as the card.

## Required worked example

For the historical card baseline `141/231` and completed validation cohort
`44/71`, the active evidence becomes:

* active version: `v2`;
* cumulative evidence: `185/302`;
* Wilson 95% lower: `0.556553...` (display `55.7%`);
* minimum acceptable decimal odds: `1.899143...` (display `1.90`);
* fresh prospective rollover progress: `0/20`.

The old `44/71` is retained only as `legacy_prospective_cohort` in the
immutable initial-migration audit.  It is not shown as current validation
progress and is not split into `3 × 20 + 11`.

## Dashboard behavior

Both granular ranking cards now use their persisted stable condition number
and active cumulative hits/decided/Wilson/minimum odds.  They also show:

* active evidence version;
* last merged batch (including the initial full-cohort marker);
* new prospective `x/20` **decision-count** progress;
* an explicit note that the progress value is not a hit-rate display.

The separate Wilson evidence-version table already exposes the bounded
hash-only batch audit.  Public dashboard projections contain no raw fixture
or provider IDs.  Telegram and Radar code were not changed.

## Migration / rollout risks

* The first granular migration must run with the intended completed ranking
  artifact available.  An empty ranking does not consume the latch.
* It intentionally treats the old displayed `total` and `holdout` as the
  product-authorized separate cohorts, even if a legacy miner field name
  suggests a broader aggregate.  This is necessary for the confirmed
  `141/231 + 44/71 = 185/302` product semantics.
* A malformed ranking, system mismatch, invalid counts, conflicting
  evidence, missing native provenance, duplicate fixture-market hash,
  nonbinary result, or equal-time unsafe batch boundary fails closed.
* Conditions first seen after the latch receive no historical-holdout replay.
  They need explicitly trustworthy future native T-5 evidence.

## Validation completed

* focused Wilson / granular / dashboard tests;
* full `analysis/tests` suite: 260 tests passed;
* full `system/tests` suite: 239 tests passed, 10 skipped;
* full `crown/tests` suite: 224 tests passed, 11 skipped;
* Python compilation, JavaScript syntax checks, shell syntax check, and
  `git diff --check` passed.
