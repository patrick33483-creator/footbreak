"""Pure Crown-native rules. No network, filesystem writes, or result leakage."""
from collections import Counter
from datetime import datetime, timezone, timedelta
from decimal import Decimal as D
import math

VERSION="M1M6-KICKOFF-BATCH-v2"
START_MS=1789023600000  # 2026-09-10 15:00 HKT
HKT=timezone(timedelta(hours=8))
RULES=[
 {"id":"M1","market":"OU","side":"under","label":"買細3.25水位分支",
  "description":"大小3.25；主隊視角讓球≤+0.75；T30至T5讓球同線，主隊降水≥0.03或客隊升水≥0.03。"},
 {"id":"M2","market":"AH","side":"home","label":"午夜主隊降水＋模型買主",
  "description":"初盤至T5讓球同線主隊降水≥0.03；初盤模型買主；香港00:00至04:00前開賽。"},
 {"id":"M3","market":"OU","side":"under","label":"午夜買細2.25＋三時點主隊低水",
  "description":"大小2.25；初盤、T30、T5主隊讓球港賠均較客隊低至少0.02；香港00:00至04:00前開賽。"},
 {"id":"M4","market":"AH","side":"home","label":"低水買主＋細球升水",
  "description":"主隊港賠0.60至低於0.70；T30至T5大球水位絕對變動<0.03；初盤至T5細球升水≥0.03，水位比較須同線。"},
 {"id":"M5","market":"AH","side":"home","label":"主受讓1.25＋盤線增加＋模型買客",
  "description":"主受讓1.25；初盤至T5主隊視角盤線數值增加≥0.25；初盤模型買客。"},
 {"id":"M6","market":"AH","side":"home","label":"晚間買主＋細球降水",
  "description":"主平手或受讓；初盤至T5大小同線細球降水≥0.03；香港20:00至午夜前開賽。"},
]
BY_ID={q["id"]:q for q in RULES}
def fmt(ms):
    return datetime.fromtimestamp(ms/1000,HKT).strftime("%Y-%m-%d %H:%M:%S")
def num(x):
    if x is None or isinstance(x,bool):
        raise ValueError("invalid number")
    v=D(str(x))
    if not v.is_finite():
        raise ValueError("nonfinite")
    return v
def initial_model(cp,ko,now):
    c=(cp or {}).get("INITIAL",{})
    at=c.get("locked_at_ms") or 0
    repairs=[r["at_ms"] for r in c.get("prediction_repairs",[]) if "ah" in r.get("markets",[])]
    at=max([at]+repairs)
    if not 0<at<ko or at>now:
        return None
    value=(c.get("prediction") or {}).get("pred_ah") or ""
    return "home" if "主" in value else "away" if "客" in value else None
def evaluate(match,snaps,cp,now):
    """Return all fixed-rule hits, independent of rolling history or batching."""
    ko=match.get("kickoff_utc")
    if not isinstance(ko,(int,float)) or isinstance(ko,bool) or ko<START_MS:
        return [],"invalid_match_time"
    s={}
    try:
        for market in ["AH","OU"]:
            times=[]
            for stage in ["initial","T30","T5"]:
                raw=snaps[(stage,market)]
                line=num(raw["handicap"])
                h,a=num(raw["home_odds"]),num(raw["away_odds"])
                at=raw["captured_at"]
                gap=(ko-at)/60000
                if line*4!=int(line*4) or min(h,a)<=0 or not 0<at<ko or at>now:
                    return [],"invalid_snapshot"
                if stage=="T30" and not 20<gap<=35:
                    return [],"invalid_T30_time"
                if stage=="T5" and not 0<gap<=8:
                    return [],"invalid_T5_time"
                s[(stage,market)]={"line":line,"home":h,"away":a,"at":at}
                times.append(at)
            if not times[0]<times[1]<times[2]:
                return [],"nonchronological"
    except (KeyError,ValueError,TypeError,ArithmeticError):
        return [],"missing_or_invalid_six_snapshots"
    ah=s[("T5","AH")]["line"];ou=s[("T5","OU")]["line"]
    if abs(ah)>2 or not D("1.5")<=ou<=4:
        return [],"outside_market_scope"
    def delta(market,start,side):
        a,b=s[(start,market)],s[("T5",market)]
        return b[side]-a[side] if a["line"]==b["line"] else None
    def fall(d):
        return d is not None and d<=D("-.03")
    def rise(d):
        return d is not None and d>=D(".03")
    hh=datetime.fromtimestamp(ko/1000,HKT).hour
    model=initial_model(cp,ko,now)
    candidates={
      "M1":ou==D("3.25") and ah<=D(".75") and (fall(delta("AH","T30","home")) or rise(delta("AH","T30","away"))),
      "M2":0<=hh<4 and model=="home" and fall(delta("AH","initial","home")),
      "M3":ou==D("2.25") and 0<=hh<4 and all(s[(t,"AH")]["home"]-s[(t,"AH")]["away"]<=D("-.02") for t in ["initial","T30","T5"]),
      "M4":D(".60")<=s[("T5","AH")]["home"]<D(".70") and delta("OU","T30","home") is not None and abs(delta("OU","T30","home"))<D(".03") and rise(delta("OU","initial","away")),
      "M5":ah==D("1.25") and ah-s[("initial","AH")]["line"]>=D(".25") and model=="away",
      "M6":ah>=0 and 20<=hh<24 and fall(delta("OU","initial","away")),
    }
    hits=[]
    for rid,passed in candidates.items():
        if not passed:
            continue
        rule=BY_ID[rid]
        q=s[("T5",rule["market"])]
        hk=q["away"] if rule["side"]=="under" else q["home"]
        if not D(".60")<=hk<=D("1.20"):
            continue
        hits.append({"rule_id":rid,"sid":str(match["sid"]),"ko":ko,"ko_hkt":fmt(ko),
          "home":match.get("home",""),"away":match.get("away",""),"league":match.get("league",""),
          "market":rule["market"],"side":rule["side"],"line":float(q["line"]),"hk":float(hk),
          "t5_at":q["at"],"six_t5_min_at":min(s[("T5","AH")]["at"],s[("T5","OU")]["at"]),
          "snapshot_evidence":{stage+"_"+market:{k:float(v) if isinstance(v,D) else v for k,v in x.items()} for (stage,market),x in s.items()},
          "initial_model":model})
    return hits,"matched" if hits else "fixed_conditions_not_met"
