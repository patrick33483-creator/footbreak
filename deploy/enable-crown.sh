#!/usr/bin/env bash
# Explicitly enable the production Crown validation gate and its timers.
# This script never prints or sources the environment file.
set -euo pipefail

APP_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
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
python="$APP_DIR/.venv/bin/python3"
[ -x "$python" ] || python=python3
bridge_value="$(
  grep -E '^[[:space:]]*(export[[:space:]]+)?CROWN_REVERSE_T5_BRIDGE_ENABLED[[:space:]]*=' \
    "$ENV_FILE" | tail -n 1 | sed 's/^[^=]*=//' || true
)"
# Match update.sh's privileged, non-sourcing parser.  The runtime wrapper
# accepts quoted shell values, so lifecycle metadata must make the same choice.
bridge_value="${bridge_value#"${bridge_value%%[![:space:]]*}"}"
bridge_value="${bridge_value%"${bridge_value##*[![:space:]]}"}"
if [ "${bridge_value#\'}" != "$bridge_value" ] && [ "${bridge_value%\'}" != "$bridge_value" ]; then
  bridge_value="${bridge_value#\'}"
  bridge_value="${bridge_value%\'}"
elif [ "${bridge_value#\"}" != "$bridge_value" ] && [ "${bridge_value%\"}" != "$bridge_value" ]; then
  bridge_value="${bridge_value#\"}"
  bridge_value="${bridge_value%\"}"
fi
case "$bridge_value" in
  1|true|TRUE|yes|YES|on|ON)
    PYTHONPATH="$APP_DIR${PYTHONPATH:+:$PYTHONPATH}" \
    CROWN_STATE_DIR="${CROWN_STATE_DIR:-/var/lib/footbreak/crown}" \
      "$python" -m crown.reverse_t5_bridge_health mark-enabled
    ;;
  *)
    PYTHONPATH="$APP_DIR${PYTHONPATH:+:$PYTHONPATH}" \
    CROWN_STATE_DIR="${CROWN_STATE_DIR:-/var/lib/footbreak/crown}" \
      "$python" -m crown.reverse_t5_bridge_health mark-disabled
    ;;
esac
for timer in crown-round-update.timer crown-first-look-reconcile.timer crown-sweep.timer crown-tick.timer crown-settle.timer crown-reverse-t5-drain.timer; do
  systemctl enable "$timer"
  systemctl restart "$timer"
  systemctl is-enabled --quiet "$timer"
  systemctl is-active --quiet "$timer"
done

echo "Crown validation gate enabled; Crown daily-update/reconcile/sweep/tick/settle/reverse-T-5 timers are active."
