#!/usr/bin/env bash
# 徹底揾 conf reverter
set -uo pipefail

echo "===== 1. 目前 conf 內 kwan-v2 定 fb-v2 存唔存在 ====="
for f in /opt/footbreak/deploy/nginx-unified-dashboard.conf \
         /etc/nginx/sites-available/unified-dashboard \
         /etc/nginx/sites-enabled/unified-dashboard; do
    echo "--- $f ---"
    grep -c "kwan-v2\|stage-v2\|fb-v2" "$f" 2>&1
    stat -c "mtime: %y" "$f" 2>&1
done

echo ""
echo "===== 2. 揾邊個 process 最近 open 過個 file for write ====="
# fuser 睇有無現時打開
fuser -v /opt/footbreak/deploy/nginx-unified-dashboard.conf 2>&1 | head -10 || true
lsof /opt/footbreak/deploy/nginx-unified-dashboard.conf 2>&1 | head -10 || true

echo ""
echo "===== 3. 過去 30 分鐘 update.sh / setup.sh 有無被 call ====="
journalctl --since "30 min ago" --no-pager 2>&1 | grep -iE "update\.sh|setup\.sh|nginx-unified" | head -20

echo ""
echo "===== 4. 目前運行中嘅 process 有無 touching update.sh ====="
ps auxf 2>&1 | grep -iE "update\.sh|setup\.sh|shipyard|deploy" | grep -v grep | head -20

echo ""
echo "===== 5. GitHub Actions runner 有無 running ====="
ps auxf 2>&1 | grep -iE "actions-runner|Runner\.Listener" | grep -v grep | head -10
systemctl list-units --all 2>&1 | grep -i "runner\|actions" | head -10

echo ""
echo "===== 6. 用 inotifywait 監視 30 秒睇邊個寫個 file ====="
# 之前見過 install 完成後 file 被覆寫，即係一定有 process 每次 nginx reload 就 install
# inotifywait 就會捕捉到 filename / pid
if ! command -v inotifywait >/dev/null; then
    apt-get install -y inotify-tools >/dev/null 2>&1
fi

# 開 3 個 watcher 併行 30 秒
(timeout 45 inotifywait -m -e modify,create,attrib,close_write \
    /opt/footbreak/deploy/nginx-unified-dashboard.conf \
    /etc/nginx/sites-available/unified-dashboard \
    /etc/nginx/sites-enabled/ \
    2>&1 | tee /tmp/inotify.log) &
IW_PID=$!

# 同步時間，等 40 秒
sleep 42
kill $IW_PID 2>/dev/null || true
wait 2>/dev/null

echo ""
echo "===== 7. 30 秒後個 conf ====="
for f in /opt/footbreak/deploy/nginx-unified-dashboard.conf \
         /etc/nginx/sites-available/unified-dashboard; do
    echo "--- $f ---"
    grep -c "kwan-v2\|stage-v2\|fb-v2" "$f" 2>&1
    stat -c "mtime: %y" "$f" 2>&1
done

echo ""
echo "===== 8. 揾邊個 script 講 'install ... unified-dashboard' ====="
grep -rn "install.*unified-dashboard\|cp.*unified-dashboard\|sites-available/unified-dashboard" /opt/footbreak/ /root/ /etc/systemd/ 2>/dev/null | head -20

echo ""
echo "===== 9. 有無 shipyard / auto-deploy service ====="
find /etc/systemd -name "*.service" -o -name "*.timer" 2>/dev/null | xargs grep -l "footbreak\|update\.sh\|nginx-unified" 2>/dev/null | head -20

echo ""
echo "===== 10. Auth log 睇邊個做過 sudo sh update.sh ====="
grep -iE "update\.sh|setup\.sh|nginx-unified" /var/log/auth.log 2>&1 | tail -20 || echo "no auth.log"

echo ""
echo "===== DONE ====="
