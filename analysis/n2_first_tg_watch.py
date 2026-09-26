"""Read-only N2 natural-delivery observer; independent state and private alerts."""
import datetime as dt
import fcntl
import hashlib
import json
import os
from pathlib import Path
import sqlite3
import subprocess
import sys
import time

ROOT = Path("/opt/crown-radar-v2")
OPS = ROOT / "ops/n2-first-tg"
STATE = OPS / "state.json"
CONFIG = OPS / "config.json"
TIMER = "crown-n2-first-tg-watch.timer"

def hkt(ms):
    return dt.datetime.fromtimestamp(ms/1000,dt.timezone(dt.timedelta(hours=8))).strftime("%Y-%m-%d %H:%M:%S")

def choose(hits, notices, state, now):
    """Return at most one monitoring event; no bets, no production writes."""
    by_sid={str(h["sid"]):h for h in hits}
    if notices:
        n=sorted(notices,key=lambda x:(x["notified_at"],str(x["sid"])))[0]
        h=by_sid.get(str(n["sid"]),n)
        before=n["notified_at"] < n["kickoff_utc"]
        return {"kind":"first_notice","terminal":True,"sid":n["sid"],"fixture":h,
                "notified_at":n["notified_at"],"seconds_before":round((n["kickoff_utc"]-n["notified_at"])/1000,3),
                "on_time":before,"rule_recheck_ok":str(n["sid"]) in by_sid}
    for h in sorted(hits,key=lambda x:(x["t5_at"],str(x["sid"]))):
        sid=str(h["sid"]); gap=h["kickoff_utc"]-now
        if gap <= -30000 and not state.get("missed_alerted"):
            return {"kind":"missed","sid":sid,"fixture":h,"terminal":False}
        if 0 < gap <= 90000 and now-h["t5_at"]>=60000 and not state.get("warning_alerted"):
            return {"kind":"prestart_warning","sid":sid,"fixture":h,"terminal":False}
    return None

def text_for(event):
    h=event.get("fixture",{})
    parts=["新2 v2 首筆自然TG通知監察",f"檢查時間：{hkt(event['checked_at'])} HKT"]
    if h:
        parts += [f"賽事：{h.get('league','')}｜{h.get('home','')} vs {h.get('away','')}",
                  f"開賽：{hkt(h['kickoff_utc'])} HKT",f"賽事ID：{event['sid']}"]
        if h.get("t5_at"):parts.append(f"皇冠T-5入庫：{hkt(h['t5_at'])} HKT")
        if h.get("line") is not None:parts.append(f"買大 {h['line']}，十進制賠率 {h['odds']:.2f}")
    kind=event["kind"]
    if kind=="first_notice":
        parts += [f"通知成功記帳：{hkt(event['notified_at'])} HKT",
                  f"結果：{'早於開賽，通過' if event['on_time'] else '未早於開賽，不合格'}；距開賽 {event['seconds_before']:.1f} 秒。",
                  f"當前皇冠快照重核：{'符合新2 v2' if event['rule_recheck_ok'] else '不一致，需人工核對'}。",
                  "證據：正式發送流程收到Telegram成功回應後，才寫入逐規則通知帳本。",
                  "這只確認Telegram接受及記帳時間，不代表手機推送到達或已讀。",
                  "首筆監察完成，這項獨立監察將停止；新2正式通知繼續運作。"]
    elif kind=="missed":
        parts += ["結果：快照符合新2 v2，但開賽後仍未有成功通知帳本，疑似漏發。",
                  "沒有補發投注訊息、沒有修改策略；會繼續等候首筆正式自然通知。"]
    elif kind=="prestart_warning":
        parts += ["結果：距開賽不足90秒，已具合資格T-5快照超過60秒，仍未有成功通知帳本。",
                  "這是監察警告，不是投注建議；沒有重發投注訊息或修改策略。"]
    else:
        parts += ["結果：監察無法繼續核實。",event.get("reason","監察檢查異常"),
                  "未修改或停用正式新2規則，沒有補發投注訊息。"]
    return "\n".join(parts)

def save(state):
    temp=STATE.with_suffix(".tmp")
    temp.write_text(json.dumps(state,ensure_ascii=False,indent=2))
    os.chmod(temp,0o600);os.replace(temp,STATE)

def runtime(payload):
    result=subprocess.run(["docker","exec","-i","crown-radar-v2","node","--input-type=module","-e",(OPS/"runtime.mjs").read_text()],
      input=json.dumps(payload,ensure_ascii=False),capture_output=True,text=True,timeout=35)
    if result.returncode:raise RuntimeError("observer_runtime_failure")
    return json.loads(result.stdout)

def emit(event,state,config):
    key=event["kind"]+"/"+str(event.get("sid",""))
    previous=state.setdefault("outbox",{}).get(key)
    if previous:
        # At-most-once submission even following process interruption/timeout.
        return previous.get("result",{}).get("ok",False)
    event["checked_at"]=int(time.time()*1000)
    message=text_for(event)
    state["outbox"][key]={"state":"submitting","event":event,"text":message}
    save(state)
    try:result=runtime({"op":"notify","chat_id":config["monitor_chat_id"],"text":message})
    except Exception:result={"ok":False,"uncertain":True,"reason":"observer_runtime_failure"}
    state["outbox"][key].update(state="accepted" if result.get("ok") else "needs_manual_review",result=result)
    save(state)
    return result.get("ok",False)

