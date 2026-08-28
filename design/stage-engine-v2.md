# Stage Engine v2 設計

**目標：**重寫首預／T-30／T-5 三個時點嘅觸發與預測寫入。**唔動任何現有系統。**
歷史數據 100% 保留。舊系統繼續跑，新系統 shadow 對比一段時間先切換。

---

## 一、絕對唔動嘅嘢

### 保留原封嘅檔案／服務

- `/opt/footbreak/system/sim_ledger.json` — **read-only** by v2
- `/opt/footbreak/system/record_picks.py` — footbreak 賽事預測，v2 唔 import
- `/opt/footbreak/analysis/wilson_validation.py` — Wilson 驗證，v2 唔 import
- `/opt/footbreak/crown/*` — 舊 crown code 全部保留
- `crown-tick.service`, `crown-sweep.service`, `crown-settle.service`, `crown-first-look-reconcile.service`, `crown-early-admission-reconcile.service`, `crown-round-update.service`, `crown-reverse-t5-drain.service`, `crown-dashboard-api.service` — **繼續運作**
- `/var/www/crown/data.json` — 舊 crown dashboard，唔改
- `/var/www/footbreak/data.json` — 舊 footbreak dashboard，唔改
- Telegram bot（現有）繼續發舊系統嘅通知

### 保留原封嘅 ledger keys

`bankroll`, `bets`, `condition_simulation_audit`, `footbreak_crown_execution_test`, `footbreak_probability_research`, `log`, `stats`, `watch`, `wilson_validation`, `native_post_commit_jobs`, `native_stage_attempts`, `hkjc_execution_test`, `hourly_first_look_reconciliation_incidents`, `independent_validation`

v2 **一個字都唔寫入 sim_ledger.json**。

---

## 二、v2 加咩

### 新目錄

```
/opt/footbreak/stage_engine_v2/          # 全新 Python package
  __init__.py
  fixtures.py                             # 讀 HKJC + Pinnapi + Titan，統一 fixture schema
  scheduler.py                            # 計 T-30 / T-5 絕對時間
  predictor.py                            # 三時點預測（可直接 reuse 舊 model 只 import 唔改）
  writer.py                               # 寫入 v2 ledger
  telegram.py                             # 發 v2 頻道通知
  cli.py                                  # entry: python -m stage_engine_v2 <cmd>

/var/lib/footbreak/stage_engine_v2/       # 全新 state 目錄
  ledger.json                             # v2 自己嘅 ledger
  fixtures_cache.json                     # 已抓 fixture 緩存
  telegram_sent.jsonl                     # 已發通知 idempotency log

/var/www/stage_engine_v2/                 # 全新 dashboard 目錄
  data.json                               # v2 儀表板數據
  index.html                              # 唔同 URL 睇 v2 結果，唔覆蓋舊 dashboard
```

### 新 systemd 單元（獨立，唔動舊 crown-tick）

```
stage-engine-v2-tick.service     # 每 30 秒
stage-engine-v2-tick.timer       # OnBootSec=30s, OnUnitActiveSec=30s
```

**只有一個 timer。**無 sweep / preempt / drain / reconcile / self-heal 呢啲層級。

---

## 三、v2 核心邏輯（~500 行）

### 3.1 Fixture source of truth

```python
# fixtures.py
def refresh_fixtures(window_hours: int = 48) -> list[Fixture]:
    """從 HKJC + Pinnapi + Titan 抓未來 48 小時比賽。"""
    # 讀舊 crown 已經 cache 好嘅嘢：/var/www/crown/data.json ("matches" key)
    # 唔重新 fetch，直接用舊嘅 canonical fixture list
    # 呢個係 read-only reuse，避免重複 API cost 同 rate limit
    fixtures = read_matches_from_crown_dashboard()
    # 加上 HKJC 本身嘅 next-24h feed（若可直取）作 double-check
    return dedupe_by_native_id(fixtures)


@dataclass
class Fixture:
    id: str                    # canonical id (native_fixture_id 或 titan_match_id)
    league: str
    home: str
    away: str
    kickoff_utc: datetime      # 唯一權威時間
    kickoff_hkt: datetime
    source: str                # "hkjc" / "pinnapi" / "titan"
```

