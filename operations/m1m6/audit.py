"""Read-only evidence collection. Never invoke notifier tick or Telegram API."""
import collections
import datetime
import importlib.util
import json
from pathlib import Path
import sqlite3
import subprocess
import time


def connect(path):
    db = sqlite3.connect(f"file:{path}?mode=ro", uri=True, timeout=10)
    db.row_factory = sqlite3.Row
    db.execute("PRAGMA query_only=ON")
    db.execute("BEGIN")
    return db


def audit():
    now = int(time.time() * 1000)
    base = Path("/var/lib/crown-m1m6")
    config = json.loads(Path("/etc/crown-m1m6.json").read_text())
    public = json.loads((base / "status.json").read_text())
    db = connect(base / "ledger.sqlite")
    batches = [dict(r) for r in db.execute("SELECT * FROM batches ORDER BY id")]
    items = []
    for row in db.execute("SELECT * FROM items ORDER BY batch_id,attempt_at,bet_key"):
        r = dict(row)
        r["payload"] = json.loads(r["payload"])
        r["result_json"] = json.loads(r["result_json"]) if r["result_json"] else None
        items.append(r)
    observation_counts = [
        dict(r) for r in db.execute(
            "SELECT origin,COUNT(*) AS n,SUM(result_json IS NOT NULL) AS settled "
            "FROM observations GROUP BY origin"
        )
    ]
    db.rollback()
    db.close()
    crown = connect("/opt/crown-radar-v2/data/crown.db")
    upcoming = [dict(r) for r in crown.execute(
        "SELECT sid,kickoff_utc,home,away,league FROM matches "
        "WHERE kickoff_utc>? AND kickoff_utc<? ORDER BY kickoff_utc LIMIT 100",
        (now, now + 6 * 3600000),
    )]
    results = {}
    for sid in sorted({r["sid"] for r in items}):
        row = crown.execute("SELECT * FROM finished_matches WHERE sid=?", (sid,)).fetchone()
        results[sid] = dict(row) if row else None
    checkpoints = json.loads(Path("/var/lib/crownsystem-v4/checkpoints.json").read_text())
    spec = importlib.util.spec_from_file_location("live_policy", "/opt/crown-m1m6/policy.py")
    live_policy = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(live_policy)
    sample_ids = {str(r["sid"]) for r in upcoming[:25]}
    for rule in public["rules"]:
        if rule["id"] in ("M2", "M5"):
            sample_ids.update(rule["gate"]["20"]["sids"][-5:])
    model_evidence = []
    for sid in sorted(sample_ids):
        row = crown.execute("SELECT * FROM matches WHERE sid=?", (sid,)).fetchone()
        if not row:
            continue
        m = dict(row)
        cp = checkpoints.get(sid, {})
        initial = cp.get("INITIAL", {})
        snaps = {(r["stage"], r["market"]): dict(r) for r in crown.execute(
            "SELECT * FROM crown_snapshots WHERE sid=?", (sid,)
        )}
        hits, reason = live_policy.evaluate(m, snaps, cp, now)
        model_evidence.append({
            "sid": sid, "home": m.get("home"), "away": m.get("away"),
            "ko": m["kickoff_utc"], "checkpoint_exists": sid in checkpoints,
            "checkpoint_stages": list(cp), "initial_keys": list(initial),
            "initial_locked_at": initial.get("locked_at_ms"),
            "initial_pred_ah": initial.get("prediction", {}).get("pred_ah"),
            "model_read_by_live_policy": live_policy.initial_model(cp, m["kickoff_utc"], now),
            "evaluation_reason": reason, "fixed_rule_hits": [h["rule_id"] for h in hits],
            "checkpoint_identity": {k: initial[k] for k in
                ("sid", "home", "away", "kickoff_utc", "match", "fixture") if k in initial},
        })
    crown.rollback()
    crown.close()
    # Only parse the notifier's structured JSON lines, never dump arbitrary logs.
    journal = subprocess.run(
        ["journalctl", "-u", "crown-m1m6.service", "--since",
         datetime.datetime.fromtimestamp(config["activated_at"]/1000,
                                         datetime.timezone.utc).isoformat(),
         "-o", "cat", "--no-pager"], capture_output=True, text=True, timeout=30,
    )
    ticks = []
    for line in journal.stdout.splitlines():
        try:
            x = json.loads(line)
        except (ValueError, TypeError):
            continue
        if isinstance(x, dict) and ("gate_pass" in x or "error_type" in x):
            ticks.append(x)
    checks = []
    for batch in batches:
        rows = [r for r in items if r["batch_id"] == batch["id"]]
        attempted = [r for r in rows if r["status"] in ("sent", "uncertain", "sending")]
        checks.append({
            "batch_id": batch["id"],
            "items": len(rows),
            "acknowledged_pre_kickoff": sum(
                r["status"] == "sent" and r["message_id"] is not None
                and r["ack_at"] is not None and r["ack_at"] < r["ko"] for r in rows
            ),
            "unresolved_attempted": sum(r["result_json"] is None for r in attempted),
            "closed_without_all_results": batch["status"] == "closed"
                and any(r["result_json"] is None for r in attempted),
            "closed_before_result_available": bool(batch["closed_at"]) and any(
                r["result_json"] and r["result_json"]["result_at"] > batch["closed_at"]
                for r in attempted
            ),
            "all_saved_gates_pass": all(
                g["pass"] for r in rows
                for g in r["payload"].get("gate_evidence", {}).values()
            ),
        })
    overlaps = []
    for a, b in zip(batches, batches[1:]):
        if a["closed_at"] is None or b["created_at"] < a["closed_at"]:
            overlaps.append([a["id"], b["id"]])
    return {
        "summary": {
            "action": "audit_first_natural_batch", "at_ms": now,
            "activated_at": config["activated_at"], "enabled": config["enabled"],
            "status_updated_at": public["updated_at"],
            "status_age_seconds": (now-public["updated_at"])/1000,
            "batch_count": len(batches), "item_count": len(items),
            "status_counts": dict(collections.Counter(r["status"] for r in items)),
            "locked_batch": public["locked_batch"], "overlapping_batches": overlaps,
            "checks": checks, "tick_count": len(ticks),
            "error_ticks": [x for x in ticks if "error_type" in x],
            "latest_ticks": ticks[-5:],
        },
        "batches": batches, "items": items, "official_results": results,
        "observations": observation_counts, "upcoming": upcoming,
        "model_evidence": model_evidence,
        "public_status": public, "ticks": ticks,
    }
