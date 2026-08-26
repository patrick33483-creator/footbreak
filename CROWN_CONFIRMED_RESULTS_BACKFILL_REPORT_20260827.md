# Crown confirmed-result backfill preparation

## Scope and authorization boundary

- Input is pinned to `/home/user/workspace/fixtures_42_verified.json`, SHA-256
  `6e996b0c8caf7330b39885afc4d3fd03ba9cc9b4d084364050d4f2542991d3d1`.
- The tool requires exactly 42 unique fixtures: 34 `CONFIRMED` and 8
  `CONFLICT`.
- It requires exactly 66 corresponding confirmed Crown settlement rows and 17
  corresponding conflict rows.
- Only the 66 confirmed rows may be changed. The 17 conflict rows are hashed
  before and after the staged update and must remain identical.
- There are no provider, Telegram, or dashboard-publication calls.

## Reused production paths

- `crown.settle._settle` performs canonical Asian-line grading, score
  persistence, PnL, settlement metadata, and row history.
- `crown.ledger.recompute_stats` performs the existing Crown statistics,
  Wilson namespace, and challenger recomputation.
- `crown.state.settlement_lock` excludes a concurrent settlement pass.
- `crown.state.state_lock` serializes the state transaction.
- `crown.common.write_json_atomic` performs the final atomic ledger
  replacement.

No suitable existing one-time verified-result backfill command existed.

## Safety behavior

- Dry-run is the default.
- Apply requires `--apply` and the exact 64-character
  `--expected-ledger-sha256` emitted by the immediately preceding dry-run.
- The SHA is checked after both locks are held and again immediately before
  replacement.
- Every corresponding row must match the fixture's exact `match_id`, recorded
  home and away names, kickoff instant, market code, side, and normalized
  quarter line.
- The few input cells written as `recorded name -> researched identity` bind
  exactly to the recorded name on the left; the researched annotation is not
  written into the ledger.
- Every confirmed source grade is independently reproduced through the
  existing settlement function before any ledger planning occurs.
- A content-addressed, exclusive-create, mode `0400` byte-for-byte backup is
  fsynced before the atomic ledger write.
- A partial prior application is refused. A complete repeat is a byte-exact
  no-op.
- Rows outside the target set are hashed before and after staging; any
  unrelated or conflict-row change aborts the transaction.
- The manifest records source hashes, ledger before/after hashes, all 42 input
  fixture identities, each target row's before/after settlement fields, all
  already-applied rows, and all protected conflict rows.

## Production commands

Run from the deployed repository root. Do not use the sample hash below; use
the value printed by the production dry-run.

```bash
python -m crown.backfill_confirmed_results \
  --fixtures /path/to/fixtures_42_verified.json \
  --state-dir /var/lib/footbreak/crown \
  --manifest /var/lib/footbreak/crown/backfill-manifests/confirmed-34-dry-run.json
```

Review that the output says:

- `safe_to_apply: true`
- `pending_rows_before: 66`
- `changed_rows: 66`
- `conflict_rows_unchanged: 17`
- `provider_calls: 0`
- `telegram_calls: 0`

Then, without any intervening Crown state write:

```bash
python -m crown.backfill_confirmed_results \
  --fixtures /path/to/fixtures_42_verified.json \
  --state-dir /var/lib/footbreak/crown \
  --manifest /var/lib/footbreak/crown/backfill-manifests/confirmed-34-apply.json \
  --apply \
  --expected-ledger-sha256 <ledger_before_sha256-from-dry-run>
```

An intervening state change makes apply fail with `ledger_cas_mismatch`; rerun
the dry-run and review the new manifest rather than reusing the old hash.

## Focused validation

No full suite was run.

```text
python -m unittest crown.tests.test_backfill_confirmed_results -v
9 passed

python -m unittest \
  crown.tests.test_backfill_confirmed_results \
  crown.tests.test_settlement_fairness \
  crown.tests.test_stale_live_settlement
18 passed

python -m py_compile \
  crown/backfill_confirmed_results.py \
  crown/tests/test_backfill_confirmed_results.py \
  crown/settle.py
passed

git diff --check
passed
```

A local production-shaped validation used the exact authorized fixture file:

- dry-run: 42 fixtures, 34 confirmed fixtures, 8 conflict fixtures, 66 planned
  row changes, and 17 protected conflict rows;
- apply: 66 settled rows, 17 conflict rows still pending and byte-identical,
  unrelated row unchanged;
- backup SHA matched the pre-apply ledger SHA and mode was `0400`;
- second apply: 66 already-applied rows, zero changed rows, byte-exact no-op.

Artifacts are retained at:

- `/home/user/workspace/backfill-validation-real-fixtures-dry-run-20260827.json`
- `/home/user/workspace/backfill-validation-real-fixtures-apply-20260827.json`
- `/home/user/workspace/backfill-validation-real-fixtures-idempotent-20260827.json`
- `/home/user/workspace/backfill-validation-state-20260827/`

## Production readiness

The implementation is safe to advance to a production dry-run. Production
apply is not yet declared safe because this task intentionally did not deploy
the command or inspect the real production ledger. Apply becomes safe only
after the production dry-run passes all hard invariants above and its exact
ledger SHA is supplied unchanged to `--apply`.
