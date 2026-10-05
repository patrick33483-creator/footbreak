"""Scoped deployment and evidence for score-revision synchronization."""
import ast
import fcntl
import hashlib
import json
from pathlib import Path
import shutil
import sqlite3
import subprocess
import tempfile
import time
from contextlib import closing

HERE = Path(__file__).parent
DEST = Path("/opt/crown-m1m6")
BASE = Path("/var/lib/crown-m1m6")
FILES = ("result_revisions.py", "result_refresh.py", "research_cycle.py", "notifier.py",
         "test_result_revisions.py", "test_result_refresh.py")


def protected_rows(db):
    result = {}
    for table in ("batches", "strategy_batches", "strategy_batch_items", "observations"):
        result[table] = [tuple(r) for r in db.execute(f"SELECT * FROM {table} ORDER BY rowid")]
    result["items"] = [tuple(r) for r in db.execute("""SELECT bet_key,batch_id,sid,ko,payload,status,
        attempt_at,ack_at,message_id,error FROM items ORDER BY bet_key""")]
    return result


def preflight():
    import notifier
    from policy import gates
    from strategy_runtime import collect
    from result_revisions import synchronize
    from performance_view import summarize, load_epoch
    for name in ("policy.py", "dynamic_rules.py", "signal_guard.py", "strategy_runtime.py",
                 "performance_view.py", "observation_store.py"):
        assert (DEST / name).read_bytes() == (HERE / name).read_bytes(), f"Unrelated change: {name}"
    def outside_tick(path):
        tree = ast.parse(path.read_text())
        tree.body = [n for n in tree.body if not isinstance(n, ast.FunctionDef) or n.name != "tick"]
        return ast.dump(tree)
    assert outside_tick(DEST / "notifier.py") == outside_tick(HERE / "notifier.py")
    tests = subprocess.run(["python3", "-m", "unittest", "discover", "-s", str(HERE), "-p", "test_*.py"],
                           capture_output=True, text=True, timeout=100)
    assert tests.returncode == 0, tests.stderr
    now = int(time.time() * 1000)
    registry = json.loads((BASE / "registry.json").read_text())
    src = notifier.source(now)
    history, _, observations, finished, _ = collect(registry, src, now)
    kos = {h["sid"]: h["ko"] for h, _ in observations}
    gate_before = {rid: gates(rows) for rid, rows in history.items()}
    with tempfile.TemporaryDirectory(prefix="crown-score-revision-") as tmp:
        with closing(sqlite3.connect(Path(tmp) / "ledger.sqlite")) as scratch:
            scratch.row_factory = sqlite3.Row
            with closing(sqlite3.connect(f"file:{BASE/'ledger.sqlite'}?mode=ro", uri=True)) as live:
                live.backup(scratch)
            before = protected_rows(scratch)
            perf_before = summarize(scratch, load_epoch(), now)
            started = time.monotonic()
            first = synchronize(scratch, finished, now, kos)
            duration = time.monotonic() - started
            assert before == protected_rows(scratch), "Immutable notification / lock / observation changed"
            again = synchronize(scratch, finished, now + 1, kos)
            assert again["items_corrected"] == again["sources_changed"] == 0
            perf_after = summarize(scratch, load_epoch(), now)
            changed = []
            for row in scratch.execute("SELECT * FROM result_score_corrections WHERE observed_at=?", (now,)):
                old = json.loads(row["old_result_json"]) if row["old_result_json"] else {}
                new = json.loads(row["new_result_json"]) if row["new_result_json"] else {}
                changed.append({"sid": row["sid"], "bet_key": row["bet_key"], "kind": row["kind"],
                                "old_score": row["old_score"], "new_score": row["new_score"],
                                "old_result": old.get("result"), "new_result": new.get("result"),
                                "old_pnl": old.get("pnl"), "new_pnl": new.get("pnl")})
    assert gate_before == {rid: gates(rows) for rid, rows in history.items()}
    return {"tests": tests.stderr, "scratch_first": first, "scratch_repeat": again,
            "scratch_seconds": duration, "expected_changes": changed,
            "original_payloads_delivery_and_locks_unchanged": True,
            "observation_rows_unchanged_by_settlement_sync": True,
            "current_period_before": perf_before["total"], "current_period_after": perf_after["total"],
            "policy_and_source_based_gates_unchanged": True, "live_database_writes": 0}


