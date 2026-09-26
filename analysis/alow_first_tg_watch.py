"""Read-only A+ v2 acceptance monitor. Independent state; never calls betting notifier."""
import datetime as dt
import fcntl
import hashlib
import json
import math
import os
from pathlib import Path
import sqlite3
import subprocess
import sys
import time

ROOT=Path("/opt/crown-radar-v2")
OPS=ROOT/"ops/alow-first-tg"
TIMER="crown-alow-first-tg-watch.timer"
VERSION="ALOW-PRICE-DIR-v2"
def hkt(ms):
    return dt.datetime.fromtimestamp(ms/1000,dt.timezone(dt.timedelta(hours=8))).strftime("%Y-%m-%d %H:%M:%S")
def finite(v):
    return isinstance(v,(float,int)) and not isinstance(v,bool) and math.isfinite(v)
def expected_side(home):
    if not finite(home):return None
    if 1.70<=home<1.80 or 2.00<=home<2.10:return "H"
    if home>=2.10:return "A"
    return None
def assess(n,h,start):
    """Validate immutable receipt plus independent Crown snapshot reconstruction."""
    issues=[]
    r=n.get("receipt") or {}
    if not r:issues.append("缺少新版TG成功回應收據")
    home=r.get("trigger_home_dec")
    side=expected_side(home)
    if r.get("version")!=VERSION:issues.append("收據版本不符")
    if side is None or r.get("side")!=side:issues.append("主賠區間與主客方向不符")
    if r.get("pick")!=("home" if side=="H" else "away"):issues.append("買主／買客代碼不符")
    if r.get("line")!=0:issues.append("不是平手盤")
    if not finite(r.get("odds")) or r["odds"]<=1:issues.append("實際投注方向賠率無效")
    if not r.get("telegram_message_id"):issues.append("缺少Telegram訊息編號")
    ack=r.get("telegram_date_ms")
    ko=n["kickoff_utc"]
    ledger_at=n.get("notified_at")
    timely=finite(ledger_at) and start<=ledger_at<ko and finite(ack) and start-1000<=ack<ko
    if not timely:issues.append("未確認Telegram接收及成功記帳均早於開賽")
    if r.get("kickoff_utc")!=ko:issues.append("收據與賽事開賽時間不一致")
    if not h:issues.append("當前皇冠快照未能重新命中，需人工核對")
    else:
        for key in ["side","line","odds","trigger_home_dec"]:
            a,b=r.get(key),h.get(key)
            equal=abs(a-b)<0.00001 if finite(a) and finite(b) else a==b
            if not equal:issues.append(f"收據與皇冠快照不一致：{key}")
        if not (start<=h["ah_at"]<ko and start<=h["ou_at"]<ko):issues.append("T-5時間不符合重開後／開賽前")
    return {"passed":not issues,"on_time":timely,"issues":issues,
        "seconds_before":round((ko-ledger_at)/1000,3) if finite(ledger_at) else None,
        "telegram_seconds_before":round((ko-ack)/1000,3) if finite(ack) else None}
def choose(hits,notices,state,now,start):
    by_sid={str(h["sid"]):h for h in hits}
    if notices:
        n=sorted(notices,key=lambda x:(x["notified_at"],str(x["sid"])))[0]
        return {"kind":"first_notice","terminal":True,"sid":str(n["sid"]),"fixture":n,
            "assessment":assess(n,by_sid.get(str(n["sid"])),start)}
    for h in sorted(hits,key=lambda x:(x["t5_at"],str(x["sid"]))):
        gap=h["kickoff_utc"]-now
        if gap<=-30000 and not state.get("missed_alerted"):
            return {"kind":"missed","terminal":False,"sid":str(h["sid"]),"fixture":h}
        if 0<gap<=90000 and now-h["t5_at"]>=60000 and not state.get("warning_alerted"):
            return {"kind":"prestart_warning","terminal":False,"sid":str(h["sid"]),"fixture":h}
    return None