def valid_result(f,ko,now):
    return bool(f and f.get("status")=="完" and all(isinstance(f.get(k),int) and not isinstance(f[k],bool) and f[k]>=0 for k in ["home_score","away_score"]) and ko<f.get("fetched_at",0)<=now)
def settle(hit,f):
    h,a=f["home_score"],f["away_score"]
    line=int(num(hit["line"])*4)
    legs=[line] if line%2==0 else [line-1,line+1]
    signs=[]
    for q in legs:
        margin=(h+a)*4-q if hit["market"]=="OU" else (h-a)*4+q
        if hit["side"] in ["under","away"]:
            margin=-margin
        signs.append((margin>0)-(margin<0))
    p=sum((num(hit["hk"]) if x>0 else D(-1) if x<0 else D(0) for x in signs),D(0))/len(signs)
    label={D(1):"W",D(".5"):"HW",D(0):"P",D("-.5"):"HL",D(-1):"L"}[D(sum(signs))/len(signs)]
    return {**hit,"result":label,"pnl":float(p),"score":f"{h}:{a}","result_at":f["fetched_at"]}
def window_stats(history,n):
    rr=sorted(history,key=lambda r:(r["ko"],int(r["sid"])))[-n:]
    c=Counter(r["result"] for r in rr)
    den=len(rr)-c["P"];wins=c["W"]+c["HW"]
    pnl=sum((num(r["pnl"]) for r in rr),D(0))
    passed=len(rr)==n and den>=n*4//5 and wins*100>=(95 if n==20 else 90)*den and pnl>0
    return {"n":len(rr),"required_n":n,**{k:c[k] for k in ["W","HW","P","HL","L"]},
            "den":den,"wins":wins,"hit":wins/den if den else None,"pnl":float(pnl),
            "pass":passed,"sids":[r["sid"] for r in rr]}
def gates(history):
    a,b=window_stats(history,20),window_stats(history,30)
    return {"20":a,"30":b,"pass":a["pass"] or b["pass"]}
def key(hit):
    return f"{hit['sid']}:{hit['market']}:{hit['side']}:{num(hit['line']):g}"
def choose_batch(live,gate_by_rule,now,activated_at,locked=False,attempted=None):
    """A batch is one kickoff timestamp; append only to that open batch."""
    if locked and (not isinstance(locked,dict) or not locked.get("kickoff_utc")):
        return []
    selected={}
    attempted=attempted or set()
    for hit in live:
        if not now+1500<hit["ko"] or hit["six_t5_min_at"]<activated_at:
            continue
        if locked and hit["ko"]!=locked["kickoff_utc"]:
            continue
        rid=hit["rule_id"]
        if not gate_by_rule[rid]["pass"] or key(hit) in attempted:
            continue
        if key(hit) not in selected:
            selected[key(hit)]={**hit,"rules":[],"gate_evidence":{}}
        selected[key(hit)]["rules"].append(rid)
        selected[key(hit)]["gate_evidence"][rid]=gate_by_rule[rid]
    rows=list(selected.values())
    if not locked and rows:
        first_ko=min(h["ko"] for h in rows)
        rows=[h for h in rows if h["ko"]==first_ko]
    return rows
