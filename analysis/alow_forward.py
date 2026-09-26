"""Two frozen A+ price-band forward cohorts. Own SQLite only; production read-only."""
import datetime as dt
import fcntl
import hashlib
import json
import math
import os
import sqlite3
import sys
import time
from collections import Counter
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP
from pathlib import Path

ROOT=Path("/opt/crown-radar-v2")
OPS=ROOT/"ops/alow-forward-v1"
BANDS={"ALOW-P170-v1":(1700,1800),"ALOW-P200-v1":(2000,2100)}
RULE_FIELDS={"type":"channel","market":"AH","side":"H","pick":"home",
             "ah_line_side":"flat","ah_hcp_eq_t30_t5":True,"ou_hcp_le":2.25}

def hkt(ms):
    return dt.datetime.fromtimestamp(ms/1000,dt.timezone(dt.timedelta(hours=8))).isoformat(timespec="seconds")

def number(v):
    if v is None or isinstance(v,bool) or str(v).strip()=="":raise ValueError("missing_number")
    x=Decimal(str(v))
    if not x.is_finite():raise ValueError("nonfinite_number")
    return x

def price_milli(hk):
    return int(((number(hk)+1)*1000).quantize(Decimal(1),rounding=ROUND_HALF_UP))

def classify(price):
    return next((key for key,(lo,hi) in BANDS.items() if lo<=price<hi),None)

def original_match(m):
    """Equivalent to frozen ch-Alow on valid sports inputs, no six-cell gate."""
    s=m["snapshots"]
    try:
        a,b,c,o=[s[k] for k in ["initial_AH","T30_AH","T5_AH","T5_OU"]]
        # Original matcher requires initial AH but does not filter its handicap.
        if number(c["handicap"])!=0:return None
        if abs(number(b["handicap"])-number(c["handicap"]))>=Decimal(".001"):return None
        if number(o["handicap"])>Decimal("2.25"):return None
        price=price_milli(c["home_odds"])
        return price if price>=1700 else None
    except (KeyError,ValueError,InvalidOperation,TypeError):
        return None

def quality(m):
    errors=[];s=m["snapshots"];ko=m["kickoff_utc"]
    for market in ["AH","OU"]:
        times=[]
        for stage in ["initial","T30","T5"]:
            key=f"{stage}_{market}"
            try:
                snap=s[key];line=number(snap["handicap"])
                if number(snap["home_odds"])<=0 or number(snap["away_odds"])<=0 or line*4!=(line*4).to_integral_value():
                    raise ValueError()
                t=int(number(snap["captured_at"]));times.append(t);gap=(ko-t)/60000
                if not (gap>0 if stage=="initial" else 20<gap<=35 if stage=="T30" else 0<gap<=8):
                    errors.append(key+":時點超窗")
            except (KeyError,ValueError,InvalidOperation,TypeError):
                errors.append(key+":缺失或無效")
        if len(times)==3 and times!=sorted(times):errors.append(market+":時序倒置")
    return errors

def eligibility(m,now,start):
    """No hindsight: observe before kickoff, T5 after installation, no future timestamps."""
    price=original_match(m)
    band=classify(price) if price is not None else None
    if not band:return None,None
    if m.get("has_crown")!=1:return None,"not_crown_universe"
    ko=m["kickoff_utc"]
    if ko<=start:return None,"pre_activation_fixture"
    if now>=ko:return None,"first_observed_after_kickoff"
    s=m["snapshots"]
    try:
        for key in ["initial_AH","T30_AH","T5_AH","T5_OU"]:
            t=int(number(s[key]["captured_at"]))
            if t>now or t>=ko:return None,"future_or_postkickoff_snapshot"
            if key.startswith("T5") and t<start:return None,"pre_activation_T5"
        if not (s["initial_AH"]["captured_at"]<=s["T30_AH"]["captured_at"]<=s["T5_AH"]["captured_at"]):
            return None,"required_AH_time_reversed"
        for key in ["T5_AH","T5_OU"]:
            if not 0<ko-int(number(s[key]["captured_at"]))<=8*60000:
                return None,"stale_T5"
        for key in ["initial_AH","T30_AH","T5_AH","T5_OU"]:
            for field in ["home_odds","away_odds"]:
                if number(s[key][field])<=0:return None,"invalid_required_price"
    except (KeyError,ValueError,InvalidOperation,TypeError):return None,"invalid_required_data"
    return {"band":band,"price_milli":price,"observed_at":now,"strict_six":not quality(m),
            "quality_flags":quality(m)},None

