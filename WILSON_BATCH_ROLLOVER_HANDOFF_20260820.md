# Wilson batch rollover — handoff

## Branch and scope

- **Base:** `c3793b7dae772df27992145bb08895cd0a7eaff6` (`release/server-monitor-crown-hour`)
- **Branch:** `add-wilson-batch-rollover-20260820`
- **Systems:** Footbreak and Crown `wilson-test-strategy-v1` only.
- **Out of scope / unchanged:** Radar, provider access, deployment scripts, and Telegram transport/message replay behavior.

## Final evidence semantics

1. Every frozen exact condition has an immutable `evidence_versions` chain. Exact identity remains the existing canonical condition signature, which includes system, market, decision stage, direction/role, line bucket, path, odds tier/trajectory, movement, and miner key.
2. Version 1 is the frozen historical discovery baseline. It stores hits/decided, Wilson 95% lower bound, raw/display minimum acceptable odds, activation boundary, and irreversible evidence hash.
3. **One-time migration only:** a pre-rollover condition with a completed aggregate validation cohort merges that whole cohort once. Thus historical `141/231` plus completed validation `44/71` becomes v2 `185/302`, with an immutable `initial_migration_full_cohort` audit record. It is explicitly not split into `3 × 20 + 11`; the new post-migration counter is `0/20`.
4. After that one-time migration, only newly admitted rows with persisted native pre-kickoff T-5 provenance and a stage time strictly after the active evidence boundary can count. Each exact condition independently merges the next chronological 20 binary outcomes into version N+1. `26` becomes one 20-row version plus `6/20` pending; `40` becomes two sequential versions.
5. A valid binary result is `Won`, `Half Won`, `Lost`, or `Half Lost`. Pending, refunded/push/void results, post-hoc/backfill rows, malformed provenance, duplicate fixture-markets, conflicting duplicates, and ambiguity at an equal-time boundary are excluded/fail closed.
6. Prior versions, old bet rows, and observations are never rewritten. The public audit carries only SHA-256 fixture-market hashes, never provider/fixture IDs. The dashboard’s compact rollover audit is limited to the last 64 entries; each ordinary batch contains exactly 20 hashes.
7. The active version changes only future native T-5 admission: it recomputes the raw Wilson comparison `L >= 1/current_odds + 0.03` and raw/display minimum acceptable odds. The existing historical sample threshold, quote checks, identity checks, market caps, and formal-bet rule remain in force. A sample rollover never itself creates a bet.
8. `18/26`, `44/71`, etc. remain hit/decided outcome displays, not progress bars. New-batch progress is separately rendered as `x/20`.

## Persistence and dashboard

- `analysis/wilson_validation.py` owns schema v2 migration, provenance markers, immutable version/hash chain, batch selection, and recalculated admission arithmetic.
- New formal bets contain private `rollover_provenance` at admission time. This is required for later inclusion and prevents reconstruction of old post-cutover rows as prospective evidence.
- Footbreak: `system/gen_app_data.py` projects `independent_validation.rollover` with active evidence, last merged batch summary, and pending progress.
- Crown: `crown/dashboard_data.py` projects the identical structure.
- Both dashboards render **Wilson 證據版本** per condition: active version, cumulative hits/decided, lower bound, minimum odds, last batch, and separate progress. Existing bet rows label the immutable admission version and current active evidence.

## Migration/rollout risks

- The full-cohort migration intentionally trusts only the already persisted aggregate prospective metrics on schema-v1 conditions, as explicitly requested. It records that fixture-market IDs were unavailable rather than inventing them. It runs once and is idempotent.
- Rows from before the new provenance marker cannot enter later 20-result batches, even if their settlement is old or newly discovered. This is deliberate fail-closed behavior.
- If a 20th chronological row ties the 21st at exactly the same native T-5 timestamp, batching pauses with `blocked_ambiguous_equal_stage_boundary` instead of choosing an arbitrary order.
- Initial migration applies when a schema-v1 Wilson namespace is loaded and the resulting ledger is next atomically saved by normal runtime persistence. No deployment was performed here.

## Validation completed

Focused checks:

```text
python -m unittest analysis.tests.test_wilson_validation \
  system.tests.test_wilson_notifications crown.tests.test_wilson_notifications \
  crown.tests.test_wilson_dashboard_projection \
  system.tests.test_prediction_history_payload -v
# 39 tests OK
```

Focused rollover coverage includes 19/no rollover, 20/one rollover, 40/two rollovers, 26/plus 6 pending, push exclusion, duplicate/conflict handling, immutability, rerun idempotency, activation boundary, one-time 44/71 migration, both systems, dashboard contract, hash-only audit, and recalculated formal odds gate.

Full suites:

```text
python -m unittest discover -s analysis/tests -p 'test_*.py'  # 258 OK
python -m unittest discover -s system/tests -p 'test_*.py'    # 239 OK, 10 skipped
python -m unittest discover -s crown/tests -p 'test_*.py'     # 224 OK, 11 skipped
```

Static checks passed:

```text
python -m compileall -q analysis system crown
node --check hkjc-dashboard/app.js
node --check crown/dashboard/app.js
find system crown deploy -type f -name '*.sh' -print0 | xargs -0 -r -n1 bash -n
git diff --check
```
