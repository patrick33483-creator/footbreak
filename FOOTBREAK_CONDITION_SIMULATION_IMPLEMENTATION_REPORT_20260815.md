# Footbreak 條件模擬倉重建 — 實作與驗證報告

日期：2026-08-15（本機工作樹）  
分支：`footbreak-condition-simulation-20260815`  
範圍：只作本機程式修改與驗證；沒有提交、推送、部署、連接生產環境、發送 Telegram 或執行重設。

## 已實作的設計

### 單一條件模擬倉

- 以 `footbreak_condition_simulation` / `granular-condition-v1` 作為唯一活躍模擬倉。
- 起始本金固定為 HK$50,000；每注固定 HK$1,000。
- 僅在**首次保存**的 T-5 階段評估。T-30、同階段重跑、歷史回填及賽後寫入只會更新預測證據，永不建立或變更模擬注。
- 歷史樣本只讀取已結算的 Footbreak 準繩度歷史，排除現時同一場賽事。合資格門檻為命中率**嚴格大於 60%**及最少 **10** 個已判定樣本。
- 同場可對讓球、入球大細、角球大細分別建立一注；每一市場只可有一個方向。條件指向的方向或盤口不一致即拒絕建立。
- 建立前必須檢查：唯一市場選擇、有效方向、有限盤口、實際賠率大於 1，以及賠率觀測時間可證明早於開賽時間。
- 注單 ID 為 `fixture|market|T-5|strategy`，因此每場／市場／T-5／策略具冪等性。
- 保留亞洲盤結算結果：`Won`、`Half Won`、`Refunded`、`Half Lost`、`Lost`。

### 停用影子倉及資料邊界

- 已移除 Footbreak 影子倉的建立、結算、統計、Dashboard 導覽／檢視／動作與公開 payload。
- 舊有帳本資料在受保護重設前會留在本機帳本中，但不會被活躍流程讀取、結算或公開；Dashboard 另設防禦性過濾，只顯示新條件模擬倉注單。
- 條件研究報告仍是唯讀研究資料，不會建立注單或影響活躍模擬倉。
- `condition_simulation_audit` 上限為 1,600 筆；帳本 log 上限為 100 筆；通知狀態中的陣列上限為 1,600 筆；公開 log 最多 30 筆。

### Dashboard 與通知

- Dashboard 只保留「模擬倉」，並顯示建立規則、起始本金、固定注碼及中文多市場注單表格。
- 注單表列出開賽、聯賽、主客隊、市場、方向、盤口、賠率、歷史命中率／樣本、注碼、狀態、結果及盈虧；舊注及影子注不會顯示。
- CSS 更新為小螢幕可橫向閱讀的表格及規則卡排列。
- 新模擬注通知只會在帳本成功保存後觸發；訊息採繁體中文，列出聯賽、主客隊、開賽、中文市場、方向、盤口、賠率及歷史命中率／樣本。
- 缺少聯賽、隊名、開賽時間、方向、盤口、賠率或有效條件證據時，通知與建倉皆會失敗關閉。內部市場代碼及路徑代碼不會顯示給使用者。
- 沒有改動獨立 Odds Radar 的真新模擬注提示；沒有改動 Crown 的執行邏輯。

### 受保護的一次性重設

- 新增 `system/reset_condition_simulation.py`：只清除 Footbreak 舊主模擬注、統計、已退役影子狀態及條件審計；保留預測、歷史、學習、賽果與供應商資料。
- 重設會以原子寫入把本金設回 HK$50,000、重新產生 Dashboard，且只輸出聚合結果。
- 新增手動 GitHub Actions workflow：`.github/workflows/reset-footbreak-condition-simulation.yml`。它要求兩個明確確認值，並在遠端重設期間暫停相關 timer、完成後自動重新啟動。
- 本次**沒有執行**該重設。

## 變更檔案

- `.github/workflows/reset-footbreak-condition-simulation.yml`（新增）
- `hkjc-dashboard/app.js`
- `hkjc-dashboard/index.html`
- `hkjc-dashboard/styles.css`
- `system/condition_portfolio.py`（新增）
- `system/gen_app_data.py`
- `system/notify.py`
- `system/record_picks.py`
- `system/reset_condition_simulation.py`（新增）
- `system/run_predict.py`
- `system/settle.py`
- `system/tests/test_dashboard_api.py`
- `system/tests/test_data_health_dashboard_ui.py`
- `system/tests/test_granular_condition_notifications.py`
- `system/tests/test_reset_condition_simulation.py`（新增）
- `system/tests/test_results.py`
- `system/tests/test_shadow_portfolio.py`

## 驗證結果

| 檢查 | 結果 |
| --- | --- |
| 針對性條件模擬／重設／通知／Dashboard／結算回歸 | 29 passed |
| Footbreak 系統完整回歸 | 176 passed，10 skipped |
| 皇冠完整回歸 | 140 passed，11 skipped |
| 分析完整回歸 | 217 passed |
| 修改 Python AST、YAML、JavaScript 語法及 `git diff --check` | passed |
| 所有 workflow `run` 命令區塊 shell 語法 | passed |
| repository shell scripts 語法 | passed |

測試輸出中的資料健康與 Telegram transport 失敗文字由既有故障模擬測試刻意產生，對應測試整體仍通過；沒有發送實際 Telegram。

## 重設確認值

主要精確確認字串：

```text
RESET_FOOTBREAK_CONDITION_SIMULATION_50000
```

另外必須提供部署後確認字串：

```text
FOOTBREAK_CONDITION_SIMULATION_DEPLOYED
```

## 風險／操作注意

1. 重設前，舊主倉及影子倉原始資料會仍在帳本內以便可逆檢查；活躍程式與公開 Dashboard 已防禦性排除它們。部署成功並核實後，才可透過上述受保護 workflow 清除。
2. 新建倉刻意要求可證明賠率在開賽前觀測；上游若未提供 `observed_at` 或開賽時間，系統會跳過而非猜測。
3. 因為不回補歷史或重跑注單，重設後必須等待真正新保存的 T-5 預測才會出現第一筆新模擬注；這是為避免回測式建倉的既定安全限制。