def initialize(db,config):
    db.executescript("""
    CREATE TABLE IF NOT EXISTS metadata (key TEXT PRIMARY KEY,value TEXT NOT NULL);
    CREATE TABLE IF NOT EXISTS signals (
      sid TEXT PRIMARY KEY,band TEXT NOT NULL,observed_at INTEGER NOT NULL,kickoff INTEGER NOT NULL,
      league TEXT,home TEXT,away TEXT,price_milli INTEGER NOT NULL,
      strict_six INTEGER NOT NULL,quality_flags TEXT NOT NULL,evidence TEXT NOT NULL,
      evidence_sha256 TEXT NOT NULL,config_sha256 TEXT NOT NULL);
    CREATE TABLE IF NOT EXISTS settlements (
      sid TEXT PRIMARY KEY,state TEXT NOT NULL,outcome TEXT,units REAL,home_score INTEGER,away_score INTEGER,
      fetched_at INTEGER,checked_at INTEGER NOT NULL,note TEXT);
    CREATE TABLE IF NOT EXISTS result_history (
      id INTEGER PRIMARY KEY, sid TEXT,at INTEGER,previous TEXT,current TEXT);
    CREATE TABLE IF NOT EXISTS audit (
      sid TEXT,reason TEXT,first_seen INTEGER,last_seen INTEGER,detail TEXT,PRIMARY KEY(sid,reason));
    CREATE TABLE IF NOT EXISTS runs (
      id INTEGER PRIMARY KEY,started_at INTEGER,finished_at INTEGER,status TEXT,details TEXT);
    """)
    frozen=json.dumps(config,ensure_ascii=False,sort_keys=True)
    prior=db.execute("SELECT value FROM metadata WHERE key='config'").fetchone()
    if prior and prior[0]!=frozen:raise RuntimeError("frozen_config_mismatch")
    db.execute("INSERT OR IGNORE INTO metadata VALUES('config',?)",(frozen,))
    db.commit()

def audit(db,sid,reason,now,detail):
    db.execute("""INSERT INTO audit VALUES(?,?,?,?,?) ON CONFLICT(sid,reason)
      DO UPDATE SET last_seen=excluded.last_seen,detail=excluded.detail""",
      (sid,reason,now,now,json.dumps(detail,ensure_ascii=False,sort_keys=True)))

def record(db,m,now,config):
    found,reason=eligibility(m,now,config["effective_at_ms"])
    existing=db.execute("SELECT band FROM signals WHERE sid=?",(m["sid"],)).fetchone()
    if existing:
        price=original_match(m);new=classify(price) if price is not None else None
        if new and new!=existing[0]:audit(db,m["sid"],"later_band_change_no_reentry",now,{"frozen":existing[0],"later":new})
        return False
    if reason:
        audit(db,m["sid"],reason,now,{"kickoff":m["kickoff_utc"],"snapshots":m["snapshots"]})
        return False
    if not found:return False
    payload=json.dumps(m,ensure_ascii=False,sort_keys=True)
    configsha=hashlib.sha256(json.dumps(config,ensure_ascii=False,sort_keys=True).encode()).hexdigest()
    db.execute("INSERT INTO signals VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)",(
        str(m["sid"]),found["band"],now,m["kickoff_utc"],m["league"],m["home"],m["away"],
        found["price_milli"],int(found["strict_six"]),json.dumps(found["quality_flags"],ensure_ascii=False),
        payload,hashlib.sha256(payload.encode()).hexdigest(),configsha))
    return True

