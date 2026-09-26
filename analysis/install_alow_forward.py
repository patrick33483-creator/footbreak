"""Install isolated durable forward observer. No app restart, no production DB writes."""
import hashlib
import json
import os
import shutil
import subprocess
import sys
import time
from pathlib import Path
from alow_forward import BANDS,RULE_FIELDS,hkt

ROOT=Path("/opt/crown-radar-v2")
OPS=ROOT/"ops/alow-forward-v1"
SRC=Path(__file__).parent
TIMER="crown-alow-forward-v1.timer"
SERVICE="crown-alow-forward-v1.service"
def run(cmd):
    return subprocess.run(cmd,check=True,capture_output=True,text=True,timeout=100).stdout
def sha(p):return hashlib.sha256(p.read_bytes()).hexdigest()
protected=["server.js","rule_matcher.js","data/rules.json"]
before={f:sha(ROOT/f) for f in protected}
config=json.loads((ROOT/"data/rules.json").read_text())
rule=next(r for r in config["rules"] if r["id"]=="ch-Alow")
assert all(rule.get(k)==v for k,v in RULE_FIELDS.items()),"unexpected original A+ terms"
assert rule["enabled"] is False,"A+ production unexpectedly enabled"
n2=next(r for r in config["rules"] if r["id"]=="ch-N2")
before_container=run(["docker","inspect","--format","{{.State.StartedAt}}","crown-radar-v2"]).strip()
unitpaths=[Path("/etc/systemd/system")/name for name in [SERVICE,TIMER]]
if (OPS/"config.json").exists():
    assert sha(OPS/"alow_forward.py")==sha(SRC/"alow_forward.py"),"Existing version differs; refuse overwrite"
    print(json.dumps({"status":"already_installed","config":json.loads((OPS/"config.json").read_text()),
          "latest":json.loads((OPS/"status.json").read_text()),"timer":run(["systemctl","is-active",TIMER]).strip()},ensure_ascii=False))
    sys.exit(0)
assert not any(p.exists() for p in unitpaths),"unit already exists"
OPS.mkdir(parents=True,exist_ok=True);os.chmod(OPS,0o700)
for name in ["alow_forward.py","test_alow_forward.py"]:
    shutil.copyfile(SRC/name,OPS/name);os.chmod(OPS/name,0o600)
shutil.copyfile(ROOT/"rule_matcher.js",OPS/"original_matcher_frozen.mjs")
# Freeze only approved terms and the full original rule for audit, never editing production config.
effective=int(time.time()*1000)
frozen={"version":"ALOW-PRICE-FORWARD-v1","effective_at_ms":effective,"effective_at_hkt":hkt(effective),
 "bands":{k:{"min_inclusive":v[0]/1000,"max_exclusive":v[1]/1000} for k,v in BANDS.items()},
 "original_rule":rule,"source":"crown_snapshots","market":"AH","side":"home","stake_units":1,
 "entry":"first eligible pre-kickoff observation; both T5 AH/OU captured after effective time; no retrospective entry",
 "dedupe":"one sid across both bands; first accepted T5 fixes cohort and price",
 "formal_TG":False,"historical_159_included":False,"required_T5_max_age_minutes_before_kickoff":8,
 "strict_six":"sensitivity only, not main entry filter","production_hashes_at_install":before,
 "tracker_sha256":sha(OPS/"alow_forward.py")}
(OPS/"config.json").write_text(json.dumps(frozen,ensure_ascii=False,indent=2));os.chmod(OPS/"config.json",0o600)
service=f"""[Unit]
Description=Frozen A+ two price-band forward shadow observer
After=docker.service
[Service]
Type=oneshot
ExecStart=/usr/bin/python3 {OPS}/alow_forward.py
TimeoutStartSec=90
UMask=0077
Nice=10
"""
timer=f"""[Unit]
Description=A+ two independent forward cohorts, every 30 seconds
[Timer]
OnCalendar=*-*-* *:*:00,30
AccuracySec=1s
Persistent=true
Unit={SERVICE}
[Install]
WantedBy=timers.target
"""
run(["systemd-analyze","calendar","*-*-* *:*:00,30"])
initial=json.loads(run(["python3",str(OPS/"alow_forward.py")]))
for p,content in zip(unitpaths,[service,timer]):p.write_text(content)
run(["systemctl","daemon-reload"])
run(["systemctl","enable","--now",TIMER])
assert run(["systemctl","is-active",TIMER]).strip()=="active"
after={f:sha(ROOT/f) for f in protected}
assert before==after,"production code/config changed"
after_container=run(["docker","inspect","--format","{{.State.StartedAt}}","crown-radar-v2"]).strip()
assert before_container==after_container,"production restarted"
receipt={"status":"installed","effective_at_hkt":hkt(effective),"config":frozen,"initial":initial,
 "production_unchanged":before==after,"container_not_restarted":True,
 "Aplus_production_enabled":rule["enabled"],"new2_unchanged":True,
 "new2_version":n2.get("version"),"new2_enabled":n2["enabled"],"TG_messages_sent":0,
 "timer":run(["systemctl","list-timers",TIMER,"--no-pager"]),"ops_path":str(OPS),
 "stop_command":f"systemctl disable --now {TIMER}","status_command":f"python3 {OPS}/alow_forward.py --status"}
(OPS/"install-receipt.json").write_text(json.dumps(receipt,ensure_ascii=False,indent=2))
print(json.dumps(receipt,ensure_ascii=False,indent=2))
