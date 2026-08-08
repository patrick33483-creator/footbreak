#!/usr/bin/env bash
# 足破 · 自動部署鉤子
# GitHub Actions 每次 push 到 main 就會 SSH 入嚟跑呢個。
# 只更新程式碼,絕對唔會掂模擬倉同任何狀態檔(佢哋喺 .gitignore 入面)。
set -euo pipefail

APP_DIR="/opt/footbreak"
WEB_ROOT="/var/www/footbreak"
BRANCH="${1:-main}"

cd "$APP_DIR"

echo "▸ 拉取最新程式碼($BRANCH)"
git fetch --quiet origin "$BRANCH"
BEFORE=$(git rev-parse HEAD)
git reset --hard --quiet "origin/$BRANCH"
AFTER=$(git rev-parse HEAD)

if [ "$BEFORE" = "$AFTER" ]; then
  echo "  已經係最新($AFTER),冇嘢要做"
else
  echo "  $BEFORE → $AFTER"
  git --no-pager log --oneline "$BEFORE..$AFTER" | head -20
fi

echo "▸ 同步 Python 依賴"
if [ -f requirements.txt ] && [ -x .venv/bin/pip ]; then
  .venv/bin/pip install -q -r requirements.txt
fi

echo "▸ 更新 external-tool 相容層"
install -m 0755 "$APP_DIR/bin/external-tool" /usr/local/bin/external-tool

echo "▸ 更新 systemd 單元"
install -m 0644 "$APP_DIR"/deploy/systemd/*.service /etc/systemd/system/
install -m 0644 "$APP_DIR"/deploy/systemd/*.timer   /etc/systemd/system/
systemctl daemon-reload
systemctl restart footbreak-tick.timer footbreak-sweep.timer

echo "▸ 更新儀表板靜態檔"
# --exclude data.json:web root 嗰份係跑出嚟嘅實時資料,唔可以用 repo 嗰份覆蓋
rsync -a --exclude 'data.json' "$APP_DIR/hkjc-dashboard/" "$WEB_ROOT/"

echo "▸ 重載 nginx"
install -m 0644 "$APP_DIR/deploy/nginx-footbreak.conf" /etc/nginx/sites-available/footbreak
nginx -t && systemctl reload nginx

echo "✅ 部署完成 @ $(date '+%F %T %Z')"
systemctl list-timers 'footbreak*' --no-pager | head -5