def text_for(event):
    h=event.get("fixture",{})
    lines=["A+低盤 v2 首筆自然TG通知監察",f"檢查：{hkt(event['checked_at'])} HKT"]
    if h:
        lines += [f"賽事：{h.get('league','')}｜{h.get('home','')} vs {h.get('away','')}",
            f"開賽：{hkt(h['kickoff_utc'])} HKT",f"賽事ID：{event['sid']}"]
    if event["kind"]=="first_notice":
        r=h.get("receipt") or {};a=event["assessment"]
        lines += [f"結果：{'核對通過' if a['passed'] else '發現不一致，未通過'}",
            f"正式通知成功記帳：{hkt(h['notified_at'])} HKT",
            f"距開賽：{a['seconds_before']}秒",
            f"觸發主賠：{r.get('trigger_home_dec','未取得')}；方向：{r.get('display','未取得')}",
            f"平手盤實際賠率：{r.get('odds','未取得')}",
            f"Telegram訊息編號：{r.get('telegram_message_id','未取得')}"]
        if r.get("telegram_date_ms"):lines.append(f"Telegram接收時間：{hkt(r['telegram_date_ms'])} HKT")
        lines.extend("待處理："+i for i in a["issues"])
        lines += ["確認範圍：Telegram成功回應、正式帳本及皇冠T-5重核；不代表手機推送到達或已讀。",
            "首筆監察完成並停止；正式A+策略及TG繼續運作。"]
    elif event["kind"]=="prestart_warning":
        lines += ["距開賽不足90秒，皇冠T-5符合A+新版已超過60秒，但未有成功通知帳本。",
            "這是監察警告，不是投注建議；不補發投注訊息，繼續追蹤首筆。"]
    elif event["kind"]=="missed":
        lines += ["已開賽但符合A+新版的訊號仍未有成功通知帳本，疑似漏發。",
            "沒有補發投注訊息、沒有改策略；會繼續追蹤首筆正式通知。"]
    else:
        lines += [event.get("reason","監察出現異常"),"未修改或停用正式策略，沒有補發投注訊息。"]
    return "\n".join(lines)
def save(state):
    tmp=OPS/"state.tmp";tmp.write_text(json.dumps(state,ensure_ascii=False,indent=2))
    os.chmod(tmp,0o600);os.replace(tmp,OPS/"state.json")
def runtime(p):
    result=subprocess.run(["docker","exec","-i","crown-radar-v2","node","--input-type=module","-e",
        (OPS/"runtime.mjs").read_text()],input=json.dumps(p,ensure_ascii=False),capture_output=True,text=True,timeout=40)
    if result.returncode:raise RuntimeError("observer_runtime_failure")
    return json.loads(result.stdout)
def emit(event,state,config):
    key=event["kind"]+"/"+str(event.get("sid",""))
    previous=state.setdefault("outbox",{}).get(key)
    if previous:return previous.get("result",{}).get("ok",False)
    event["checked_at"]=int(time.time()*1000)
    state["outbox"][key]={"state":"submitting","event":event,"text":text_for(event)}
    save(state)
    try:result=runtime({"op":"notify","chat_id":config["monitor_chat_id"],"text":text_for(event)})
    except Exception:result={"ok":False,"uncertain":True,"reason":"observer_runtime_failure"}
    state["outbox"][key].update(state="accepted" if result.get("ok") else "needs_manual_review",result=result)
    save(state);return result.get("ok",False)
def disable():
    subprocess.run(["systemctl","disable","--now",TIMER],capture_output=True,timeout=15)
