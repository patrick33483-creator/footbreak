"""Authorized A+ production reactivation; no automatic betting or synthetic TG."""
import datetime
import hashlib
import json
import os
from pathlib import Path
import sqlite3
import subprocess
import sys
import time

BASE=Path("/opt/crown-radar-v2")
BUNDLE=Path(sys.argv[1])
P=json.loads((BUNDLE/"alow_activation_payload.json").read_text())
NAMES=["rule_matcher.js","strategy.html","server.js","data/rules.json"]
originals={n:(BASE/n).read_bytes() for n in NAMES}
for n,h in P["expected_sha256"].items():
    assert hashlib.sha256(originals[n]).hexdigest()==h,f"Concurrent modification: {n}"
server=originals["server.js"].decode()
for prefix in ["history","live","formatter","notify","polling"]:
    before,after=P[prefix+"_before"],P[prefix+"_after"]
    assert server.count(before)==1,f"Patch mismatch: {prefix}"
    server=server.replace(before,after,1)
config=json.loads(originals["data/rules.json"])
rule=next(r for r in config["rules"] if r["id"]=="ch-Alow")
assert rule["enabled"] is False,"Already active, refuse resetting version"
rule.update(P["rule_patch"])
def run(args,**kw):
    return subprocess.run(args,check=True,capture_output=True,text=True,timeout=150,**kw)
def api(path,as_json=True):
    code="""
    const fs=require('fs'),vm=require('vm');
    const s=fs.readFileSync('/app/server.js','utf8');
    const read=n=>vm.runInNewContext(s.match(new RegExp('^const '+n+' = .+$','m'))[0]+'\\n'+n,{process});
    const headers={authorization:'Basic '+Buffer.from(read('AUTH_USER')+':'+read('AUTH_PASSWORD')).toString('base64')};
    fetch('http://127.0.0.1:5000'+process.argv[1],{headers,signal:AbortSignal.timeout(25000)})
      .then(async r=>{if(!r.ok)throw new Error('HTTP '+r.status);process.stdout.write(await r.text());})
      .catch(e=>{console.error(e.message);process.exit(1);});
    """
    text=run(["docker","exec","crown-radar-v2","node","-e",code,path]).stdout
    return json.loads(text) if as_json else text
run(["docker","cp",str(BUNDLE),"crown-radar-v2:/tmp/alow-authorized-activation"])
test=json.loads(run(["docker","exec","crown-radar-v2","node",
    "/tmp/alow-authorized-activation/alow_activation_tests.mjs",
    "/tmp/alow-authorized-activation/alow_activation_payload.json"]).stdout)
run(["docker","exec","-i","crown-radar-v2","node","--input-type=module","--check"],input=server)
before_api=api("/api/strategy-2plus-overlap")
backup=BASE/"backups"/("alow-price-dir-v2-"+str(int(time.time()*1000)))
backup.mkdir(parents=True,exist_ok=False)
for n,b in originals.items():
    target=backup/n;target.parent.mkdir(parents=True,exist_ok=True);target.write_bytes(b)
db=sqlite3.connect(f"file:{BASE}/data/crown.db?mode=ro",uri=True)
old_ledger=db.execute("SELECT sid,rule_id,notified_at FROM heavy_notified_rule ORDER BY sid,rule_id").fetchall()
dst=sqlite3.connect(backup/"crown.db");db.backup(dst);dst.close();db.close()
(backup/"pre-history.json").write_text(json.dumps(before_api,ensure_ascii=False))
timer="crown-alow-forward-v1.timer"
timer_was_active=subprocess.run(["systemctl","is-active","--quiet",timer]).returncode==0
watch_path=BASE/"ops/n2-first-tg/config.json"
watch_before=watch_path.read_bytes() if watch_path.exists() else None
watch_timer="crown-n2-first-tg-watch.timer"
watch_was_active=subprocess.run(["systemctl","is-active","--quiet",watch_timer]).returncode==0
if watch_before:
    watch_config=json.loads(watch_before)
    assert watch_config["server_sha256"]==P["expected_sha256"]["server.js"],"N2 watcher already drifted"
    n2=next(r for r in config["rules"] if r["id"]=="ch-N2")
    assert all(n2.get(k)==v for k,v in watch_config["rule"].items())
    (backup/"n2-watcher-config.json").write_bytes(watch_before)
