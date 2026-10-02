"""Frozen feature grammar, exhaustive three-family search, versioned merging."""
import collections
import hashlib
import itertools
import json
import re
from decimal import Decimal as D
from policy import evaluate, valid_result, settle, gates, fmt, num, key, RULES, THRESHOLDS, GATE_VERSION, MIN_HK, MIN_DECIMAL_ODDS, eligible_price

GRAMMAR="crown-grid-0to3-families-v1"
DIR={"home":"買主","away":"買客","over":"買大","under":"買細"}
GROUPS=[("OU","over"),("OU","under"),("AH","home"),("AH","away")]


def atom(family,value,op="eq"):
    return {"family":family,"op":op,"value":value}


def signature(group,clauses):
    normalized=sorted([sorted([(a["family"],a["op"],a["value"]) for a in c],key=str) for c in clauses],key=str)
    return hashlib.sha256(json.dumps([group,normalized],sort_keys=True).encode()).hexdigest()[:20]


def matches(row,clauses):
    def one(a):
        x=row["features"].get(a["family"])
        if x is None:
            return False
        v=a["value"]
        return x==v if a["op"]=="eq" else x<=v if a["op"]=="le" else x>=v
    return any(all(one(a) for a in c) for c in clauses)


def tier(name):
    if name in ["英超","西甲","意甲","德甲","法甲"]:
        return "五大聯賽"
    if "女" in name:
        return "女子"
    if re.search(r"U\d",name):
        return "青年"
    if "杯" in name or "盃" in name:
        return "盃賽"
    if any(x in name for x in ["后备","後備","预备","預備"]):
        return "預備"
    return "名稱低組" if name.endswith(("乙","丙","丁")) else "其他"


def saved_model(cp,stage,market,ko,now,line):
    c=cp.get(stage,{})
    at=max([c.get("locked_at_ms") or 0]+[
        r["at_ms"] for r in c.get("prediction_repairs",[]) if market.lower() in r.get("markets",[])])
    if not 0<at<ko or at>now:
        return None,None
    p=c.get("prediction") or {}
    v=p.get("pred_"+market.lower()) or ""
    side=("over" if "大" in v else "under" if "細" in v or "细" in v else None) if market=="OU" else (
        "home" if "主" in v else "away" if "客" in v else None)
    f=p.get("fair_lines") or {}
    fair=f.get("ou_p_over_nv" if market=="OU" else "ah_p_home_nv")
    ownline=f.get("ou_h" if market=="OU" else "ah_h")
    try:
        if ownline is None or num(ownline)!=num(line) or not 0<num(fair)<1:
            fair=None
    except (ValueError,TypeError,ArithmeticError):
        fair=None
    return side,float(fair) if fair is not None else None


def feature_rows(m,snaps,cp,finished,now):
    # Reuse exactly the original six-snapshot and market quality gate.
    _,reason=evaluate(m,snaps,cp,now)
    if reason not in ("matched","fixed_conditions_not_met"):
        return [],reason
    sid=str(m["sid"]);ko=m["kickoff_utc"]
    ft={"ah_line":float(snaps[("T5","AH")]["handicap"]),
        "ou_line":float(snaps[("T5","OU")]["handicap"]),
        "hour":int((ko/3600000+8)%24)//4,"tier":tier(m.get("league") or ""),
        "home_price_high":num(snaps[("T5","AH")]["home_odds"])>=D("1.10"),
        "ah30_max_low":max(num(snaps[("T30","AH")][x]) for x in ("home_odds","away_odds"))<D("1.10")}
    for market in ("AH","OU"):
        for start,label in [("initial","初盤至T5"),("T30","T30至T5")]:
            a,b=snaps[(start,market)],snaps[("T5",market)]
            d=num(b["handicap"])-num(a["handicap"])
            ft[f"{market}_{label}_line"]="上升" if d>=D(".25") else "下降" if d<=D("-.25") else "不變"
            for field,side in [("home_odds","主邊"),("away_odds","客邊")]:
                delta=num(b[field])-num(a[field])
                ft[f"{market}_{label}_{side}_drop"]=(
                    "下降≥0.03" if delta<=D("-.03") else "上升≥0.03" if delta>=D(".03") else "變動<0.03") if d==0 else None
        lean=[]
        for stage in ("initial","T30","T5"):
            q=snaps[(stage,market)]
            d=num(q["home_odds"])-num(q["away_odds"])
            lean.append("主邊" if d<=D("-.02") else "客邊" if d>=D(".02") else "平衡")
        ft[market+"_leanpath"]="/".join(lean)
    rows=[]
    for market,side in GROUPS:
        q=snaps[("T5",market)]
        hk=num(q["home_odds" if side in ("home","over") else "away_odds"])
        if not MIN_HK<=hk<=D("1.20"):
            continue
        f=dict(ft)
        f["price_band"]=min(5,int((hk-D(".60"))/D(".10")))
        chosen=num(q["handicap"])*(-1 if side=="away" else 1)
        f["chosen_line"]=float(chosen)
        f["selected_handicap"]=("受讓" if chosen>0 else "讓球" if chosen<0 else "平手") if market=="AH" else None
        fair=None
        for stage in ("INITIAL","T5"):
            md,p=saved_model(cp,stage,market,ko,now,q["handicap"])
            f[stage+"_model_relation"]="同向" if md==side else "反向" if md else None
            if stage=="T5":
                fair=p
        if fair is not None:
            p=fair if side in ("over","home") else 1-fair
            f["fair_band"]="≥0.52" if p>=.52 else "<0.48" if p<.48 else "0.48至<0.52"
        else:
            f["fair_band"]=None
        row={"sid":sid,"ko":ko,"ko_hkt":fmt(ko),"home":m.get("home",""),"away":m.get("away",""),
             "league":m.get("league",""),"market":market,"side":side,"line":float(q["handicap"]),
             "hk":float(hk),"features":f,"t5_at":q["captured_at"],
             "snapshot_evidence":{stage+"_"+mkt:{k:v.get(k) for k in
                 ("handicap","home_odds","away_odds","captured_at")} for (stage,mkt),v in snaps.items()
                 if stage in ("initial","T30","T5") and mkt in ("AH","OU")},
             "model_evidence":{stage:{k:(cp.get(stage) or {}).get(k) for k in
                 ("locked_at_ms","prediction_repairs")} for stage in ("INITIAL","T5")},
             "six_t5_min_at":min(snaps[("T5",x)]["captured_at"] for x in ("AH","OU")),
             "result":None,"pnl":None}
        if valid_result(finished.get(sid),ko,now):
            row=settle(row,finished[sid])
        rows.append(row)
    return rows,"valid"


