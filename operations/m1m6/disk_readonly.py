"""Read-only disk inventory and bounded collector-error checks."""
import json
import os
from pathlib import Path
import re
import sqlite3
import subprocess
import time


def command(args, timeout=120):
    try:
        p = subprocess.run(args, capture_output=True, text=True, timeout=timeout)
        return {"code": p.returncode, "stdout": p.stdout[-45000:], "stderr": p.stderr[-1500:]}
    except subprocess.TimeoutExpired as e:
        out = e.stdout or b""
        if isinstance(out, bytes):
            out = out.decode("utf-8", errors="replace")
        return {"timeout": True, "stdout": out[-45000:], "partial": True}


def inspect():
    report = {"summary": {"action": "disk_readonly", "at_ms": int(time.time()*1000),
                           "deletes": 0, "database_writes": 0, "service_changes": 0}}
    report["df"] = command(["df", "-B1", "-T"])
    report["inodes"] = command(["df", "-i", "/"])
    report["directories"] = {}
    for root in ("/opt", "/var/lib/crown-m1m6", "/var/backups", "/root", "/tmp", "/var/lib", "/var/log"):
        report["directories"][root] = command(["ionice", "-c", "3", "du", "-x", "-B1", "--max-depth=2", root], 25)
    report["var_lib_children"] = [p.name for p in Path("/var/lib").iterdir()]
    report["journal_size"] = command(["journalctl", "--disk-usage"])
    report["large_files"] = command(["find", "/opt", "/var/lib/crown-m1m6", "/var/backups", "/root", "/tmp", "-xdev", "-type", "f",
                                     "-size", "+200M", "-printf", "%s %TY-%Tm-%Td %TH:%TM %p\n"], 30)
    report["deleted_open"] = command(["bash", "-lc", "command -v lsof >/dev/null && lsof -nP +L1 | head -60"], 30)
    units = ["crown-m1m6", "crown-tick", "crown-sweep", "crown-strategy-results",
             "crown-strategy-search", "crown-tmp-cleanup", "footbreak-tick"]
    report["services"] = {u: command(["systemctl", "show", u+".service", "-p", "ActiveState",
                                      "-p", "Result", "-p", "ExecMainStatus",
                                      "-p", "ExecMainExitTimestamp"]) for u in units}
    logs = command(["journalctl", "-u", "crown-m1m6.service", "-u", "crown-tick.service",
                    "-u", "crown-sweep.service", "--since", "30 minutes ago", "--no-pager", "-o", "short-iso",
                    "--case-sensitive=no", "--grep",
                    "no space left|database or disk is full|sqlite_full|disk quota exceeded|database is locked|disk i/o error"], 60)
    text = logs.get("stdout", "")
    # Do not export unrelated journals or authentication material.
    patterns = ("no space left", "database or disk is full", "sqlite_full", "disk quota exceeded",
                "database is locked", "disk i/o error")
    selected = [line for line in text.splitlines() if any(p in line.lower() for p in patterns)]
    report["write_errors"] = {"matches": len(selected), "last": [
        re.sub(r"https?://\S+", "[URL REDACTED]", line) for line in selected[-35:]],
        "scope": "crown m1m6/tick/sweep matching records within 30min, output capped at 45000 chars",
        "query_code": logs.get("code"), "query_timeout": logs.get("timeout", False)}
    report["database_files"] = {}
    for name in ("/opt/crown-radar-v2/data/crown.db", "/var/lib/crown-m1m6/ledger.sqlite"):
        p = Path(name)
        if not p.exists():
            continue
        entry = {"files": {str(f): {"bytes": f.stat().st_size, "mtime": f.stat().st_mtime}
                           for f in (p, Path(name+"-wal"), Path(name+"-shm")) if f.exists()}}
        try:
            db = sqlite3.connect(f"file:{name}?mode=ro", uri=True, timeout=2)
            db.execute("PRAGMA query_only=ON")
            entry["page_count"] = db.execute("PRAGMA page_count").fetchone()[0]
            entry["page_size"] = db.execute("PRAGMA page_size").fetchone()[0]
            entry["freelist_count"] = db.execute("PRAGMA freelist_count").fetchone()[0]
            entry["latest_rows"] = {}
            for table in ("crown_snapshots", "items", "observations"):
                if not db.execute("SELECT 1 FROM sqlite_master WHERE name=?", (table,)).fetchone():
                    continue
                columns = [r[1] for r in db.execute(f"PRAGMA table_info({table})")]
                keep = [c for c in columns if c in ("sid", "captured_at", "created_at", "attempt_at", "ack_at", "first_seen_at", "ko", "status")]
                if keep:
                    row = db.execute(f"SELECT {','.join(keep)} FROM {table} ORDER BY rowid DESC LIMIT 1").fetchone()
                    entry["latest_rows"][table] = dict(zip(keep, row)) if row else None
            db.close()
        except Exception as e:
            entry["error"] = str(e)
        report["database_files"][name] = entry
    for name in ("/var/lib/crown-m1m6/status.json", "/var/lib/crown-m1m6/research_status.json"):
        p = Path(name)
        if p.exists():
            report.setdefault("status_files", {})[name] = {"bytes": p.stat().st_size, "mtime": p.stat().st_mtime}
    return report


def inspect_tmp():
    report = {"summary": {"action": "disk_tmp_readonly", "at_ms": int(time.time()*1000),
                           "deletes": 0, "database_writes": 0}}
    report["df"] = command(["df", "-B1", "/"])
    report["samples"] = {}
    roots = ["/tmp/snap-private-tmp", "/tmp/snap-private-tmp/snap.chromium",
             "/tmp/snap-private-tmp/snap.chromium/tmp", "/var/tmp", "/home"]
    for root in roots:
        if not Path(root).exists():
            continue
        rows = []
        with os.scandir(root) as entries:
            for i, entry in enumerate(entries):
                if i >= 40:
                    break
                s = entry.stat(follow_symlinks=False)
                rows.append({"name": entry.name, "is_dir": entry.is_dir(follow_symlinks=False),
                             "bytes": s.st_size, "mtime": s.st_mtime})
        report["samples"][root] = rows
    report["tmp_browser_usage"] = command(["ionice", "-c", "3", "du", "-x", "-B1", "--max-depth=4",
                                          "/tmp/snap-private-tmp/snap.chromium"], 150)
    report["other_usage"] = command(["ionice", "-c", "3", "du", "-x", "-B1", "--max-depth=1",
                                    "/var/tmp", "/home"], 20)
    report["cleanup_config"] = command(["systemctl", "cat", "crown-tmp-cleanup.service",
                                       "crown-tmp-cleanup.timer"])
    return report
