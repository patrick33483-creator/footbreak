"""Three-hour/manual result sync and exhaustive discovery. Never sends Telegram."""
import fcntl
import hashlib
import json
from pathlib import Path
import subprocess
import time
import notifier
from dynamic_rules import universe,scan,merge,registry,GRAMMAR,seeds,matched_rows,signature
from policy import gates

STATE=Path("/var/lib/crown-m1m6")
REGISTRY=STATE/"registry.json"


def progress(phase,**extra):
    data={"phase":phase,"at":int(time.time()*1000),**extra}
    notifier.atomic(STATE/"research_status.json",data)
    for p in [Path("/var/www/crownsystem-v3/research_status.json"),
              Path("/opt/crown-radar-v2/data/research_status.json")]:
        notifier.atomic(p,data)


def compute(now,previous):
    rows,reasons=universe(notifier.source(now),now)
    found,counts=scan(rows)
    # Existing merged OR strategies are also candidates in their own right:
    # they need not have a freshly qualifying individual grid leaf to remain active.
    existing_pass=0
    for s in previous.get("strategies",seeds()):
        g=gates([r for r in matched_rows(rows,s) if r["result"]])
        if g["pass"]:
            existing_pass+=1
            found.append({"market":s["market"],"side":s["side"],"clauses":s["clauses"],
                          "fingerprint":signature(s["market"]+"_"+s["side"],s["clauses"]),"gate":g})
    counts["existing_rechecked"]=len(previous.get("strategies",seeds()))
    counts["existing_passing"]=existing_pass
    groups=merge(found,rows)
    result=registry(groups,rows,previous,now)
    result["search"]={**counts,"merged_active":len(groups),"rows":len(rows),
                      "settled_rows":sum(bool(r["result"]) for r in rows),
                      "pending_rows":sum(not r["result"] for r in rows),"quality":reasons,
                      "grammar":GRAMMAR,"source_digest":hashlib.sha256(
                          json.dumps(rows,sort_keys=True,ensure_ascii=False).encode()).hexdigest()}
    return result


def cycle(sync=True):
    started=int(time.time()*1000)
    with (STATE/"research.lock").open("a") as lock:
        try:
            fcntl.flock(lock,fcntl.LOCK_EX|fcntl.LOCK_NB)
        except BlockingIOError:
            return {"busy":True}
        try:
            progress("核對賽果",started_at=started)
            if sync:
                p=subprocess.run(["systemctl","start","crown-strategy-results.service"],
                                 capture_output=True,timeout=115)
                sync_result="completed" if p.returncode==0 else "provider_partial_or_failed"
            else:
                sync_result="not_requested"
            now=int(time.time()*1000)
            progress("搜尋全部組合及重算現有策略",started_at=started)
            previous=json.loads(REGISTRY.read_text()) if REGISTRY.exists() else {}
            result=compute(now,previous)
            result["next_search_at"]=started+3*3600000
            result["result_sync"]=sync_result
            progress("合併核驗並更新策略版本",started_at=started)
            # Registry changes and sends cannot interleave. No deletion of any lock.
            with (STATE/"run.lock").open("a") as ticklock:
                fcntl.flock(ticklock,fcntl.LOCK_EX)
                archive=STATE/"research_history"
                archive.mkdir(exist_ok=True)
                notifier.atomic(archive/f"{now}.json",result)
                notifier.atomic(REGISTRY,result)
            progress("完成",started_at=started,completed_at=int(time.time()*1000),
                     next_search_at=result["next_search_at"],search=result["search"],
                     result_sync=sync_result)
            return result
        except Exception as exc:
            progress("失敗，保留上次策略及全部封鎖",started_at=started,error_type=type(exc).__name__)
            raise


if __name__=="__main__":
    r=cycle()
    print(json.dumps({"search":r.get("search"),"updated_at":r.get("updated_at"),"busy":r.get("busy",False)},ensure_ascii=False))
