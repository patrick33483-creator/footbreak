"""Two-fixture evidence export. No result, ledger, registry or service writes."""
import json
from pathlib import Path
import sqlite3
import time

from recent_five_audit import export as frozen_export, compact
from notifier import source
from strategy_runtime import collect
from policy import gates, settle

SCORES = {"3096888": ((0, 2), (1, 3)), "3095046": ((2, 2), (3, 2))}
BASE = Path("/var/lib/crown-m1m6")


def readonly(path):
    db = sqlite3.connect(f"file:{path}?mode=ro", uri=True, timeout=20)
    db.row_factory = sqlite3.Row
    db.execute("PRAGMA query_only=ON")
    db.execute("BEGIN")
    return db


def export():
    result = frozen_export()
    now = int(time.time() * 1000)
    registry = json.loads((BASE / "registry.json").read_text())
    src = source(now)
    history, _, _, _, _ = collect(registry, src, now)
    rules = {s["id"]: s for s in registry["strategies"]}
    current = []
    for rid, rows in history.items():
        affected = [r for r in rows if r["sid"] in SCORES]
        if not affected:
            continue
        scenarios = {}
        for label, index in (("old", 0), ("verified", 1)):
            replay = []
            for row in rows:
                if row["sid"] in SCORES:
                    h, a = SCORES[row["sid"]][index]
                    row = settle(row, {"home_score": h, "away_score": a, "fetched_at": now})
                replay.append(row)
            scenarios[label] = gates(replay)
        current.append({"rule": rules[rid], "actual": gates(rows), **scenarios,
                        "affected_rows": [compact(r) for r in affected],
                        "last30": [compact(r) for r in sorted(rows, key=lambda r: (r["ko"], int(r["sid"])))[-30:]]})
    db = readonly("/opt/crown-radar-v2/data/crown.db")
    try:
        raw = {}
        for sid in SCORES:
            raw[sid] = {}
            for table in ("matches", "finished_matches", "strategy_result_sync_audit"):
                raw[sid][table] = [dict(r) for r in db.execute(f"SELECT * FROM {table} WHERE sid=?", (sid,))]
        db.rollback()
    finally:
        db.close()
    db = readonly(BASE / "ledger.sqlite")
    try:
        all_observations = []
        target_items = []
        for sid in SCORES:
            for row in db.execute("SELECT * FROM observations WHERE sid=?", (sid,)):
                r = dict(row)
                r["payload"] = compact(json.loads(r["payload"]))
                r["result_json"] = compact(json.loads(r["result_json"])) if r["result_json"] else None
                all_observations.append(r)
            for row in db.execute("SELECT * FROM items WHERE sid=?", (sid,)):
                r = dict(row)
                r["payload"] = json.loads(r["payload"])
                r["result_json"] = json.loads(r["result_json"]) if r["result_json"] else None
                target_items.append(r)
        db.rollback()
    finally:
        db.close()
    # Archives are evidence only; do not recalculate discovery or rewrite them.
    archives = []
    for path in sorted((BASE / "research_history").glob("*.json")):
        archive = json.loads(path.read_text())
        if any(sid in json.dumps(archive) for sid in SCORES):
            archives.append({"filename": path.name, "data": archive})
    result.update(current_strategies=current, source_evidence=raw,
                  target_observations=all_observations, target_items=target_items,
                  research_archives=archives)
    result["summary"].update(action="score_conflict_readonly", current_at_ms=now,
                             registry_strategy_count=len(rules),
                             current_affected_strategy_count=len(current),
                             archive_count=len(archives), sends=0, database_writes=0)
    return result