def main():
    OPS.mkdir(parents=True,exist_ok=True)
    lock=(OPS/"lock").open("w")
    try:fcntl.flock(lock,fcntl.LOCK_EX|fcntl.LOCK_NB)
    except BlockingIOError:return
    config=json.loads((OPS/"config.json").read_text())
    state=json.loads((OPS/"state.json").read_text()) if (OPS/"state.json").exists() else {"started_at":int(time.time()*1000)}
    if sys.argv[-1:]==["--status"]:print(json.dumps(state,ensure_ascii=False));return
    if sys.argv[-1:]==["--verify-chat"]:
        j=runtime({"op":"verify-chat","chat_id":config["monitor_chat_id"]})
        assert j.get("ok") and j.get("chat_type")=="private"
        print(json.dumps(j));return
    if state.get("completed"):disable();print(json.dumps(state,ensure_ascii=False));return
    now=int(time.time()*1000)
    try:
        rule=next(r for r in json.loads((ROOT/"data/rules.json").read_text())["rules"] if r["id"]=="ch-Alow")
        hashes={n:hashlib.sha256((ROOT/n).read_bytes()).hexdigest() for n in config["protected_hashes"]}
        if rule!=config["rule"] or hashes!=config["protected_hashes"]:
            event={"kind":"configuration_changed","terminal":True,"reason":"A+版本／規則或通知程式變更，停止監察舊版本以免錯報。"}
            emit(event,state,config);state.update(completed=True,status="configuration_changed",completion=event)
            save(state);disable();print(json.dumps(state,ensure_ascii=False));return
        start=rule["version_effective_at_ms"]
        db=sqlite3.connect(f"file:{ROOT}/data/crown.db?mode=ro",uri=True,timeout=8)
        db.row_factory=sqlite3.Row;db.execute("PRAGMA query_only=ON");db.execute("BEGIN")
        notices=[dict(r) for r in db.execute("""
            SELECT n.sid,n.notified_at,m.league,m.home,m.away,m.kickoff_utc,a.payload_json
            FROM heavy_notified_rule n JOIN matches m ON m.sid=n.sid
            LEFT JOIN alow_notification_receipts a ON a.sid=n.sid
            WHERE n.rule_id='ch-Alow' AND n.notified_at>=? ORDER BY n.notified_at,n.sid
        """,(start,))]
        for n in notices:
            try:n["receipt"]=json.loads(n.pop("payload_json") or "null")
            except Exception:n["receipt"]=None
        matches=[dict(r) for r in db.execute("""
            SELECT m.sid,m.league,m.home,m.away,m.kickoff_utc FROM matches m
            WHERE m.has_crown=1 AND m.kickoff_utc>=? AND m.kickoff_utc<=?
              AND EXISTS(SELECT 1 FROM crown_snapshots s WHERE s.sid=m.sid AND s.stage='T5' AND s.market='AH')
        """,(start,now+30*60000))]
        for m in matches:
            m["snapshots"]={f"{s['stage']}_{s['market']}":{
                "h":s["handicap"],"home":s["home_odds"],"away":s["away_odds"],"captured_at":s["captured_at"]}
                for s in db.execute("SELECT * FROM crown_snapshots WHERE sid=?",(m["sid"],))}
            m["sublines"]={}
        db.close()
        hits=runtime({"op":"match","matches":matches})["hits"] if matches else []
        state.update(last_check_ms=now,last_check_hkt=hkt(now),matched_count=len(hits),
            natural_notifications=len(notices),status="waiting_for_first_natural",consecutive_errors=0)
        for h in hits:state.setdefault("observed",{}).setdefault(str(h["sid"]),{"first_seen":now,**h})
        event=choose(hits,notices,state,now,start)
        if event:
            accepted=emit(event,state,config)
            if event["kind"]=="prestart_warning":state["warning_alerted"]=True
            if event["kind"]=="missed":state["missed_alerted"]=True
            if event["terminal"]:state.update(completed=True,status="completed" if accepted else "report_delivery_unverified",completion=event)
        save(state)
        if state.get("completed"):disable()
        print(json.dumps({k:v for k,v in state.items() if k not in ("observed","outbox")},ensure_ascii=False))
    except Exception as e:
        state["consecutive_errors"]=state.get("consecutive_errors",0)+1
        state.update(last_error_ms=now,last_error_type=type(e).__name__,status="monitor_error");save(state)
        if state["consecutive_errors"]>=3:
            emit({"kind":"monitor_error","terminal":False,"reason":"連續3次無法讀取或核對首筆A+通知，需檢查監察服務。"},state,config)
        if state["consecutive_errors"]>=10:
            state.update(completed=True,status="stopped_after_errors");save(state);disable()
        raise
if __name__=="__main__":main()