def grade(signal,result,current_kickoff,now):
    base=dict(sid=signal["sid"],state="pending",outcome=None,units=None,home_score=None,
              away_score=None,fetched_at=None,checked_at=now,note=None)
    if current_kickoff is None or current_kickoff!=signal["kickoff"]:
        return {**base,"state":"review","note":"賽程時間改動，保留原訊號，不自動重入"}
    if now<signal["kickoff"] or not result:return base
    if result["status"] in ["取消","腰斬","中断","中斷"]:
        return {**base,"state":"review","note":"取消或中斷，未擅自按走盤結算"}
    if result["status"]!="完":return base
    h,a=result["home_score"],result["away_score"]
    if not all(type(v) is int and v>=0 for v in [h,a]):return {**base,"state":"review","note":"比分無效"}
    if any(result.get(k) is not None and result[k]>score for k,score in [("ht_home_score",h),("ht_away_score",a)]):
        return {**base,"state":"review","note":"半場比分大於全場"}
    if result["fetched_at"]<signal["kickoff"]:return {**base,"state":"review","note":"完場證據早於開賽"}
    outcome="W" if h>a else "L" if h<a else "P"
    units=(signal["price_milli"]-1000)/1000 if outcome=="W" else -1 if outcome=="L" else 0
    return {**base,"state":"settled","outcome":outcome,"units":units,"home_score":h,"away_score":a,"fetched_at":result["fetched_at"]}

def settle(db,source,now):
    for signal in db.execute("SELECT * FROM signals").fetchall():
        m=source.execute("SELECT kickoff_utc FROM matches WHERE sid=?",(signal["sid"],)).fetchone()
        r=source.execute("SELECT * FROM finished_matches WHERE sid=?",(signal["sid"],)).fetchone()
        current=grade(dict(signal),dict(r) if r else None,m[0] if m else None,now)
        old=db.execute("SELECT * FROM settlements WHERE sid=?",(signal["sid"],)).fetchone()
        prev=dict(old) if old else None
        if prev and all(prev[k]==v for k,v in current.items() if k not in ["checked_at","fetched_at"]):continue
        db.execute("INSERT INTO result_history(sid,at,previous,current) VALUES(?,?,?,?)",(
          signal["sid"],now,json.dumps(prev,ensure_ascii=False),json.dumps(current,ensure_ascii=False)))
        fields=list(current)
        db.execute("INSERT OR REPLACE INTO settlements("+",".join(fields)+") VALUES("+",".join("?" for _ in fields)+")",list(current.values()))

def metrics(rs):
    settled=[r for r in rs if r["state"]=="settled"];c=Counter(r["outcome"] for r in settled)
    p=sum(r["units"] for r in settled);n=len(settled);dec=c["W"]+c["L"]
    b=peak=dd=streak=longest=0
    for r in settled:
        b+=r["units"];peak=max(peak,b);dd=max(dd,peak-b)
        streak=streak+1 if r["outcome"]=="L" else 0;longest=max(longest,streak)
    return {"observed":len(rs),"settled":n,"pending":sum(r["state"] in [None,"pending"] for r in rs),
       "review":sum(r["state"]=="review" for r in rs),"W":c["W"],"P":c["P"],"L":c["L"],
       "hit_rate":c["W"]/dec if dec else None,"pnl_units":round(p,6),"roi":p/n if n else None,
       "max_drawdown_units":round(dd,6),"max_consecutive_losses_push_resets":longest}

def summary(db,config,now,health):
    rs=[dict(r) for r in db.execute("""SELECT s.sid,s.band,s.strict_six,s.kickoff,t.state,t.outcome,t.units
      FROM signals s LEFT JOIN settlements t USING(sid) ORDER BY s.kickoff,s.sid""")]
    return {"version":config["version"],"effective_at_ms":config["effective_at_ms"],
      "effective_at_hkt":hkt(config["effective_at_ms"]),"as_of_hkt":hkt(now),
      "mode":"shadow_only_no_TG","health":health,
      "bands":{b:{"odds_min":lo/1000,"odds_max_exclusive":hi/1000,"main":metrics([r for r in rs if r["band"]==b]),
                    "strict_six_sensitivity":metrics([r for r in rs if r["band"]==b and r["strict_six"]])}
               for b,(lo,hi) in BANDS.items()},
      "audit_counts":{r[0]:r[1] for r in db.execute("SELECT reason,count(*) FROM audit GROUP BY reason")},
      "historical_159_included":False,"formal_notifications_enabled":False}

