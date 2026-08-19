#!/usr/bin/env bash
# ═══════════════════════════════════════════════════════════════════
# 足破 · DigitalOcean 首次安裝腳本
# 喺一部全新 Ubuntu 22.04 / 24.04 droplet 上面用 root 執行一次:
#     bash deploy/setup.sh
# ═══════════════════════════════════════════════════════════════════
set -euo pipefail

APP_DIR="/opt/footbreak"
STATE_DIR="/var/lib/footbreak"
ENV_FILE="/etc/footbreak.env"
WEB_ROOT="/var/www/footbreak"
CROWN_STATE_DIR="/var/lib/footbreak/crown"
CROWN_ENV_FILE="/etc/footbreak-crown.env"
CROWN_WEB_ROOT="/var/www/crown"
BACKTEST_STATE_DIR="/var/lib/footbreak/backtest"
REPO_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"

sync_crown_web_root() {
  # nginx's www-data worker needs x on every directory and r on static files.
  # Crown state remains under /var/lib/footbreak/crown at mode 0600.
  install -d -o root -g www-data -m 0755 /var/www "$CROWN_WEB_ROOT"
  rsync -a --delete --exclude 'data.json' --exclude 'history.json' --chmod=Du=rwx,Dgo=rx,Fu=rw,Fgo=r \
    "$APP_DIR/crown/dashboard/" "$CROWN_WEB_ROOT/"
  chown -R root:www-data "$CROWN_WEB_ROOT"
  find "$CROWN_WEB_ROOT" -type d -exec chmod 0755 {} +
  find "$CROWN_WEB_ROOT" -type f -exec chmod 0644 {} +
}

echo "▸ 1/7 安裝系統套件"
export DEBIAN_FRONTEND=noninteractive
apt-get update -qq
apt-get install -y -qq python3 python3-pip python3-venv git nginx rsync tzdata curl apache2-utils icu-devtools

echo "▸ 2/7 設定時區為香港"
timedatectl set-timezone Asia/Hong_Kong || ln -sf /usr/share/zoneinfo/Asia/Hong_Kong /etc/localtime

echo "▸ 3/7 建立目錄"
mkdir -p "$APP_DIR" "$STATE_DIR" "$WEB_ROOT" "$CROWN_WEB_ROOT" /var/log/footbreak
# Private runtime state is never served by nginx and never made group-readable.
install -d -o root -g root -m 0700 "$CROWN_STATE_DIR"
install -d -o root -g root -m 0700 "$BACKTEST_STATE_DIR"
if [ "$REPO_DIR" != "$APP_DIR" ]; then
  rsync -a --delete --exclude '.git' "$REPO_DIR/" "$APP_DIR/"
fi

echo "▸ 4/7 安裝 Python 依賴"
python3 -m venv "$APP_DIR/.venv"
"$APP_DIR/.venv/bin/pip" install -q --upgrade pip
if [ -f "$APP_DIR/requirements.txt" ]; then
  "$APP_DIR/.venv/bin/pip" install -q -r "$APP_DIR/requirements.txt"
fi

echo "▸ 5/7 安裝 external-tool 相容層"
install -m 0755 "$APP_DIR/bin/external-tool" /usr/local/bin/external-tool

echo "▸ 6/7 建立環境變數檔"
if [ ! -f "$ENV_FILE" ]; then
  cp "$APP_DIR/deploy/footbreak.env.example" "$ENV_FILE"
  chmod 600 "$ENV_FILE"
  echo "  ⚠ 已建立 $ENV_FILE —— 請填 Telegram 設定；PinnAPI Edge 設定放入 $CROWN_ENV_FILE"
else
  echo "  已存在 $ENV_FILE,唔覆蓋"
fi

if [ ! -f "$CROWN_ENV_FILE" ]; then
  cp "$APP_DIR/deploy/footbreak-crown.env.example" "$CROWN_ENV_FILE"
  chmod 600 "$CROWN_ENV_FILE"
  echo "  已建立 $CROWN_ENV_FILE —— Crown 預設停用，唔會複製任何現有憑證"
else
  echo "  Crown 環境檔已存在,唔覆蓋"
fi

# 首次由 repo 嘅 state-seed 帶入模擬倉,之後永遠唔再覆蓋
echo "▸ 6.5/7 帶入模擬倉初始狀態(只做一次)"
for f in sim_ledger.json notify_state.json predictions.json accuracy.json hk_snapshots.json; do
  if [ -f "$APP_DIR/state-seed/$f" ] && [ ! -f "$APP_DIR/system/$f" ]; then
    cp "$APP_DIR/state-seed/$f" "$APP_DIR/system/$f"
    echo "  帶入 $f"
  fi
done
mkdir -p "$APP_DIR/system/cache"

# Crown 有完全獨立 state；seed 只帶入一次，更新或重裝都不覆蓋。
for f in ledger.json predictions.json notify_state.json; do
  if [ -f "$APP_DIR/crown/state-seed/$f" ] && [ ! -f "$CROWN_STATE_DIR/$f" ]; then
    install -m 0600 "$APP_DIR/crown/state-seed/$f" "$CROWN_STATE_DIR/$f"
    echo "  帶入 Crown $f"
  fi
done
find "$CROWN_STATE_DIR" -type f -exec chmod 0600 {} +

