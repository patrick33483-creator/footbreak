# 足破 · Footbreak

HKJC 香港賽馬會足球賽事三階段預測系統 —— 自主 host 版。

原本喺 Perplexity Computer 上面跑,每個 T-30 / T-5 時點都要叫醒一個 agent,一晚二十幾次。
搬咗上 DigitalOcean 之後,systemd timer 每 2 分鐘跑一次係零成本,而且順帶解決咗平台
「一個對話最多 15 個排程」同「排程自我補位」呢啲限制。

---

## 架構

```
┌─────────────┐   push    ┌──────────┐  SSH   ┌────────────────────────┐
│ 本機 / PPLX │ ────────▶ │  GitHub  │ ─────▶ │  DigitalOcean droplet  │
└─────────────┘           │  Actions │        │                        │
                          └──────────┘        │  systemd timer         │
                            語法檢查           │   ├ tick   每 2 分鐘   │
                                              │   └ sweep  每晚 23:59  │
                                              │  nginx → 儀表板        │
                                              └───────────┬────────────┘
                                                          │
                                     ┌────────────────────┼────────────────────┐
                                     ▼                    ▼                    ▼
                                HKJC GraphQL         PinnAPI Edge         Telegram Bot
                                 (賽事 / 盤口)      (銳利盤基準)             (通知)
```

### 三階段流程

| 階段 | 時點 | 做咩 | 會唔會落注 |
|---|---|---|---|
| 首預 | 每晚 23:59 全板掃描 | 建立基準預測 | ❌ |
| T-30 | 開賽前 20–40 分鐘 | 更新預測,記錄變化 | ❌ |
| T-5 | 開賽前 1–10 分鐘 | 尾盤賠率,最終決定 | ✅ **只有呢度** |

### 決策五關

一注要落,五關全部要過:

1. **信念 ≥ 58**(`CONF_FLOOR`)
2. **優勢 ≥ +2.0%**(`MIN_EDGE = 0.02`)
3. **注碼上限隨賠率遞減** —— `0.04 × min(1, b/0.80)`,短賠自動縮細
4. **收縮後仍然正 EV** —— `shrink = 0.35 + 0.65 × (信念/100)`
5. 分數凱利 —— `f = (pe·b − (1−pe)) / b`,再乘階段係數,角球再乘 0.5

本金 $50,000 HKD。市場:讓球 HDC、入球大小 HIL、總角球大小 CHL。

---

## 首次安裝

喺一部乾淨嘅 Ubuntu 22.04 / 24.04 droplet(最平嘅 $6/月 1GB 已經夠用):

```bash
# 1. 攞程式碼
sudo git clone https://github.com/<你嘅帳號>/footbreak.git /opt/footbreak
cd /opt/footbreak

# 2. 一鍵安裝(裝套件、設香港時區、安裝 systemd timer、開 nginx)
sudo bash deploy/setup.sh

# 3. 填 API key
sudo nano /etc/footbreak.env
#    PINNAPI_API_KEY=...  # normally in /etc/footbreak-crown.env
#    TELEGRAM_BOT_TOKEN=...
#    TELEGRAM_CHAT_ID=...

# 4. 手動試一次
sudo /opt/footbreak/deploy/run.sh tick
```

手動驗證成功後，先執行
`sudo systemctl enable --now footbreak-tick.timer footbreak-sweep.timer`。
儀表板喺 `http://<droplet-IP>:8081/`，登入名稱係 `footbreak`，
密碼保存於 `/root/footbreak-dashboard-password.txt`。

### 皇冠 / Crown（另一個隔離、預設停用的後端）

`crown/` 有自己嘅 `/var/lib/footbreak/crown` 狀態、`/etc/footbreak-crown.env`、
systemd lock、Telegram 狀態與 `http://<droplet-IP>:8082/` Basic Auth 儀表板（帳號
`crown`、密碼 `/root/crown-dashboard-password.txt`）。Crown 使用 PinnAPI Edge 作
sharp reference，Titan007 Crown company ID 3 只作 HDC/HIL 報價；所有缺資料、過期
資料、同場配對不唯一或盤口不完全相同的情況都會 fail closed。它只有模擬注，沒有
實際投注路徑。

安裝後兩個 Crown timer 都是 disabled，部署亦不會把 disabled timer 打開：

```bash
cd /opt/footbreak
.venv/bin/python -m unittest discover -s crown/tests -t .
.venv/bin/python -m crown.health
.venv/bin/python -m crown.run tick --dry-run
# 由操作員以 live validation 驗證後才執行：
sudo systemctl enable --now crown-tick.timer crown-sweep.timer
```

### 開通自動部署

喺 GitHub repo 嘅 **Settings → Secrets and variables → Actions** 加:

| Secret | 內容 |
|---|---|
| `DO_HOST` | droplet 嘅 IP |
| `DO_USER` | `root`(或者有 sudo 權限嘅 user) |
| `DO_SSH_KEY` | 私鑰全文(對應嘅公鑰要放咗入 droplet 嘅 `~/.ssh/authorized_keys`) |
| `DO_PORT` | 可選,預設 22 |

