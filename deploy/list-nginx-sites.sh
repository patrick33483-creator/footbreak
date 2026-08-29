#!/usr/bin/env bash
set -uo pipefail
echo "===== sites-enabled ====="
ls -la /etc/nginx/sites-enabled/ 2>&1
echo ""
echo "===== unified-dashboard listen lines ====="
grep -nE "listen|server_name" /etc/nginx/sites-enabled/unified-dashboard 2>&1 | head -10 || true
echo ""
echo "===== footbreak listen lines ====="
grep -nE "listen|server_name" /etc/nginx/sites-enabled/footbreak 2>&1 | head -10 || true
echo ""
echo "===== other confs listen 80 ====="
grep -lE "listen 80|listen\s+80" /etc/nginx/sites-enabled/* 2>&1
