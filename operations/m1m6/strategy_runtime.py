"""Per-strategy locks survive registry revisions and share duplicate branches."""
import json
from policy import gates,key
from dynamic_rules import universe,matched_rows,description


def collect(registry,source,now):
    rows,reasons=universe(source,now)
    history={};live=[];observations=[]
    for s in registry["strategies"]:
        history[s["id"]]=[]
        for r in matched_rows(rows,s):
            hit={**r,"rule_id":s["id"],"strategy_version":s["version"],
                 "strategy_description":s.get("description") or description(s["clauses"]),
                 "strategy_lock_ids":s["lock_ids"]}
            result=hit if hit.get("result") else None
            observations.append((hit,result))
            if result:
                history[s["id"]].append(result)
            if s["active"] and r["ko"]>now and r["six_t5_min_at"]>=s["version_at"]:
                live.append(hit)
    return history,live,observations,source[2],reasons


def schema(db):
    db.executescript("""
    CREATE TABLE IF NOT EXISTS strategy_batches(
      id INTEGER PRIMARY KEY, strategy_id TEXT NOT NULL, version INTEGER NOT NULL,
      ko INTEGER NOT NULL, created_at INTEGER NOT NULL, closed_at INTEGER,
      lock_ids TEXT NOT NULL);
    CREATE TABLE IF NOT EXISTS strategy_batch_items(
      batch_id INTEGER NOT NULL, bet_key TEXT NOT NULL, evidence TEXT NOT NULL,
      PRIMARY KEY(batch_id,bet_key));
    """)


def reconcile(db,now):
    schema(db)
    pending=[]
    for r in db.execute("SELECT * FROM strategy_batches WHERE closed_at IS NULL ORDER BY id").fetchall():
        links=db.execute("""SELECT i.status,i.result_json FROM strategy_batch_items x
            LEFT JOIN items i ON i.bet_key=x.bet_key WHERE x.batch_id=?""",(r["id"],)).fetchall()
        unresolved=sum(x["status"] is None or (
            x["status"] not in ("skipped","rejected") and not x["result_json"]) for x in links)
        if links and not unresolved:
            db.execute("UPDATE strategy_batches SET closed_at=? WHERE id=?",(now,r["id"]))
        else:
            pending.append({**dict(r),"waiting":unresolved,"lock_ids":json.loads(r["lock_ids"])})
    db.commit()
    return pending


def locks_for(rule,pending):
    return [b for b in pending if set(b["lock_ids"])&set(rule.get("lock_ids",[rule["id"]]))]


def choose(live,gate_by_rule,now,activated_at,pending,rules,items):
    byrule={r["id"]:r for r in rules}
    candidates={}
    for h in live:
        rid=h["rule_id"]
        if not byrule[rid]["active"] or not gate_by_rule[rid]["pass"]:
            continue
        if now+1500>=h["ko"] or h["six_t5_min_at"]<max(activated_at,byrule[rid]["version_at"]):
            continue
        old=items.get(key(h))
        if old and (old["result_json"] or old["status"] not in ("sent","uncertain","sending")):
            continue
        candidates.setdefault(rid,[]).append(h)
    # Shared ancestry also constrains distinct branches selected in THIS scan.
    selected={};reservations=list(pending)
    for rid in sorted(candidates):
        rule=byrule[rid];hits=candidates[rid]
        locks=locks_for(rule,reservations)
        kos={r["ko"] for r in locks}
        if len(kos)>1:
            continue
        ko=next(iter(kos)) if kos else min(h["ko"] for h in hits)
        chosen=[h for h in hits if h["ko"]==ko]
        if not chosen:
            continue
        reservations.append({"ko":ko,"lock_ids":rule["lock_ids"]})
        for h in chosen:
            k=key(h)
            if k not in selected:
                selected[k]={**h,"rules":[],"gate_evidence":{},"rule_descriptions":{},"rule_versions":{}}
            q=selected[k];q["rules"].append(rid);q["gate_evidence"][rid]=gate_by_rule[rid]
            q["rule_descriptions"][rid]=h["strategy_description"]
            q["rule_versions"][rid]=h["strategy_version"]
    return list(selected.values())


def attach(db,selected,rules,now):
    byrule={r["id"]:r for r in rules}
    for h in selected:
        for rid in h["rules"]:
            rule=byrule[rid]
            row=db.execute("SELECT id FROM strategy_batches WHERE strategy_id=? AND ko=? AND closed_at IS NULL",
                           (rid,h["ko"])).fetchone()
            if row:
                bid=row["id"]
            else:
                bid=db.execute("""INSERT INTO strategy_batches(strategy_id,version,ko,created_at,lock_ids)
                    VALUES(?,?,?,?,?)""",(rid,rule["version"],h["ko"],now,json.dumps(rule["lock_ids"]))).lastrowid
            db.execute("INSERT OR IGNORE INTO strategy_batch_items VALUES(?,?,?)",
                       (bid,key(h),json.dumps({"gate":h["gate_evidence"][rid],
                        "version":rule["version"],"description":h["rule_descriptions"][rid]},ensure_ascii=False)))
    db.commit()


def project_rules(registry,pending,gate_by_rule):
    out=[]
    for s in registry["strategies"]:
        locks=locks_for(s,pending)
        out.append({**s,"gate":gate_by_rule[s["id"]],"pending_batches":locks,
                    "pending_result_count":sum(x["waiting"] for x in locks)})
    return out
