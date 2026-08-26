# Crown settlement backlog hotfix report

## Scope

Diagnosed and fixed starvation in `crown/settle.py` without deployment,
production access, network-dependent tests, Wilson admission changes, or
changes to exact-ID/result identity validation.

## Root cause

- Due rows were always traversed in stable ledger order.
- One settlement pass shared a short provider deadline.
- `TitanClient.result_detail` allowed only three exact-detail lookups per
  client/pass.
- Therefore the same first rows could consume the lookup cap or deadline on
  every pass while later unresolved rows received no detail lookup.

## Implementation

Changed `crown/settle.py`:

- Due rows are deterministically sorted by kickoff and namespace-qualified
  durable row ID.
- A small ledger-resident `settlement_state.titan_detail_cursor` records the
  kickoff/order key of the last row for which an exact Titan detail call was
  actually started.
- Each pass rotates the deterministic due order to the first row after that
  cursor.
- The cursor advances immediately before the provider detail call, including
  calls that fail or consume the remaining deadline.
- The existing three-detail-request cap remains in place and is also enforced
  in the settlement loop.
- Cursor persistence is merged into the latest ledger under the existing
  `state_lock`, alongside the existing settlement-owned merge. A concurrent
  T-5 commit is not replaced by a stale ledger snapshot.
- Existing exact-ID checks, strict fixture identity matching, official-score
  verification for corner fallback, and fail-closed pending behavior remain
  unchanged.

For 42 continuously unresolved eligible detail rows and an available
three-request pass budget, complete first-attempt coverage is bounded at 14
passes.

Added `crown/tests/test_settlement_fairness.py` with network-free tests for:

1. all 42 unresolved rows attempted exactly once within 14 passes;
2. failed early lookups advancing to later rows;
3. an early lookup consuming the shared deadline and the next pass resuming at
   the following row;
4. deterministic restart behavior from persisted cursor state; and
5. successful exact-detail settlement remaining idempotent.

## State and migration implications

- No one-off migration or schema rewrite is required.
- Legacy ledgers without `settlement_state` start from deterministic oldest
  due order and create the cursor lazily on the first actual detail lookup.
- The additive state shape is:

  ```json
  {
    "settlement_state": {
      "schema_version": 1,
      "titan_detail_cursor": {
        "kickoff_epoch": 0.0,
        "row_key": "bet:<durable-id>"
      }
    }
  }
  ```

- Rollback is safe: older code ignores the additive top-level key.
- If the final state lock cannot be acquired, both settlement updates and
  cursor advancement remain uncommitted and retryable, matching existing
  fail-closed commit behavior.

## Validation

Commands run from the repository root:

- `python -m unittest crown.tests.test_settlement_fairness -v`
  - 5 tests passed.
- Focused pre-existing settlement suite covering stale/fresh live fallback,
  deadline fail-closed behavior, concurrent T-5 merge preservation, formal
  observation settlement, provider reuse, inactive legacy shadow rows, exact
  HKJC corners, verified Titan corner detail, and score mismatch rejection.
  - 12 tests passed.
- `python -m unittest discover -s crown/tests -t .`
  - 590 tests passed; 11 skipped.
- `python -m py_compile crown/settle.py
  crown/tests/test_settlement_fairness.py`
  - passed.
- `git diff --check`
  - passed.

## Final diff

- Modified: `crown/settle.py`
- Added: `crown/tests/test_settlement_fairness.py`
- Added: `CROWN_SETTLEMENT_BACKLOG_HOTFIX_REPORT_20260826.md`
- No Wilson admission, portfolio admission, ledger selection, Titan parsing,
  deployment, or production files changed.
