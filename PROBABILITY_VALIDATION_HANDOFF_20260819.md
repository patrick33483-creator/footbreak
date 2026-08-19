# 機率驗證優化交接（研究用途）

## 範圍與基準

- worktree：`/home/user/workspace/worktrees/footbreak-probability-validation-20260819`
- branch：`research/probability-validation-20260819`
- base：`origin/main` cached ref，`539a7a9fdf943943b75bfca3911aa4ec8c00f34b`
- 沒有 commit、push、deploy、provider/production 存取或 Telegram 發送。

## 已實作

### Footbreak

新增獨立 `footbreak_probability_research` namespace（策略 `footbreak-hierarchical-probability-challenger-v1`）。它只保存 research rows，絕不寫入既有 `independent_validation`、`bets`、正式 ranking、PnL 或通知流程。

- 固定、可審計四層 empirical-Bayes beta-binomial 回退：`exact` → `no_league` → `relaxed_line` → `market_prior`。
- 固定 prior strength = 30；每層保存 raw `n/hits/rate`、prior `n/rate`、posterior mean、Wilson 95%、實際權重、完整 condition signature 與版本。
- 原始證據會拒絕其他 system/stage/market/path/odds tier/direction/role、post-hoc/backfill、走水／退款與未知結果；缺少凍結證據時明確為 `unavailable`，不補造數值。
- 每個合格候選同時建立 `exact_only` 與 `hierarchical_shrunk` 前瞻 ablation row，含 fixture identity、原生 T-5 timestamp、盤口來源／觀測時間、market/side/line/odds、break-even、估計機率、edge 與 status。
- `record_picks` 僅作隔離呼叫；新研究列沒有正式 bet、沒有 actionable Telegram、沒有 Kelly、沒有自動升級。
- 啟用／cutover 邊界不可變，舊 T-5 或回補資料不會成為 prospective row。

註：目前 Footbreak production-shaped caller 故意傳入 `evidence=None`，因現有 accuracy artifact 沒有可安全驗證的完整分層 raw axes。故會保存「未有證據」研究列、不可晉級；後續只有新增「凍結、賽前、完整 axes」的 evidence adapter 才可啟用數值估計，嚴禁以舊 validation、ranking aggregate 或回補資料代替。

### Crown v2

擴充既有 `crown_v2_challenger`，維持 v1 frozen benchmark 唯讀。

- 同市場無水 baseline 只接受兩邊同 fixture、market、line、observed_at、source 的 Crown quote，且兩邊皆可證明賽前；任何不符均 `unavailable`。
- 每列保存 market-implied、model／shrunk probability、break-even、edge、Brier、log loss、calibration 所需欄位及 CLV 狀態。
- CLV 只使用同市場、同方向、同盤口、同 source、真實 pre-kickoff closing quote；沒有即 unavailable，絕不用賽後或後見資料。
- scorecard 一律 `unique fixture + market`；league shrinkage 沿用 frozen pre-admission evidence 與 `n/(n+30)`，不分裂 league×odds 細格。
- promotion gate：至少 100 unique fixtures（200 preferred）、ROI 正、Wilson lower > weighted break-even +3pp、Brier/Log Loss 不差於 no-vig market baseline、CLV coverage >=70%、mean CLV >=0；任何必要資料不存在即 blocked，仍只可人手覆核。

### Dashboard

- Footbreak 獨立驗證倉加「機率驗證研究」繁中卡：研究中／非正式推介、exact-only 與分層收縮、sample、ROI、Wilson、weighted break-even、Brier、Log Loss、calibration、CLV unavailable semantics、保守晉級條件。
- Crown challenger 頁加 v2 中文研究摘要，清楚說明 strict same-quote no-vig、unique fixture+market、CLV coverage 與不自動升格／不發 Telegram／不用 Kelly。
- 所有沒有資料位置顯示「未有證據」，不顯示 0。

## 變更檔案

1. `analysis/probability_research.py`（新增）
2. `analysis/tests/test_probability_research.py`（新增）
3. `system/probability_research.py`（新增）
4. `system/tests/test_probability_research.py`（新增）
5. `system/record_picks.py`
6. `system/gen_app_data.py`
7. `hkjc-dashboard/app.js`
8. `crown/challenger_v2.py`
9. `crown/dashboard/app.js`
10. `crown/tests/test_challenger_v2.py`
11. `PROBABILITY_VALIDATION_HANDOFF_20260819.md`（本檔）

## 測試結果

最終完整 suite：

- `python -m unittest discover -s analysis/tests -t .`：230 passed（125.249s）
- `python -m unittest discover -s system/tests -t .`：205 passed，10 skipped（15.162s）
- `python -m unittest discover -s crown/tests -t .`：202 passed，11 skipped（1.615s）
- focused research／Crown／Footbreak smoke：13 passed
- `python -m compileall -q analysis system crown`：passed
- `node --check hkjc-dashboard/app.js`：passed
- `node --check crown/dashboard/app.js`：passed
- `bash -n crown/validate.sh deploy/crown-run.sh deploy/setup.sh deploy/update.sh`：passed
- `git diff --check`：passed