之後 push 落 `main` 就會自動:語法檢查 → SSH 入 droplet → `git reset --hard` → 重載 systemd 同 nginx。

---

## 常用指令

```bash
# 睇排程幾時跑
systemctl list-timers 'footbreak*'

# 睇 log
journalctl -u footbreak-tick -f
journalctl -u footbreak-sweep --since today

# 手動跑
sudo /opt/footbreak/deploy/run.sh tick     # 檢查有冇場到 T-30 / T-5
sudo /opt/footbreak/deploy/run.sh sweep    # 全板首預
sudo /opt/footbreak/deploy/run.sh settle   # 只結算

# 暫停 / 恢復落注
sudo systemctl stop footbreak-tick.timer
sudo systemctl start footbreak-tick.timer

# 睇模擬倉
python3 -c "import json;d=json.load(open('/opt/footbreak/system/sim_ledger.json'));print(d['bankroll'], len(d['bets']))"
```

---

## 目錄結構

```
bin/external-tool          Perplexity 連接器嘅相容層(直打 OpticOdds / Telegram)
system/                    預測系統本體
  run_all.sh               主流程 sweep / tick / settle
  hkjc_feed.py             HKJC GraphQL 抓賽事同盤口
  sharp.py                 OpticOdds 銳利盤(Pinnacle)
  model.py                 Dixon-Coles / 負二項 入球同角球模型
  predict.py               三階段預測邏輯
  staking.py               分數凱利注碼
  record_picks.py          寫入模擬倉
  settle.py                賽果結算
  notify.py                Telegram 通知(唯一發訊息嘅地方)
  accuracy.py              準繩度記分板
  gen_app_data.py          出前端 data.json
hkjc-dashboard/            儀表板前端(nginx 靜態)
deploy/                    setup.sh / update.sh / run.sh / systemd / nginx
crown/                     隔離皇冠模擬後端、PinnAPI/Titan/HKJC 相容層、測試、dashboard
state-seed/                首次安裝帶入嘅模擬倉初始狀態(之後永遠唔覆蓋)
.github/workflows/         GitHub Actions 自動部署
```

---

## 相容層點解可以做到零改碼

`settle.py`、部分歷史情境工具、`notify.py` 原本係咁叫平台連接器:

```python
subprocess.run(["external-tool", "call", json.dumps({...})])
```

`bin/external-tool` 係一個同名同介面嘅 Python 腳本,裝去 `/usr/local/bin/`,
收到同樣嘅 JSON 就直接打 OpticOdds REST v3 或者 Telegram Bot API,
回傳格式一樣係 `{"status_code": N, "data": <vendor JSON>}`。

`sharp.py` 而家直接使用 PinnAPI Edge，保留原本的 fixture / odds / structure
介面畀預測模型；PinnAPI failure 會令服務非零退出，唔會回退到舊 OpticOdds
快取或覆蓋 dashboard。

---

## 狀態檔點樣保護

以下檔案喺 `.gitignore` 入面,`git reset --hard` 唔會掂到,所以每次部署模擬倉都安全:

`sim_ledger.json` · `notify_state.json` · `predictions.json` · `accuracy.json` ·
`hk_snapshots.json` · `cache/` · `hkjc-dashboard/data.json`

首次安裝時 `setup.sh` 會由 `state-seed/` 帶入一份,之後就唔會再碰。

---

## 已知問題

| 問題 | 影響 | 狀態 |
|---|---|---|
| Telegram 4096 字上限 | T-5 觀望場多過 ~15 場時 `notify.py --watch` 會失敗 | 未修,要加分段 |
| `whatif_stakes.py` $200 下限不對稱 | 反事實注碼估算有偏差 | 未修 |
| `form.py` 壞咗 | 冇用到,唔影響主流程 | 已棄用 |
| 部分聯賽賽果資料損壞 | 日職、荷甲、挪超、英聯盃、Leagues Cup | 上游資料問題 |
| Footbreak 歷史開盤/賽果 | PinnAPI Edge 現時無 Optic-style 歷史盤或完整賽果端點 | 初盤改為本機首次 PinnAPI 觀測；已到結算門檻而無結果時服務會非零退出，不會靜默使用舊資料 |
| PinnAPI 角球 | 現有足球 full-match parser 無已驗證角球盤 | CHL 銳利擬合留空、fail closed；HDC/HIL/HAD 仍可擬合 |

---

## 模型本質(要老實講)

基礎機率係 Dixon-Coles / 負二項模型**擬合自銳利盤去水盤**,本質上係「盤口翻譯器」。
真正屬於獨立判斷嘅係調整層(賠率移動、天氣、休息日、陣容傷患)。
角球冇任何獨立資料源,100% 由盤口反推。

歷史觀察:信念 58–64 呢個區間嘅命中率反而係最差嘅 —— **高信念 ≠ 準**。
短賠危險,因為注碼 = EV/b,會把模型誤差放大近十倍。
