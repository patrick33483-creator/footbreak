# Crown condition-driven simulation portfolio — implementation report

Branch: `unified-condition-simulation-20260814`  
Status: complete locally; no commit, push, deployment, production access, or Telegram send was performed.

## Delivered behavior

- Retired Crown Shadow Portfolio and Handicap World dashboard navigation/sections and active creation/settlement flows.
- Added the sole active Crown portfolio: `granular-condition-v1`, beginning bankroll HK$50,000 and HK$1,000 fixed stake per bet.
- Creates only on a newly persisted T-5 stage, never T-30/backfill; requires a selected odds value >1, selected line/side, and auditable pre-kickoff evidence.
- Condition eligibility uses only historical pre-kickoff settled `GRADED` rows through `analysis.granular_conditions.mine`, excluding the live fixture; requires strictly >60% accuracy and at least 10 decided samples.
- Supports one fixed-stake bet per fixture/market for HDC/HIL/CHL and permits multiple distinct markets for one fixture. Conflicting condition direction/line for one market fails closed and emits an audit reason. Bet identity is fixture + market + T-5 + strategy.
- Stores the best condition explanation, accuracy, hits/decided, badge, odds tier, exact selection line/side/odds, and Chinese public labels.
- Settlement filters only active condition-simulation bets and retains canonical HDC/HIL/CHL settlement logic, including the existing confirmed-corners handling.
- Added a manually dispatched GitHub reset workflow and `crown.reset_condition_simulation`. It requires exact phrase `RESET_CROWN_CONDITION_SIMULATION_50000`, stops Crown timers during reset, resets bankroll/bets/stats, removes retired keys, regenerates dashboard data, and prints aggregate-only output.
- Kept Odds Radar untouched and left granular T-30/T-5 notifications active.
- User-facing market names are Chinese in dashboards, portfolio/audit cards, condition rankings/matches, and Crown/Footbreak granular Telegram content: `讓球`, `入球大細`, `角球大細`. New public granular descriptors use observed Chinese paths such as `主讓→客受讓→主讓`, `大→細→大`, and `角球大→角球細→角球大`; sanitizers remove legacy raw codes and A/B/C path tokens at display/message boundaries.

## Main changed files

### Product code
- `analysis/granular_conditions.py`
- `crown/condition_portfolio.py` (new)
- `crown/ledger.py`
- `crown/settle.py`
- `crown/engine.py`
- `crown/handicap_world.py`
- `crown/dashboard_data.py`
- `crown/dashboard_api.py`
- `crown/dashboard/index.html`
- `crown/dashboard/app.js`
- `crown/notify.py`
- `system/notify.py`
- `hkjc-dashboard/app.js`
- `crown/reset_condition_simulation.py` (new)

### Workflows
- `.github/workflows/reset-crown-condition-simulation.yml` (new)
- `.github/workflows/settle-handicap-world.yml`
- `.github/workflows/settle.yml`
- `.github/workflows/footbreak-live-diagnose.yml`

### Tests
- `analysis/tests/test_granular_conditions.py`
- `crown/tests/test_condition_portfolio.py` (new)
- `crown/tests/test_condition_simulation_ui.py` (new)
- `crown/tests/test_reset_condition_simulation.py` (new)
- `crown/tests/test_granular_condition_notifications.py`
- `system/tests/test_granular_condition_notifications.py`
- Existing Crown/system UI, settlement, retired-workflow, history, and data-health tests updated for the new active-only behavior.

## Verification executed

- Targeted condition/localization/reset tests: 23 tests, passed.
- Full Crown suite: 139 tests, passed; 11 intentionally skipped.
- Full system suite: 173 tests, passed; 10 intentionally skipped.
- Full analysis suite: 217 tests, passed.
- `node --check crown/dashboard/app.js` and `node --check hkjc-dashboard/app.js`: passed.
- YAML parsed for all workflows; changed workflow YAML structures passed.
- Shell syntax for every `run` command in each changed workflow passed via `bash -n`.
- `git diff --check`: passed.
- Static public-label guard checks passed.

No repository commit was created.
