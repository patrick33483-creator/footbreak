#!/usr/bin/env bash
set -uo pipefail
for f in predictor writer scheduler publisher config telegram __main__; do
  echo "===== ${f}.py ====="
  cat /opt/footbreak/stage_engine_v2/${f}.py 2>&1 | head -200
  echo ""
done
echo "===== v2 systemd units ====="
ls -l /etc/systemd/system/stage-engine-v2* 2>&1
for u in stage-engine-v2-tick.service stage-engine-v2-tick.timer; do
  echo "--- $u ---"
  cat /etc/systemd/system/$u 2>&1
done
echo ""
echo "===== crown v2 dashboard html ====="
ls -l /var/www/stage_engine_v2/ 2>&1
head -30 /var/www/stage_engine_v2/index.html 2>&1
echo ""
echo "===== nginx unified conf 相關段 ====="
grep -nE "stage_engine_v2|/v2/|proxy_pass.*808[0-9]" /etc/nginx/sites-available/unified-dashboard 2>&1