def disable():
    subprocess.run(["systemctl","disable","--now",TIMER],capture_output=True,timeout=15)

def main():
    OPS.mkdir(parents=True,exist_ok=True)
    lock=(OPS/"lock").open("w")
    try:fcntl.flock(lock,fcntl.LOCK_EX|fcntl.LOCK_NB)
    except BlockingIOError:return
    config=json.loads(CONFIG.read_text())
    state=json.loads(STATE.read_text()) if STATE.exists() else {"started_at":int(time.time()*1000)}
    now=int(time.time()*1000)
    if state.get("completed"):disable();return
    if sys.argv[-1:] == ["--verify-chat"]:
        result=runtime({"op":"verify-chat","chat_id":config["monitor_chat_id"]})
        assert result.get("ok") and result["chat_type"]=="private"
        print(json.dumps(result));return
    if sys.argv[-1:] == ["--status"]:
        print(json.dumps(state,ensure_ascii=False));return
    try:
        rules=json.loads((ROOT/"data/rules.json").read_text())
        rule=next(r for r in rules["rules"] if r["id"]=="ch-N2")
        watched=("version","version_effective_at_ms","hour_hkt_min","hour_hkt_max_exclusive",
                 "ou_hcp_le","t5_dec_min","enabled")
        if any(rule.get(k)!=config["rule"][k] for k in watched):
            event={"kind":"configuration_changed","terminal":True,"reason":"新2已停用或版本／條件改動，停止追蹤舊版本。"}
            emit(event,state,config);state.update(completed=True,completion=event);save(state);disable();return
        actual=hashlib.sha256((ROOT/"server.js").read_bytes()).hexdigest()
        if actual!=config["server_sha256"]:
            event={"kind":"configuration_changed","terminal":True,"reason":"正式通知程式已改動，需要重新核對成功記帳口徑。"}
            emit(event,state,config);state.update(completed=True,completion=event);save(state);disable();return
        start=config["rule"]["version_effective_at_ms"]
        db=sqlite3.connect(f"file:{ROOT}/data/crown.db?mode=ro",uri=True,timeout=8)
        db.row_factory=sqlite3.Row;db.execute("PRAGMA query_only=ON");db.execute("BEGIN")
        notices=[dict(r) for r in db.execute("""
          SELECT n.sid,n.notified_at,m.league,m.home,m.away,m.kickoff_utc
          FROM heavy_notified_rule n JOIN matches m ON m.sid=n.sid
          WHERE n.rule_id='ch-N2' AND n.notified_at>=? ORDER BY n.notified_at,n.sid""",(start,))]
        matches=[dict(r) for r in db.execute("""
          SELECT m.sid,m.league,m.home,m.away,m.kickoff_utc FROM matches m
          WHERE m.has_crown=1 AND m.kickoff_utc>=? AND m.kickoff_utc<=?
          AND ((m.kickoff_utc/3600000+8)%24)>=8 AND ((m.kickoff_utc/3600000+8)%24)<12
          AND EXISTS(SELECT 1 FROM crown_snapshots s WHERE s.sid=m.sid AND s.stage='T5' AND s.market='OU')
          """,(start,now+15*60000))]
        for m in matches:
            m["snapshots"]={f"{s['stage']}_{s['market']}":{
                "h":s["handicap"],"home":s["home_odds"],"away":s["away_odds"],"captured_at":s["captured_at"]}
                for s in db.execute("SELECT * FROM crown_snapshots WHERE sid=?",(m["sid"],))}
            m["sublines"]={}
        db.close()
        hits=runtime({"op":"match","matches":matches})["hits"] if matches else []
        state.update(last_check_ms=now,last_check_hkt=hkt(now),matched_count=len(hits),
                     natural_notifications=len(notices),status="waiting_for_first_natural",
                     consecutive_errors=0)
        for h in hits:
            state.setdefault("observed",{}).setdefault(str(h["sid"]),{"first_seen":now,**h})
        event=choose(hits,notices,state,now)
        if event:
            accepted=emit(event,state,config)
            if event["kind"]=="missed":state["missed_alerted"]=True
            if event["kind"]=="prestart_warning":state["warning_alerted"]=True
            if event["terminal"]:
                state.update(completed=True,status="completed" if accepted else "report_delivery_unverified",completion=event)
        save(state)
        if state.get("completed"):disable()
        print(json.dumps({k:v for k,v in state.items() if k not in ("observed","outbox")},ensure_ascii=False))
    except Exception as exc:
        state["consecutive_errors"]=state.get("consecutive_errors",0)+1
        state.update(last_error_ms=now,last_error_type=type(exc).__name__,status="monitor_error")
        save(state)
        if state["consecutive_errors"]>=3:
            event={"kind":"monitor_error","terminal":False,"reason":"連續3次讀取或核對失敗，未能驗證首筆通知。"}
            emit(event,state,config)
        if state["consecutive_errors"]>=10:
            state.update(completed=True,status="stopped_after_errors")
            save(state);disable()
        raise

if __name__=="__main__":main()
