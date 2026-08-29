#!/usr/bin/env bash
set -uo pipefail
echo "===== A. Source of truth conf ====="
cat /opt/footbreak/deploy/nginx-unified-dashboard.conf | head -200

echo ""
echo "===== B. Total lines ====="
wc -l /opt/footbreak/deploy/nginx-unified-dashboard.conf

echo ""
echo "===== C. Location blocks ====="
grep -nE "location |^server|^}" /opt/footbreak/deploy/nginx-unified-dashboard.conf | head -50

echo ""
echo "===== D. Git status of conf file ====="
cd /opt/footbreak && git log --oneline -5 -- deploy/nginx-unified-dashboard.conf 2>&1 | head -5
