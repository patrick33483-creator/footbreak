# Stage Engine v2 — EV／Conviction 門檻設計（Cutover 前條件）

## 背景

Real snapshot dry-run 揭示：

- crown data.json 內全部 market_predictions 都係 `no_vig` 處理過嘅 fair probability，所以 `prob * odds - 1` 幾乎必定 ≤ 0（book margin）
- **crown pipeline 揀 lead 用嘅係 `conviction` 分數（0-100）**，唔係 EV
- 今晚 259 場 conviction 全部 50-58（lull mode），舊系統呢一刻應該 pick=None、Telegram 唔會發
- v2 揀「最高 EV」呢個策略錯咗方向——用 conviction 先啱

## 修正後策略

### Layer 1：Lead selection（predictor.py 內）

**現時：** 揀 EV 最高一行。
**修正：** 揀「pick」欄位（如果有），否則 fallback 到揀 conviction 最高嘅 forecast row。冇任何 pick／forecast 時 return None。

crown data.json 每場都有 `conviction` (0-100) 同 `forecast` (含中文 label 及 probability)——用呢兩樣。

### Layer 2：Publish decision（新加 `publisher.py`）

門檻用 **conviction** 而非 EV：

| Stage | min_conviction | 備註 |
|-------|----------------|------|
| 首預  | 60             | 提早通知寬鬆啲 |
| T-30  | 65             | 收斂中，中等 |
| T-5   | 70             | 最後一步嚴格啲 |

- 冇 pick／forecast 一律唔發
- 保留 EV 作為 secondary check：如果 `pick` 提供 odds／prob，補做 EV≥0.05 驗證

### Layer 3：Rate limiting（telegram.py 內）

- 現有 JSONL append log dedupe：**保留**
- 每分鐘上限 6 條：**保留設計**

## Shadow 期評估指標

Shadow 一週後睇：

- **記錄率**（ledger 全部 stage 齊備率）——目標 >95%
- **推播率**（過門檻 / 總 stage）——按 stage 分：今晚 lull 期預期 0-5%；正常會有 10-30%
- **重複／浪費率**——應該 0
- **Backtest 命中率**——目標 T-5 >55%

## Cutover 條件

1. Shadow 跑滿 7 日連續無 crash、無 tmp file residue
2. 記錄率 >95%
3. 推播率／命中率符合上表數字（要真實 sample size ≥ 100 場）

## Cutover 步驟

1. 舊 crown-tick.service 停用（保留 unit file 一個月）
2. 舊 Telegram bot token 保留但暫停
3. 開啟 `STAGE_V2_TELEGRAM_ENABLED=1`
4. 舊 dashboard 保留 30 日
5. 有問題可以 30 秒內 revert

## 不做嘅嘢

- 唔動 crown pipeline 內部——v2 只讀 output
- 唔加 preempt／lock-race／dedup lock
- 唔改 predictor lead selection 之後——lock 死喺 conviction+pick 為主
