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

dashboard_json_is_healthy() {
  local port="$1" user="$2" password_file="$3" contract="$4" dashboard_password
  [ -s "$password_file" ] || return 1
  IFS= read -r dashboard_password < "$password_file"
  [ -n "$dashboard_password" ] || return 1
  if ! curl --silent --show-error --fail --max-time 8 \
      --user "${user}:${dashboard_password}" \
      "http://127.0.0.1:${port}/data.json?health=$(date +%s)" \
      | python3 -c 'import json,sys; p=json.load(sys.stdin); c=sys.argv[1]; assert (c=="crown-dashboard-v2" and p.get("schema_version")==c) or (c=="footbreak-dashboard" and isinstance(p.get("matches"),list) and isinstance(p.get("ledger"),dict) and bool(p.get("generated_at")))' "$contract" \
      >/dev/null 2>&1; then
    unset dashboard_password
    return 1
  fi
  unset dashboard_password
}

public_dashboard_json_is_healthy() {
  local system="$1" user="$2" password_file="$3" contract="$4" endpoint="$5"
  local dashboard_password
  [ -s "$password_file" ] || return 1
  IFS= read -r dashboard_password < "$password_file"
  [ -n "$dashboard_password" ] || return 1
  if ! curl --silent --show-error --fail --max-time 15 \
      --user "${user}:${dashboard_password}" \
      "http://127.0.0.1/${system}/${endpoint}?health=$(date +%s)" \
      | python3 -c 'import json,sys; p=json.load(sys.stdin); c=sys.argv[1]; assert (c=="crown-dashboard-v2" and p.get("schema_version")==c) or (c=="footbreak-dashboard" and isinstance(p.get("matches"),list) and isinstance(p.get("ledger"),dict) and bool(p.get("generated_at")))' "$contract" \
      >/dev/null 2>&1; then
    unset dashboard_password
    return 1
  fi
  unset dashboard_password
}

republish_dashboard_json() {
  local system="$1"
  case "$system" in
    footbreak)
      /opt/footbreak/.venv/bin/python3 -m system.gen_app_data \
        --out /var/www/footbreak/data.json
      chown root:www-data /var/www/footbreak/data.json
      chmod 0644 /var/www/footbreak/data.json
      ;;
    crown)
      /opt/footbreak/.venv/bin/python3 -m crown.dashboard_data \
        --out /var/www/crown/data.json
      chown root:www-data /var/www/crown/data.json
      chmod 0644 /var/www/crown/data.json
      ;;
    *)
      return 2
      ;;
  esac
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
  runuser -u www-data -- test -r "$path"
}

repair_auth_pair() {
  local footbreak=/etc/nginx/.htpasswd-footbreak
  local crown=/etc/nginx/.htpasswd-crown
  local auth_file expected_user password_backup dashboard_password
  chown root:root /etc /etc/nginx
  chmod 0755 /etc /etc/nginx
  for auth_file in "$footbreak" "$crown"; do
    if [ "$auth_file" = "$footbreak" ]; then
      expected_user=footbreak
      password_backup=/root/footbreak-dashboard-password.txt
    else
      expected_user=crown
      password_backup=/root/crown-dashboard-password.txt
    fi
    if [ ! -s "$auth_file" ] || ! grep -q "^${expected_user}:" "$auth_file"; then
      [ -s "$password_backup" ] || return 1
      IFS= read -r dashboard_password < "$password_backup"
      [ -n "$dashboard_password" ] || return 1
      htpasswd -bc "$auth_file" "$expected_user" "$dashboard_password" >/dev/null
      unset dashboard_password
    fi
  done
  repair_auth_file "$footbreak"
  repair_auth_file "$crown"
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
  repair_auth_pair || failed=1
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

if ! dashboard_json_is_healthy \
    8081 footbreak /root/footbreak-dashboard-password.txt footbreak-dashboard; then
  log "Footbreak /data.json is not valid JSON; republishing from persisted state"
  republish_dashboard_json footbreak || failed=1
fi
if ! dashboard_json_is_healthy \
    8082 crown /root/crown-dashboard-password.txt crown-dashboard-v2; then
  log "Crown /data.json is not valid JSON; republishing from persisted state"
  republish_dashboard_json crown || failed=1
fi
dashboard_json_is_healthy \
  8081 footbreak /root/footbreak-dashboard-password.txt footbreak-dashboard || failed=1
dashboard_json_is_healthy \
  8082 crown /root/crown-dashboard-password.txt crown-dashboard-v2 || failed=1

public_routes_healthy=1
for public_spec in \
  "footbreak:footbreak:/root/footbreak-dashboard-password.txt:footbreak-dashboard" \
  "crown:crown:/root/crown-dashboard-password.txt:crown-dashboard-v2"; do
  IFS=: read -r system user password_file contract <<< "$public_spec"
  for endpoint in data.json api/data; do
    public_dashboard_json_is_healthy \
      "$system" "$user" "$password_file" "$contract" "$endpoint" \
      || public_routes_healthy=0
  done
done
if [ "$public_routes_healthy" != 1 ]; then
  log "Public dashboard route unhealthy; restoring tracked unified nginx routing"
  install -m 0644 /opt/footbreak/deploy/nginx-unified-dashboard.conf \
    /etc/nginx/sites-available/unified-dashboard || failed=1
  ln -sf /etc/nginx/sites-available/unified-dashboard \
    /etc/nginx/sites-enabled/unified-dashboard || failed=1
  if nginx -t; then
    systemctl reload nginx || systemctl restart nginx
  else
    failed=1
  fi
fi
for public_spec in \
  "footbreak:footbreak:/root/footbreak-dashboard-password.txt:footbreak-dashboard" \
  "crown:crown:/root/crown-dashboard-password.txt:crown-dashboard-v2"; do
  IFS=: read -r system user password_file contract <<< "$public_spec"
  for endpoint in data.json api/data; do
    public_dashboard_json_is_healthy \
      "$system" "$user" "$password_file" "$contract" "$endpoint" \
      || failed=1
  done
done

if [ "$failed" != 0 ]; then
  exit 1
fi

log "Dashboard self-check healthy"
