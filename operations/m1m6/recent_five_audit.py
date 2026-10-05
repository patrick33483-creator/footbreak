"""Export frozen notification-window membership and matching history, read-only."""
import json
from pathlib import Path
import sqlite3
import time

BASE=Path("/var/lib/crown-m1m6")


def compact(payload):
    return {k:payload.get(k) for k in (
        "sid","rule_id","strategy_version","ko","home","away","league",
        "market","side","line","hk","result","pnl","score","result_at","t5_at")}


def export():
    db=sqlite3.connect(f"file:{BASE/'ledger.sqlite'}?mode=ro",uri=True,timeout=20)
    db.row_factory=sqlite3.Row
    db.execute("PRAGMA query_only=ON")
    db.execute("BEGIN")
    try:
        items=[];wanted=set()
        for row in db.execute("SELECT * FROM items ORDER BY ko,sid,bet_key"):
            item=dict(row)
            item["payload"]=json.loads(item["payload"])
            item["result_json"]=json.loads(item["result_json"]) if item["result_json"] else None
            if not any(r.startswith("D") for r in item["payload"].get("rules",[])):
                continue
            items.append(item)
            for rid,g in item["payload"].get("gate_evidence",{}).items():
                if rid.startswith("D"):
                    for n in ("20","30"):
                        wanted.update((str(sid),rid) for sid in g.get(n,{}).get("sids",[]))
        observations=[]
        for sid,rid in sorted(wanted):
            row=db.execute("SELECT * FROM observations WHERE sid=? AND rule_id=?",(sid,rid)).fetchone()
            if row:
                payload=json.loads(row["payload"])
                result=json.loads(row["result_json"]) if row["result_json"] else None
                observations.append({"sid":sid,"rule_id":rid,"first_seen_at":row["first_seen_at"],
                                     "original":compact(payload),
                                     "result":compact(result) if result else None})
        db.rollback()
    finally:
        db.close()
    # Independent immutable importer audit can corroborate scores available then.
    crown=sqlite3.connect("file:/opt/crown-radar-v2/data/crown.db?mode=ro",uri=True,timeout=20)
    crown.row_factory=sqlite3.Row
    crown.execute("PRAGMA query_only=ON")
    crown.execute("BEGIN")
    evidence=[]
    try:
        if crown.execute("SELECT 1 FROM sqlite_master WHERE name='strategy_result_sync_audit'").fetchone():
            for sid in sorted({s for s,r in wanted}):
                for row in crown.execute("""SELECT observed_at,written_at,evidence_json
                        FROM strategy_result_sync_audit WHERE sid=? ORDER BY written_at""",(sid,)):
                    proof=json.loads(row["evidence_json"])
                    evidence.append({"sid":sid,"observed_at":row["observed_at"],"written_at":row["written_at"],
                                     "home_score":proof.get("home_score"),"away_score":proof.get("away_score")})
        crown.rollback()
    finally:
        crown.close()
    config=json.loads(Path("/etc/crown-m1m6.json").read_text())
    return {"summary":{"action":"recent_five_readonly","at_ms":int(time.time()*1000),
                       "gate_version":config.get("gate_version"),"requested_history_pairs":len(wanted),
                       "exported_history_pairs":len(observations),"sends":0,"database_writes":0},
            "items":items,"observations":observations,"result_import_evidence":evidence}
