#!/usr/bin/env python3
"""Global batch lock, causal rolling OR gate, Crown-only notifier."""
import argparse
import collections
import fcntl
import json
import os
from pathlib import Path
import sqlite3
import sys
import time
import urllib.error
import urllib.request

from policy import RULES,BY_ID,VERSION,START_MS,evaluate,valid_result,settle,gates,key,choose_batch,fmt

STATE=Path("/var/lib/crown-m1m6")
CONFIG=Path("/etc/crown-m1m6.json")
CROWN_DB=Path("/opt/crown-radar-v2/data/crown.db")
CHECKPOINTS=Path("/var/lib/crownsystem-v4/checkpoints.json")
PUBLIC=Path("/var/www/crownsystem-v3/m1m6_status.json")
def nowms():
    return int(time.time()*1000)
def atomic(path,data):
    tmp=path.with_suffix(path.suffix+".tmp")
    tmp.write_text(json.dumps(data,ensure_ascii=False))
    os.replace(tmp,path)
def env():
    result={}
    for line in Path("/etc/footbreak.env").read_text().splitlines():
        if "=" in line and not line.lstrip().startswith("#"):
            k,v=line.split("=",1)
            result[k.strip()]=v.strip().strip("'\"")
    return result
def source(now):
    db=sqlite3.connect(f"file:{CROWN_DB}?mode=ro",uri=True,timeout=5)
    db.row_factory=sqlite3.Row
    db.execute("PRAGMA query_only=ON")
    db.execute("BEGIN")
    matches=[dict(r) for r in db.execute("SELECT * FROM matches WHERE kickoff_utc>=? AND kickoff_utc<=? ORDER BY kickoff_utc,CAST(sid AS INTEGER)",(START_MS,now+8*60000))]
    ids={str(m["sid"]) for m in matches}
    snaps=collections.defaultdict(dict)
    for row in db.execute("SELECT * FROM crown_snapshots"):
        r=dict(row)
        if str(r["sid"]) in ids:
            snaps[str(r["sid"])][(r["stage"],r["market"])]=r
    finished={str(r["sid"]):dict(r) for r in db.execute("SELECT * FROM finished_matches")}
    db.rollback()
    db.close()
    cp=json.loads(CHECKPOINTS.read_text())
    return matches,snaps,finished,cp
def state_db():
    db=sqlite3.connect(STATE/"ledger.sqlite",timeout=10)
    db.row_factory=sqlite3.Row
    db.execute("PRAGMA journal_mode=WAL")
    db.execute("PRAGMA synchronous=FULL")
    db.executescript("""
    CREATE TABLE IF NOT EXISTS batches(id INTEGER PRIMARY KEY AUTOINCREMENT,created_at INTEGER NOT NULL,status TEXT NOT NULL,closed_at INTEGER);
    CREATE TABLE IF NOT EXISTS items(bet_key TEXT PRIMARY KEY,batch_id INTEGER NOT NULL,sid TEXT NOT NULL,ko INTEGER NOT NULL,payload TEXT NOT NULL,status TEXT NOT NULL,attempt_at INTEGER,ack_at INTEGER,message_id INTEGER,result_json TEXT,error TEXT);
    CREATE TABLE IF NOT EXISTS observations(sid TEXT NOT NULL,rule_id TEXT NOT NULL,first_seen_at INTEGER NOT NULL,origin TEXT NOT NULL,payload TEXT NOT NULL,result_json TEXT,PRIMARY KEY(sid,rule_id));
    """)
    columns={r[1] for r in db.execute("PRAGMA table_info(batches)")}
    for name,kind in [("kickoff_utc","INTEGER"),("policy_version","TEXT")]:
        if name not in columns:
            db.execute(f"ALTER TABLE batches ADD COLUMN {name} {kind}")
    return db
def collect(now):
    matches,snaps,finished,cp=source(now)
    history={q["id"]:[] for q in RULES}
    live=[];observations=[];reasons=collections.Counter()
    for m in matches:
        sid=str(m["sid"])
        hits,reason=evaluate(m,snaps[sid],cp.get(sid,{}),now)
        if m["kickoff_utc"]>now:
            reasons[reason]+=1
        for h in hits:
            result=settle(h,finished[sid]) if valid_result(finished.get(sid),h["ko"],now) else None
            observations.append((h,result))
            if result:
                history[h["rule_id"]].append(result)
            if h["ko"]>now:
                live.append(h)
    return history,live,observations,finished,reasons
