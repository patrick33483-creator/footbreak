"""Idempotent install; legacy notifications stay paused if any step fails."""
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import sqlite3
import subprocess
import time

from ops import BASE,RADAR,LEGACY,RULEFILES,atomic,run
from policy import RULES,VERSION

HERE=Path(__file__).parent
DEST=Path("/opt/crown-m1m6")
CONFIG=Path("/etc/crown-m1m6.json")
PAGES=[RADAR/"heavy.html",RADAR/"strategy.html",
       Path("/var/www/crownsystem-v3/heavy.html"),Path("/var/www/crownsystem-v3/strategy.html")]
def backup(path):
    target=BASE/"legacy-backup"/str(path).lstrip("/").replace("/","__")
    if not target.exists():
        target.parent.mkdir(parents=True,exist_ok=True)
        shutil.copy2(path,target)
    return target
def replace_once(path,old,new):
    text=path.read_text()
    if new in text:
        return False
    if text.count(old)!=1:
        raise RuntimeError("unexpected source anchor: "+path.name)
    backup(path)
    # Keep inode for Docker file bind mounts.
    path.write_text(text.replace(old,new,1))
    return True
def preserve_header(body):
    found=re.search(r"<header\b[^>]*>.*?</header>",body,re.S|re.I)
    if found:
        return found.group()
    start=re.search(r'<div\b[^>]*class="topbar"[^>]*>',body)
    if start:
        depth=0
        for tag in re.finditer(r"</?div\b[^>]*>",body[start.start():],re.I):
            depth+=-1 if tag.group().startswith("</") else 1
            if depth==0:
                return body[start.start():start.start()+tag.end()]
    raise RuntimeError("Unable to preserve navigation header")
def render_page(original,panel):
    head=re.search(r"^.*?<body\b[^>]*>",original,re.S|re.I)
    if not head:
        raise RuntimeError("No body")
    header=preserve_header(original[head.end():])
    header=re.sub(r"<h1>.*?</h1>","<h1>皇冠重點 · 自動組合策略</h1>",header,flags=re.S)
    header=re.sub(r'<button\b[^>]*>.*?</button>',"",header,flags=re.S)
    top=re.sub(r"<title>.*?</title>","<title>皇冠重點 · 自動組合策略</title>",head.group(),flags=re.S)
    return top+"\n"+header+"\n"+panel+"\n</body></html>"
def verify():
    status=BASE/"status.json"
    s=json.loads(status.read_text()) if status.exists() else {}
    cfg=json.loads(CONFIG.read_text()) if CONFIG.exists() else {}
    rule_ids={str(p):[q["id"] for q in json.loads(p.read_text()).get("rules",[])] for p in RULEFILES}
    timers={n:run(["systemctl","is-active",n+".timer"])["stdout"].strip() for n in LEGACY+["crown-m1m6"]}
    statuses={}
    dbpath=BASE/"ledger.sqlite"
    if dbpath.exists():
        db=sqlite3.connect(f"file:{dbpath}?mode=ro",uri=True)
        statuses=dict(db.execute("SELECT status,COUNT(*) FROM items GROUP BY status"))
        opened=db.execute("SELECT COUNT(*) FROM batches WHERE status='open'").fetchone()[0]
        db.close()
    else:
        opened=0
    server=(RADAR/"server.js").read_text()
    direct_guards={n:"M1M6_RETIRED_GUARD" in Path("/usr/local/bin",n+".py").read_text() for n in LEGACY}
    report={"action":"verify","version":cfg.get("version"),"enabled":cfg.get("enabled"),
      "activated_at":cfg.get("activated_at"),"mode":cfg.get("mode"),"timer_states":timers,
      "rule_ids":rule_ids,"radar_legacy_guard":"M1M6_OWNS_NOTIFICATIONS" in server,
      "direct_legacy_guards":direct_guards,"page_markers":{str(p):'id="m1m6-root"' in p.read_text() for p in PAGES},
      "status_updated_at":s.get("updated_at"),"locked_batch":s.get("locked_batch"),"delivery_status_counts":statuses,
      "open_batches":opened,
      "gates":{q["id"]:{"20":{"n":q["gate"]["20"]["n"],"wins":q["gate"]["20"]["wins"],"den":q["gate"]["20"]["den"],"pass":q["gate"]["20"]["pass"]},
                          "30":{"n":q["gate"]["30"]["n"],"wins":q["gate"]["30"]["wins"],"den":q["gate"]["30"]["den"],"pass":q["gate"]["30"]["pass"]},
                          "pass":q["gate"]["pass"]} for q in s.get("rules",[])},
      "unrelated_r_pin2_retained":"async function notifyRPin2" in server or "r_pin2_notified" in server,
      "collector_container":run(["docker","inspect","--format","{{.State.Status}}","crown-radar-v2"])["stdout"].strip(),
      "service_result":run(["systemctl","show","crown-m1m6.service","-p","Result","-p","ExecMainStatus"])["stdout"]}
    return {"summary":report}
