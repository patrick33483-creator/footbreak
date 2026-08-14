#!/usr/bin/env bash
set -euo pipefail

LOCK_FILE="${DASHBOARD_SELF_HEAL_LOCK:-/run/lock/footbreak-dashboard-self-heal.lock}"
exec 9>"$LOCK_FILE"
flock -n 9 || exit 0

log() {
  printf '%s %s\n' "$(date -u '+%Y-%m-%dT%H:%M:%SZ')" "$*"
}

api_is_healthy() {
  local port="$1"
  python3 - "$port" <<'PY' >/dev/null 2>&1
import json
import sys
from urllib.request import urlopen

port = int(sys.argv[1])
with urlopen(f"http://127.0.0.1:{port}/api/health", timeout=5) as response:
    payload = json.load(response)
assert payload.get("ok") is True
PY
}

nginx_status() {
  local port="$1"
  curl --silent --show-error --output /dev/null \
    --write-out '%{http_code}' --max-time 5 "http://127.0.0.1:${port}/" || true
}

repair_static_tree() {
  local root="$1"
  [ -d "$root" ] || return 1
  chown -R root:www-data "$root"
  find "$root" -type d -exec chmod 0755 {} +
  find "$root" -type f -exec chmod 0644 {} +
}

repair_auth_file() {
  local path="$1"
  [ -f "$path" ] && [ -s "$path" ] || return 1
  chown root:www-data "$path"
  chmod 0640 "$path"
  sudo -u www-data test -r "$path"
}

failed=0

if ! api_is_healthy 8766; then
  log "Footbreak dashboard API unhealthy; restarting local service"
  systemctl restart footbreak-dashboard-api.service
  sleep 2
  api_is_healthy 8766 || failed=1
fi

if ! api_is_healthy 8765; then
  log "Crown dashboard API unhealthy; restarting local service"
  systemctl restart crown-dashboard-api.service
  sleep 2
  api_is_healthy 8765 || failed=1
fi

footbreak_status="$(nginx_status 8081)"
crown_status="$(nginx_status 8082)"
if [ "$footbreak_status" != 401 ] || [ "$crown_status" != 401 ]; then
  log "Nginx dashboard check failed; repairing local permissions and reloading"
  repair_static_tree /var/www/footbreak || failed=1
  repair_static_tree /var/www/crown || failed=1
  repair_auth_file /etc/nginx/.htpasswd-footbreak || failed=1
  repair_auth_file /etc/nginx/.htpasswd-crown || failed=1
  if nginx -t; then
    systemctl reload nginx || systemctl restart nginx
  else
    failed=1
  fi
fi

footbreak_status="$(nginx_status 8081)"
crown_status="$(nginx_status 8082)"
if [ "$footbreak_status" != 401 ] || [ "$crown_status" != 401 ]; then
  log "Dashboard self-heal failed: footbreak_http=$footbreak_status crown_http=$crown_status"
  failed=1
fi

if [ "$failed" != 0 ]; then
  exit 1
fi

log "Dashboard self-check healthy"
