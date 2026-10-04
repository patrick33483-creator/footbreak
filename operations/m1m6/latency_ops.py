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

HERE=Path(__file__).parent
DEST=Path("/opt/crown-m1m6")
BASE=Path("/var/lib/crown-m1m6")


def load(name,path):
    import importlib.util
    spec=importlib.util.spec_from_file_location(name,path)
    module=importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def preflight():
    """Compare both implementations on ONE source snapshot, no live writes."""
    import ast
    import hashlib
    import tempfile
    from ops import run
    def unchanged(path,omitted):
        tree=ast.parse(path.read_text())
        tree.body=[n for n in tree.body if not isinstance(n,(ast.FunctionDef,ast.AsyncFunctionDef))
                   or n.name not in omitted]
        # Imports may add the exact existing predicate, never change a rule.
        return ast.dump(ast.Module(body=[n for n in tree.body
                        if not isinstance(n,(ast.Import,ast.ImportFrom))],type_ignores=[]))
    for file,omitted in (("strategy_runtime.py",{"collect"}),("notifier.py",{"tick"})):
        assert unchanged(DEST/file,omitted)==unchanged(HERE/file,omitted),file+" changed unrelated behavior"
    for file in ("policy.py","dynamic_rules.py","signal_guard.py"):
        assert (DEST/file).read_bytes()==(HERE/file).read_bytes(),file+" differs"
    tests=run(["python3","-m","unittest","discover","-s",str(HERE),"-p","test_*.py"],True)
    sys.path.insert(0,str(DEST))
    import notifier
    old=load("baseline_runtime",DEST/"strategy_runtime.py")
    new=load("candidate_runtime",HERE/"strategy_runtime.py")
    store=load("candidate_store",HERE/"observation_store.py")
    now=notifier.nowms()
    registry=json.loads((BASE/"registry.json").read_text())
    source=notifier.source(now)
    timings={}
    started=time.monotonic()
    baseline=old.collect(registry,source,now)
    timings["baseline_collect_seconds"]=time.monotonic()-started
    started=time.monotonic()
    candidate=new.collect(registry,source,now)
    timings["candidate_collect_seconds"]=time.monotonic()-started
    assert baseline==candidate,"Collect output mismatch: block deployment"
    assert {k:notifier.gates(v) for k,v in baseline[0].items()}=={
            k:notifier.gates(v) for k,v in candidate[0].items()},"Gate mismatch"
    activated=json.loads(Path("/etc/crown-m1m6.json").read_text())["activated_at"]
    # Independent scratch SQLite copies: production is opened mode=ro only.
    with tempfile.TemporaryDirectory(prefix="crown-latency-") as tmp:
        left=sqlite3.connect(str(Path(tmp)/"old.sqlite"))
        right=sqlite3.connect(str(Path(tmp)/"new.sqlite"))
        with sqlite3.connect(f"file:{BASE/'ledger.sqlite'}?mode=ro",uri=True) as live:
            live.backup(left)
        left.backup(right)
        try:
            started=time.monotonic();before=left.total_changes
            for h,result in baseline[2]:
                left.execute("INSERT OR IGNORE INTO observations VALUES(?,?,?,?,?,?)",
                    (h["sid"],h["rule_id"],now,"historical_seed" if h["ko"]<=activated else "observed",
                     json.dumps(h,ensure_ascii=False),json.dumps(result,ensure_ascii=False) if result else None))
                if result:
                    left.execute("UPDATE observations SET result_json=? WHERE sid=? AND rule_id=?",
                                 (json.dumps(result,ensure_ascii=False),h["sid"],h["rule_id"]))
            left.commit()
            timings["baseline_archive_seconds"]=time.monotonic()-started
            timings["baseline_changed_rows"]=left.total_changes-before
            started=time.monotonic()
            writes=store.persist(right,candidate[2],now,activated)
            timings["candidate_archive_seconds"]=time.monotonic()-started
            timings["candidate_changed_rows"]=writes["changed"]
            def digest(db):
                sha=hashlib.sha256()
                for row in db.execute("SELECT * FROM observations ORDER BY sid,rule_id"):
                    sha.update(json.dumps(tuple(row),ensure_ascii=False).encode())
                return sha.hexdigest()
            assert digest(left)==digest(right),"Observation persistence mismatch"
        finally:
            left.close();right.close()
    return {"tests":tests["stderr"],"same_source_collect_equal":True,"all_gates_equal":True,
            "archive_equal":True,"strategies":len(registry["strategies"]),
            "observations":len(candidate[2]),"timings":timings,"live_database_writes":0}


def deploy():
    """Deploy only equivalent runtime optimizations; never force a notifier tick."""
    import fcntl
    import hashlib
    import shutil
    verified=preflight()
    protected=[BASE/"ledger.sqlite",BASE/"registry.json",BASE/"performance_epoch.json",
               Path("/etc/crown-m1m6.json"),DEST/"policy.py",DEST/"dynamic_rules.py",
               DEST/"signal_guard.py",DEST/"research_cycle.py",DEST/"result_refresh.py"]
    with (BASE/"run.lock").open("a") as lock:
        fcntl.flock(lock,fcntl.LOCK_EX)
        digest=lambda:{str(p):hashlib.sha256(p.read_bytes()).hexdigest() for p in protected}
        before=digest()
        backup=BASE/("latency-backup-"+str(int(time.time()*1000)));backup.mkdir()
        deployed={}
        for file in ("notifier.py","strategy_runtime.py","observation_store.py","test_observation_store.py"):
            if (DEST/file).exists():
                shutil.copy2(DEST/file,backup/file)
            temp=(DEST/file).with_suffix(".py.tmp")
            shutil.copy2(HERE/file,temp)
            temp.replace(DEST/file)
            deployed[file]=hashlib.sha256((DEST/file).read_bytes()).hexdigest()
            assert deployed[file]==hashlib.sha256((HERE/file).read_bytes()).hexdigest()
        assert before==digest(),"Protected state changed"
    natural=[]
    for _ in range(6):
        time.sleep(20)
        output=subprocess.run(["journalctl","-u","crown-m1m6.service","--since","3 minutes ago",
                               "-o","cat","--no-pager"],capture_output=True,text=True,timeout=10).stdout
        for line in output.splitlines():
            try:
                record=json.loads(line)
            except ValueError:
                continue
            if isinstance(record,dict) and record.get("latency_version")=="grouped-collect-incremental-archive-v1":
                natural.append(record)
        if natural:
            break
    return {"summary":{"action":"latency_deploy","preflight":verified,
            "protected_files_unchanged":True,"deployed":deployed,"forced_sends":0,
            "timing_rules_and_schedules_unchanged":True,"natural_ticks":natural}}


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