def universe(source,now):
    ms,snaps,finished,cps=source
    rows=[];reasons=collections.Counter()
    for m in ms:
        rr,reason=feature_rows(m,snaps[str(m["sid"])],cps.get(str(m["sid"]),{}),finished,now)
        rows.extend(rr);reasons[reason]+=1
    return sorted(rows,key=lambda r:(r["ko"],int(r["sid"]))),dict(reasons)


def label(a):
    f,v,op=a["family"],a["value"],a["op"]
    sym={"eq":"＝","le":"≤","ge":"≥"}[op]
    if f in ("ah_line","ou_line"):
        return ("T5主隊視角讓球" if f=="ah_line" else "T5大小")+sym+str(v)
    if f=="price_band":
        return f"投注港賠{.6+v*.1:.2f}至{'≤' if v==5 else '<'}{.7+v*.1:.2f}"
    if f=="hour":
        return f"香港開賽{v*4:02}:00至{(v+1)*4:02}:00前"
    names={"tier":"聯賽名稱分類","INITIAL_model_relation":"初盤模型與投注方向",
           "T5_model_relation":"T5模型與投注方向","fair_band":"T5同線模型選邊公平概率",
           "selected_handicap":"投注方向讓受讓","home_price_high":"T5主隊港賠≥1.10",
           "ah30_max_low":"T30讓球兩邊最高港賠<1.10"}
    if f.endswith("_leanpath"):
        market=f.split("_")[0]
        return ("讓球" if market=="AH" else "大小")+"三時點低水傾向"+sym+str(v).replace("主邊","主" if market=="AH" else "大").replace("客邊","客" if market=="AH" else "細")
    name=names.get(f,f.replace("AH","讓球").replace("OU","大小").replace("_主邊_drop","主邊水位").replace("_客邊_drop","客邊水位").replace("_line","盤線").replace("_",""))
    name=name.replace("主邊","大球" if f.startswith("OU") else "主隊").replace("客邊","細球" if f.startswith("OU") else "客隊")
    return name+sym+("是" if v is True else "否" if v is False else str(v))+("（同盤線）" if f.endswith("_drop") else "")


def description(clauses):
    return " 或 ".join("（"+"；".join(label(a) for a in c)+"）" for c in clauses)


def bits(indices):
    mask=0
    for i in indices:
        mask|=1<<i
    return mask


def indices(mask):
    while mask:
        bit=mask&-mask
        yield bit.bit_length()-1
        mask^=bit


