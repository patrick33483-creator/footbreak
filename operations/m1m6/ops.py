"""Scoped production migration. Does not print credentials or delete history."""
import hashlib
import json
import os
import re
from pathlib import Path
import shutil
import subprocess
import sys
import time

BASE=Path("/var/lib/crown-m1m6")
RADAR=Path("/opt/crown-radar-v2")
LEGACY=["crown-goldpool-notify","u1-notify","ce-notify"]
RULEFILES=[RADAR/"data/rules.json",RADAR/"rules.json"]
def run(args,check=False):
    p=subprocess.run(args,capture_output=True,text=True,timeout=90)
    if check and p.returncode:
        raise RuntimeError(f"command failed: {args[0:3]} exit={p.returncode}")
    return {"returncode":p.returncode,"stdout":p.stdout[-100000:],"stderr":p.stderr[-2000:]}
def atomic(p,obj):
    p=Path(p)
    p.parent.mkdir(parents=True,exist_ok=True)
    tmp=p.with_suffix(p.suffix+".tmp")
    tmp.write_text(json.dumps(obj,ensure_ascii=False,indent=2))
    os.replace(tmp,p)
def env(p):
    out={}
    for line in Path(p).read_text().splitlines():
        if "=" in line and not line.lstrip().startswith("#"):
            k,v=line.split("=",1)
            out[k.strip()]=v.strip().strip("'\"")
    return out
def inspect():
    files={}
    for p in [RADAR/"docker-compose.yml",RADAR/"heavy.html",RADAR/"strategy.html",
              Path("/var/www/crownsystem-v3/strategy.html"),Path("/var/www/crownsystem-v3/heavy.html")]:
        if p.exists():
            content=p.read_text()
            if "compose" in p.name:
                content=re.sub(r'(?im)^(\s*[A-Z_]*(?:TOKEN|SECRET|PASSWORD|KEY)[A-Z_]*\s*:\s*).+$',r'\1"[REDACTED]"',content)
            files[str(p)]={"content":content,"sha256":hashlib.sha256(p.read_bytes()).hexdigest()}
    services={name:run(["systemctl","show",name+".service","-p","ExecStart","-p","ActiveState","-p","UnitFileState"]) for name in LEGACY+["crown-m1m6"]}
    f=env("/etc/footbreak.env")
    c=env(str(RADAR/".env"))
    creds={"footbreak_bot_configured":bool(f.get("TELEGRAM_BOT_TOKEN")),"footbreak_chat_id":f.get("TELEGRAM_CHAT_ID"),
           "radar_bot_configured":bool(c.get("TELEGRAM_BOT_TOKEN")),"radar_chat_id":c.get("TELEGRAM_CHAT_ID"),
           "same_bot":f.get("TELEGRAM_BOT_TOKEN")==c.get("TELEGRAM_BOT_TOKEN")}
    rules={str(p):json.loads(p.read_text()) for p in RULEFILES if p.exists()}
    paths=run(["bash","-lc","grep -rlE 'crown-goldpool-notify|u1-notify|ce-notify|full_sweep|writeRulesFile' /etc/cron* /etc/systemd/system /usr/local/bin 2>/dev/null || true"])
    return {"summary":{"action":"inspect","at_ms":int(time.time()*1000),"credentials":creds},
            "services":services,"files":files,"rules":rules,"related_paths":paths}
def pause_legacy():
    BASE.mkdir(parents=True,exist_ok=True)
    backup=BASE/"legacy-backup"
    backup.mkdir(exist_ok=True)
    for p in RULEFILES:
        dest=backup/("data-rules.json" if p.parent.name=="data" else "root-rules.json")
        if not dest.exists():
            shutil.copy2(p,dest)
    for name in LEGACY:
        p=backup/(name+"-unit.txt")
        if not p.exists():
            p.write_text(run(["systemctl","cat",name+".service",name+".timer"])["stdout"])
        run(["systemctl","disable","--now",name+".timer"],True)
        run(["systemctl","stop",name+".service"],True)
        # Persistent drop-in guards avoid conflicts with existing unit files.
        d=Path("/etc/systemd/system")/(name+".service.d")
        d.mkdir(exist_ok=True)
        (d/"90-m1m6-retired.conf").write_text("[Unit]\nConditionPathExists=/var/lib/crown-m1m6/ALLOW_RETIRED_LEGACY\n")
    run(["systemctl","daemon-reload"],True)
    for p in RULEFILES:
        atomic(p,{"version":"M1M6-migration-paused","generated_at_ms":int(time.time()*1000),
                  "note":"Old active rules archived; history retained. New fixed rules pending installation.",
                  "rules":[]})
    atomic(BASE/"migration.json",{"phase":"legacy_paused","paused_at_ms":int(time.time()*1000),
                                 "backup_dir":str(backup),"history_deleted":False})
    return {"summary":{"action":"pause_legacy","old_rules_active":0,"backup_dir":str(backup),
                       "legacy_timers":{n:run(["systemctl","is-active",n+".timer"])["stdout"].strip() for n in LEGACY},
                       "history_deleted":False}}
def main():
    action=sys.argv[1]
    if action=="inspect":
        result=inspect()
    elif action=="pause_legacy":
        result=pause_legacy()
    elif action=="install":
        from install import install
        result=install()
    elif action=="verify":
        from install import verify
        result=verify()
    elif action=="audit":
        from audit import audit
        result=audit()
    elif action=="inspect_hkjc":
        from audit import inspect_hkjc
        result=inspect_hkjc()
    elif action=="stop_hkjc":
        from stop_hkjc import stop_hkjc
        result=stop_hkjc()
    elif action=="verify_hkjc":
        from stop_hkjc import verify_hkjc
        result=verify_hkjc()
    elif action=="diagnose_results":
        from result_diag import diagnose
        result=diagnose()
    elif action=="upgrade_batch":
        from upgrade_batch import upgrade
        result=upgrade()
    elif action=="upgrade_nolock":
        from upgrade_nolock import upgrade
        result=upgrade()
    elif action=="dynamic_inspect":
        from dynamic_ops import inspect
        result=inspect()
    elif action=="dynamic_preflight":
        from dynamic_ops import preflight
        result=preflight()
    elif action=="dynamic_deploy":
        from dynamic_ops import deploy
        result=deploy()
    elif action=="dynamic_refresh_test":
        from dynamic_ops import refresh_test
        result=refresh_test()
    elif action=="results_preflight":
        from repair_results import preflight
        result=preflight()
    elif action=="repair_results":
        from repair_results import install_fix
        result=install_fix()
    elif action=="examples":
        from examples import examples
        result=examples()
    else:
        raise ValueError("Unknown action")
    print(json.dumps(result,ensure_ascii=False))
if __name__=="__main__":
    main()