涵蓋：leakage／post-hoc、opposite direction、push/refund、缺證據、same-market two-side validation、idempotency、unique fixture+market、activation immutability、no v1 mutation／no notification、unavailable metric semantics、promotion fail-closed，以及 dashboard 靜態 smoke。

## 已知風險／後續工作

1. Footbreak 需要一個新、append-only、frozen pre-admission raw evidence artifact（完整 system/stage/market/path/tier/direction/role/line bucket，可能有 league）才可把目前 unavailable 的 row 安全轉為數值機率。不可從 aggregate ranking 重建，否則會重疊計樣本及引入 leakage。
2. Crown 現有 stage payload 通常只保存 selected quote；未持久化兩邊同 timestamp/source 時 no-vig 與 CLV coverage 會保持 unavailable。這是設計上的 fail-closed，而非數據缺陷被歸零。
3. 目前沒有任何 promotion 或正式投注 cutover；即使未來 gate 滿足也必須獨立人手審批與另一次明確 activation。

---

# 證據管線補充交接（2026-08-19）

## 已消除的 runtime 缺口

Footbreak 不再依賴 aggregate granular ranking 重建機率證據。`system/accuracy.py` 在每次正式 accuracy update 完成後，從 `accuracy_history` 內的 raw formally-graded stage rows 生成並原子置換：

`system/footbreak_probability_evidence.json`

若測試或隔離環境覆寫 `HISTORY_OUT`，artifact 會相應寫到該 history 同一目錄；正式 runtime 可用 `FOOTBREAK_PROBABILITY_EVIDENCE_PATH` 明確覆寫。

Artifact contract：

- schema `1`、system `footbreak`、source boundary、generated_at、entries SHA-256、固定 50,000 行上限與 bounded aggregate diagnostics。
- 只收原生 T-5、正式 `GRADED`、可決定 Win/Loss、具有效 fixture/kickoff/stage timestamp、source/observed_at pre-kickoff quote、market/side/line/odds 的 raw rows。
- `Refunded`／push（`hit=None`）保留原本 settlement/ROI 語義，但不會進入 binary hits/decided evidence。
- post-hoc/backfill/excluded、缺 field、quote 不可證明賽前、無 result、錯 market/side/line/odds 均會排除並只以 aggregate reason 計數。
- 從同一 fixture-market 的可驗證 staged snapshots 衍生 path；每個 entry 自帶 exact/no_league/relaxed_line/market_prior 所需 axes（system、stage、market、path、odds tier、relative direction、role、line bucket，和可用 league）。

## Admission 與 immutable as-of

`system/record_picks.py` 現在把 artifact path 傳給隔離 research evaluator。Admission 會：

1. 驗證 schema/system/hash、entry completeness、source boundary、artifact freshness（48 小時）與 `generated_at <= native T-5`。
2. 只取 `decided_at <= stage_at` 的 entries；stage 後才知的結果不會進入該 candidate。
3. 對每個建立 row 寫入 `evidence_snapshot`：artifact entries SHA-256、artifact generated/source boundary、as-of time、as-of entries SHA-256、usable row count、coverage。
4. 同一 candidate 的 dedupe row 不能重寫 probability；即使之後 artifact 更新或加入新結果，既有 row 仍使用首次 admission 的 frozen estimate。

artifact 缺失、malformed、hash 不符、stale、generated/source boundary 晚於 candidate、或任一 entry 不合規時，一律 `unavailable`，而非回退到 ranking、v1 validation、provider 或 post-hoc 資料。

## Dashboard

Footbreak「機率驗證研究」新增不含 fixture/provider ID 的 artifact aggregate 資訊：freshness timestamp、source boundary、accepted rows、按市場 coverage、排除原因。不可用時清楚顯示「未有證據」。

## 本次新增／修改檔案

新增：

- `analysis/footbreak_probability_evidence.py`
- `analysis/tests/test_footbreak_probability_evidence.py`
- `system/tests/test_probability_evidence_generation.py`

修改：

- `system/accuracy.py`
- `system/probability_research.py`
- `system/record_picks.py`
- `system/gen_app_data.py`
- `hkjc-dashboard/app.js`
- `system/tests/test_probability_research.py`

（保留前一節所列 Footbreak/Crown probability 與 dashboard 變更。）

## 本次驗證

- focused evidence/admission/Crown regression：18 passed
- `analysis` full suite：233 passed（138.632s）
- `system` full suite：207 passed，10 skipped（13.709s）
- `crown` full suite：202 passed，11 skipped（1.755s）
- Python compile、兩個 JS syntax、shell syntax、`git diff --check`：passed

