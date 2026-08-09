#!/usr/bin/env bash
# 足破 · 自動部署鉤子
# GitHub Actions 每次 push 到 main 就會 SSH 入嚟跑呢個。
# 只更新程式碼,絕對唔會掂模擬倉同任何狀態檔(佢哋喺 .gitignore 入面)。
set -euo pipefail

APP_DIR="/opt/footbreak"
WEB_ROOT="/var/www/footbreak"
BRANCH="${1:-main}"

sync_crown_web_root() {
  # Only the static nginx tree is made readable.  Never recurse into
  # /var/lib/footbreak/crown, which is private runtime state.
  install -d -o root -g www-data -m 0755 /var/www /var/www/crown
  rsync -a --delete --exclude 'data.json' --chmod=Du=rwx,Dgo=rx,Fu=rw,Fgo=r \
    "$APP_DIR/crown/dashboard/" /var/www/crown/
  chown -R root:www-data /var/www/crown
  find /var/www/crown -type d -exec chmod 0755 {} +
  find /var/www/crown -type f -exec chmod 0644 {} +
}

cd "$APP_DIR"

echo "▸ 拉取最新程式碼($BRANCH)"
GIT_SSH_COMMAND="ssh -i /home/radar/.ssh/footbreak_github -o IdentitiesOnly=yes -o StrictHostKeyChecking=yes -o UserKnownHostsFile=/home/radar/.ssh/known_hosts" \
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
for timer in footbreak-tick.timer footbreak-sweep.timer crown-tick.timer crown-sweep.timer footbreak-backtest.timer; do
  if systemctl is-enabled --quiet "$timer"; then
    systemctl restart "$timer"
  fi
done

echo "▸ 更新儀表板靜態檔"
# --exclude data.json:web root 嗰份係跑出嚟嘅實時資料,唔可以用 repo 嗰份覆蓋
rsync -a --exclude 'data.json' "$APP_DIR/hkjc-dashboard/" "$WEB_ROOT/"
install -d -o root -g root -m 0700 /var/lib/footbreak/crown
# Runtime dashboard data is deliberately excluded: a deploy never replaces
# Crown's ledger/state-derived data with the recovered archive snapshot.
sync_crown_web_root
CROWN_STATE_DIR=/var/lib/footbreak/crown CROWN_WEB_ROOT=/var/www/crown \
  "$APP_DIR/.venv/bin/python3" -m crown.dashboard_data --out /var/www/crown/data.json
chown root:www-data /var/www/crown/data.json
chmod 0644 /var/www/crown/data.json

echo "▸ 重載 nginx"
install -m 0644 "$APP_DIR/deploy/nginx-footbreak.conf" /etc/nginx/sites-available/footbreak
install -m 0644 "$APP_DIR/deploy/nginx-crown.conf" /etc/nginx/sites-available/crown
nginx -t
systemctl reload nginx || systemctl restart nginx

echo "✅ 部署完成 @ $(date '+%F %T %Z')"
systemctl list-timers 'footbreak*' --no-pager | head -5
