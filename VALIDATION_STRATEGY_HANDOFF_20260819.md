# 修復驗證策略：rollout handoff（2026-08-19 HKT）

## 範圍與作業保護

- 隔離 worktree：`/home/user/workspace/worktrees/footbreak-repair-validation-strategy`
- 分支：`repair-validation-strategy-20260819`
- 基礎提交：`origin/main` 的 `b09b1da3343d3bf03bac65ba736dc78644a2a0b0`
- 本次只讀寫本機隔離 worktree 和本機測試暫存目錄；**沒有**存取 production/provider、發 Telegram、部署或 push。

## 已確認根因：Footbreak `no_granular_match`

T-5 admission 原本從 `system/accuracy_history.json` 的 `stats.granular_conditions.ranking` 取得 frozen discovery ranking；但該檔只保存 accuracy history，ranking 只在 dashboard 建構期間即時計算，從未持久化。因此 ranking 永遠缺失，admission fail-closed 為 `cached_discovery_ranking_unavailable`，而診斷對應為 `no_granular_match`；尚未走到 historical gate。

## 實作語義

### A. Footbreak：嚴格、可持久化 granular matching

1. `system/accuracy.py` 於正常的已結算 accuracy 更新後，從已保存且已結算的 market grades 產出 `system/granular_condition_ranking.json`。artifact 帶有 `schema_version=1`、`system=footbreak`、生成時間與 ranking；檔案以 atomic replace 寫入。
2. `system/record_picks.py` 只讀取上述 artifact，並驗證 schema/system；artifact 不存在、格式錯誤或系統不符時仍 fail-closed，絕不臨場重新挖掘或使用事後資料。
3. `analysis/granular_conditions.py` 新增窄範圍 canonicalization：只容許已持久化 key 的安全同義欄位（例如 `odds_tier`/`tier`、`line_bucket`/`bucket`、`observed_path`/`path`、`stage`/`decision`）。
4. admission ranking 必須完整且完全相同：`system`、`market`、`observed path`、`decision stage`、`odds tier`、`direction`、`role`、`line bucket`。缺任何一項、欄位互相矛盾、跨系統/市場、錯 stage 或錯 path 均直接排除；沒有 fuzzy match、跨市場、反方向或補填。
5. 原有安全邊界維持：首次原生賽前 T-5、有效 quote provenance、完整方向/盤口/市場、每場最多兩市場、冪等與 fail-closed。每注仍為 HK$250，既有每場 HK$500 上限不變。

### B. Crown v2：隔離的 champion/challenger 研究倉

1. 新增 `crown_v2_challenger` namespace，strategy 為 `crown-independent-validation-v2-challenger`，固定 policy floor 為 `CUTOVER_AT=2026-08-19T20:00:00+08:00`。第一次建立 namespace 時同時凍結不可變 `activation_at`；實際 prospective admission boundary 是兩者較晚者。這避免部署時 policy floor 已過而把 namespace 啟用前的舊 T-5 追溯收進 v2。
2. v1 benchmark 在 **v2 activation**（不是虛構的 policy cutover 時點）對 validation bets 與 stats 做 SHA-256、數量與深拷貝快照；欄位明確為 `*_at_activation`，並保留 policy cutover 供治理追溯。benchmark 唯讀；v2 不會篩選、重分類、寫入或混入 v1 rows/stats/dedupe。
3. v2 只接受嚴格晚於 `max(policy cutover, activation_at)`、原生、首次、賽前的 T-5；backfill、回放、無 fixture context、無 provenance、非賽前、等於/早於 policy floor 或 activation boundary 一律拒絕。
4. v2 使用獨立 `research_bets`、research IDs、dedupe keys、audit、stats，且不 append 到既有 `ledger['bets']`。每列保留 `namespace_activation_at` 及實際 `admission_boundary_at`；研究列均標示 shadow/simulation only、real betting false、Kelly false、actionable Telegram false，固定 HK$250，最多兩市場。
5. 候選格是版本化研究條件，不是由近期結果硬編盈利規則：
   - HIL：`1.80 <= odds < 1.90`
   - HDC：`1.90 <= odds < 2.00`，僅研究
   - HDC：`1.80 <= odds < 1.90` 明確排除
   - HIL/HDC 保持分市場、精確 side/line/quote provenance gate。
6. 每個合資格市場同時建立 `no_league` 與 `league_shrunk` prospective ablation。league effect 只可使用明確 `frozen_pre_cutover_ready`、且 freeze 時間嚴格早於 cutover 的市場層全局/聯賽概率；以 `n/(n+30)` partial pooling 回縮。沒有足夠 frozen evidence 時會維持 research-only，不能自動聲稱某聯賽有優勢，也不會使用 raw exact-league × odds cell 作 entry。
7. 報告以每市場 unique fixture 為主要單位，league × odds × market 只供 prospective 聚合展示，`promotion_gate_use=false`。promotion 永遠不自動：至少 100 unique fixtures（200 為 preferred）、正 ROI、hit rate 與 Wilson 下限均必須高於實際 weighted break-even 3pp、且 Brier/Log Loss/校準不差於 no-league ablation 和 v1 champion。任一概率欄位缺失即清楚 blocked，不偽造機率指標。
8. 結算沿用既有的已驗證賽果處理，但只會以 `research_id` 更新隔離 v2 research row，不會以此改寫 v1 row。
9. dashboard payload 把 v2 放在獨立 top-level `v2_challenger`，從 v1 ledger projection 移除；畫面以繁中顯示「v2挑戰者研究中」「非正式推介」、政策截點、啟用界線、v1 activation-time 唯讀失敗基準、無 actionable Telegram/Kelly/自動升格。v2 不會進入既有 actionable notification 流程。