def reconcile(db,finished,now):
    batch=db.execute("SELECT * FROM batches WHERE status='open' ORDER BY id LIMIT 1").fetchone()
    if not batch:
        return None
    for item in db.execute("SELECT * FROM items WHERE batch_id=?",(batch["id"],)).fetchall():
        if item["status"]=="prepared":
            db.execute("UPDATE items SET status='skipped',error='unattempted after interrupted batch; no delayed backfill' WHERE bet_key=?",(item["bet_key"],))
        elif item["status"]=="sending":
            db.execute("UPDATE items SET status='uncertain',error='interrupted delivery; no automatic duplicate retry' WHERE bet_key=?",(item["bet_key"],))
        if item["status"] in ["sent","sending","uncertain"] and not item["result_json"] and valid_result(finished.get(item["sid"]),item["ko"],now):
            result=settle(json.loads(item["payload"]),finished[item["sid"]])
            db.execute("UPDATE items SET result_json=? WHERE bet_key=?",(json.dumps(result,ensure_ascii=False),item["bet_key"]))
    waiting=db.execute("SELECT COUNT(*) FROM items WHERE batch_id=? AND status IN ('sent','uncertain','sending') AND result_json IS NULL",(batch["id"],)).fetchone()[0]
    if waiting==0:
        db.execute("UPDATE batches SET status='closed',closed_at=? WHERE id=?",(now,batch["id"]))
    db.commit()
    times={r[0] for r in db.execute("SELECT DISTINCT ko FROM items WHERE batch_id=?",(batch["id"],))}
    ko=batch["kickoff_utc"] or (next(iter(times)) if len(times)==1 else None)
    return {"batch_id":batch["id"],"waiting":waiting,"kickoff_utc":ko,
            "accepting_same_kickoff":bool(ko and now+1500<ko)} if waiting else None
def message(hit,batch_id,other_market=False):
    side="買細" if hit["side"]=="under" else "買主"
    line=f"{hit['line']:g}" if hit["market"]=="OU" else "平手" if hit["line"]==0 else ("受讓" if hit["line"]>0 else "讓")+f"{abs(hit['line']):g}"
    evidence=[]
    for rid in hit["rules"]:
        g=hit["gate_evidence"][rid]
        text=[]
        for n in ["20","30"]:
            s=g[n]
            rate="不足" if s["n"]<int(n) or not s["den"] else f"{s['wins']}/{s['den']}={s['hit']*100:.1f}%"
            text.append(f"近{n}場 {rate}")
        evidence.append(f"{rid}：{'；'.join(text)}")
    return "\n".join([
      f"皇冠 M1–M6｜賽前批次 #{batch_id}",
      f"策略：{'／'.join(hit['rules'])}（同注只計一次）",
      f"{hit['league']}｜{hit['home']} vs {hit['away']}",
      f"開賽：{fmt(hit['ko'])} 香港時間",
      f"方向：{side} {line}｜港賠 {hit['hk']:.2f}｜十進制 {1+hit['hk']:.2f}",
      f"皇冠T5：{fmt(hit['t5_at'])}",
      *[f"{rid}條件：{BY_ID[rid]['description']}" for rid in hit["rules"]],
      *evidence,
      "上述為已結算匹配場次窗口，不是下一場勝率。",
      "同場亦有另一市場訊號，存在同場風險。" if other_market else "本訊號只代表列出的市場及方向。",
      "同一開賽時間屬同批，開賽前可追加其他合資格場次；整批正式賽果齊全前不開其他時間的新批。",
      "結算後重新核對20場95%或30場90%，達標先推下一批。",
      "盤價已變即不等同本訊號；不會自動下注。",
    ])
def send(text,ko,creds):
    remaining=(ko-nowms())/1000
    if remaining<=1.5:
        return "skipped",None,"kickoff deadline"
    url="https://api.telegram.org/bot"+creds["TELEGRAM_BOT_TOKEN"]+"/sendMessage"
    req=urllib.request.Request(url,data=json.dumps({"chat_id":creds["TELEGRAM_CHAT_ID"],"text":text,"disable_web_page_preview":True}).encode(),headers={"Content-Type":"application/json"})
    try:
        with urllib.request.urlopen(req,timeout=min(8,remaining-.5)) as res:
            ack=json.loads(res.read())
        if ack.get("ok"):
            return "sent",ack["result"],None
        return "rejected",None,"telegram explicit rejection"
    except urllib.error.HTTPError as e:
        # Do not print URL, body, token, or exception text.
        return ("rejected" if 400<=e.code<500 else "uncertain"),None,f"HTTP {e.code}"
    except Exception as e:
        return "uncertain",None,type(e).__name__
def publish(db,config,gate_by_rule,reasons,live,locked,now):
    ledger=[]
    for x in db.execute("SELECT * FROM items ORDER BY batch_id DESC,bet_key LIMIT 100"):
        p=json.loads(x["payload"])
        ledger.append({**{k:p.get(k) for k in ["sid","ko","ko_hkt","home","away","league","market","side","line","hk","rules"]},
          "batch_id":x["batch_id"],"delivery_status":x["status"],"attempt_at":x["attempt_at"],"ack_at":x["ack_at"],
          "message_id":x["message_id"],"result":json.loads(x["result_json"]) if x["result_json"] else None})
    data={"version":VERSION,"updated_at":now,"activated_at":config["activated_at"],"mode":"same_kickoff_batch_then_rolling_OR",
          "locked_batch":locked,"rules":[{**q,"gate":gate_by_rule[q["id"]]} for q in RULES],
          "live_condition_hits":[{k:v for k,v in h.items() if k!="snapshot_evidence"} for h in live],
          "ledger":ledger,"scan_reasons":dict(reasons),"legacy_retired":True,
          "history_definition":"全部符合固定條件且當時已知正式賽果；包括未通知的場次",
          "old_history_archived":True}
    atomic(STATE/"status.json",data)
    atomic(PUBLIC,data)
    atomic(Path("/opt/crown-radar-v2/data/m1m6_status.json"),data)
