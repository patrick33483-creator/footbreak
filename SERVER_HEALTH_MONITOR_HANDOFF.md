# Footbreak / Crown 伺服器健康監察 handoff

## 目的與範圍

新增 `footbreak-server-health-monitor.timer`：DigitalOcean 主機本機每 30 分鐘
執行一次 `system/server_health_monitor.py`。它只讀取本機 JSON、dashboard sidecar
和 `systemctl show`，**不會**執行 provider 查詢、Prediction / settlement runner、
`deploy/health-check.sh`、Radar 或任何 Computer / Perplexity 排程。

Footbreak 和 Crown 會分別評估；Crown 即使停用驗證 gate 也只檢查其本機 artifact
及 service 狀態，不會因本監察器而啟動 Crown runner。

## 已部署內容

| 檔案 | 用途 |
|---|---|
| `system/server_health_monitor.py` | 純本機、read-only 評估器 |
| `deploy/systemd/footbreak-server-health-monitor.service` | root oneshot、20 秒上限、`UMask=0077` |
| `deploy/systemd/footbreak-server-health-monitor.timer` | `OnBootSec=7min`、`OnUnitActiveSec=30min`、`Persistent=true` |
| `system/incident_alert.py` | 擴充 incident 類別與 cooldown；重用原有 private atomic state/lock 和分系統 Telegram transport |

`deploy/setup.sh` 現在會啟動這個不接觸 provider 的 timer；`deploy/update.sh`
會 enable/restart 並驗證它 active；既有 `deploy/health-check.sh` 亦要求 timer
active 和 enabled。

## 檢查規則

每次 run 會各自檢查：

1. **預期 native stages**：在過去 30 分鐘曾到期的已知 fixture，T-30 / T-5
   是否真的存在持久化 stage。發現 fixture 晚於其到期窗才寫入不算漏 stage。
2. **Wilson / Telegram**：不會把「沒有 TG 訊息」視為正常。它只把已建立、仍未
   開賽、超過 12 分鐘且未於 `wilson_match_alerts` acknowledged 的 Wilson bet 或
   `NO_BET_LOW_ODDS` observation 當作 stuck notification；如未來 state 有明示
   `outbox` / `notification_outbox` 的 aged `PENDING`、`FAILED`、`RETRY` row 亦會
   納入。純 sample gate rejection、無 Wilson candidate、或沒有符合條件的賽事會靜默。
3. **dashboard/history**：檢查既有 `history_data_url` sibling、schema、version、
   `prediction_history.rows` 合約；不重建 dashboard。
4. **settlement backlog**：`PENDING` 模擬 bet 的 kickoff 超過
   `SERVER_HEALTH_SETTLEMENT_GRACE_SECONDS`（預設 4 小時）才告警。
5. **local service health**：只讀 systemd `Result` / `ExecMainStatus` /
   `ActiveState`。`Result=timeout` 需連續兩輪才是 `repeated_timeout`；
   非 0、非 75 的 local service result 是 `health_check_failure`。特別地，
   `footbreak-result-reconcile.service` status=1 是 health signal，但**絕不**
   單獨推論 T-30/T-5 漏失。

## 告警與狀態

告警由既有 `IncidentAlerts` 發送，故 Footbreak 使用 `TELEGRAM_*`，Crown 使用
既有 `CROWN_TELEGRAM_*` / `CROWN_TELEGRAM_ENABLED`。訊息只包含簡短繁中 incident
類別和 aggregate count，不會寫入 fixture ID、provider payload 或憑證。

狀態仍在既有私有 `/var/lib/footbreak/incident-alerts.json`（可用
`INCIDENT_ALERT_STATE_PATH` 覆寫），以 0600 atomic replace 和 `flock` lock 更新，
incidents / audit 均有既有上限。每個 active incident 只發一次；健康兩輪後才發
一次恢復。`SERVER_HEALTH_ALERT_COOLDOWN_SECONDS` 預設 6 小時，短暫 recovery /
reopen 不會重複發送；若 incident 持續超過 cooldown 才會補發一次。

## 運維指令

```bash
systemctl status footbreak-server-health-monitor.timer
systemctl list-timers footbreak-server-health-monitor.timer
journalctl -u footbreak-server-health-monitor.service --since '-2 hours' --no-pager
systemctl start footbreak-server-health-monitor.service
```

可調整但不應把值設得過短的環境項目：

```ini
SERVER_HEALTH_SETTLEMENT_GRACE_SECONDS=14400
SERVER_HEALTH_ALERT_COOLDOWN_SECONDS=21600
```

## 本地驗證

已通過：

```bash
python3 -m unittest system.tests.test_server_health_monitor system.tests.test_incident_alert -v
python3 -m unittest discover -s system/tests -t .
python3 -m compileall -q system crown analysis bin
bash -n deploy/setup.sh deploy/update.sh deploy/health-check.sh
systemd-analyze verify  # 以本機 Python path 替代 production venv path
git diff --check
```

`crown/tests` 全量 suite 在此 checkout 的 `crown.tests.test_challenger_v2`
有四個失敗（candidate lane fixture expectation；本次未修改 `crown/` 或該測試）。
其餘本次相關 Footbreak / incident regression 均通過；部署前可先由 Crown owner
處理該既有 suite 問題，再要求完整 green gate。

未有 push、deploy、provider access 或真實 Telegram 發送。
