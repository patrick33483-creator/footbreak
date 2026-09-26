"""Read production without modifications; emit only sports data and strategy code."""
import hashlib
import json
import sqlite3
import time
from pathlib import Path

base = Path("/opt/crown-radar-v2")
out = {"captured_at_ms": int(time.time()*1000), "files": {}, "databases": {}}
for name in ("rule_matcher.js", "rules_stats.js", "data/rules.json", "public/strategy.html", "strategy.html"):
    p = base / name
    if p.is_file():
        content = p.read_text()
        out["files"][name] = {"content": content, "sha256": hashlib.sha256(content.encode()).hexdigest()}
out["top_level_names"] = [p.name for p in base.iterdir()]
lines = (base / "server.js").read_text().splitlines()
out["server_relevant_lines"] = [
    {"line": i+1, "text": line}
    for i, line in enumerate(lines)
    if any(k in line for k in ("strategy-2plus-overlap", "four-channels-live", "ch-Alow", "ch-N2", "heavy_notified_rule", "matchAll("))
]
out["server_strategy_sections"] = []
out["notification_code"] = "\n".join(lines[1220:1780])
out["server_sha256"] = hashlib.sha256((base / "server.js").read_bytes()).hexdigest()
import subprocess
probe = subprocess.run(["docker", "inspect", "--format", "{{json .Mounts}}", "crown-radar-v2"],capture_output=True,text=True,timeout=15)
out["mounts"] = json.loads(probe.stdout) if probe.returncode==0 else []
out["container_state"] = subprocess.run(["docker", "inspect", "--format", "{{.State.Status}} {{.State.StartedAt}}", "crown-radar-v2"],capture_output=True,text=True,timeout=15).stdout
out["ports"] = subprocess.run(["docker", "inspect", "--format", "{{json .NetworkSettings.Ports}}", "crown-radar-v2"],capture_output=True,text=True,timeout=15).stdout
for i, line in enumerate(lines):
    if 'if (url.pathname ===' in line and any(k in line for k in ("strategy-2plus-overlap", "four-channels-live")):
        end = next((j for j in range(i+1, len(lines)) if lines[j].startswith('    if (url.pathname')), min(i+650, len(lines)))
        out["server_strategy_sections"].append({"start": i+1, "code": "\n".join(lines[i:end])})
paths = sorted(set(base.glob("*.db")) | set(base.glob("data/*.db")) | set(base.glob("data/*.sqlite")))
allowed = {"matches", "crown_snapshots", "odds_snapshots", "finished_matches", "heavy_notified_rule"}
for p in paths:
    db = sqlite3.connect(f"file:{p}?mode=ro", uri=True, timeout=10)
    db.row_factory = sqlite3.Row
    db.execute("PRAGMA query_only=ON")
    db.execute("BEGIN")
    tables = [dict(r) for r in db.execute("SELECT name,sql FROM sqlite_master WHERE type='table'")]
    entry = {"schema": tables, "rows": {}}
    for t in tables:
        name = t["name"]
        if name in allowed:
            entry["rows"][name] = [dict(r) for r in db.execute('SELECT * FROM "' + name + '"')]
    db.rollback()
    db.close()
    out["databases"][str(p)] = entry
print(json.dumps(out, ensure_ascii=False))