**規則：**`kickoff_utc` 係唯一權威。所有比較用 UTC。任何 fixture 無 kickoff_utc 就跳過（唔會靜默出錯）。

### 3.2 Scheduler

```python
# scheduler.py
STAGES = {
    "首預": None,      # 唔係時點觸發：只要 fixture 出現就 fire 一次
    "T-30": 30 * 60,   # 開賽前 30 分鐘
    "T-5":  5 * 60,    # 開賽前 5 分鐘
}

def due_stages(fixture: Fixture, now_utc: datetime, done: set[str]) -> list[str]:
    """返回而家該 fire 邊啲 stage。"""
    due = []
    if "首預" not in done:
        due.append("首預")
    for stage, seconds_before in STAGES.items():
        if stage == "首預" or stage in done:
            continue
        fire_at = fixture.kickoff_utc - timedelta(seconds=seconds_before)
        # 窗口：due 時間之後 5 分鐘內容許補跑
        if fire_at <= now_utc <= fixture.kickoff_utc:
            due.append(stage)
    return due
```

**明確窗口：**
- 首預：fixture 出現後 asap，只跑一次
- T-30：kickoff 前 30 分開始，直至 kickoff 前補跑
- T-5：kickoff 前 5 分開始，直至 kickoff 前補跑
- **開賽後永不 fire**（避免舊系統嗰啲事後追跑）

### 3.3 Predictor

```python
# predictor.py
def predict(fixture: Fixture, stage: str) -> Prediction:
    """呢度 reuse 舊 model，但只 import，唔改。"""
    # Option A（推薦）：直接讀舊 crown/data.json "matches" 入面對應
    #   fixture 嘅最新 stage 預測——舊系統本身 predict 得到，只係唔 fire。
    #   v2 只做時機 + 通知 + v2 儀表板。
    # Option B：獨立 predict——需要 import crown.engine 部份 function。
    ...


@dataclass
class Prediction:
    fixture_id: str
    stage: str                 # "首預" / "T-30" / "T-5"
    predicted_at_utc: datetime
    lead_market: str           # e.g. "入球大小"
    lead_label: str            # e.g. "大 2.5"
    lead_odds: float
    lead_prob: float
    lead_ev: float
    conviction: float
    raw_snapshot: dict         # 完整原始預測，用嚟事後 audit
```

**首選 Option A**：v2 只做「時機管理 + 通知 + 儀表板」，唔重寫預測模型。
呢個係最重要嘅設計決定——重寫預測會引入新錯誤，時機管理先係 bug 所在。

### 3.4 Writer（v2 ledger）

```python
# writer.py
# v2 ledger schema (完全新 file，唔碰舊 sim_ledger.json)
{
  "schema_version": 1,
  "fixtures": {
    "<fixture_id>": {
      "id": "...",
      "kickoff_utc": "...",
      "kickoff_hkt": "...",
      "league": "...",
      "home": "...",
      "away": "...",
      "source": "hkjc",
      "stages": {
        "首預": {"predicted_at_utc": "...", "lead_market": "...", ...},
        "T-30": {...},
        "T-5":  {...}
      }
    }
  }
}
```

**寫入語義：**
- 每個 stage 只寫一次（append-only，唔 overwrite）
- 用 `os.replace()` 原子寫入
- 有 file lock（`fcntl.flock`）避免 tick 撞

### 3.5 Telegram

```python
# telegram.py
def send_stage(prediction: Prediction) -> None:
    key = f"{prediction.fixture_id}:{prediction.stage}"
    if already_sent(key):
        return
    text = format_stage_message(prediction)
    post_to_telegram(text)
    mark_sent(key)  # append to telegram_sent.jsonl
```

**Idempotent：**同一場同一 stage 永不會發第二次。用 JSONL append log 做 dedupe。

### 3.6 Tick 主流程

