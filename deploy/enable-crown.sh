#!/usr/bin/env bash
# Explicitly enable the production Crown validation gate and its timers.
# This script never prints or sources the environment file.
set -euo pipefail

ENV_FILE="${CROWN_ENV_FILE:-/etc/footbreak-crown.env}"
TMP_FILE="$(mktemp "${ENV_FILE}.tmp.XXXXXX")"
cleanup() {
  rm -f "$TMP_FILE"
}
trap cleanup EXIT

install -d -m 0700 "$(dirname "$ENV_FILE")"
if [ -f "$ENV_FILE" ]; then
  awk '
    BEGIN { replaced = 0 }
    /^[[:space:]]*(export[[:space:]]+)?CROWN_ENABLED[[:space:]]*=/ {
      if (!replaced) {
        print "CROWN_ENABLED=1"
        replaced = 1
      }
      next
    }
    { print }
    END {
      if (!replaced) print "CROWN_ENABLED=1"
    }
  ' "$ENV_FILE" > "$TMP_FILE"
else
  printf '%s\n' 'CROWN_ENABLED=1' > "$TMP_FILE"
fi

chown root:root "$TMP_FILE"
chmod 0600 "$TMP_FILE"
mv -f "$TMP_FILE" "$ENV_FILE"
trap - EXIT

systemctl daemon-reload
for timer in crown-sweep.timer crown-tick.timer crown-settle.timer; do
  systemctl enable "$timer"
  systemctl restart "$timer"
  systemctl is-enabled --quiet "$timer"
  systemctl is-active --quiet "$timer"
done

echo "Crown validation gate enabled; Crown sweep/tick/settle timers are active."
