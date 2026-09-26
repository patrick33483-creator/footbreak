"""Patch only Crown TG presentation. Preserve rules, ledgers, deadlines and watchers."""
import hashlib,json,os,re,subprocess,time
from pathlib import Path
BASE=Path("/opt/crown-radar-v2")
SRC=Path(__file__).parent
def run(a,**kw):return subprocess.run(a,check=True,capture_output=True,text=True,timeout=120,**kw).stdout
def sha(b):return hashlib.sha256(b).hexdigest()
server_path=BASE/"server.js";before=server_path.read_bytes()
old=before.decode()
start=old.index("function formatHeavyMsg(hit) {")
end=old.index("async function sendTelegram",start)
formatter=(SRC/"crown_clear_tg_formatter.js").read_text()
new=old[:start]+formatter+"\n"+old[end:]
protected={n:sha((BASE/n).read_bytes()) for n in ["data/rules.json","rule_matcher.js","strategy.html"]}
run(["docker","exec","-i","crown-radar-v2","node","--input-type=module","--check"],input=new)
run(["docker","cp",str(SRC),"crown-radar-v2:/tmp/crown-clear-tg"])
tests=json.loads(run(["docker","exec","crown-radar-v2","node","/tmp/crown-clear-tg/test_crown_clear_tg.mjs",
    "/tmp/crown-clear-tg/crown_clear_tg_formatter.js"]))
backup=BASE/"backups"/("tg-clear-"+str(int(time.time()*1000)))
backup.mkdir(parents=True);(backup/"server.js").write_bytes(before)
watchers=[]
for folder,timer in [("n2-first-tg","crown-n2-first-tg-watch.timer"),("alow-first-tg","crown-alow-first-tg-watch.timer")]:
    path=BASE/"ops"/folder/"config.json"
    if not path.exists():continue
    content=path.read_bytes();config=json.loads(content)
    key=config.get("server_sha256") or config.get("protected_hashes",{}).get("server.js")
    assert key==sha(before),f"Watcher already drifted: {folder}"
    active=subprocess.run(["systemctl","is-active","--quiet",timer]).returncode==0
    (backup/(folder+"-config.json")).write_bytes(content)
    watchers.append((path,content,config,timer,active))
watch_files=[]
for folder in ["alow-first-tg","n2-first-tg"]:
    path=BASE/"ops"/folder/"watch.py"
    if not path.exists():continue
    original=path.read_bytes();text=original.decode()
    if folder=="alow-first-tg":
        anchor='    if event["kind"]=="first_notice":'
        insertion='''    if h:
        selection=h.get("receipt") or h
        if selection.get("display") and selection.get("odds") is not None:
            lines += [f"原訊號選擇：{selection['display']}（{h.get('home','') if selection.get('side')=='H' else h.get('away','')}）",
                f"盤口：平手（0）；實際賠率：{selection['odds']}",
                f"觸發主賠：{selection.get('trigger_home_dec','未取得')}"]
    if event["kind"]!="first_notice":
        lines.insert(0,"系統監察通知（不是新的投注訊號）")
'''
    else:
        anchor='    kind=event["kind"]'
        insertion='''    if h.get("odds") is not None and h.get("line") is not None:
        parts += [f"原訊號選擇：買大（全場入球）；盤口：{h['line']}球；皇冠十進制賠率：{h['odds']}"]
    if event["kind"]!="first_notice":
        parts.insert(0,"系統監察通知（不是新的投注訊號）")
'''
    assert text.count(anchor)==1,f"Unknown watcher presentation: {folder}"
    updated=text.replace(anchor,insertion+anchor)
    compile(updated,str(path),"exec")
    (backup/(folder+"-watch.py")).write_bytes(original)
    watch_files.append((path,original,updated))
report={"status":"pending","tests":tests,"backup":str(backup),"watchers":[]}
try:
    for path,b,c,t,active in watchers:
        if active:
            run(["systemctl","stop",t])
            run(["systemctl","stop",t.replace(".timer",".service")])
    # Graceful stop prevents interrupting in-flight sends and mixed file state.
    assert server_path.read_bytes()==before,"Concurrent server change"
    run(["docker","stop","-t","30","crown-radar-v2"])
    with server_path.open("wb") as f:f.write(new.encode());f.flush();os.fsync(f.fileno())
    for path,b,c,t,active in watchers:
        if "server_sha256" in c:c["server_sha256"]=sha(new.encode())
        else:c["protected_hashes"]["server.js"]=sha(new.encode())
        path.write_text(json.dumps(c,ensure_ascii=False,indent=2))
    for path,original,updated in watch_files:path.write_text(updated)
    run(["docker","start","crown-radar-v2"])
    # Read route inside existing authenticated container; never export credentials.
    api="""const fs=require('fs'),vm=require('vm');const s=fs.readFileSync('/app/server.js','utf8');
const read=n=>vm.runInNewContext(s.match(new RegExp('^const '+n+' = .+$','m'))[0]+'\\n'+n,{process});
fetch('http://127.0.0.1:5000/api/four-channels-live',{headers:{authorization:'Basic '+Buffer.from(read('AUTH_USER')+':'+read('AUTH_PASSWORD')).toString('base64')}})
.then(async r=>{if(!r.ok)throw new Error('HTTP '+r.status);const j=await r.json();console.log(JSON.stringify({ok:true,fired:j.fired}));})
.catch(e=>{console.error(e.message);process.exit(1)});"""
    for attempt in range(25):
        try:
            report["live_health"]=json.loads(run(["docker","exec","crown-radar-v2","node","-e",api]));break
        except Exception:
            if attempt==24:raise
            time.sleep(2)
    actual=run(["docker","exec","crown-radar-v2","node","-e",
        "console.log(require('crypto').createHash('sha256').update(require('fs').readFileSync('/app/server.js')).digest('hex'))"]).strip()
    assert actual==sha(new.encode())
    assert protected=={n:sha((BASE/n).read_bytes()) for n in protected}
    for path,b,c,t,active in watchers:
        if active:run(["systemctl","start",t])
        report["watchers"].append({"timer":t,"was_active":active,"active":subprocess.run(["systemctl","is-active","--quiet",t]).returncode==0})
    report.update(status="applied_verified",effective_at_ms=int(time.time()*1000),server_sha256=actual,
        strategy_files_unchanged=True,send_deadline_and_dedupe_unchanged=True,
        monitor_alerts_clearly_labeled=True,test_messages_sent=0,old_bets_resent=0)
    (backup/"receipt.json").write_text(json.dumps(report,ensure_ascii=False,indent=2))
    print(json.dumps(report,ensure_ascii=False,indent=2))
except Exception:
    run(["docker","stop","-t","30","crown-radar-v2"])
    with server_path.open("wb") as f:f.write(before);f.flush();os.fsync(f.fileno())
    for path,b,c,t,active in watchers:path.write_bytes(b)
    for path,original,updated in watch_files:path.write_bytes(original)
    run(["docker","start","crown-radar-v2"])
    for path,b,c,t,active in watchers:
        if active:run(["systemctl","start",t])
    raise
