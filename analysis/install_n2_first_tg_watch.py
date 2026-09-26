"""Install an isolated observer only; never change application or production DB."""
import hashlib,json,os,shutil,subprocess,sys,time
from pathlib import Path

BASE=Path("/opt/crown-radar-v2")
OPS=BASE/"ops/n2-first-tg"
SRC=Path(sys.argv[1])
def run(cmd):
    return subprocess.run(cmd,check=True,capture_output=True,text=True,timeout=60).stdout
files=["server.js","rule_matcher.js","data/rules.json"]
before={f:hashlib.sha256((BASE/f).read_bytes()).hexdigest() for f in files}
rule=next(r for r in json.loads((BASE/"data/rules.json").read_text())["rules"] if r["id"]=="ch-N2")
assert rule["version"]=="N2-AM8-v2" and rule["enabled"]
assert rule["version_effective_at_ms"]==1790417139796
assert not (OPS/"config.json").exists(),"Already installed; do not reset watcher"
OPS.mkdir(parents=True,exist_ok=True);os.chmod(OPS,0o700)
for src,dest in [("n2_first_tg_watch.py","watch.py"),("n2_first_tg_runtime.mjs","runtime.mjs")]:
    shutil.copyfile(SRC/src,OPS/dest);os.chmod(OPS/dest,0o600)
keys=("version","version_effective_at_ms","hour_hkt_min","hour_hkt_max_exclusive","ou_hcp_le","t5_dec_min","enabled")
config={"monitor_chat_id":"703318555","server_sha256":before["server.js"],
        "installed_at":int(time.time()*1000),"rule":{k:rule[k] for k in keys}}
(OPS/"config.json").write_text(json.dumps(config,ensure_ascii=False,indent=2));os.chmod(OPS/"config.json",0o600)
# Read-only getChat verifies private routing; no startup/test send.
chat=json.loads(run(["python3",str(OPS/"watch.py"),"--verify-chat"]))
initial=json.loads(run(["python3",str(OPS/"watch.py")]))
service="""[Unit]
Description=Read-only first natural Crown N2 TG acceptance observer
After=docker.service
[Service]
Type=oneshot
ExecStart=/usr/bin/python3 /opt/crown-radar-v2/ops/n2-first-tg/watch.py
TimeoutStartSec=90
UMask=0077
"""
timer="""[Unit]
Description=Watch first natural N2 TG notification during HKT morning
[Timer]
OnCalendar=*-*-* 07..12:*:00,30 Asia/Hong_Kong
AccuracySec=1s
Persistent=true
Unit=crown-n2-first-tg-watch.service
[Install]
WantedBy=timers.target
"""
run(["systemd-analyze","calendar","*-*-* 07..12:*:00,30 Asia/Hong_Kong"])
for name,content in [("crown-n2-first-tg-watch.service",service),("crown-n2-first-tg-watch.timer",timer)]:
    p=Path("/etc/systemd/system")/name
    assert not p.exists(),f"Existing unrelated unit: {name}"
    p.write_text(content)
run(["systemctl","daemon-reload"])
run(["systemctl","enable","--now","crown-n2-first-tg-watch.timer"])
assert run(["systemctl","is-active","crown-n2-first-tg-watch.timer"]).strip()=="active"
after={f:hashlib.sha256((BASE/f).read_bytes()).hexdigest() for f in files}
assert before==after,"Production files changed"
receipt={"status":"installed","private_chat":chat,"production_files_unchanged":True,
         "initial":initial,"schedule_hkt":"每日07:00至12:59，每30秒檢查；完成首筆後停用自身",
         "timer":run(["systemctl","list-timers","crown-n2-first-tg-watch.timer","--no-pager"]),
         "test_messages_sent":0,"monitor_path":str(OPS),"hashes":before}
(OPS/"install-receipt.json").write_text(json.dumps(receipt,ensure_ascii=False,indent=2))
print(json.dumps(receipt,ensure_ascii=False,indent=2))
