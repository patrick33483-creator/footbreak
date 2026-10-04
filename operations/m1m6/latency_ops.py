"""Read-only performance diagnosis; never invokes tick, Telegram or settlement."""
import cProfile
import io
import json
from pathlib import Path
import pstats
import sqlite3
import subprocess
import sys
import time


def diagnose():
    sys.path.insert(0,"/opt/crown-m1m6")
    import notifier
    now=notifier.nowms()
    profile=cProfile.Profile()
    start=time.monotonic()
    history,live,observations,finished,reasons=profile.runcall(notifier.collect,now)
    collect_seconds=time.monotonic()-start
    stream=io.StringIO()
    pstats.Stats(profile,stream=stream).strip_dirs().sort_stats("cumulative").print_stats(25)
    start=time.monotonic()
    gates={rid:notifier.gates(rr) for rid,rr in history.items()}
    gates_seconds=time.monotonic()-start
    start=time.monotonic()
    encoded=[(h["sid"],h["rule_id"],json.dumps(h,ensure_ascii=False),
              json.dumps(r,ensure_ascii=False) if r else None) for h,r in observations]
    encode_seconds=time.monotonic()-start
    with sqlite3.connect("file:/var/lib/crown-m1m6/ledger.sqlite?mode=ro",uri=True) as db:
        db.execute("PRAGMA query_only=ON")
        ledger={t:db.execute(f"SELECT COUNT(*) FROM {t}").fetchone()[0]
                for t in ("observations","items","strategy_batches")}
        plan=db.execute("EXPLAIN QUERY PLAN UPDATE observations SET result_json=? WHERE sid=? AND rule_id=?",
                        ("{}","0","M1")).fetchall()
    services={}
    for name in ("crown-m1m6.service","crown-m1m6.timer",
                 "crown-strategy-results.service","crown-strategy-results.timer"):
        services[name]=subprocess.run(
            ["systemctl","show",name,"-p","ActiveState","-p","Result",
             "-p","ExecMainStartTimestamp","-p","ExecMainExitTimestamp","-p","TimersMonotonic"],
            capture_output=True,text=True,timeout=10).stdout
    return {"summary":{"action":"latency_readonly","at_ms":now,
            "collect_seconds":collect_seconds,"gates_seconds":gates_seconds,
            "encode_seconds":encode_seconds,"observations":len(encoded),
            "history_rows":sum(map(len,history.values())),"live":len(live),
            "ledger_counts":ledger,"sends":0,"database_writes":0},
            "profile":stream.getvalue(),"observation_update_plan":plan,
            "services":services}
