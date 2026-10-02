"""Read-only evidence collection. Never invoke notifier tick or Telegram API."""
import collections
import fcntl
import datetime
import importlib.util
import json
import re
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


def inspect_hkjc():
    paths = [Path("/opt/crown-radar-v2/server.js")]
    paths += [Path("/opt/odds-radar/server.js"), Path("/opt/footbreak/v2/telegram.py"),
              Path("/opt/footbreak/v2/config.py")]
    paths += list(Path("/usr/local/bin").glob("*notify*.py"))
    paths += list(Path("/opt").glob("*/*telegram*.py"))
    paths += list(Path("/opt").glob("*/*notify*.py"))
    excerpts = {}
    for path in paths:
        if not path.is_file():
            continue
        lines = path.read_text(errors="replace").splitlines()
        hits = set()
        for i, line in enumerate(lines):
            if re.search(r"notifyRPin|r_pin2|HKJC|馬會|hkjc|TELEGRAM_ENABLED", line):
                hits.update(range(max(0, i-3), min(len(lines), i+7)))
        if path.name == "server.js":
            for i, line in enumerate(lines):
                if re.search(r"(async )?function .*([Nn]otify|sendTelegram)|setInterval.*[Nn]otif", line):
                    hits.update(range(max(0, i-2), min(len(lines), i+12)))
            if "crown-radar-v2" in str(path):
                hits = set(range(1895, min(len(lines), 1998)))
        excerpts[str(path)] = [
            f"{i+1}: {lines[i]}" for i in sorted(hits)
            if not re.search(r"token|secret|password|api.key", lines[i], re.I)
        ][:240]
    units = subprocess.run(
        ["systemctl", "list-timers", "--all", "--no-pager"],
        capture_output=True, text=True, timeout=20,
    ).stdout
    flags = {}
    for p in [Path("/etc/footbreak.env"), Path("/opt/odds-radar/.env"),
              Path("/opt/crown-radar-v2/.env")]:
        if p.exists():
            flags[str(p)] = [line for line in p.read_text().splitlines()
                if re.match(r"^[A-Z_]*(?:TELEGRAM|NOTIFY)[A-Z_]*ENABLED\s*=\s*[01]\s*$", line)]
    return {"summary": {"action": "inspect_hkjc_notifications"}, "flags": flags,
            "excerpts": excerpts, "timers": units,
            "containers": subprocess.run(["docker","ps","--format","{{.Names}} {{.Image}}"],
                         capture_output=True,text=True,timeout=20).stdout,
            "opt_dirs": [p.name for p in Path("/opt").iterdir() if p.is_dir()]}


def audit():
    now = int(time.time() * 1000)
    base = Path("/var/lib/crown-m1m6")
    config = json.loads(Path("/etc/crown-m1m6.json").read_text())
    with (base/"run.lock").open("a") as lock:
        fcntl.flock(lock,fcntl.LOCK_EX)
        public = json.loads((base / "status.json").read_text())
        status_copies=[json.loads(p.read_text()) for p in
            [Path("/var/www/crownsystem-v3/m1m6_status.json"),
             Path("/opt/crown-radar-v2/data/m1m6_status.json")]]
        now=int(time.time()*1000)
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
    result_sync_path=Path("/var/lib/crown-strategy-results/last-run.json")
    result_sync=json.loads(result_sync_path.read_text()) if result_sync_path.exists() else {}
    return {
        "summary": {
            "action": "audit_natural_notifications", "at_ms": now,
            "version": config.get("version"), "mode": config.get("mode"),
            "gate_version": config.get("gate_version"), "thresholds": config.get("thresholds"),
            "gate": config.get("gate"),
            "min_decimal_odds":config.get("min_decimal_odds"),"tg_format":config.get("tg_format"),
            "batch_lock_enabled": public.get("batch_lock_enabled",True),
            "pending_result_count": public.get("pending_result_count"),
            "overlap_allowed": config.get("batch_lock_enabled") is False or config.get("lock_scope")=="per_strategy",
            "public_copies_consistent": all(s==public for s in status_copies),
            "lock_scope": config.get("lock_scope"),
            "strategy_locks":public.get("strategy_pending_batches",[]),
            "search":public.get("search"),
            "registry_updated_at":public.get("registry_updated_at"),
            "next_search_at":public.get("next_search_at"),
            "existing_strategy_checks":public.get("existing_strategy_checks",[]),
            "dynamic_services":{name:subprocess.run(["systemctl","show",name,"-p","ActiveState","-p","SubState","-p","Result","-p","NextElapseUSecMonotonic"],capture_output=True,text=True).stdout for name in
                ["crown-strategy-search.timer","crown-strategy-search.service","crown-strategy-refresh-api.service"]},
            "research_status":json.loads((base/"research_status.json").read_text()) if (base/"research_status.json").exists() else None,
            "dynamic_page_markers":{str(p):"每條合併後策略各自封鎖" in p.read_text() and
                'method:"POST"' in p.read_text() for p in [
                    Path("/var/www/crownsystem-v3/strategy.html"),Path("/var/www/crownsystem-v3/heavy.html"),
                    Path("/opt/crown-radar-v2/strategy.html"),Path("/opt/crown-radar-v2/heavy.html")]},
            "no_lock_page_markers": {
                str(p): "不設批次封鎖" in p.read_text() and "整批正式賽果齊全前不開下一批" not in p.read_text()
                for p in [Path("/var/www/crownsystem-v3/strategy.html"),
                          Path("/var/www/crownsystem-v3/heavy.html"),
                          Path("/opt/crown-radar-v2/strategy.html"),
                          Path("/opt/crown-radar-v2/heavy.html")]},
            "activated_at": config["activated_at"], "enabled": config["enabled"],
            "status_updated_at": public["updated_at"],
            "status_age_seconds": (now-public["updated_at"])/1000,
            "batch_count": len(batches), "item_count": len(items),
            "status_counts": dict(collections.Counter(r["status"] for r in items)),
            "locked_batch": public["locked_batch"], "overlapping_batches": overlaps,
            "checks": checks, "tick_count": len(ticks),
            "error_ticks": [x for x in ticks if "error_type" in x],
            "latest_ticks": ticks[-5:],
            "result_sync": {k:result_sync.get(k) for k in
                ("checked_at_hkt","candidates","written","errors","conflicts",
                 "status","pending_candidates_after")},
            "hkjc_notification_stop_flag": Path("/opt/crown-radar-v2/data/HKJC_STRATEGY_TG_DISABLED").exists(),
        },
        "batches": batches, "items": items, "official_results": results,
        "observations": observation_counts, "upcoming": upcoming,
        "model_evidence": model_evidence,
        "public_status": public, "ticks": ticks,
    }
