"""Read-only recent settled examples using the deployed policy and source DB."""
import json
from pathlib import Path
import sqlite3
import sys
import time


def examples():
    sys.path.insert(0, "/opt/crown-m1m6")
    import notifier
    import policy
    now = int(time.time()*1000)
    matches, snapshots, results, checkpoints = notifier.source(now)
    history = {r["id"]: [] for r in policy.RULES}
    for match in matches:
        sid = str(match["sid"])
        if not policy.valid_result(results.get(sid), match["kickoff_utc"], now):
            continue
        hits, _ = policy.evaluate(match, snapshots[sid], checkpoints.get(sid, {}), now)
        for hit in hits:
            row = policy.settle(hit, results[sid])
            initial = checkpoints.get(sid, {}).get("INITIAL", {})
            row["initial_model_raw"] = initial.get("prediction", {}).get("pred_ah")
            row["initial_model_locked_at"] = initial.get("locked_at_ms")
            row["official_result"] = results[sid]
            history[hit["rule_id"]].append(row)
    db = sqlite3.connect("file:/var/lib/crown-m1m6/ledger.sqlite?mode=ro", uri=True)
    db.row_factory = sqlite3.Row
    receipts = {r["bet_key"]: dict(r) for r in db.execute(
        "SELECT bet_key,batch_id,status,ack_at,message_id,payload FROM items")}
    observations = {(r["sid"],r["rule_id"]): dict(r) for r in db.execute(
        "SELECT sid,rule_id,origin,first_seen_at FROM observations")}
    selected = []
    for rule in policy.RULES:
        rows = sorted(history[rule["id"]],key=lambda r:(r["ko"],int(r["sid"])),reverse=True)[:5]
        for row in rows:
            receipt = receipts.get(policy.key(row))
            notified_rules = json.loads(receipt["payload"])["rules"] if receipt else []
            row["telegram_receipt"] = ({k:receipt[k] for k in
                ("batch_id","status","ack_at","message_id")} if receipt and rule["id"] in notified_rules else None)
            row["observation"] = observations.get((row["sid"],rule["id"]))
        selected.append({"rule":rule,"total_settled_matches":len(history[rule["id"]]),
                         "gate":policy.gates(history[rule["id"]]),"examples":rows})
    db.close()
    return {"summary":{"action":"m1m6_latest_five_examples","at_ms":now,"at_hkt":policy.fmt(now),
                       "version":policy.VERSION,"examples":sum(len(r["examples"]) for r in selected),
                       "unique_fixtures":len({x["sid"] for r in selected for x in r["examples"]}),
                       "production_writes":0,"notification_sends":0},
            "strategies":selected}
