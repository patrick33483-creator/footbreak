# 獨立驗證倉交接

## 範圍

已在既有 Footbreak／Crown 分析、帳本、結算、Telegram 文案及 dashboard 內完成獨立驗證倉切換；沒有建立新網站。兩個系統只會把新注寫入各自的 `independent_validation` namespace 與 `*_independent_validation` active portfolio。

## Migration 語意

- 第一次處理既有 ledger 時，`ensure_namespace()` 只追加 versioned `independent_validation` metadata；它不清空、不覆寫、也不搬移舊 `bets`、`stats`、`bankroll` 或其他 legacy keys。
- namespace 保留系統各自的 `validation_started_at`、read-only `historical_discovery_archive` snapshot、凍結條件、bounded audit arrays 與 validation-only 統計。
- 舊 condition portfolio 注單／統計保持 archive；dashboard、settlement 和 active flow 只看 `portfolio=<system>_independent_validation` 且 `strategy=independent-validation-v1` 的新注。
- `reset_condition_simulation` 現為 guarded migration-only compatibility command：只確保 namespace 存在，回傳 `cleared_main_bets: 0`，不再破壞性重置任何 archive。
- active entry 僅限第一次持久化的 native pre-kickoff `T-5`。必須有有效 odds（>1）、有效盤口／方向、真實來源識別及可證明早於 kickoff 的觀測時間；重跑、回填、post-hoc、T-30 和來源不完整均 fail closed。
- candidate gate 為 historical decided >=20 且 accuracy >60%。首次 admission 會凍結完整 definition/signature 與 discovery baseline；重算 ranking 或後續 validation outcome 均不會覆寫該 baseline。
- 同 fixture 限兩個市場、HK$500；每注 HK$250。候選同市場必須精確匹配被保存的方向／線位，因此不會加入 opposite/conflicting line。多候選以 usable Wilson lower bound、decided、較低 specificity、stable signature 的保守 deterministic 排序選擇；無 Wilson 時不以 raw accuracy 排名。
- 結算沿用 canonical Asian outcomes，只結算 active validation bets；prospective hit-rate 排除 `Refunded`，`Won`/`Half Won` 計 hit，PnL 取實際 Asian PnL。狀態為「驗證中」(<30 decided)、「觀察」或「已驗證」(>=30、ROI>0、accuracy > weighted implied break-even +3pp)。

## 主要變更檔案

### 共用／分析

- `analysis/independent_validation.py`（新增）：version guard、append-only migration、frozen definitions/baselines、conservative selection、caps、validation-only metrics/status。
- `analysis/granular_conditions.py`
- `analysis/tests/test_independent_validation.py`（新增）
- `analysis/tests/test_granular_conditions.py`

### Footbreak

- `system/condition_portfolio.py`
- `system/record_picks.py`
- `system/settle.py`
- `system/gen_app_data.py`
- `system/notify.py`
- `system/reset_condition_simulation.py`
- `system/tests/test_granular_condition_notifications.py`
- `system/tests/test_reset_condition_simulation.py`
- `system/tests/test_shadow_portfolio.py`
- `hkjc-dashboard/app.js`
- `hkjc-dashboard/index.html`
- `hkjc-dashboard/league_display.js`（新增；exact-only繁中聯賽 mapping）
- `hkjc-dashboard/tests_league_display.mjs`（新增）

### Crown

- `crown/condition_portfolio.py`
- `crown/ledger.py`
- `crown/prediction_history.py`
- `crown/dashboard_data.py`
- `crown/notify.py`
- `crown/reset_condition_simulation.py`
- `crown/dashboard/app.js`
- `crown/dashboard/index.html`
- `crown/tests/test_condition_portfolio.py`
- `crown/tests/test_condition_simulation_ui.py`
- `crown/tests/test_crown.py`
- `crown/tests/test_granular_condition_notifications.py`
- `crown/tests/test_reset_condition_simulation.py`
- `crown/tests/test_stale_live_settlement.py`

## UI／通知

- 兩個主模擬倉均顯示「獨立驗證倉」、驗證起點、HK$50,000 起始本金、現金／權益、HK$250 每注、HK$500 每場上限、archive 摘要及 frozen condition 的「歷史發現 x/y／獨立驗證 a/b」、狀態、前瞻盈虧／回報率；legacy totals 不會混入主數字。
- Footbreak upcoming rows 現以「主隊 vs 客隊」作主要大字；聯賽改為次要小字並透過 `league_display.js` exact mapping 顯示繁中。已覆蓋 MLS、智利甲、巴西甲、阿根廷甲及常見聯賽；未知名稱原樣回退，沒有模糊翻譯。
- committed validation bet Telegram 文案讀取 frozen discovery/prospective metrics；僅用繁中「候選條件，獨立驗證中」或「已通過獨立驗證」，未驗證絕不稱為可投注；成功送出後才更新 bounded dedupe state。Radar 沒有改動。

## 已執行驗證（2026-08-16 HKT）

- `python -m unittest discover -s analysis/tests -t .`：222 tests passed（146.320s）。
- `python -m unittest discover -s system/tests -t .`：179 tests passed，10 skipped（37.450s）。
- `python -m unittest discover -s crown/tests -t .`：183 tests passed，11 skipped（45.263s）。
- Targeted `crown.tests.test_condition_portfolio` + `analysis.tests.test_independent_validation`：15 tests passed。
- `bash crown/validate.sh`：passed；其輸出為 offline validation、`tick --dry-run` 及本機 dashboard artifact，沒有 remote provider request。
- `python -m compileall -q system bin crown analysis`：passed。
- `node --check hkjc-dashboard/app.js`、`node --check hkjc-dashboard/league_display.js`、`node --check crown/dashboard/app.js`：passed。
- `node hkjc-dashboard/tests_league_display.mjs`：passed。
- 全部 21 個 `.github/workflows/*.yml|*.yaml` 經 PyYAML parse：passed。
- `bash -n`（既有 deploy/system/crown shell list）：passed。
- `git diff --check`：passed。

## 部署注意

1. 不需要也不應以 reset 清除舊 ledger；正常 tick 首次寫入時會自動做 append-only cutover。
2. 部署後讓 dashboard generator 讀取同一份 ledger；只會公開 active validation rows，archive 只作摘要。
3. 保留 Crown T-5 cached granular ranking fast path：production tick 只讀持久化 ranking/frozen definitions，沒有在 T-5 重做全歷史 mining；Footbreak 同樣不在 deadline 路徑重算。
4. 此工作沒有 commit、push、deploy、production 存取、provider call 或 Telegram 發送；也沒有修改或刪除既有 `.runtime-validation/` 與原有 handoff files。
