"""Read-only result pipeline diagnostics, excluding credentials."""
import json
from pathlib import Path
import re
import sqlite3
import subprocess
import time


def safe(text):
    return "\n".join(line for line in text.splitlines()
                    if not re.search(r"token|password|secret|authorization|api.key", line, re.I))


def command(args):
    r = subprocess.run(args, capture_output=True, text=True, timeout=40)
    return {"rc": r.returncode, "out": safe(r.stdout)[-30000:],
            "err": safe(r.stderr)[-4000:]}


def diagnose():
    db = sqlite3.connect("file:/opt/crown-radar-v2/data/crown.db?mode=ro", uri=True)
    db.row_factory = sqlite3.Row
    now = int(time.time()*1000)
    data = {}
    data["finished_schema"] = [dict(r) for r in db.execute("PRAGMA table_info(finished_matches)")]
    data["latest_results"] = [dict(r) for r in db.execute(
        "SELECT * FROM finished_matches ORDER BY fetched_at DESC LIMIT 10")]
    data["recent_matches"] = [dict(r) for r in db.execute(
        "SELECT m.sid,m.home,m.away,m.kickoff_utc,f.status,f.home_score,f.away_score,f.fetched_at "
        "FROM matches m LEFT JOIN finished_matches f ON m.sid=f.sid "
        "WHERE m.kickoff_utc BETWEEN ? AND ? ORDER BY m.kickoff_utc DESC LIMIT 60",
        (now-12*3600000,now-2*3600000))]
    data["first_batch_match"] = [dict(r) for r in db.execute(
        "SELECT * FROM matches WHERE sid='2981443'")]
    db.close()
    server = Path("/opt/crown-radar-v2/server.js").read_text().splitlines()
    data["score_source"] = safe("\n".join(server[285:507]))
    data["score_imports"] = safe("\n".join(server[:36]))
    for unit in ["crown-strategy-results","hkjc-result-sync","crownsystem-v3-merge"]:
        data[unit] = command(["systemctl","cat",unit+".service",unit+".timer"])
        data[unit+"_journal"] = command(["journalctl","-u",unit+".service","-n","60","--no-pager","-o","cat"])
    logs = subprocess.run(["docker","logs","--since","10h","--tail","15000","crown-radar-v2"],
                          capture_output=True,text=True,timeout=40)
    data["score_logs"] = safe("\n".join(line for line in (logs.stdout+logs.stderr).splitlines()
                                if re.search(r"score|result|refresh.*err",line,re.I)))[-30000:]
    return {"summary":{"action":"diagnose_result_pipeline","at_ms":now}, "evidence":data}
