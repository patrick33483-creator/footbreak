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
REPO_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"

echo "▸ 1/7 安裝系統套件"
export DEBIAN_FRONTEND=noninteractive
apt-get update -qq
apt-get install -y -qq python3 python3-pip python3-venv git nginx rsync tzdata curl

echo "▸ 2/7 設定時區為香港"
timedatectl set-timezone Asia/Hong_Kong || ln -sf /usr/share/zoneinfo/Asia/Hong_Kong /etc/localtime

echo "▸ 3/7 建立目錄"
mkdir -p "$APP_DIR" "$STATE_DIR" "$WEB_ROOT" /var/log/footbreak
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
  echo "  ⚠ 已建立 $ENV_FILE —— 請即刻編輯,填入 OPTICODDS_API_KEY 同 TELEGRAM_BOT_TOKEN"
else
  echo "  已存在 $ENV_FILE,唔覆蓋"
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

echo "▸ 7/7 安裝 systemd 單元同 nginx"
install -m 0644 "$APP_DIR"/deploy/systemd/*.service /etc/systemd/system/
install -m 0644 "$APP_DIR"/deploy/systemd/*.timer   /etc/systemd/system/
systemctl daemon-reload
systemctl enable --now footbreak-tick.timer footbreak-sweep.timer

install -m 0644 "$APP_DIR/deploy/nginx-footbreak.conf" /etc/nginx/sites-available/footbreak
ln -sf /etc/nginx/sites-available/footbreak /etc/nginx/sites-enabled/footbreak
rm -f /etc/nginx/sites-enabled/default
rsync -a "$APP_DIR/hkjc-dashboard/" "$WEB_ROOT/"
nginx -t && systemctl reload nginx

cat <<'EOF'

═══════════════════════════════════════════════════
✅ 安裝完成

仲要做:
  1. nano /etc/footbreak.env   ← 填 OPTICODDS_API_KEY 同 TELEGRAM_BOT_TOKEN
  2. systemctl restart footbreak-tick.timer
  3. 手動試一次:  /opt/footbreak/deploy/run.sh tick
  4. 睇 log:      journalctl -u footbreak-tick -f

儀表板:  http://<你嘅-droplet-IP>/
排程狀態: systemctl list-timers 'footbreak*'
═══════════════════════════════════════════════════
EOF