def atomic(path,payload):
    temp=path.with_suffix(".tmp");temp.write_text(json.dumps(payload,ensure_ascii=False,indent=2))
    os.chmod(temp,0o600);os.replace(temp,path)

def run_once(ops=OPS,root=ROOT):
    lock=(ops/"lock").open("a")
    try:fcntl.flock(lock,fcntl.LOCK_EX|fcntl.LOCK_NB)
    except BlockingIOError:
        lock.close()
        return {"status":"another_run_active"}
    config=json.loads((ops/"config.json").read_text());now=int(time.time()*1000)
    db=sqlite3.connect(ops/"ledger.sqlite",timeout=8);db.row_factory=sqlite3.Row
    initialize(db,config)
    source=None
    try:
        # Existing A+ remains disabled; independently frozen terms do not drift with later config edits.
        current=json.loads((root/"data/rules.json").read_text())
        rule=next(r for r in current["rules"] if r["id"]=="ch-Alow")
        drift={k:rule.get(k) for k,v in RULE_FIELDS.items() if rule.get(k)!=v}
        if drift:raise RuntimeError("original_Aplus_terms_changed")
        source=sqlite3.connect(f"file:{root}/data/crown.db?mode=ro",uri=True,timeout=8)
        source.row_factory=sqlite3.Row;source.execute("PRAGMA query_only=ON");source.execute("BEGIN")
        matches=[dict(r) for r in source.execute("""SELECT sid,league,home,away,kickoff_utc,has_crown
          FROM matches WHERE has_crown=1 AND kickoff_utc>? AND kickoff_utc BETWEEN ? AND ?""",
          (config["effective_at_ms"],max(config["effective_at_ms"],now-48*3600000),now+15*60000))]
        latest=source.execute("SELECT MAX(captured_at) FROM crown_snapshots WHERE stage='T5'").fetchone()[0]
        for m in matches:
            m["snapshots"]={f"{s['stage']}_{s['market']}":dict(s) for s in source.execute(
              "SELECT * FROM crown_snapshots WHERE sid=? AND stage IN ('initial','T30','T5') ORDER BY id",(m["sid"],))}
            # Capture wall clock again after reads: never backdate first observation to run start.
            record(db,m,int(time.time()*1000),config)
        settle(db,source,int(time.time()*1000));source.close()
        end=int(time.time()*1000)
        health={"status":"ok","last_success_ms":end,"last_success_hkt":hkt(end),"matches_checked":len(matches),
                "latest_source_T5_ms":latest,"last_run_duration_ms":end-now}
        db.execute("INSERT INTO runs(started_at,finished_at,status,details) VALUES(?,?,?,?)",
                   (now,end,"ok",json.dumps(health)))
        db.execute("DELETE FROM runs WHERE started_at<?",(end-30*86400000,))
        db.commit();report=summary(db,config,end,health);atomic(ops/"status.json",report)
        print(json.dumps(report,ensure_ascii=False));return report
    except Exception as e:
        db.rollback()
        failure={"status":"error","error_type":type(e).__name__,"reason":str(e),"at_hkt":hkt(int(time.time()*1000))}
        prior=json.loads((ops/"status.json").read_text()) if (ops/"status.json").exists() else {}
        prior["health"]=failure;atomic(ops/"status.json",prior)
        raise
    finally:
        if source is not None:source.close()
        db.close()
        lock.close()

if __name__=="__main__":
    if sys.argv[-1:]==["--status"]:print((OPS/"status.json").read_text())
    else:run_once()
