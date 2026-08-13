# 預測統計根因及核對記錄（2026-08-13）

## 已證明的 361 vs 398 根因

這次差異是 **model-era / scope 不一致**，不是同步遺漏的推測：

1. Footbreak 的市場學習快照由 `system/record_picks.py` 以
   `model_version="2026-08-10-market-learning-v2"` 寫入 immutable learning DB。
2. 原 Dashboard 建表流程只取該 era 的 live snapshot，且 `system/accuracy.py`
   同樣以該 era 建構其 learning rows；因此 Dashboard 市場分數是 current-era
   scorecard。
3. 原 `analysis/data_health.py` 的 `snapshot_rows(system)` 和
   `grade_rows(system)` 只過濾 `system` + `pre_kickoff=1`，沒有
   `model_version` 條件。因此它把 current era 及舊 era 一起計入 learning DB。
4. 所以已觀察到的差為 `398 - 361 = 37`，其程式意義是 data-health 的
   all-era 額外 graded rows；新程式把這 37（以及任何其他歷史 era）保留於
   `all_history_audit`，不再混入可比較的當前統計。

本地環境沒有 production learning DB 或公開 artifact，故未讀取 production，
亦不聲稱已逐筆識別那 37 行；部署後按下列「生產核對」以只讀/既有本地
rebuild 驗證其實際 model-version 分布。

## 實作口徑

* `analysis/market_statistics.py` 是 Footbreak/Crown 唯一市場統計契約。
* 每個 `by_market`、`by_stage_market`、`market_overall` 均有：
  * 主層：selected odds `>= 1.70`；
  * `odds_groups.at_or_above_1_70`、`below_1_70`、`missing`；
  * `all_odds` 完整稽核合計。
* `odds=1.70` 屬主層；`1.699` 屬低賠；None、NaN、Infinity、非正 decimal odds
  屬 missing。三組互斥，合計等於 `all_odds`。
* `grade_status != GRADED` 不入市場統計；GRADED push（`hit is None`）保留在
  所屬 odds group，但不入 `decided` 分母；未結算不會被當作輸。
* Dashboard 頂部 WDL 指標改用 `wdl_graded`、`wdl_hits`、`wdl_accuracy`，畫面
  名稱為「1X2 已評分／1X2 命中／1X2 命中率」。
* 三階段最高命中條件只用已結算、T-5 selected odds `>=1.70`；樣本
  `>=30` 優先，再按命中率、已判定樣本排序。低賠及 missing 只在 odds audit
  顯示。
* Dashboard current scorecard 與 data-health 均依 system 固定 current
  `model_version` 篩選；全歷史仍保留在 rows / `all_history_audit`，不會刪除或
  標成當前模型。

## 生產部署後的安全重算與核對（不要在本次工作中執行）

1. 以既有正常更新流程部署程式；不需要 migration，也不能刪除 learning DB。
2. 在已部署主機先執行既有 `deploy/reconcile-results.sh` 一次。該流程先完成
   Footbreak/Crown settlement、既有 learning duplicate reconciliation，之後以
   `analysis.data_health` 重生兩份 aggregate artifact。
3. 分別重生/發布 Footbreak 與 Crown dashboard data（沿用既有 runner）；不要把
   `all_history_audit` 當主 KPI。
4. 比較同一個 system 的：
   * dashboard `prediction_history.stats.scope.model_version`；
   * data-health `scope.model_version`；
   * dashboard `prediction_history.stats.market_overall.all_odds.graded`；
   * data-health `baseline.graded_rows`；
   * data-health `completeness.overall.all_history_audit.graded_rows`。
5. 前兩個 model version 必須相同；前兩個 current-scope graded 總數必須一致。
   all-history audit 大於 current-scope 屬預期，並應量化為各舊 era 的歷史數，
   而非改寫任何原始 row。
6. 若 current-scope totals 仍不一致，才以 immutable snapshot/grade 的
   `model_version`、canonical snapshot、grade revision 作只讀逐筆 reconciliation；
   不應以重跑模型或刪歷史資料修正。
