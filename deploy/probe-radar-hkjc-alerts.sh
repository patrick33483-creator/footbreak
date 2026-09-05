#!/usr/bin/env bash
# Read-only probe: recent Odds Radar HKJC picks + Telegram delivery state.
set -euo pipefail
DB=/opt/odds-radar/data/data.db
if [ ! -r "$DB" ]; then
  echo "ERROR: cannot read $DB" >&2
  exit 2
fi

echo "=== unit status ==="
for u in odds-radar odds-radar-worker odds-radar.timer; do
  systemctl is-active "$u" 2>/dev/null | sed "s|^|$u=|" || true
done
echo

echo "=== provider_health ==="
sqlite3 -readonly "$DB" \
  "SELECT provider, ok, consecutive_failures, updated_at FROM provider_health ORDER BY provider;"
echo

echo "=== recent HKJC picks (last 30) ==="
sqlite3 -readonly -header -column "$DB" <<'SQL'
SELECT
  datetime(placed_at/1000,'unixepoch','+8 hours') AS placed_hkt,
  substr(unique_key,1,80) AS unique_key,
  match_id, market, selection, line, price,
  excluded_from_stats AS excl
FROM simulation_bets
WHERE (bookmaker LIKE '%hkjc%' OR bookmaker LIKE '%馬會%' OR source LIKE '%hkjc%')
ORDER BY placed_at DESC
LIMIT 30;
SQL
echo

echo "=== recent HKJC alerts overall counts (7d/30d/90d) ==="
sqlite3 -readonly "$DB" <<'SQL'
SELECT '7d' AS window, COUNT(*) FROM simulation_bets
 WHERE (bookmaker LIKE '%hkjc%' OR bookmaker LIKE '%馬會%' OR source LIKE '%hkjc%')
   AND placed_at >= (strftime('%s','now')-7*86400)*1000
UNION ALL
SELECT '30d', COUNT(*) FROM simulation_bets
 WHERE (bookmaker LIKE '%hkjc%' OR bookmaker LIKE '%馬會%' OR source LIKE '%hkjc%')
   AND placed_at >= (strftime('%s','now')-30*86400)*1000
UNION ALL
SELECT '90d', COUNT(*) FROM simulation_bets
 WHERE (bookmaker LIKE '%hkjc%' OR bookmaker LIKE '%馬會%' OR source LIKE '%hkjc%')
   AND placed_at >= (strftime('%s','now')-90*86400)*1000;
SQL
echo

echo "=== ALL recent picks last 20 (any book) — sanity check radar runs ==="
sqlite3 -readonly -header -column "$DB" <<'SQL'
SELECT
  datetime(placed_at/1000,'unixepoch','+8 hours') AS placed_hkt,
  bookmaker, market, selection, line, price, excluded_from_stats AS excl
FROM simulation_bets
ORDER BY placed_at DESC
LIMIT 20;
SQL
echo

echo "=== last telegram_sent records (top 10) ==="
sqlite3 -readonly -header -column "$DB" <<'SQL'
SELECT
  key,
  datetime(updated_at/1000,'unixepoch','+8 hours') AS sent_hkt,
  substr(value,1,60) AS value_head
FROM app_state
WHERE key LIKE 'telegram_sent:%'
ORDER BY updated_at DESC
LIMIT 10;
SQL
echo

echo "=== last telegram_error/skipped/muted app_state (if any) ==="
sqlite3 -readonly -header -column "$DB" <<'SQL'
SELECT key, datetime(updated_at/1000,'unixepoch','+8 hours') AS ts,
       substr(value,1,120) AS value_head
FROM app_state
WHERE key LIKE '%telegram%'
ORDER BY updated_at DESC
LIMIT 20;
SQL
echo

echo "=== last radar service journal 60 lines (any errors?) ==="
journalctl -u odds-radar -n 60 --no-pager 2>/dev/null || echo "no journal access"
echo

echo "=== telegram silence monitor last state ==="
if [ -r /var/lib/footbreak/telegram-silence-monitor.json ]; then
  jq . /var/lib/footbreak/telegram-silence-monitor.json
else
  echo "(missing)"
fi