def atoms_for(rows):
    if not rows:
        return []
    if not rows:
        return []
    out=[]
    def add(f,v,op="eq"):
        a=atom(f,v,op)
        mask=bits(i for i,r in enumerate(rows) if matches(r,[[a]]))
        if mask:
            out.append((a,mask))
    for f in ("ah_line","ou_line"):
        for v in sorted({r["features"][f] for r in rows}):
            add(f,v)
        for v in ([-.75,0,.75] if f=="ah_line" else [2.25,2.75,3.25]):
            add(f,v,"le");add(f,v,"ge")
    for v in range(6):
        add("price_band",v)
    for f in sorted(set(rows[0]["features"])-{"ah_line","ou_line","price_band","chosen_line"}):
        for v in sorted({r["features"].get(f) for r in rows if r["features"].get(f) is not None},key=str):
            add(f,v)
    return out


def scan(rows):
    rows=[r for r in rows if eligible_price(r)]
    found=[];counts=collections.Counter()
    for market,side in GROUPS:
        group=market+"_"+side
        rr=[r for r in rows if (r["market"],r["side"])==(market,side)]
        aa=atoms_for(rr);full=(1<<len(rr))-1
        settled=bits(i for i,r in enumerate(rr) if r["result"])
        win=bits(i for i,r in enumerate(rr) if r["result"] in ("W","HW"))
        push=bits(i for i,r in enumerate(rr) if r["result"]=="P")
        combos=itertools.chain([()],*(itertools.combinations(range(len(aa)),d) for d in (1,2,3)))
        for combo in combos:
            if len({aa[i][0]["family"] for i in combo})!=len(combo):
                continue
            counts["enumerated"]+=1
            mask=full
            for i in combo:
                mask&=aa[i][1]
            sm=mask&settled
            if sm.bit_count()<20:
                continue
            counts["at_least20"]+=1
            rest=sm;last=0;passed=False
            for n in range(1,min(30,sm.bit_count())+1):
                bit=1<<(rest.bit_length()-1);rest^=bit;last|=bit
                if n not in (20,30):
                    continue
                den=n-(last&push).bit_count()
                if den>=n*4//5 and (last&win).bit_count()*100>=THRESHOLDS[n]*den:
                    g=gates([rr[i] for i in indices(sm)])
                    if g["pass"]:
                        passed=True;break
            if passed:
                clauses=[[aa[i][0] for i in combo]]
                found.append({"market":market,"side":side,"clauses":clauses,
                              "fingerprint":signature(group,clauses),"gate":g})
                counts["passing_raw"]+=1
    return found,dict(counts)


def implied(a,b):
    # a narrower than b, with exactly comparable scalar atoms.
    def covers(x,y):
        if x["family"]!=y["family"]:
            return False
        xv,yv=x["value"],y["value"]
        if x["op"]=="eq":
            return xv==yv if y["op"]=="eq" else xv<=yv if y["op"]=="le" else xv>=yv
        return x["op"]==y["op"] and (xv<=yv if y["op"]=="le" else xv>=yv)
    return all(any(covers(x,y) for x in a) for y in b)


def related(a,b):
    if (a["market"],a["side"])!=(b["market"],b["side"]):
        return False
    for x in a["clauses"]:
        for y in b["clauses"]:
            if implied(x,y) or implied(y,x):
                return True
            common={json.dumps(t,sort_keys=True) for t in x}&{json.dumps(t,sort_keys=True) for t in y}
            if len(common)>=2:
                return True
    return False


def matched_rows(rows,s):
    return [r for r in rows if eligible_price(r) and (r["market"],r["side"])==(s["market"],s["side"]) and matches(r,s["clauses"])]


def merge(found,rows):
    # Explicit OR, never erase a distinct future branch merely for equal past IDs.
    groups=[]
    for c in sorted(found,key=lambda s:(s["market"],s["side"],len(s["clauses"][0]),s["fingerprint"])):
        for g in groups:
            if not related(c,g):
                continue
            old=matched_rows(rows,g);new=matched_rows(rows,c)
            a={key(r) for r in old};b={key(r) for r in new}
            logical=any(implied(x,y) or implied(y,x) for x in c["clauses"] for y in g["clauses"])
            if not logical and len(a&b)/max(1,len(a|b))<.8:
                continue
            union=g["clauses"]+c["clauses"]
            union=[x for i,x in enumerate(union) if not any(
                i!=j and implied(x,y) and (not implied(y,x) or j<i) for j,y in enumerate(union))]
            merged={**g,"clauses":union}
            evidence=gates([r for r in matched_rows(rows,merged) if r["result"]])
            if not evidence["pass"]:
                continue
            g.update(clauses=union,gate=evidence)
            g["members"]=sorted(set(g["members"]+[c["fingerprint"]]))
            break
        else:
            groups.append({**c,"members":[c["fingerprint"]]})
    # A broad branch encountered later can bridge two earlier groups.
    # Finish to a fixed point rather than leaving order-dependent duplicates.
    changed=True
    while changed:
        changed=False
        for i,a in enumerate(groups):
            for j in range(i+1,len(groups)):
                b=groups[j]
                if not related(a,b):
                    continue
                x={key(r) for r in matched_rows(rows,a)}
                y={key(r) for r in matched_rows(rows,b)}
                logical=any(implied(c,e) or implied(e,c) for c in a["clauses"] for e in b["clauses"])
                if not logical and len(x&y)/max(1,len(x|y))<.8:
                    continue
                cc=a["clauses"]+b["clauses"]
                cc=[c for k,c in enumerate(cc) if not any(k!=l and implied(c,e) and
                    (not implied(e,c) or l<k) for l,e in enumerate(cc))]
                merged={**a,"clauses":cc}
                g=gates([r for r in matched_rows(rows,merged) if r["result"]])
                if g["pass"]:
                    a.update(clauses=cc,gate=g,members=sorted(set(a["members"]+b["members"])))
                    groups.pop(j);changed=True;break
            if changed:
                break
    return groups