def tick(dry=False):
    config=json.loads(CONFIG.read_text())
    now=nowms()
    history,live,observations,finished,reasons=collect(now)
    gate_by_rule={rid:gates(rr) for rid,rr in history.items()}
    if dry:
        return {"dry_run":True,"gates":gate_by_rule,"live_hits":len(live),"reasons":dict(reasons),"sends":0}
    db=state_db()
    locked=reconcile(db,finished,now)
    for h,result in observations:
        db.execute("INSERT OR IGNORE INTO observations VALUES(?,?,?,?,?,?)",
          (h["sid"],h["rule_id"],now,"historical_seed" if h["ko"]<=config["activated_at"] else "observed",
           json.dumps(h,ensure_ascii=False),json.dumps(result,ensure_ascii=False) if result else None))
        if result:
            db.execute("UPDATE observations SET result_json=? WHERE sid=? AND rule_id=?",(json.dumps(result,ensure_ascii=False),h["sid"],h["rule_id"]))
    db.commit()
    attempted={r[0] for r in db.execute("SELECT bet_key FROM items")}
    selected=choose_batch(live,gate_by_rule,now,config["activated_at"],locked,attempted) if config.get("enabled") else []
    sent=0
    if selected:
        creds=env()
        if not creds.get("TELEGRAM_BOT_TOKEN") or not creds.get("TELEGRAM_CHAT_ID"):
            raise RuntimeError("Telegram configuration missing")
        if locked:
            batch_id=locked["batch_id"]
        else:
            cur=db.execute("INSERT INTO batches(created_at,status,kickoff_utc,policy_version) VALUES(?,'open',?,?)",
                           (now,selected[0]["ko"],VERSION))
            batch_id=cur.lastrowid
        for h in selected:
            db.execute("INSERT INTO items(bet_key,batch_id,sid,ko,payload,status) VALUES(?,?,?,?,?,'prepared')",
              (key(h),batch_id,h["sid"],h["ko"],json.dumps(h,ensure_ascii=False)))
        db.commit()
        for h in selected:
            if nowms()+1500>=h["ko"]:
                db.execute("UPDATE items SET status='skipped',error='kickoff deadline' WHERE bet_key=?",(key(h),))
                db.commit()
                continue
            at=nowms()
            db.execute("UPDATE items SET status='sending',attempt_at=? WHERE bet_key=?",(at,key(h)))
            db.commit()
            other_market=db.execute("SELECT 1 FROM items WHERE batch_id=? AND sid=? AND bet_key<>? AND status IN ('sent','uncertain','prepared')",
                                    (batch_id,h["sid"],key(h))).fetchone() is not None
            status,ack,error=send(message(h,batch_id,other_market),h["ko"],creds)
            ack_at=ack.get("date",0)*1000 if ack else None
            # Preserve late acknowledgements honestly; never label as pre-match.
            if status=="sent" and (not ack_at or ack_at>=h["ko"]):
                status="uncertain"
                error="ack missing or not pre-kickoff"
            db.execute("UPDATE items SET status=?,ack_at=?,message_id=?,error=? WHERE bet_key=?",
              (status,ack_at,ack.get("message_id") if ack else None,error,key(h)))
            db.commit()
            sent+=status=="sent"
        locked=reconcile(db,finished,nowms())
    publish(db,config,gate_by_rule,reasons,live,locked,nowms())
    db.close()
    return {"version":VERSION,"at":fmt(now),"locked_batch":locked,"fixed_live_hits":len(live),"batch_selected":len(selected),"sent":sent,
            "gate_pass":[r for r,g in gate_by_rule.items() if g["pass"]]}
def main():
    ap=argparse.ArgumentParser()
    ap.add_argument("--dry-run",action="store_true")
    args=ap.parse_args()
    STATE.mkdir(parents=True,exist_ok=True)
    with (STATE/"run.lock").open("a") as lock:
        try:
            fcntl.flock(lock,fcntl.LOCK_EX|fcntl.LOCK_NB)
        except BlockingIOError:
            print(json.dumps({"skipped":"another tick holds lock"}))
            return
        print(json.dumps(tick(args.dry_run),ensure_ascii=False))
if __name__=="__main__":
    try:
        main()
    except Exception as e:
        # Exception text may contain provider URLs. Log only safe class.
        print(json.dumps({"error_type":type(e).__name__}),file=sys.stderr)
        raise SystemExit(1)