started=False
try:
    # Stop only the superseded isolated observer; retain all its data.
    run(["systemctl","disable","--now",timer])
    run(["systemctl","stop","crown-alow-forward-v1.service"])
    if watch_was_active:
        run(["systemctl","stop",watch_timer])
        run(["systemctl","stop","crown-n2-first-tg-watch.service"])
    # Stop production briefly so code/config cannot be consumed half-written.
    run(["docker","stop","-t","20","crown-radar-v2"]);started=True
    now=int(time.time()*1000)
    rule["version_effective_at_ms"]=now
    rule["version_effective_at"]=datetime.datetime.fromtimestamp(now/1000,datetime.timezone.utc).isoformat()
    for r in config["rules"]:
        if r["id"]!="ch-Alow":
            assert r==next(x for x in P["original_config"]["rules"] if x["id"]==r["id"])
    newfiles={"rule_matcher.js":P["matcher"].encode(),"strategy.html":P["html"].encode(),
        "server.js":server.encode(),"data/rules.json":(json.dumps(config,ensure_ascii=False,indent=2)+"\n").encode()}
    for n,b in newfiles.items():
        with (BASE/n).open("wb") as f:f.write(b);f.flush();os.fsync(f.fileno())
    if watch_before:
        # N2 send/ledger contract is unchanged and regression-tested above.
        # Update only the audited server hash so the existing first-TG watcher continues.
        watch_config["server_sha256"]=hashlib.sha256(newfiles["server.js"]).hexdigest()
        watch_path.write_text(json.dumps(watch_config,ensure_ascii=False,indent=2))
    run(["docker","start","crown-radar-v2"])
    for attempt in range(30):
        try:
            history=api("/api/strategy-2plus-overlap")
            live=api("/api/four-channels-live");break
        except Exception:
            if attempt==29:raise
            time.sleep(2)
    assert history.get("ok") is not False
    stats=history["summary"]["by_channel"]
    assert "A+低盤（舊版已停用）" in stats
    # Other channels retain every existing result; natural new results are allowed.
    after_other={(g["sid"],g["channel"]):g for g in history["games"] if g["rule_id"]!="ch-Alow"}
    for g in before_api["games"]:
        if g["rule_id"]!="ch-Alow":
            assert after_other.get((g["sid"],g["channel"]))==g,"Unrelated history changed"
    assert api("/strategy.html",False)==P["html"]
    actual=json.loads(run(["docker","exec","crown-radar-v2","node","--input-type=module","-e",
        "import fs from 'fs';import crypto from 'crypto';console.log(JSON.stringify(Object.fromEntries("
        "['rule_matcher.js','strategy.html','server.js','data/rules.json'].map(n=>[n,crypto.createHash('sha256').update(fs.readFileSync('/app/'+n)).digest('hex')]))));"]).stdout)
    assert actual=={n:hashlib.sha256(b).hexdigest() for n,b in newfiles.items()}
    db=sqlite3.connect(f"file:{BASE}/data/crown.db?mode=ro",uri=True)
    ledger=db.execute("SELECT sid,rule_id,notified_at FROM heavy_notified_rule ORDER BY sid,rule_id").fetchall()
    assert set(old_ledger).issubset(set(ledger)),"Existing notification ledger changed"
    assert db.execute("SELECT COUNT(*) FROM alow_notification_receipts WHERE notified_at<?",(now,)).fetchone()[0]==0
    receipts=db.execute("SELECT sid,version,notified_at,payload_json FROM alow_notification_receipts").fetchall();db.close()
    assert run(["docker","exec","crown-radar-v2","node","-e",
        "console.log(Boolean(process.env.TELEGRAM_BOT_TOKEN && process.env.TELEGRAM_CHAT_ID))"]).stdout.strip()=="true"
    assert subprocess.run(["systemctl","is-active","--quiet",timer]).returncode!=0
    assert subprocess.run(["systemctl","is-enabled","--quiet",timer]).returncode!=0
    if watch_was_active:
        run(["systemctl","start",watch_timer])
        assert subprocess.run(["systemctl","is-active","--quiet",watch_timer]).returncode==0
    receipt={"status":"applied_verified","activation_ms":now,"activation_utc":rule["version_effective_at"],
        "backup":str(backup),"rule":rule,"tests":test,"hashes":actual,"new_alow_stats":stats["A+低盤"],
        "old_alow_stats":stats["A+低盤（舊版已停用）"],"new2_unchanged":True,
        "new2_first_tg_watcher_preserved":watch_was_active,
        "independent_observer_stopped":True,"independent_observer_data_retained":True,
        "old_ledger_retained":len(old_ledger),"notification_configured":True,"synthetic_TG_sent":0,
        "alow_receipts":receipts,"post_history":history,"post_live":live}
    (backup/"receipt.json").write_text(json.dumps(receipt,ensure_ascii=False,indent=2))
    print(json.dumps(receipt,ensure_ascii=False,indent=2))
except Exception:
    if started:
        run(["docker","stop","-t","20","crown-radar-v2"])
        for n,b in originals.items():
            with (BASE/n).open("wb") as f:f.write(b);f.flush();os.fsync(f.fileno())
        if watch_before:watch_path.write_bytes(watch_before)
        run(["docker","start","crown-radar-v2"])
    if timer_was_active:run(["systemctl","enable","--now",timer])
    if watch_was_active:run(["systemctl","start",watch_timer])
    print(json.dumps({"status":"rolled_back","backup":str(backup)}))
    raise
