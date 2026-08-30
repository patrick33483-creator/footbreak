#!/usr/bin/env bash
# 足破 · 自動部署鉤子
# GitHub Actions 每次 push 到 main 就會 SSH 入嚟跑呢個。
# 只更新程式碼,絕對唔會掂模擬倉同任何狀態檔(佢哋喺 .gitignore 入面)。
set -euo pipefail

APP_DIR="/opt/footbreak"
WEB_ROOT="/var/www/footbreak"
BRANCH="${1:-main}"

crown_is_enabled_in_config() {
  # Read only the validation-gate assignment; never source an environment file
  # in this privileged deploy hook and never print its contents.
  local file line value=""
  for file in /etc/footbreak.env /etc/footbreak-crown.env; do
    [ -r "$file" ] || continue
    line="$(grep -E '^[[:space:]]*(export[[:space:]]+)?CROWN_ENABLED[[:space:]]*=' "$file" | tail -n 1 || true)"
    [ -n "$line" ] || continue
    value="${line#*=}"
    value="${value#"${value%%[![:space:]]*}"}"
    value="${value%"${value##*[![:space:]]}"}"
    if [ "${value#\'}" != "$value" ] && [ "${value%\'}" != "$value" ]; then
      value="${value#\'}"
      value="${value%\'}"
    elif [ "${value#\"}" != "$value" ] && [ "${value%\"}" != "$value" ]; then
      value="${value#\"}"
      value="${value%\"}"
    fi
  done
  case "$value" in
    1|true|TRUE|yes|YES|on|ON) return 0 ;;
    *) return 1 ;;
  esac
}

reverse_t5_bridge_is_enabled_in_config() {
  # Match crown-run.sh's source order without sourcing secrets in this
  # privileged updater.  The marker is lifecycle metadata only.
  local file line value=""
  for file in /etc/footbreak.env /etc/footbreak-crown.env; do
    [ -r "$file" ] || continue
    line="$(grep -E '^[[:space:]]*(export[[:space:]]+)?CROWN_REVERSE_T5_BRIDGE_ENABLED[[:space:]]*=' "$file" | tail -n 1 || true)"
    [ -n "$line" ] || continue
    value="${line#*=}"
    value="${value#"${value%%[![:space:]]*}"}"
    value="${value%"${value##*[![:space:]]}"}"
    if [ "${value#\'}" != "$value" ] && [ "${value%\'}" != "$value" ]; then
      value="${value#\'}"
      value="${value%\'}"
    elif [ "${value#\"}" != "$value" ] && [ "${value%\"}" != "$value" ]; then
      value="${value#\"}"
      value="${value%\"}"
    fi
  done
  case "$value" in
    1|true|TRUE|yes|YES|on|ON) return 0 ;;
    *) return 1 ;;
  esac
}

sync_reverse_t5_bridge_enablement_marker() {
  local command="mark-disabled"
  if crown_is_enabled_in_config && reverse_t5_bridge_is_enabled_in_config; then
    command="mark-enabled"
  fi
  local python="$APP_DIR/.venv/bin/python3"
  [ -x "$python" ] || python=python3
  CROWN_STATE_DIR="${CROWN_STATE_DIR:-/var/lib/footbreak/crown}" \
    "$python" -m crown.reverse_t5_bridge_health "$command"
}

sync_crown_web_root() {
  # Only the static nginx tree is made readable.  Never recurse into
  # /var/lib/footbreak/crown, which is private runtime state.
  install -d -o root -g www-data -m 0755 /var/www /var/www/crown
  rsync -a --delete --exclude 'data.json' --exclude 'history.json' --exclude 'history-*.json' --chmod=Du=rwx,Dgo=rx,Fu=rw,Fgo=r \
    "$APP_DIR/crown/dashboard/" /var/www/crown/
  chown -R root:www-data /var/www/crown
  find /var/www/crown -type d -exec chmod 0755 {} +
  find /var/www/crown -type f -exec chmod 0644 {} +
}

cd "$APP_DIR"

echo "▸ 拉取最新程式碼($BRANCH)"
GITHUB_SSH_BASE="ssh -i /home/radar/.ssh/footbreak_github -o IdentitiesOnly=yes -o StrictHostKeyChecking=yes -o UserKnownHostsFile=/home/radar/.ssh/known_hosts -o ConnectTimeout=10"
if ! GIT_SSH_COMMAND="$GITHUB_SSH_BASE" git fetch --quiet origin "$BRANCH"; then
  echo "  GitHub SSH 22 無法連線，改用官方 SSH 443 備援"
  GIT_SSH_COMMAND="$GITHUB_SSH_BASE -o Hostname=ssh.github.com -p 443 -o HostKeyAlias=github.com" \
    git fetch --quiet origin "$BRANCH"
