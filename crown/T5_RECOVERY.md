# Crown missed T-5 recovery

`crown.t5_recovery` is an offline-only repair utility for **missed** Crown T-5
records. It never contacts a provider, sends Telegram, creates a simulated
bet, writes learning data, or runs settlement.

## Evidence and exclusions

The tool requires exact local fixture identity (`match_id`, Titan fixture ID,
kickoff, home, away) and accepts only saved Crown company-ID-3 HDC/HIL
selections with an exact market, numeric line, and valid side. It rejects
post-kickoff source stages and quotes.

For every carried-forward selected market it uses this evidence ladder:

1. exact pre-kickoff T-5 evidence, or the latest valid quote at/before T-5
   (LOCF);
2. only if unavailable, the final valid quote before kickoff, labelled
   `closing_substitution`.

If a native `T-5` stage already exists, the tool skips it. If no native T-5
model stage exists, it copies only the latest valid pre-kickoff saved stage
payload into `T-5（事後回補）`. This is a visible `POST-HOC / BACKFILLED` audit
record, never a native T-5 prediction. It is excluded from Telegram,
simulations, learning, settlement/grade processing, hit-rate/ranking/consensus
statistics, and primary market statistics.

## Local use

Run the mandatory read-only audit first:

```bash
PYTHONPATH=. python3 -m crown.t5_recovery --dry-run --provider-company-id 3
```

Only after reviewing the aggregate JSON audit, apply with the exact phrase:

```bash
PYTHONPATH=. python3 -m crown.t5_recovery \
  --apply \
  --apply-confirmation APPLY_CROWN_T5_RECOVERY \
  --provider-company-id 3
```

`--apply` requires exactly that confirmation. It takes the Crown state lock,
rechecks idempotency, writes a fsynced backup set under
`$CROWN_STATE_DIR/t5-recovery-backups/`, then atomically replaces only
`ledger.json` and `prediction_history.json`. It does not alter bets, results,
or settlement state.

For an isolated state directory in a local test:

```bash
PYTHONPATH=. python3 -m crown.t5_recovery --dry-run --state-dir /tmp/crown-state
```

## GitHub Actions manual workflow

Run **Crown missed T-5 recovery (audit first)** from Actions. Select:

- `mode: AUDIT` for the read-only dry run; or
- `mode: APPLY` and set `apply_confirmation` to exactly
  `APPLY_CROWN_T5_RECOVERY`.

Every run performs the read-only remote audit before any eligible apply. Its
artifacts contain aggregate kickoff/stage/market/evidence and unresolved-reason
counts only; they contain no fixture IDs, provider names, quote payloads, or
source snapshots.
