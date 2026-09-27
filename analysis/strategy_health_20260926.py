"""Read-only diagnostics. No providers, writes, imports of production code or sends."""
import json
import sqlite3
import subprocess
import time
from pathlib import Path

out = {"captured_at_ms": int(time.time()*1000), "files": {}, "listings": {}, "runtime": {}}
def run(args):
    try:
        r = subprocess.run(args, capture_output=True, text=True, timeout=25)
        return {"status": r.returncode, "stdout": r.stdout[-50000:], "stderr": r.stderr[-1000:]}
    except Exception as e:
        return {"error": type(e).__name__}
def collect(p):
    try:
        if p.is_file() and p.stat().st_size < 25000000:
            out["files"][str(p)] = {"mtime": p.stat().st_mtime, "content": p.read_text()}
    except Exception as e:
        out["files"][str(p)] = {"error": type(e).__name__}
for root in ["/var/www", "/opt", "/var/lib"]:
    p = Path(root)
    out["listings"][root] = [x.name for x in p.iterdir() if "crown" in x.name.lower() or "footbreak" in x.name.lower()]
    for x in p.iterdir():
        if "crown" not in x.name.lower() or not x.is_dir():
            continue
        out["listings"][str(x)] = [v.name for v in x.iterdir()]
        for sub in [x, x/"data", x/"public", x/"public"/"data"]:
            if not sub.is_dir():
                continue
            out["listings"][str(sub)] = [v.name for v in sub.iterdir()]
            for f in sub.iterdir():
                n = f.name
                if any(t in n.lower() for t in ["bak", "backup", "token", "secret", "credential", "package", "lock"]):
                    continue
                if n in ["server.js", "rule_matcher.js", "strategy.html", "index.html", "rules_stats.js"] or n.endswith(".json"):
                    collect(f)
for f in Path("/usr/local/bin").iterdir():
    if any(t in f.name.lower() for t in ["crown", "v3", "v4", "ce_policy"]):
        if f.suffix in [".py", ".js", ".sh"]:
            collect(f)
out["runtime"]["timers"] = run(["systemctl","list-timers","--all","--no-pager"])
out["runtime"]["docker"] = run(["docker","ps","--format","{{.Names}} {{.Status}}"])
out["runtime"]["crown_logs"] = run(["docker","logs","--since","45m","--tail","240","crown-radar-v2"])
out["databases"] = {}
for f in Path("/usr/local/bin").iterdir():
    if any(t in f.name.lower() for t in ["u1", "ce-", "ogb", "strategy", "b_policy"]):
        if f.suffix in [".py", ".js", ".sh"]:
            collect(f)
for f in Path("/opt/crownsystem-v4").glob("*.py"):
    collect(f)
for f in Path("/etc").glob("*policy.json"):
    if any(t in f.name for t in ["crown", "u1", "ce", "b-"]):
        collect(f)
for root in ["/var/lib/u1-notify", "/var/lib/ce-notify"]:
    p = Path(root)
    if p.is_dir():
        out["listings"][root] = [v.name for v in p.iterdir()]
        for name in ["state.json", "audit.json", "fires.json"]:
            collect(p/name)
for name in ["crown-goldpool-notify", "u1-notify", "ce-notify"]:
    out["runtime"][name+"_overnight_logs"] = run([
        "journalctl", "-u", name+".service",
        "--since", "2026-09-26 18:00:00", "--no-pager",
        "--grep", "sent [1-9]|failed|ERROR|error|2951580"
    ])
for name in ["u1-notify","ce-notify","ogb-fires-builder","strategy-merged-builder","crownsystem-v3-merge"]:
    out["runtime"][name] = run(["systemctl","show",name+".service","--no-pager","-p","ExecStart","-p","Result","-p","ExecMainStatus"])
cp = Path("/var/lib/crownsystem-v4/checkpoints.json")
if cp.is_file():
    data = json.loads(cp.read_text())
    out["checkpoint_shape"] = {"type": type(data).__name__, "length": len(data), "keys": list(data)[:12]}
    out["checkpoints"] = data
for name in ["u1-notify","ce-notify","crownsystem-v3-merge"]:
    out["runtime"][name+"_logs"] = run(["journalctl","-u",name+".service","--since","90 minutes ago","-n","100","--no-pager"])
p = Path("/opt/crown-radar-v2/data/crown.db")
if p.is_file():
    db = sqlite3.connect(f"file:{p}?mode=ro",uri=True,timeout=10)
    db.row_factory = sqlite3.Row
    db.execute("PRAGMA query_only=ON")
    db.execute("BEGIN")
    out["database_schema"] = [dict(r) for r in db.execute("SELECT name,sql FROM sqlite_master WHERE type='table'")]
    for table in ["matches","odds_snapshots","crown_snapshots","finished_matches","heavy_notified_rule"]:
        out["databases"][table] = [dict(r) for r in db.execute('SELECT * FROM "'+table+'"')]
    db.rollback()
    db.close()
print(json.dumps(out,ensure_ascii=False))
