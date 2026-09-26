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
paths = sorted(set(base.glob("*.db")) | set(base.glob("data/*.db")) | set(base.glob("data/*.sqlite")))
allowed = {"matches", "crown_snapshots", "odds_snapshots", "rule_notifications", "strategy_notifications", "notifications", "notification_log", "notification_ledger"}
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