def seeds():
    a=atom
    cc=[
      [[a("ou_line",3.25),a("ah_line",.75,"le"),a("AH_T30至T5_主邊_drop","下降≥0.03")],
       [a("ou_line",3.25),a("ah_line",.75,"le"),a("AH_T30至T5_客邊_drop","上升≥0.03")]],
      [[a("hour",0),a("INITIAL_model_relation","同向"),a("AH_初盤至T5_主邊_drop","下降≥0.03")]],
      [[a("ou_line",2.25),a("hour",0),a("AH_leanpath","主邊/主邊/主邊")]],
      [[a("price_band",0),a("OU_T30至T5_主邊_drop","變動<0.03"),a("OU_初盤至T5_客邊_drop","上升≥0.03")]],
      [[a("ah_line",1.25),a("AH_初盤至T5_line","上升"),a("INITIAL_model_relation","反向")]],
      [[a("ah_line",0,"ge"),a("hour",5),a("OU_初盤至T5_客邊_drop","下降≥0.03")]],
    ]
    return [{**r,"clauses":c,"version":1,"version_at":0,"born_at":0,"active":False,
             "lock_ids":[r["id"]],"members":[signature(r["market"]+"_"+r["side"],c)]}
            for r,c in zip(RULES,cc)]


def registry(groups,rows,previous,now):
    old=previous.get("strategies",seeds())
    used=set();out=[];sequence=previous.get("sequence",0)
    for g in groups:
        parents=[]
        for s in old:
            if set(s.get("members",[]))&set(g["members"]):
                parents.append(s);continue
            if not related(s,g):
                continue
            logical=any(implied(x,y) or implied(y,x) for x in s["clauses"] for y in g["clauses"])
            a={key(r) for r in matched_rows(rows,s)};b={key(r) for r in matched_rows(rows,g)}
            if logical or len(a&b)/max(1,len(a|b))>=.8:
                parents.append(s)
        exact=[s for s in parents if signature(s["market"]+"_"+s["side"],s["clauses"])==signature(g["market"]+"_"+g["side"],g["clauses"])]
        choices=sorted(exact+parents,key=lambda s:(s not in exact,s["id"]))
        pick=next((s for s in choices if s["id"] not in used),None)
        if pick:
            rid=pick["id"]
        else:
            sequence+=1;rid=f"D{sequence:04}"
        used.add(rid)
        same=bool(pick and signature(pick["market"]+"_"+pick["side"],pick["clauses"])==signature(g["market"]+"_"+g["side"],g["clauses"]))
        lock_ids=sorted({rid}|{i for s in parents for i in s.get("lock_ids",[s["id"]])})
        out.append({**g,"id":rid,"label":DIR[g["side"]]+"自動組合",
                    "description":description(g["clauses"]),"active":True,
                    "version":pick.get("version",1)+(not same) if pick else 1,
                    "version_at":pick["version_at"] if same else now,
                    "born_at":pick.get("born_at",now) if pick else now,
                    "lock_ids":lock_ids})
    # Preserve all inactive definitions and their pending accounting, never delete losers.
    for s in old:
        if s["id"] not in used:
            out.append({**s,"active":False,"gate":gates([r for r in matched_rows(rows,s) if r["result"]])})
    checks=[{"id":s["id"],"version":s["version"],
             "gate":gates([r for r in matched_rows(rows,s) if r["result"]])} for s in old]
    return {"grammar":GRAMMAR,"gate_version":GATE_VERSION,"thresholds":THRESHOLDS,
            "min_decimal_odds":MIN_DECIMAL_ODDS,
            "updated_at":now,"next_search_at":now+3*3600000,
            "sequence":sequence,"strategies":out,"existing_strategy_checks":checks}
