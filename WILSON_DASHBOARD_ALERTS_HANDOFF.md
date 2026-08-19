# Wilson dashboard alerts — handoff

- **Baseline / branch:** `109181fe5ed87fc258331d9f4e37242f2a311529` / `improve-wilson-dashboard-alerts-20260820`.
- **Scope:** Durable Wilson condition numbering is persisted in `wilson_validation.condition_order` plus the frozen record. The dashboard projections carry both raw admission odds and the frozen display value. Match cards show every matched market, including `因賠率不足，不投注` observations which stay outside formal bets.
- **Telegram:** Footbreak and Crown now emit short, durable, bounded-retry Wilson match alerts for both formal simulations and low-odds no-bets. Alert IDs use the persisted formal bet/observation identity; no generic no-match alert is sent. On load, the shared `wilson_match_alerts` outbox is deduped/bounded and seeded from the old formal-only acknowledgement list (`condition_simulation_bets` / `wilson_bets`), so an already-acknowledged pending formal bet cannot replay after upgrade. Observations only enter the shared outbox, never either legacy formal-bet key. A transport failure or timeout is not acknowledged.
- **Footbreak history:** `data.json` is summary-only; atomic `history.json` has full rows, version, and no-store serving. The browser defers fetching until `純預測紀錄`, caches the sidecar, renders 50 rows/page, resets on stage filter, handles version changes/in-flight requests, retryable errors, and legacy inline rows. `run_all.sh` was not changed: deadline-bound `tick` still skips `gen_app_data.py`; sweep/settle publication paths produce the sidecar.
- **Deployment:** runtime history is copied only after a successful pass, preserved through setup/update static sync, permissioned `0644`, and served `no-store`. `setup.sh` keeps existing runtime `data.json`/`history.json` untouched on upgrade. If (and only if) `WEB_ROOT/data.json` is absent, `system.gen_app_data --bootstrap-empty --out ...` writes an atomic, schema-compatible, state-free empty `data.json` plus `history.json` before nginx starts. It does not read `predictions.json`, ledger/archive data, or providers; the dashboard explicitly says that its first scan has not run.
- **T-5/Radar guard:** no changes to `system/run_all.sh`, `system/run_predict.py`, `crown/run.py`, `crown/t5_recovery.py`, deadline scheduler tests, or any Radar path.

## Validation performed

- Full analysis suite: 250 passed.
- Full Footbreak suite: 228 passed, 10 skipped.
- Full Crown suite: 209 passed, 11 skipped.
- Focused notifier/deployment/bootstrap suite: 28 passed.
- Python compile, JS syntax, Footbreak lazy-history/challenger/data-health smoke, shell syntax, YAML parsing, and `git diff --check` passed.

## Rollout verification

1. Run a sweep/settle publication and verify Footbreak `data.json` has no `prediction_history.rows`, but includes `history_data_url` and `history_data_version`; verify matching `history.json` is `0644` and version-equal.
2. Load both dashboards: network boot should request only `data.json`; opening history should request `history.json` once; switch stage and load more rows; verify forced sidecar error exposes `重新讀取`.
3. On a controlled Wilson candidate, verify same `條件 #N` on list/card/alert. Check an above-minimum quote becomes a formal simulated bet and a below-minimum quote stays an observation with `不投注（賠率不足）`.
4. Before enabling any timer, verify a pre-upgrade notify state containing only a formal legacy ID does not emit that formal alert, while a fresh low-odds observation emits once and never appears in the legacy formal key.
5. On a genuinely empty staging host (no dashboard artifacts or Footbreak state files), complete setup without credentials/provider access and confirm parseable `data.json` and empty `history.json` exist before nginx starts; the initial prediction view must state that the first scan has not run. Rerun setup with sentinel runtime `data.json`/`history.json` and confirm both remain unchanged.
6. Verify timer tick timing/T-30/T-5 completeness before and after rollout; no Telegram or provider call was performed in this worktree.
