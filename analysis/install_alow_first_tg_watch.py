"""Install only isolated read-only monitoring; production stays untouched."""
import hashlib,json,os,shutil,subprocess,time
from pathlib import Path
BASE=Path("/opt/crown-radar-v2")
OPS=BASE/"ops/alow-first-tg"
SRC=Path(__file__).parent
TIMER="crown-alow-first-tg-watch.timer"
SERVICE="crown-alow-first-tg-watch.service"
def run(cmd):return subprocess.run(cmd,check=True,capture_output=True,text=True,timeout=100).stdout
def sha(p):return hashlib.sha256(p.read_bytes()).hexdigest()
protected=["server.js","rule_matcher.js","data/rules.json","strategy.html"]
before={n:sha(BASE/n) for n in protected}
container_before=run(["docker","inspect","--format","{{.State.StartedAt}}","crown-radar-v2"]).strip()
rule=next(r for r in json.loads((BASE/"data/rules.json").read_text())["rules"] if r["id"]=="ch-Alow")
assert rule["enabled"] and rule["version"]=="ALOW-PRICE-DIR-v2"
assert rule["version_effective_at_ms"]==1790420923226
if (OPS/"config.json").exists():
    assert sha(OPS/"watch.py")==sha(SRC/"alow_first_tg_watch.py"),"Existing monitor differs; no overwrite"
    print(json.dumps({"status":"already_installed","state":json.loads(run(["python3",str(OPS/"watch.py"),"--status"]))},ensure_ascii=False))
    raise SystemExit
assert not any((Path("/etc/systemd/system")/n).exists() for n in [TIMER,SERVICE])
OPS.mkdir(parents=True);os.chmod(OPS,0o700)
for src,dest in [("alow_first_tg_watch.py","watch.py"),("alow_first_tg_runtime.mjs","runtime.mjs")]:
    shutil.copyfile(SRC/src,OPS/dest);os.chmod(OPS/dest,0o600)
config={"monitor_chat_id":"703318555","installed_at":int(time.time()*1000),"rule":rule,
    "protected_hashes":{n:before[n] for n in ["server.js","rule_matcher.js"]}}
(OPS/"config.json").write_text(json.dumps(config,ensure_ascii=False,indent=2));os.chmod(OPS/"config.json",0o600)
chat=json.loads(run(["python3",str(OPS/"watch.py"),"--verify-chat"]))
service=f"""[Unit]
Description=Read-only first natural Crown Aplus TG acceptance monitor
After=docker.service
[Service]
Type=oneshot
ExecStart=/usr/bin/python3 {OPS}/watch.py
TimeoutStartSec=90
UMask=0077
Nice=10
"""
timer=f"""[Unit]
Description=First natural Aplus directional TG monitor every 30 seconds
[Timer]
OnCalendar=*-*-* *:*:00,30
AccuracySec=1s
Persistent=true
Unit={SERVICE}
[Install]
WantedBy=timers.target
"""
for n,content in [(SERVICE,service),(TIMER,timer)]: (Path("/etc/systemd/system")/n).write_text(content)
run(["systemctl","daemon-reload"])
initial=json.loads(run(["python3",str(OPS/"watch.py")]))
if not initial.get("completed"):
    run(["systemctl","enable","--now",TIMER])
    assert run(["systemctl","is-active",TIMER]).strip()=="active"
after={n:sha(BASE/n) for n in protected}
assert before==after,"Production files changed"
assert container_before==run(["docker","inspect","--format","{{.State.StartedAt}}","crown-radar-v2"]).strip(),"Container restarted"
report={"status":"installed","initial":initial,"private_chat":chat,"production_unchanged":True,
    "container_not_restarted":True,"ops_path":str(OPS),"schedule":"每30秒；首筆完成後停止",
    "timer":run(["systemctl","list-timers",TIMER,"--no-pager"]),"test_messages_sent":0,
    "activation_ms":rule["version_effective_at_ms"],"hashes":before}
(OPS/"install-receipt.json").write_text(json.dumps(report,ensure_ascii=False,indent=2))
print(json.dumps(report,ensure_ascii=False,indent=2))