fi
BEFORE=$(git rev-parse HEAD)
git reset --hard --quiet "origin/$BRANCH"
AFTER=$(git rev-parse HEAD)

if [ "$BEFORE" = "$AFTER" ]; then
  echo "  已經係最新($AFTER),冇嘢要做"
else
  echo "  $BEFORE → $AFTER"
  git --no-pager log --oneline "$BEFORE..$AFTER" | sed -n '1,20p'
fi

echo "▸ 同步 Python 依賴"
if [ -f requirements.txt ] && [ -x .venv/bin/pip ]; then
  .venv/bin/pip install -q -r requirements.txt
fi

echo "▸ 執行本機磁碟防護及安全衍生檔清理"
/usr/bin/python3 "$APP_DIR/system/disk_guard.py"

echo "▸ 更新 external-tool 相容層"
install -m 0755 "$APP_DIR/bin/external-tool" /usr/local/bin/external-tool

echo "▸ 更新 systemd 單元"
install -m 0644 "$APP_DIR"/deploy/systemd/*.service /etc/systemd/system/
install -m 0644 "$APP_DIR"/deploy/systemd/*.timer   /etc/systemd/system/
systemctl daemon-reload
# Crown rolls at 11:59 HKT.  The dedicated 11:00 daily update establishes the
# next native board; the :05/:20/:35/:50 pass remains recovery-only, while the
# per-minute tick owns deadline-bound T-30/T-5 commits.
# The default validation gate is disabled.  A disabled Crown runner exits
# non-zero intentionally and performs no provider request, so keep its timers
# stopped until an operator explicitly enables it.
if crown_is_enabled_in_config; then
  echo "▸ Crown validation gate enabled; starting Crown timers"
  sync_reverse_t5_bridge_enablement_marker
  # A previously copied or rolled-back optional worker can retain a masked or
  # disabled unit-file state.  Clear that state before the normal Crown timer
  # reconciliation so an enabled bridge cannot remain inert.
  systemctl unmask crown-reverse-t5-drain.timer 2>/dev/null || true
  for timer in crown-round-update.timer crown-first-look-reconcile.timer crown-early-admission-reconcile.timer crown-sweep.timer crown-tick.timer crown-settle.timer crown-reverse-t5-drain.timer; do
    # A copied-in timer can retain a stale disabled unit-file state across a
    # rollback/forward deployment.  Recreate the timers.target link instead
    # of trusting `restart` to imply persistence; health-check verifies the
    # durable enabled state immediately afterwards.
    systemctl reenable "$timer"
    systemctl is-enabled --quiet "$timer" || {
      echo "ERROR: $timer was not enabled after reenable" >&2
      exit 1
    }
    systemctl restart "$timer"
    if ! systemctl is-active --quiet "$timer"; then
      systemctl reset-failed "$timer" 2>/dev/null || true
      systemctl start "$timer"
    fi
    systemctl is-active --quiet "$timer" || {
      systemctl show "$timer" -p LoadState -p ActiveState -p SubState -p Result
      echo "ERROR: $timer did not become active after restart" >&2
      exit 1
    }
  done
else
  echo "▸ Crown validation gate disabled; stopping Crown timers"
  sync_reverse_t5_bridge_enablement_marker
  systemctl disable --now crown-round-update.timer crown-first-look-reconcile.timer crown-early-admission-reconcile.timer crown-sweep.timer crown-tick.timer crown-settle.timer crown-reverse-t5-drain.timer 2>/dev/null || true
  systemctl stop crown-round-update.service crown-first-look-reconcile.service crown-early-admission-reconcile.service crown-sweep.service crown-tick.service crown-settle.service crown-reverse-t5-drain.service 2>/dev/null || true
  systemctl reset-failed crown-round-update.service crown-first-look-reconcile.service crown-early-admission-reconcile.service crown-sweep.service crown-tick.service crown-settle.service crown-reverse-t5-drain.service 2>/dev/null || true
fi
# Settlement is deliberately separate from the latency-sensitive tick.  T-30
# and T-5 now share one ordered queue, so the old second timer is retired
# completely.  Merely disabling it proved insufficient on an upgraded host:
# a still-loaded unit could continue firing and contend for footbreak.lock.
systemctl stop footbreak-t30.timer footbreak-t30.service 2>/dev/null || true
systemctl disable footbreak-t30.timer 2>/dev/null || true
rm -f /etc/systemd/system/footbreak-t30.timer
systemctl daemon-reload
systemctl reset-failed footbreak-t30.service 2>/dev/null || true
if systemctl is-active --quiet footbreak-t30.timer ||
   systemctl is-enabled --quiet footbreak-t30.timer; then
  echo "ERROR: retired footbreak-t30.timer is still active or enabled" >&2
  exit 1
fi
systemctl enable --now \
  footbreak-tick.timer footbreak-sweep.timer footbreak-settle.timer \
  footbreak-result-reconcile.timer footbreak-dashboard-self-heal.timer \
  footbreak-server-health-monitor.timer \
  footbreak-daily-condition-report.timer direction-path-conditions.timer
# The direction-path ledger is a local, read-only research worker.  It must
# remain independent of the Crown prediction gate and survive a host that has
# retained a disabled or masked unit state from an earlier setup/rollback.
# Do not trust the bulk enable alone: recreate its timers.target link and prove
# both durable enablement and runtime activation before continuing.
systemctl unmask direction-path-conditions.timer 2>/dev/null || true
systemctl reenable direction-path-conditions.timer
systemctl restart direction-path-conditions.timer
systemctl is-enabled --quiet direction-path-conditions.timer || {
  systemctl show direction-path-conditions.timer \
    -p LoadState -p UnitFileState -p ActiveState -p SubState -p Result
  echo "ERROR: direction-path-conditions.timer was not durably enabled" >&2
  exit 1
}
systemctl is-active --quiet direction-path-conditions.timer || {
  systemctl show direction-path-conditions.timer \
    -p LoadState -p UnitFileState -p ActiveState -p SubState -p Result
  echo "ERROR: direction-path-conditions.timer did not become active" >&2
  exit 1
}
# An already-active timer keeps its previous next-elapse calculation after a
# unit-file update on some systemd versions. Restart it explicitly so a
# 30-minute installation becomes the new 15-minute schedule immediately.
systemctl restart footbreak-result-reconcile.timer
systemctl is-active --quiet footbreak-result-reconcile.timer || {
  systemctl show footbreak-result-reconcile.timer \
    -p LoadState -p ActiveState -p SubState -p Result
  echo "ERROR: footbreak-result-reconcile.timer did not restart" >&2
  exit 1
}
systemctl restart footbreak-dashboard-self-heal.timer
systemctl is-active --quiet footbreak-dashboard-self-heal.timer || {
  systemctl show footbreak-dashboard-self-heal.timer \
    -p LoadState -p ActiveState -p SubState -p Result
  echo "ERROR: footbreak-dashboard-self-heal.timer did not restart" >&2
  exit 1
}
# Force-refresh the install symlink for this newly introduced timer.  Some
# upgraded hosts can retain a loaded unit while reporting it disabled after
# the unit file is first installed.
systemctl unmask footbreak-server-health-monitor.timer 2>/dev/null || true
systemctl reenable footbreak-server-health-monitor.timer
systemctl restart footbreak-server-health-monitor.timer
# The monitor is safe to invoke once after a reviewed deployment: it reads
# durable local state and repairs only bounded, documented faults.  This makes
# a just-deployed service/timer regression observable (and repairable) now,
# rather than waiting for its next half-hour cadence.
systemctl start footbreak-server-health-monitor.service
systemctl is-enabled --quiet footbreak-server-health-monitor.timer || {
  systemctl show footbreak-server-health-monitor.timer \
    -p LoadState -p ActiveState -p SubState -p UnitFileState -p Result
  echo "ERROR: footbreak-server-health-monitor.timer did not become enabled" >&2
  exit 1
}
systemctl is-active --quiet footbreak-server-health-monitor.timer || {
  systemctl show footbreak-server-health-monitor.timer \
    -p LoadState -p ActiveState -p SubState -p Result
  echo "ERROR: footbreak-server-health-monitor.timer did not restart" >&2
  exit 1
}
# Routine one-hour Telegram silence summaries were retired by operator request.
# Keep the unit files available for audit history, but stop and durably disable
# both the timer and any in-flight one-shot service on every deployment.
systemctl disable --now telegram-silence-monitor.timer 2>/dev/null || true
systemctl stop telegram-silence-monitor.service 2>/dev/null || true
# The report is generated entirely on this host from locked local snapshots.
# Reenable creates the durable timers.target symlink on both fresh and upgraded
# servers.  Persistent=true makes a missed 12:15 HKT fire once after boot.
install -d -o root -g root -m 0700 /var/lib/footbreak/daily-condition-reports
systemctl unmask footbreak-daily-condition-report.timer 2>/dev/null || true
systemctl reenable footbreak-daily-condition-report.timer
systemctl restart footbreak-daily-condition-report.timer
systemctl is-enabled --quiet footbreak-daily-condition-report.timer || {
  systemctl show footbreak-daily-condition-report.timer \
    -p LoadState -p ActiveState -p SubState -p UnitFileState -p Result
  echo "ERROR: footbreak-daily-condition-report.timer did not become enabled" >&2
  exit 1
}
systemctl is-active --quiet footbreak-daily-condition-report.timer || {
  systemctl show footbreak-daily-condition-report.timer \
    -p LoadState -p ActiveState -p SubState -p Result
  echo "ERROR: footbreak-daily-condition-report.timer did not restart" >&2
  exit 1
}
# Run once after deployment.  Per-window send state makes this idempotent, so
# a later deployment regenerates the files but cannot duplicate either the
# Telegram summary or document for an already delivered window.
systemctl start footbreak-daily-condition-report.service
systemctl enable crown-dashboard-api.service footbreak-dashboard-api.service
systemctl restart crown-dashboard-api.service footbreak-dashboard-api.service
# `systemctl is-active` can briefly report active while a crashing process is
# inside its restart loop.  Require the local HTTP socket to answer before the
# deployment is allowed to continue.
dashboard_api_ready=0
for _ in $(seq 1 60); do
  if /usr/bin/python3 - <<'PY' 2>/dev/null
import json
from urllib.request import urlopen

with urlopen("http://127.0.0.1:8765/api/health", timeout=2) as response:
    payload = json.load(response)
assert payload == {"ok": True, "service": "crown-dashboard-api"}
PY
  then
    dashboard_api_ready=1
    break
  fi
  sleep 1
done
if [ "$dashboard_api_ready" != 1 ]; then
  systemctl status crown-dashboard-api.service --no-pager -l || true
  journalctl -u crown-dashboard-api.service --since "-5 minutes" --no-pager -n 200 || true
  echo "ERROR: crown-dashboard-api.service HTTP socket did not become ready" >&2
  exit 1
fi
footbreak_api_ready=0
for _ in $(seq 1 20); do
  if /usr/bin/python3 - <<'PY' 2>/dev/null
import json
from urllib.request import urlopen

with urlopen("http://127.0.0.1:8766/api/health", timeout=2) as response:
    payload = json.load(response)
assert payload == {"ok": True, "service": "footbreak-dashboard-api"}
PY
  then
    footbreak_api_ready=1
    break
  fi
  sleep 1
done
if [ "$footbreak_api_ready" != 1 ]; then
  systemctl status footbreak-dashboard-api.service --no-pager -l || true
  journalctl -u footbreak-dashboard-api.service --since "-5 minutes" --no-pager -n 200 || true
  echo "ERROR: footbreak-dashboard-api.service HTTP socket did not become ready" >&2
  exit 1
fi
for timer in footbreak-tick.timer footbreak-sweep.timer footbreak-settle.timer footbreak-backtest.timer; do
  if systemctl is-enabled --quiet "$timer"; then
    systemctl restart "$timer"
  fi
done

echo "▸ 更新儀表板靜態檔"
# Runtime data/history are published atomically by local jobs.  A code update
# must never replace either artifact with a repository copy.
rsync -a --exclude 'data.json' --exclude 'history.json' "$APP_DIR/hkjc-dashboard/" "$WEB_ROOT/"
install -d -o root -g www-data -m 0755 /var/www "$WEB_ROOT"
chown -R root:www-data "$WEB_ROOT"
find "$WEB_ROOT" -type d -exec chmod 0755 {} +
find "$WEB_ROOT" -type f -exec chmod 0644 {} +
if [ -f "$WEB_ROOT/history.json" ]; then
  chown root:www-data "$WEB_ROOT/history.json"
  chmod 0644 "$WEB_ROOT/history.json"
fi
# A deploy can land between the sidecar-first and boot-payload-last writes of
# a normal Footbreak publisher.  Rebuild the pair from persisted local state
# while holding the same lock as tick/sweep/settle, so the health check and
# browser can never observe mixed history generations after an upgrade.
# gen_app_data is provider-free: it reads only existing local artifacts.
echo "▸ 以共用鎖重建足破儀表板資料"
exec 8>/var/lock/footbreak.lock
if ! flock -w 60 8; then
  echo "ERROR: timed out waiting for Footbreak state lock during dashboard publication" >&2
  exit 1
fi
PYTHONPATH="$APP_DIR" FOOTBREAK_DASHBOARD_DATA="$WEB_ROOT/data.json" \
  "$APP_DIR/.venv/bin/python3" -m system.gen_app_data --out "$WEB_ROOT/data.json"
flock -u 8
exec 8>&-
chown root:www-data "$WEB_ROOT/data.json" "$WEB_ROOT/history.json"
chmod 0644 "$WEB_ROOT/data.json" "$WEB_ROOT/history.json"
install -d -o root -g root -m 0700 /var/lib/footbreak/crown /var/lib/footbreak/learning
if [ -f "$APP_DIR/system/sim_ledger.json" ]; then
  chown root:root "$APP_DIR/system/sim_ledger.json"
  chmod 0600 "$APP_DIR/system/sim_ledger.json"
fi
# Runtime dashboard data is deliberately excluded: a deploy never replaces
# Crown's ledger/state-derived data with the recovered archive snapshot.
sync_crown_web_root
CROWN_STATE_DIR=/var/lib/footbreak/crown CROWN_WEB_ROOT=/var/www/crown \
  "$APP_DIR/.venv/bin/python3" -m crown.dashboard_data --out /var/www/crown/data.json
chown root:www-data /var/www/crown/data.json
chmod 0644 /var/www/crown/data.json
find /var/www/crown -maxdepth 1 -type f -name 'history-*.json' -exec chown root:www-data {} +
find /var/www/crown -maxdepth 1 -type f -name 'history-*.json' -exec chmod 0644 {} +

# Generate the isolated report-only condition artifacts immediately after a
# deploy, rather than leaving both dashboards at 404 until the next 15-minute
# reconciliation cycle.  This reads the immutable learning DB only and stays
# non-fatal so it cannot block prediction, settlement, or dashboard rollout.
LEARNING_DB=/var/lib/footbreak/learning/predictions.sqlite
SHADOW_CONDITIONS_DIR=/var/lib/footbreak/shadow-conditions
if [ -s "$LEARNING_DB" ]; then
  install -d -o root -g root -m 0700 "$SHADOW_CONDITIONS_DIR"
  if ! PYTHONPATH="$APP_DIR" "$APP_DIR/.venv/bin/python3" -m analysis.shadow_conditions \
    --learning-db "$LEARNING_DB" \
    --state "$SHADOW_CONDITIONS_DIR/state.json" \
    --public-footbreak /var/www/footbreak/shadow-condition-report.json \
    --public-crown /var/www/crown/shadow-condition-report.json >/dev/null; then
    echo "條件影子報告生成失敗；網站及結算部署不受影響，下個 15 分鐘週期會重試" >&2
  fi
fi

# Build the separate three-stage direction-path ledger from the immutable
# historical seed plus the local Odds Radar SQLite database.  This never calls
# an external API and never enters the betting or Telegram paths.
install -d -o root -g root -m 0700 /var/lib/footbreak/direction-path-conditions
install -d -o root -g www-data -m 0755 /var/www/stage_engine_v2
if [ -s /opt/odds-radar/data/data.db ]; then
  if ! PYTHONPATH="$APP_DIR" "$APP_DIR/.venv/bin/python3" -m analysis.direction_path_conditions >/dev/null; then
    echo "三階段細分條件報告生成失敗；timer 會在 5 分鐘內重試" >&2
  fi
fi

echo "▸ 重載 nginx"
install -m 0644 "$APP_DIR/deploy/nginx-footbreak.conf" /etc/nginx/sites-available/footbreak
install -m 0644 "$APP_DIR/deploy/nginx-crown.conf" /etc/nginx/sites-available/crown
install -m 0644 "$APP_DIR/deploy/nginx-unified-dashboard.conf" /etc/nginx/sites-available/unified-dashboard
ln -sf /etc/nginx/sites-available/unified-dashboard /etc/nginx/sites-enabled/unified-dashboard

# nginx workers run as www-data. A password rotation or restored file can
# leave either Basic Auth file unreadable, which nginx reports as a plain 500
# even while the private dashboard API sockets remain healthy.
# Parent-directory traversal is equally required. Restore only the standard
# owner/mode metadata; never alter any file contents here.
chown root:root /etc /etc/nginx
chmod 0755 /etc /etc/nginx
if [ -f /etc/bash.bashrc ]; then
  chown root:root /etc/bash.bashrc
  chmod 0644 /etc/bash.bashrc
fi
footbreak_auth=/etc/nginx/.htpasswd-footbreak
crown_auth=/etc/nginx/.htpasswd-crown
repair_auth_identity() {
  auth_file="$1"
  expected_user="$2"
  password_backup="$3"
  if [ -s "$auth_file" ] && grep -q "^${expected_user}:" "$auth_file"; then
    return 0
  fi
  if [ ! -s "$password_backup" ]; then
    echo "ERROR: $expected_user dashboard auth is missing or has the wrong account, and its private password backup is unavailable" >&2
    return 1
  fi
  IFS= read -r dashboard_password < "$password_backup"
  if [ -z "$dashboard_password" ]; then
    echo "ERROR: $expected_user dashboard password backup is empty" >&2
    return 1
  fi
  htpasswd -bc "$auth_file" "$expected_user" "$dashboard_password" >/dev/null
  unset dashboard_password
}
repair_auth_identity "$footbreak_auth" footbreak /root/footbreak-dashboard-password.txt
repair_auth_identity "$crown_auth" crown /root/crown-dashboard-password.txt
for auth_file in /etc/nginx/.htpasswd-footbreak /etc/nginx/.htpasswd-crown; do
  chown root:www-data "$auth_file"
  chmod 0640 "$auth_file"
  if ! runuser -u www-data -- test -r "$auth_file"; then
    echo "ERROR: nginx worker cannot read password file: $auth_file" >&2
    exit 1
  fi
done

nginx -t
systemctl reload nginx || systemctl restart nginx

# An unauthenticated request must stop at Basic Auth with 401. This checks the
# actual nginx/static/auth path users hit, not only the 8765/8766 API sockets.
for dashboard in footbreak:8081 crown:8082; do
  name="${dashboard%%:*}"
  port="${dashboard##*:}"
  status="$(curl --silent --show-error --output /dev/null \
    --write-out '%{http_code}' --max-time 5 "http://127.0.0.1:${port}/" || true)"
  if [ "$status" != 401 ]; then
    echo "ERROR: nginx $name entrypoint returned HTTP ${status:-unreachable}, expected 401" >&2
    exit 1
  fi
  echo "  nginx $name entrypoint OK (HTTP 401 auth challenge)"
done

# Timer restarts and a pending persistent settlement may run during deployment.
# The APIs were already restarted and HTTP-checked above.  Do not restart them
# a second time here: doing so creates a race with those queued timer jobs and
# can make a healthy service miss the final readiness window.
systemctl enable --now crown-dashboard-api.service footbreak-dashboard-api.service
final_api_ready=0
for _ in $(seq 1 30); do
  if systemctl is-active --quiet crown-dashboard-api.service &&
     systemctl is-active --quiet footbreak-dashboard-api.service &&
     /usr/bin/python3 - <<'PY' 2>/dev/null
import json
from urllib.request import urlopen

with urlopen("http://127.0.0.1:8765/api/health", timeout=2) as response:
    crown = json.load(response)
with urlopen("http://127.0.0.1:8766/api/health", timeout=2) as response:
    footbreak = json.load(response)
assert crown == {"ok": True, "service": "crown-dashboard-api"}
assert footbreak == {"ok": True, "service": "footbreak-dashboard-api"}
PY
  then
    final_api_ready=1
    break
  fi
  sleep 1
done
if [ "$final_api_ready" != 1 ]; then
  systemctl status crown-dashboard-api.service footbreak-dashboard-api.service --no-pager -l || true
  echo "ERROR: dashboard APIs did not remain ready at deployment completion" >&2
  exit 1
fi

echo "✅ 部署完成 @ $(date '+%F %T %Z')"
systemctl list-timers 'footbreak*' --no-pager | sed -n '1,5p'