## 精確異動檔案

- `.gitignore`
- `analysis/granular_conditions.py`
- `analysis/tests/test_granular_conditions.py`
- `analysis/tests/test_independent_validation.py`
- `system/accuracy.py`
- `system/record_picks.py`
- `crown/challenger_v2.py`（新增）
- `crown/ledger.py`
- `crown/settle.py`
- `crown/dashboard_data.py`
- `crown/dashboard/app.js`
- `crown/tests/test_challenger_v2.py`（新增）

`system/granular_condition_ranking.json` 是 runtime artifact，已加入 `.gitignore`，不納入 commit。

## 驗證結果

- Footbreak targeted：`analysis.tests.test_granular_conditions`、`analysis.tests.test_independent_validation`、`system.tests.test_shadow_portfolio`：20 passed。
- Crown targeted + UI smoke：31 passed；`system/tests/challenger_ui_smoke.mjs` passed。
- Crown v2 activation boundary focused：6 passed，涵蓋「policy 後但 activation 前拒絕」「activation 後接受」「reload 後 activation 不變」。
- 完整 analysis：227 passed（122.367s）。測試輸出的 data-health Telegram/provider failure 是本機 mock 的負向路徑斷言，沒有對外發送或存取。
- 完整 Crown：199 passed，11 skipped（1.449s）。
- 完整 system：202 passed，10 skipped（11.463s）。
- `python -m py_compile`（所有改動 Python source/tests）通過。
- `node --check`（`crown`、`system` 全部 `.js`/`.mjs`）通過。
- `bash -n`（全部 `.sh`）通過。
- PyYAML 解析 `.github` 和 `deploy` 共 22 個 YAML 檔通過。
- `git diff --check` 通過。

## 建議 rollout / 切換步驟（需由有權限的人員執行）

1. 先 review 此分支 diff 與上述 suite；合併前備份 Footbreak/Crown state，特別是既有 v1 ledger 與 stats。不可刪歷史，也不可把 v1 bets 複製到 v2。
2. 按既有受控流程發布程式碼；本次不含部署動作。確認 runtime 有權以原子方式寫入 `system/granular_condition_ranking.json`。
3. 先讓 Footbreak 正常 accuracy/settlement cycle 產出 ranking artifact，核對 `schema_version=1`、`system=footbreak` 與 ranking 結構；未生成、schema 不符或空 ranking 時，T-5 必須繼續 fail-closed，不能人工放寬 >60% 或 decided>=20。
4. 在啟用 Crown runtime 的第一個正常同步時，確認 Crown state 首次建立 `crown_v2_challenger` 並凍結 `activation_at`；核對 `v1_frozen_benchmark` 的 `*_at_activation` count/hash/stats 已保存。任何 timestamp 不嚴格晚於 `max(CUTOVER_AT, activation_at)` 的 T-5 必須拒絕；確認 v2 row 不在 root `bets`，並有獨立 research IDs/dedupe。
5. 核對 dashboard：v1 歷史失敗基準與 v2 分離，v2 出現「v2挑戰者研究中」「非正式推介」；核對 v2 不經 actionable Telegram policy。
6. 只累積 prospective research。每市場未達 100 個 unique fixtures 前不得進 promotion review；即使達標，200 為較強樣本，且仍須人工 review。任何概率欄位不完整、league effect 非 frozen pre-cutover evidence、或 metrics 遜於 no-league/v1，均保持 blocked。

## Remaining risks / 監控點

- 嚴格 key 因修復後可能顯著減少候選；這是刻意 fail-closed，應監控 artifact freshness、完整 axes 與 `no_granular_match` 分解，而不是放鬆跨盤/反向/模糊配對。
- ranking artifact 由 settled history 更新，若 accuracy cycle 延遲或無可用 settled rows，T-5 會安全拒絕；需告警 artifact 缺失/過期，但不可在 deadline 期間重新挖掘或回填。
- v2 目前沒有可用的 frozen pre-cutover league evidence 時，league ablation 仍是 research-only；不可手動填入由 cutover 後資料或 exact league×odds 表格得出的 effect。
- 部分現有/未來資料可能沒有可用 probability；此時 Brier、Log Loss、校準及 promotion 都必須 blocked，不能以 odds 或結果倒推偽造。
- 同場兩市場不被當成兩個獨立 promotion fixtures；報告以 unique fixture per market 處理，跨市場相關性仍應在人工 review 審閱。
- 固定 policy cutover 與 namespace `activation_at` 共同構成治理邊界；activation 是第一次建立時凍結，任何日後條件調整必須建立新版本/新 cutover 與新的 activation evidence，不能改寫 v2 現有 rows 或回溯套用。
