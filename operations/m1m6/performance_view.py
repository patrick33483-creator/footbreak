"""Read-only full-ledger performance since an explicit, persisted reset boundary."""
import datetime as dt
import json
import math
from pathlib import Path
from zoneinfo import ZoneInfo

VERSION="notification-performance-period-v1"
EPOCH_PATH=Path("/var/lib/crown-m1m6/performance_epoch.json")
HKT=ZoneInfo("Asia/Hong_Kong")

def load_epoch():
    return json.loads(EPOCH_PATH.read_text()) if EPOCH_PATH.exists() else None

def day(ms):
    return dt.datetime.fromtimestamp(ms/1000,HKT).strftime("%Y-%m-%d")

def bucket():
    return {"notified":0,"settled":0,"pending":0,"W":0,"HW":0,"P":0,"HL":0,"L":0,"pnl":0.0}

def finish(b):
    b["wins"]=b["W"]+b["HW"]
    b["losses"]=b["L"]+b["HL"]
    b["pnl"]=round(b["pnl"],6)
    b["roi_pct"]=round(b["pnl"]/b["settled"]*100,3) if b["settled"] else None
    return b

def summarize(db,epoch,now):
    if not epoch:
        return {"version":VERSION,"ready":False}
    start=epoch["started_at"]
    excluded_sids=set(epoch.get("excluded_sids",[]))
    total=bucket();daily={};seen=set();first=None
    excluded={"warning":0,"unconfirmed":0,"invalid_result":0}
    # Deliberately not LIMIT 100: page pagination cannot change performance.
    for raw in db.execute("SELECT * FROM items ORDER BY ko,bet_key"):
        x=dict(raw);sid=str(x["sid"])
        if (x["bet_key"] in seen or sid in excluded_sids or x["ko"]<=start
                or (x["attempt_at"] or 0)<start):
            continue
        seen.add(x["bet_key"])
        p=json.loads(x["payload"])
        if x["status"]!="sent" or not x["ack_at"] or not start<=x["ack_at"]<x["ko"]:
            excluded["unconfirmed"]+=1
            continue
        if p.get("conflict_warning"):
            excluded["warning"]+=1
            continue
        r=json.loads(x["result_json"]) if x["result_json"] else None
        if r and (r.get("result") not in ("W","HW","P","HL","L")
                  or not isinstance(r.get("pnl"),(int,float)) or not math.isfinite(r["pnl"])):
            excluded["invalid_result"]+=1
            continue
        date=day(x["ko"]);b=daily.setdefault(date,bucket())
        for target in (total,b):
            target["notified"]+=1
            if r is None:
                target["pending"]+=1
            else:
                target["settled"]+=1;target[r["result"]]+=1;target["pnl"]+=r["pnl"]
        first=x["ko"] if first is None else min(first,x["ko"])
    today=day(now)
    return {"version":VERSION,"ready":True,"epoch_id":epoch["id"],"started_at":start,
            "first_match_at":first,"stake_u":1,"date_basis":"Hong_Kong_kickoff_date",
            "total":finish(total),"today":finish(dict(daily.get(today,bucket()))),
            "today_date":today,"daily":[{"date":date,**finish(b)} for date,b in sorted(daily.items(),reverse=True)],
            "excluded":excluded,"old_history_preserved":True}