```python
# cli.py
def tick() -> None:
    now = datetime.now(timezone.utc)
    fixtures = refresh_fixtures(window_hours=48)
    ledger = load_v2_ledger()
    for fx in fixtures:
        done = set(ledger["fixtures"].get(fx.id, {}).get("stages", {}).keys())
        for stage in due_stages(fx, now, done):
            pred = predict(fx, stage)
            if pred is None:
                continue  # 預測未 ready，下個 tick 再試
            write_stage(ledger, fx, stage, pred)
            send_stage(pred)
    save_v2_ledger(ledger)
    write_dashboard(ledger)  # /var/www/stage_engine_v2/data.json
```

**完成。呢個係全部。**無 preempt、無 deadline、無 defer、無 lock 爭。

---

## 四、Shadow 模式（第一週）

1. **v2 只讀不動：**唔發 Telegram，只寫入 v2 ledger + v2 dashboard
2. 每日對比：v2 fire 咗嘅 stage vs 舊系統 fire 咗嘅 stage
3. 差異報告：邊啲場舊系統 miss v2 抓到、邊啲場 v2 miss 舊系統抓到
4. 對比一週，v2 覆蓋率 ≥ 舊系統 才進入 cutover

**開關：**`/etc/footbreak-stage-v2.env`
```
STAGE_V2_TELEGRAM_ENABLED=0     # shadow 期間 0
STAGE_V2_DASHBOARD_ENABLED=1    # v2 儀表板可睇
```

---

## 五、Cutover（一週後）

1. 舊 crown-tick 繼續跑（保護網）
2. `STAGE_V2_TELEGRAM_ENABLED=1`——v2 開始發通知
3. 舊 crown Telegram bot **收窄到只發 settlement／結果**，唔再發 stage 通知
4. Dashboard 主頁改指 `/stage-v2/`（舊 dashboard 保留為 `/crown-legacy/`）
5. 觀察一週，穩定就 disable 舊 crown-tick timer（唔刪 code）

---

## 六、Rollback

任何一步都可以即時回退：

- Shadow 期：關 v2 timer 就 rollback，一秒
- Cutover 後：`STAGE_V2_TELEGRAM_ENABLED=0` + 重啟舊 crown-tick timer
- 最壞：`systemctl stop stage-engine-v2-tick.timer` + 舊系統原封繼續

---

## 七、時間預算

- **Day 1（今日）**：呢份設計 + 骨架 code（fixtures.py、scheduler.py、cli.py、writer.py）
- **Day 2**：predictor.py (Option A 只讀舊 crown data.json) + telegram.py + dashboard renderer
- **Day 3**：Shadow 模式部署，開始對比
- **Day 4-7**：Shadow 觀察與調整
- **Day 8**：Cutover

**唔會用「今晚搞掂」呢啲字。**

---

## 八、關鍵設計取捨

| 決定 | 原因 |
|------|------|
| Reuse 舊 predict，只重寫 scheduler | 預測模型工作嘅；bug 只係時機 |
| 全新 ledger，唔碰舊嘅 | 保護歷史數據，避免 schema migration |
| 全新 dashboard URL | 可以並行對比，唔影響現有觀察 |
| 30 秒 tick 而非 1 分鐘 | T-5 精度：舊系統成日錯過因為 1 分鐘窗口太窄 |
| 無 preempt 邏輯 | 每 tick 只做 O(n_fixtures) 工作，30 秒足夠 |
| Telegram idempotency 用 JSONL append log | 簡單、可審計、崩潰安全 |

---

## 九、你要決定嘅嘢

1. **Option A（reuse 舊 predict）還是 Option B（獨立 predict）？** 我建議 A。
2. **Shadow 對比期一週得唔得？** 或者你想更短？
3. **Cutover 後係咪連舊 crown-tick 都 disable？** 我建議保留一個月做保護網。
4. **v2 dashboard 用邊個 URL？** 例如 `crown.你網域/stage-v2/`。

---

如果你 OK 呢份設計，我而家就開始寫骨架 code（唔動 production）。骨架寫完會 push 到一個新 branch `stage-engine-v2`，唔會 merge 落 main，你可以慢慢睇。