新增覆蓋：完整 native raw row 四層可用、missing axis/provenance exclusion、post-hoc/push exclusion、source boundary、artifact malformed/stale、as-of cutoff、entry hash、rerun probability immutability、deterministic generation、accuracy atomic publication。

## 實際資料 coverage 結論

此隔離 worktree **沒有** `system/accuracy_history.json`，所以未能以本機現存 raw history 實測得到接受行數；這不是把缺失當 0。測試證實：只要 raw `market_grades` 有 `quote_source/source`、`observed_at`、`odds`、side、line、native stage timestamp、kickoff、`GRADED` outcome，下一次正式 `accuracy.py`／settle 後 accuracy update 即會產生至少 market-prior（通常亦會有 relaxed-line/no-league/exact 的相應 path）evidence。

如果既有 production history 缺 `quote_source/source`、`observed_at`、`odds` 或原生 `T-5` stage timestamp，dashboard 會以 aggregate exclusion reason 指出，這些舊行保持 unavailable；未來新 T-5 已由 persisted prediction/grade 路徑保留上述欄位，會開始被收集。部署後應先讓下一次 accuracy pass 生成 artifact，再由之後的首次原生 T-5 admission 使用；不應回填舊不完整行。

---

# Cohort correctness review fix（2026-08-19）

## 修正

1. Evidence path generation 現已完全對齊 `granular_conditions._paths()` 的 combinations semantics，並只保留 terminal 為 T-5 的 path。當首預、T-30、T-5 齊全時，會產生：`T-5`、`首預→T-5`、`T-30→T-5`、`首預→T-30→T-5`；不再只取 suffix。
2. Evidence schema 已升為 **v2**。每個 entry 新增非可逆 `fixture_market_key = hash(system, match_id, market)`；`evidence_id` 亦包含該 identity，避免不同賽事在 axes/timestamp 相同時碰撞。
3. `hierarchical_estimate` 的每一層 cohort 一律按 `fixture_market_key` 去重，所以 raw n/hits/Wilson/prior weight 都以 unique fixture-market 計，不會因多 path 或 duplicate publication 重複計數。
4. 同一 fixture-market 在同一 cohort 若出現不同 outcome 或任何 condition axes 不一致，整個 estimate 對該 cohort fail closed（`fixture_market_duplicate_or_conflict_*`），絕不挑其中一列。缺 fixture-market identity 同樣 fail closed。
5. Dashboard 沒有新增或顯示任何 fixture/provider identity；只有既有 aggregate coverage/diagnostics。

## 新增測試

- 三階段生成完整四個 canonical terminal paths。
- 同 fixture-market 的四個／重複 evidence rows 在每層 raw n 均只計 1。
- 不同 fixture-market 正常累計。
- 同 key outcome conflict、axis conflict 均 unavailable。
- deterministic artifact/hash/idempotency 繼續覆蓋。

## 最終驗證（本輪）

- focused cohort/evidence tests：13 passed
- `analysis` full suite：235 passed（120.559s）
- `system` full suite：207 passed，10 skipped（17.446s）
- `crown` full suite：202 passed，11 skipped（1.902s）
- Python compile、兩個 JS syntax、shell syntax、`git diff --check`：passed

---

# Hierarchy semantics review fix（2026-08-19）

`analysis/probability_research.py` 現按固定 retained axes 實作：

| 層級 | 保留 axes |
|---|---|
| exact | system, stage, market, path, odds tier, direction, role, line bucket, league（只有 candidate 有 league 才約束） |
| no_league | system, stage, market, path, odds tier, direction, role, line bucket |
| relaxed_line | system, stage, market, path, odds tier, direction, role |
| market_prior | system, stage, market, direction, odds tier |

因此 `market_prior` 會真正放寬 path、role、line bucket、league；相反 direction、另一 system/stage/market/tier 永遠不會混合。每層仍是 broad-to-narrow：market_prior → relaxed_line → no_league → exact，且 raw n 均按 unique fixture-market。

同一 `fixture_market_key` 的 conflict signature 現只比較該層 retained axes + outcome：被該層有意放寬的 path/role/line/league 差異會合法去重，不再誤作 conflict；outcome 或 retained axis 相衝才 fail closed。

新增測試覆蓋：同 fixture 多 path 的 market_prior n=1、market prior broad merging 和 direction/tier/market isolation、relaxed-line/no-league/exact 的各自 retained axes，以及 all-relevant-cohort outcome conflict。

## 最終驗證（本輪）

- focused hierarchy/evidence tests：14 passed
- `analysis` full suite：236 passed（134.444s）
- `system` full suite：207 passed，10 skipped（12.980s）
- `crown` full suite：202 passed，11 skipped（1.859s）
- Python compile、兩個 JS syntax、shell syntax、`git diff --check`：passed