def evidence(since_ms=0):
    with closing(sqlite3.connect(f"file:{BASE/'ledger.sqlite'}?mode=ro", uri=True)) as db:
        db.row_factory = sqlite3.Row
        tables = {r[0] for r in db.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        audit = []
        if "result_score_corrections" in tables:
            for row in db.execute("SELECT * FROM result_score_corrections WHERE observed_at>=? ORDER BY id", (since_ms,)):
                old = json.loads(row["old_result_json"]) if row["old_result_json"] else {}
                new = json.loads(row["new_result_json"]) if row["new_result_json"] else {}
                audit.append({**{k: row[k] for k in ("id", "sid", "observed_at", "source_at", "old_score",
                                                     "new_score", "kind", "bet_key", "version")},
                              "old_result": old.get("result"), "new_result": new.get("result"),
                              "old_pnl": old.get("pnl"), "new_pnl": new.get("pnl")})
        revisions_exist = "revision" in {r[1] for r in db.execute("PRAGMA table_info(result_refresh_events)")}
        events = [dict(r) for r in db.execute("SELECT * FROM result_refresh_events WHERE queued_at>=?", (since_ms,))]
        target = db.execute("SELECT result_json FROM items WHERE bet_key='3096888:OU:over:2.75'").fetchone()
        target_result = json.loads(target[0]) if target and target[0] else {}
        tracked = db.execute("SELECT COUNT(*) FROM result_source_versions").fetchone()[0] if "result_source_versions" in tables else 0
    public = json.loads((BASE / "status.json").read_text())
    mirrors = [json.loads(p.read_text()) for p in (
        Path("/var/www/crownsystem-v3/m1m6_status.json"),
        Path("/opt/crown-radar-v2/data/m1m6_status.json"))]
    mirrored = all(p == public for p in mirrors)
    visible = [r for r in public["ledger"] if r["sid"] == "3096888"]
    logs = subprocess.run(["journalctl", "-u", "crown-m1m6.service", "--since", "8 minutes ago",
                           "-o", "cat", "--no-pager"], capture_output=True, text=True, timeout=15).stdout
    ticks = []
    for line in logs.splitlines():
        try:
            row = json.loads(line)
        except ValueError:
            continue
        if isinstance(row, dict) and row.get("result_revisions", {}).get("version") == "score-revision-sync-v1":
            ticks.append(row)
    research = json.loads((BASE / "research_status.json").read_text())
    services = {}
    for name in ("crown-m1m6.timer", "crown-m1m6.service", "crown-strategy-search.service"):
        services[name] = subprocess.run(
            ["systemctl", "show", name, "-p", "ActiveState", "-p", "SubState",
             "-p", "Result", "-p", "ExecMainStartTimestamp", "-p", "ExecMainExitTimestamp"],
            capture_output=True, text=True, timeout=10).stdout
    registry = json.loads((BASE / "registry.json").read_text())
    return {"at_ms": int(time.time() * 1000), "audit": audit, "refresh_events": events,
            "revisioned_queue": revisions_exist, "tracked_source_matches": tracked,
            "target_settlement": {k: target_result.get(k) for k in ("score", "result", "pnl", "result_at")},
            "public_updated_at": public["updated_at"], "public_mirrors_equal": mirrored,
            "target_public_rows": [{"sid": r["sid"], "result": {k: (r.get("result") or {}).get(k)
                                     for k in ("score", "result", "pnl")}} for r in visible],
            "performance_period": public.get("performance_period"), "research_status": research,
            "natural_ticks": ticks, "services": services,
            "registry": {"updated_at": registry["updated_at"],
                         "existing_strategy_checks": len(registry.get("existing_strategy_checks", [])),
                         "search": registry.get("search", {})},
            "runtime_hashes_match": {name: (DEST/name).read_bytes() == (HERE/name).read_bytes()
                                     for name in FILES},
            "backup_directories": [p.name for p in sorted(BASE.glob("score-revision-backup-*"))]}


def deploy():
    verified = preflight()
    protected = [BASE / "registry.json", BASE / "performance_epoch.json", Path("/etc/crown-m1m6.json"),
                 *[DEST / n for n in ("policy.py", "dynamic_rules.py", "signal_guard.py", "strategy_runtime.py")]]
    def digests():
        return {str(p): hashlib.sha256(p.read_bytes()).hexdigest() for p in protected}
    # Match the production research -> notifier lock order. No service stopped,
    # no forced tick, no reset of pending wagers or performance epoch.
    with (BASE / "research.lock").open("a") as research_lock, (BASE / "run.lock").open("a") as tick_lock:
        fcntl.flock(research_lock, fcntl.LOCK_EX)
        fcntl.flock(tick_lock, fcntl.LOCK_EX)
        started = int(time.time() * 1000)
        before = digests()
        backup = BASE / f"score-revision-backup-{started}"
        backup.mkdir()
        with closing(sqlite3.connect(f"file:{BASE/'ledger.sqlite'}?mode=ro", uri=True)) as live:
            with closing(sqlite3.connect(backup / "ledger.sqlite")) as destination:
                live.backup(destination)
        for name in FILES:
            if (DEST / name).exists():
                shutil.copy2(DEST / name, backup / name)
        try:
            for name in FILES:
                target = DEST / name
                temp = target.with_suffix(".py.tmp")
                shutil.copy2(HERE / name, temp)
                temp.replace(target)
            subprocess.run(["python3", "-m", "py_compile", *[str(DEST / n) for n in FILES]], check=True)
            assert before == digests(), "Protected configuration changed"
        except Exception:
            for name in FILES:
                if (backup / name).exists():
                    shutil.copy2(backup / name, DEST / name)
                elif (DEST / name).exists():
                    (DEST / name).unlink()
            raise
    observed = {}
    for _ in range(12):
        time.sleep(20)
        observed = evidence(started)
        # Verify the repaired ledger AND a naturally completed refresh generation.
        queued = observed["refresh_events"]
        if (observed["natural_ticks"] and observed["public_updated_at"] >= started
                and observed["target_settlement"].get("score") == "1:3"
                and observed["public_mirrors_equal"]
                and (not verified["expected_changes"] or
                     (queued and all(e["completed_at"] is not None for e in queued)))):
            break
    complete = bool(observed.get("natural_ticks") and observed.get("public_mirrors_equal")
                    and observed.get("public_updated_at", 0) >= started
                    and observed.get("target_settlement", {}).get("score") == "1:3"
                    and observed.get("target_settlement", {}).get("result") == "W"
                    and observed.get("target_settlement", {}).get("pnl") == .75
                    and (not verified["expected_changes"] or
                         (observed.get("audit") and observed.get("refresh_events")))
                    and all(e["completed_at"] is not None for e in observed.get("refresh_events", [])))
    return {"summary": {"action": "score_revision_deploy", "deployed_at": started,
                        "backup": str(backup), "files": list(FILES), "preflight": verified,
                        "protected_files_unchanged": True, "forced_sends": 0,
                        "natural_acceptance_complete": complete},
            "verification": observed}