echo "▸ 7/7 安裝 systemd 單元同 nginx"
install -m 0644 "$APP_DIR"/deploy/systemd/*.service /etc/systemd/system/
install -m 0644 "$APP_DIR"/deploy/systemd/*.timer   /etc/systemd/system/
systemctl daemon-reload
systemctl enable --now crown-dashboard-api.service footbreak-dashboard-api.service
# 首次安裝只放好服務，憑證及手動驗證完成前絕不自動掃描。
systemctl disable --now footbreak-tick.timer footbreak-t30.timer footbreak-sweep.timer footbreak-settle.timer footbreak-result-reconcile.timer 2>/dev/null || true
# Crown 更要預設停用；升版亦只會保留目前的 enable/disable 狀態。
systemctl disable --now crown-tick.timer crown-sweep.timer crown-settle.timer 2>/dev/null || true
# 回測預設停用，需先成功建立基線再啟用。
systemctl disable --now footbreak-backtest.timer 2>/dev/null || true

install -m 0644 "$APP_DIR/deploy/nginx-footbreak.conf" /etc/nginx/sites-available/footbreak
ln -sf /etc/nginx/sites-available/footbreak /etc/nginx/sites-enabled/footbreak
install -m 0644 "$APP_DIR/deploy/nginx-crown.conf" /etc/nginx/sites-available/crown
ln -sf /etc/nginx/sites-available/crown /etc/nginx/sites-enabled/crown
rm -f /etc/nginx/sites-enabled/default
if [ ! -f /etc/nginx/.htpasswd-footbreak ]; then
  DASHBOARD_PASSWORD="$(openssl rand -base64 24 | tr -d '/+=' | head -c 24)"
  htpasswd -bc /etc/nginx/.htpasswd-footbreak footbreak "$DASHBOARD_PASSWORD"
  chown root:www-data /etc/nginx/.htpasswd-footbreak
  chmod 640 /etc/nginx/.htpasswd-footbreak
  printf '%s\n' "$DASHBOARD_PASSWORD" > /root/footbreak-dashboard-password.txt
  chmod 600 /root/footbreak-dashboard-password.txt
fi
if [ ! -f /etc/nginx/.htpasswd-crown ]; then
  CROWN_DASHBOARD_PASSWORD="$(openssl rand -base64 24 | tr -d '/+=' | head -c 24)"
  htpasswd -bc /etc/nginx/.htpasswd-crown crown "$CROWN_DASHBOARD_PASSWORD"
  chown root:www-data /etc/nginx/.htpasswd-crown
  chmod 640 /etc/nginx/.htpasswd-crown
  printf '%s\n' "$CROWN_DASHBOARD_PASSWORD" > /root/crown-dashboard-password.txt
  chmod 600 /root/crown-dashboard-password.txt
fi
rsync -a "$APP_DIR/hkjc-dashboard/" "$WEB_ROOT/"
sync_crown_web_root
# 建立安全的空白/現有 state 儀表板；這個指令完全不會呼叫網絡。
(cd "$APP_DIR" && CROWN_STATE_DIR="$CROWN_STATE_DIR" CROWN_WEB_ROOT="$CROWN_WEB_ROOT" \
  "$APP_DIR/.venv/bin/python3" -m crown.dashboard_data --out "$CROWN_WEB_ROOT/data.json")
# dashboard_data atomically replaces data.json; reassert static readability.
chown root:www-data "$CROWN_WEB_ROOT/data.json"
chmod 0644 "$CROWN_WEB_ROOT/data.json"
chown root:www-data "$CROWN_WEB_ROOT/history.json"
chmod 0644 "$CROWN_WEB_ROOT/history.json"
nginx -t
systemctl enable nginx
# Reload an existing nginx instance; on a first install reload is unavailable,
# so start it only after the configuration has passed nginx -t.
systemctl reload nginx || systemctl restart nginx

cat <<'EOF'

═══════════════════════════════════════════════════
✅ 安裝完成

仲要做:
  1. nano /etc/footbreak-crown.env ← 填 PinnAPI Edge 設定（唔會顯示或複製）
  2. nano /etc/footbreak.env       ← 填 TELEGRAM_BOT_TOKEN、TELEGRAM_CHAT_ID
  3. 手動試一次:  /opt/footbreak/deploy/run.sh tick
  4. 驗證成功後:  systemctl enable --now footbreak-tick.timer footbreak-sweep.timer footbreak-settle.timer footbreak-result-reconcile.timer
  5. 睇 log:      journalctl -u footbreak-tick -f

儀表板:  http://<你嘅-droplet-IP>:8081/
登入名稱: footbreak
密碼位置: /root/footbreak-dashboard-password.txt
排程狀態: systemctl list-timers 'footbreak*'
皇冠儀表板: http://<droplet-IP>:8082/ （帳號 crown）
皇冠密碼: /root/crown-dashboard-password.txt
皇冠預設停用。先跑:
  /opt/footbreak/.venv/bin/python -m unittest discover -s /opt/footbreak/crown/tests -t /opt/footbreak
  /opt/footbreak/.venv/bin/python -m crown.run tick --dry-run
確認 PinnAPI、配對、資料時效及模擬注後才可:
  systemctl enable --now crown-tick.timer crown-sweep.timer crown-settle.timer
═══════════════════════════════════════════════════
EOF
