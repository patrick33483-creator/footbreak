# Wilson 測試攻略 — implementation handoff

## Scope and activation

`Wilson 測試攻略` is a simulation-only strategy, independently namespaced as
`footbreak_wilson_test` and `crown_wilson_test`.  Each starts with HK$50,000,
stakes HK$500 per qualifying market, permits one selection per market and no
more than HDC/HIL/CHL (HK$1,500) per fixture.  It has no real execution, Kelly
calculation, or stake escalation.

Runtime loaders call `analysis.wilson_validation.ensure_namespace()` for an
idempotent cutover.  Its first call creates a fixed `activation_at` /
`cutover_at` plus a read-only `retired_v1` snapshot.  Existing
`independent-validation-v1` rows are never modified or deleted; old pending
rows remain in the settlement queue but cannot trigger entry notifications.

For an offline migration, run:

```text
python -m analysis.migrate_wilson_strategy PATH_TO_LEDGER footbreak
python -m analysis.migrate_wilson_strategy PATH_TO_LEDGER crown
```

The migration adds only the Wilson namespace and then recomputes Wilson-only
prospective metrics; it is rerunnable.

## Admission semantics

`analysis/wilson_portfolio.py` admits only the sole, persisted native T-5
snapshot before kickoff, with exact fixture, market, side, finite line,
decimal odds > 1, source provenance, and pre-kickoff quote timestamp.
Backfills, replays, stale/invalid quotes, duplicate T-5 rows, missing fixture
identity, and ambiguous market selections fail closed.

The historical/discovery ranking is passed as a frozen input.  A condition
requires at least 50 unique decided fixture-markets. Push/refund rows do not
enter its denominator; provided duplicate fixture-market evidence is deduped,
and conflicting outcomes reject the condition. The prospective Wilson result
store is not consulted at admission and is never folded back into evidence.

For hits \(h\), decided \(n\), actual decimal odds \(o\), and Wilson lower
bound \(L\), the exact raw comparison is:

\[
L \ge (1/o) + 0.03
\]

When \(L>0.03\), the stored raw minimum acceptable odds are
\(1/(L-0.03)\). Display values are rounded only after the comparison. Candidate
ties rank by raw safety margin, larger decided sample, higher Wilson lower,
then condition signature.

Worked example: the frozen HDC condition “首預→T-30→T-5 all 主讓；主隊讓
0.25–0.5；T-5 odds >=1.70；方向不變” with 41/59 at odds 1.90 passes:
observed hit rate is 69.5%, Wilson lower is approximately 56.9%, break-even is
52.6%, required is 55.6%, and the minimum acceptable odds are approximately
1.86. Code/tests assert the raw formula instead of rounded-number equality.

## Integration files

- `analysis/wilson_validation.py`: shared math, immutable evidence, portfolio,
  cutover archive, migration primitive and prospective metrics.
- `analysis/wilson_portfolio.py`: strict native T-5 admission adapter.
- `system/condition_portfolio.py`, `system/record_picks.py`,
  `system/settle.py`, `system/reset_condition_simulation.py`: Footbreak
  integration, non-destructive migration/reset entry point, and
  legacy-settlement retention.
- `crown/condition_portfolio.py`, `crown/ledger.py`,
  `crown/prediction_history.py`, `crown/reset_condition_simulation.py`: Crown
  integration and non-destructive migration/reset entry point; challenger v2
  remains research-only.
- `system/notify.py`, `crown/notify.py`: only committed Wilson simulation
  bets can send, using their existing system-specific configuration and durable
  idempotent retry state.
- `system/gen_app_data.py`, `hkjc-dashboard/app.js`,
  `crown/dashboard_data.py`, `crown/dashboard/app.js`: active Wilson cards,
  frozen eligibility arithmetic, prospective metrics, and a separate v1
  archive.

No Radar runtime, routes, config, notification path, or data file is changed.

### Exact test updates

- New: `analysis/tests/test_wilson_validation.py`,
  `system/tests/test_wilson_notifications.py`, and
  `crown/tests/test_wilson_notifications.py`.
- Updated Wilson/archive expectations:
  `analysis/tests/test_independent_validation.py`,
  `system/tests/test_shadow_portfolio.py`,
  `system/tests/test_reset_condition_simulation.py`,
  `system/tests/test_granular_condition_notifications.py`,
  `crown/tests/test_condition_portfolio.py`,
  `crown/tests/test_condition_simulation_ui.py`,
  `crown/tests/test_crown.py`,
  `crown/tests/test_reset_condition_simulation.py`, and
  `crown/tests/test_granular_condition_notifications.py`.
- The final two release-blocking Crown assertions were
  `test_confirmation_is_exact_and_migration_preserves_historical_archive`
  (now checks `wilson_validation.retired_v1`) and
  `test_recompute_stats_preserves_recovered_bet_results`
  (now supplies/validates the active `crown_wilson_test` metrics rather than
  retired v1 metrics).

## Example Telegram snapshot

```text
Wilson 測試攻略｜模擬注
系統：Footbreak / Crown
市場：讓球
實際十進制賠率：1.90
模擬注碼：HK$500
歷史：命中 41/59 · 69.5%
Wilson 95% 下限：56.9%
損益平衡命中率：52.6% + 3% = 55.6%
PASS：Wilson下限 56.9% ≥ 55.6%
最低可接受賠率 1.86；目前賠率 1.90
此為獨立測試模擬，沒有任何保證，並非真實投注或投資建議。
```

## Validation

Focused tests are in `analysis/tests/test_wilson_validation.py`,
`system/tests/test_wilson_notifications.py`, and
`crown/tests/test_wilson_notifications.py`. They cover formula boundaries,
minimum sample size, invalid odds, lower-bound guard, duplicate/conflicting
evidence, caps/opposite-side blocking, native/provenance gates, immutable
evidence, migration/cutover isolation, and Telegram content/retry/dedupe.
In particular, the low-odds regression proves that an otherwise valid frozen
41/59 condition is rejected when its actual odds are below its raw minimum
acceptable odds, while the supplied frozen evidence remains unchanged.

Run the focused gate before rollout:

```text
python -m unittest analysis.tests.test_wilson_validation \
  system.tests.test_wilson_notifications crown.tests.test_wilson_notifications -v
python -m compileall -q analysis system crown
node --check hkjc-dashboard/app.js
node --check crown/dashboard/app.js
git diff --check
```

The full suites completed after the legacy assertions were precisely replaced
with active-Wilson / read-only-v1-archive expectations:

```text
python -m unittest discover -s analysis/tests -p 'test_*.py'  # 246 OK
python -m unittest discover -s system/tests -p 'test_*.py'    # 211 OK, 10 skipped
python -m unittest discover -s crown/tests -p 'test_*.py'     # 204 OK, 11 skipped
```

The replaced v1 assertions cover only superseded policy: 20-sample / 60%
admission, HK$250 two-market caps, old v1 metrics/namespace, and actionable
old granular entry alerts. Archive retention, pending settlement, and all
unrelated safety assertions remain covered; no test or compatibility adapter
can create a new v1 entry or old actionable entry message.