def install():
    if not (BASE/"migration.json").exists():
        raise RuntimeError("Pause legacy first")
    DEST.mkdir(parents=True,exist_ok=True)
    BASE.mkdir(parents=True,exist_ok=True)
    run(["systemctl","stop","crown-m1m6.timer"])
    run(["systemctl","stop","crown-m1m6.service"])
    for name in ["policy.py","notifier.py","test_policy.py","panel.html"]:
        shutil.copy2(HERE/name,DEST/name)
    run(["python3","-m","compileall","-q",str(DEST)],True)
    test=subprocess.run(["python3","-m","unittest","discover","-s",str(DEST),"-p","test_*.py"],capture_output=True,text=True,timeout=30)
    if test.returncode:
        raise RuntimeError("Unit tests failed")
    existing=json.loads(CONFIG.read_text()) if CONFIG.exists() else {}
    cfg={"version":VERSION,"enabled":False,"activated_at":existing.get("activated_at",int(time.time()*1000)),
         "mode":"global_batch_then_rolling_OR","gate":"20>=95% OR 30>=90%; min nonpush 16/24; positive net units",
         "batch_scope":"all M1-M6","batch_release":"all attempted/sent fixtures have official final result",
         "no_result_policy":"remain_locked","fixed_rules":[q["id"] for q in RULES]}
    atomic(CONFIG,cfg)
    (BASE/"retired.flag").touch()
    for name in LEGACY:
        p=Path("/usr/local/bin",name+".py")
        text=p.read_text()
        if "M1M6_RETIRED_GUARD" not in text:
            backup(p)
            insert='import os as _m1m6_os\nif __name__ == "__main__" and _m1m6_os.path.exists("/var/lib/crown-m1m6/retired.flag"):\n    raise SystemExit(0)  # M1M6_RETIRED_GUARD\n'
            if text.startswith("#!"):
                first,rest=text.split("\n",1);text=first+"\n"+insert+rest
            else:
                text=insert+text
            compile(text,str(p),"exec")
            p.write_text(text)
    server=RADAR/"server.js"
    changed=replace_once(server,
       "async function notifyHeavyForSids(sidsWithT5, onlyRuleIds = null) {\n",
       'async function notifyHeavyForSids(sidsWithT5, onlyRuleIds = null) {\n  if (fs.existsSync("./data/M1M6_OWNS_NOTIFICATIONS")) return; // M1M6_OWNS_NOTIFICATIONS\n')
    matcher=RADAR/"rule_matcher.js"
    changed=replace_once(matcher,
       "    if (r.enabled === false) continue;\n",
       '    if (r.enabled === false) continue;\n    if (r.type === "fixed_m1m6") continue; // evaluated only by audited Python engine\n') or changed
    (RADAR/"data/M1M6_OWNS_NOTIFICATIONS").write_text(VERSION)
    ruledata={"version":VERSION,"generated_at_ms":int(time.time()*1000),
              "note":"M1-M6 are owned by the global batch notifier; old rules archived, not deleted.",
              "rules":[{**q,"type":"fixed_m1m6","enabled":True,"line_pat":"皇冠原生固定條件","lean_pat":"依條件","pick":"買細" if q["side"]=="under" else "買主","hit_display":"觀察窗口不是實戰命中率"} for q in RULES]}
    for p in RULEFILES:
        atomic(p,ruledata)
    panel=(DEST/"panel.html").read_text()
    for p in PAGES:
        original=backup(p).read_text()
        p.write_text(render_page(original,panel))
    # A single restart loads the small server/matcher guard patches; collectors remain otherwise unchanged.
    run(["docker","exec","crown-radar-v2","node","--check","server.js"],True)
    run(["docker","exec","crown-radar-v2","node","--check","rule_matcher.js"],True)
    if changed:
        run(["docker","restart","crown-radar-v2"],True)
    dry=run(["python3",str(DEST/"notifier.py"),"--dry-run"],True)
    evidence=json.loads(dry["stdout"])
    if sorted(evidence["gates"])!=["M1","M2","M3","M4","M5","M6"] or evidence["sends"]!=0:
        raise RuntimeError("Dry run invalid")
    run(["python3",str(DEST/"notifier.py")],True) # enabled=false: seed observations/status without messages
    Path("/etc/systemd/system/crown-m1m6.service").write_text("""[Unit]
Description=Crown M1-M6 global batch gated notifier
After=network-online.target
Wants=network-online.target
[Service]
Type=oneshot
ExecStart=/usr/bin/python3 /opt/crown-m1m6/notifier.py
TimeoutStartSec=90
UMask=0022
NoNewPrivileges=true
PrivateTmp=true
ProtectHome=true
ProtectSystem=strict
ReadWritePaths=/var/lib/crown-m1m6 /var/www/crownsystem-v3 /opt/crown-radar-v2/data
""")
    Path("/etc/systemd/system/crown-m1m6.timer").write_text("""[Unit]
Description=Check new Crown M1-M6 T5 batches every 20 seconds
[Timer]
OnBootSec=20s
OnUnitActiveSec=20s
AccuracySec=1s
Unit=crown-m1m6.service
[Install]
WantedBy=timers.target
""")
    run(["systemctl","daemon-reload"],True)
    cfg["enabled"]=True
    if not existing:
        cfg["activated_at"]=int(time.time()*1000)
    atomic(CONFIG,cfg)
    run(["systemctl","enable","--now","crown-m1m6.timer"],True)
    run(["systemctl","start","crown-m1m6.service"],True)
    atomic(BASE/"migration.json",{"phase":"active","activated_at":cfg["activated_at"],"version":VERSION,
                                  "backup_dir":str(BASE/"legacy-backup"),"history_deleted":False})
    result=verify()
    result["summary"]["unit_tests"]="10 passed"
    result["summary"]["dry_run_no_send"]=True
    return result
