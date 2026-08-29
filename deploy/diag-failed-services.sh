#!/usr/bin/env bash
set -uo pipefail

echo "===== A. crown-early-admission-reconcile fail 詳情 ====="
systemctl status crown-early-admission-reconcile.service --no-pager -l 2>&1 | head -30
echo "--- last 60 log lines ---"
journalctl -u crown-early-admission-reconcile.service --no-pager -n 60 2>&1 | tail -60

echo ""
echo "===== B. footbreak-settle fail ====="
systemctl status footbreak-settle.service --no-pager -l 2>&1 | head -20
journalctl -u footbreak-settle.service --no-pager -n 40 2>&1 | tail -40

echo ""
echo "===== C. footbreak-sweep fail ====="
systemctl status footbreak-sweep.service --no-pager -l 2>&1 | head -20
journalctl -u footbreak-sweep.service --no-pager -n 40 2>&1 | tail -40

echo ""
echo "===== D. footbreak-dashboard-self-heal fail ====="
systemctl status footbreak-dashboard-self-heal.service --no-pager -l 2>&1 | head -20
journalctl -u footbreak-dashboard-self-heal.service --no-pager -n 40 2>&1 | tail -40

echo ""
echo "===== E. 呢啲 service 幾時開始 fail ====="
journalctl -u crown-early-admission-reconcile.service --no-pager --since '48 hours ago' 2>&1 | grep -iE "fail|error|success" | tail -20
