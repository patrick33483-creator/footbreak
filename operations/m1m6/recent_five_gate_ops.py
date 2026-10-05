"""Approved v6 qualification cutover. Never sends or resets settlement/locks."""
import ast
import copy
import fcntl
import hashlib
import json
from pathlib import Path
import shutil
import sqlite3
import subprocess
import time
from contextlib import closing

from ops import atomic
import notifier
from policy import gates, GATE_VERSION, GATE_CONFIG, GATE_TEXT, THRESHOLDS
from strategy_runtime import collect

BASE=Path("/var/lib/crown-m1m6")
HERE=Path(__file__).parent
DEST=Path("/opt/crown-m1m6")
CONFIG=Path("/etc/crown-m1m6.json")
FILES=("policy.py","test_policy.py","test_dynamic.py","test_recent_five_gate.py")


def ledger_digest():
    with closing(sqlite3.connect(f"file:{BASE/'ledger.sqlite'}?mode=ro",uri=True)) as db:
        digest=hashlib.sha256()
        for table in ("items","batches","strategy_batches","strategy_batch_items"):
            for row in db.execute(f"SELECT * FROM {table} ORDER BY rowid"):
                digest.update(json.dumps([table,tuple(row)],ensure_ascii=False).encode())
        counts={t:db.execute(f"SELECT COUNT(*) FROM {t}").fetchone()[0]
                for t in ("items","observations","strategy_batches","strategy_batch_items")}
        return {"digest":digest.hexdigest(),"counts":counts}


def validate():
    # Only the qualification composition and its version/text may change.
    def preserved(path):
        tree=ast.parse(path.read_text())
        tree.body=[n for n in tree.body if not
                   (isinstance(n,ast.FunctionDef) and n.name in ("gates","recent_five"))
                   and not (isinstance(n,ast.Assign) and any(
                       isinstance(t,ast.Name) and t.id in ("GATE_VERSION","GATE_TEXT","GATE_CONFIG")
                       for t in n.targets))]
        return ast.dump(tree)
    assert preserved(DEST/"policy.py")==preserved(HERE/"policy.py"),"Unrelated policy change"
    for name in ("notifier.py","dynamic_rules.py","strategy_runtime.py","research_cycle.py",
                 "result_revisions.py","result_refresh.py","signal_guard.py","performance_view.py"):
        assert (DEST/name).read_bytes()==(HERE/name).read_bytes(),f"Unrelated runtime change: {name}"
    tests=subprocess.run(["python3","-m","unittest","discover","-s",str(HERE),"-p","test_*.py"],
                         capture_output=True,text=True,timeout=100)
    assert tests.returncode==0,tests.stderr
    return tests.stderr


def deploy():
    tests=validate()
    with (BASE/"research.lock").open("a") as research,(BASE/"run.lock").open("a") as tick:
        fcntl.flock(research,fcntl.LOCK_EX)
        fcntl.flock(tick,fcntl.LOCK_EX)
        now=int(time.time()*1000)
        before=ledger_digest()
        epoch=(BASE/"performance_epoch.json").read_bytes()
        cfg=json.loads(CONFIG.read_text())
        previous=json.loads((BASE/"registry.json").read_text())
        history,_,_,_,_=collect(previous,notifier.source(now),now)
        computed={rid:gates(rows) for rid,rows in history.items()}
        registry=copy.deepcopy(previous)
        checks=[]
        for rule in registry["strategies"]:
            g=computed[rule["id"]]
            # Immediate tightening only; discovery may activate other qualified
            # branches in the subsequent complete search.
            old_active=rule["active"]
            rule["active"]=bool(old_active and g["pass"])
            rule["gate"]=g
            checks.append({"id":rule["id"],"active_before":old_active,"active_after":rule["active"],
                           "rolling_pass":g["rolling_pass"],"recent5":g["recent5"]})
            assert not rule["active"] or (g["rolling_pass"] and g["recent5"]["pass"])
        registry.update(gate_version=GATE_VERSION,thresholds=THRESHOLDS,updated_at=now,next_search_at=now,
                        qualification_cutover={"at":now,"full_search_pending":True},
                        existing_strategy_checks=[{"id":r["id"],"version":r["version"],
                                                    "gate":computed[r["id"]]} for r in registry["strategies"]])
        registry["search"]={**registry.get("search",{}),"existing_rechecked":len(checks),
                            "existing_passing":sum(g["pass"] for g in computed.values()),
                            "merged_active":sum(r["active"] for r in registry["strategies"]),
                            "cutover_existing_only":True}
        backup=BASE/f"recent-five-gate-backup-{now}"
        backup.mkdir()
        for path in (CONFIG,BASE/"registry.json",BASE/"performance_epoch.json"):
            shutil.copy2(path,backup/path.name)
        with closing(sqlite3.connect(f"file:{BASE/'ledger.sqlite'}?mode=ro",uri=True)) as live:
            with closing(sqlite3.connect(backup/"ledger.sqlite")) as target:
                live.backup(target)
        for name in FILES:
            if (DEST/name).exists():
                shutil.copy2(DEST/name,backup/name)
        try:
            for name in FILES:
                temp=(DEST/name).with_suffix(".py.tmp")
                shutil.copy2(HERE/name,temp)
                temp.replace(DEST/name)
            cfg.update(gate=GATE_CONFIG,gate_version=GATE_VERSION,thresholds=THRESHOLDS,
                       recent_five_no_loss=True,recent_five_push_allowed=True)
            atomic(CONFIG,cfg)
            atomic(BASE/"registry.json",registry)
            assert ledger_digest()==before,"Ledger or batch state changed during cutover"
            assert (BASE/"performance_epoch.json").read_bytes()==epoch,"Performance reset changed"
        except Exception:
            for name in FILES:
                if (backup/name).exists():
                    shutil.copy2(backup/name,DEST/name)
                elif (DEST/name).exists():
                    (DEST/name).unlink()
            shutil.copy2(backup/CONFIG.name,CONFIG)
            shutil.copy2(backup/"registry.json",BASE/"registry.json")
            raise
        receipt={"action":"recent_five_gate_deploy","at_ms":now,"gate_version":GATE_VERSION,
                 "gate_text":GATE_TEXT,"tests":tests,"backup":str(backup),"checks":checks,
                 "ledger_and_locks_unchanged":True,"performance_epoch_unchanged":True,
                 "original_clause_definitions_unchanged":True,"forced_sends":0,
                 "active_before":sum(c["active_before"] for c in checks),
                 "active_after_immediate_recheck":sum(c["active_after"] for c in checks)}
        atomic(BASE/"recent_five_gate_deploy_receipt.json",receipt)
    # The prior research lock holder may still be exiting; do not kill it.
    for _ in range(15):
        state=subprocess.run(["systemctl","is-active","crown-strategy-search.service"],
                             capture_output=True,text=True,timeout=10).stdout.strip()
        if state not in ("active","activating","reloading"):
            break
        time.sleep(2)
    request=subprocess.run(["systemctl","start","--no-block","crown-strategy-search.service"],
                           capture_output=True,text=True,timeout=15)
    receipt["full_search_requested"]=request.returncode==0
    atomic(BASE/"recent_five_gate_deploy_receipt.json",receipt)
    return {"summary":receipt}


def verify():
    config=json.loads(CONFIG.read_text())
    registry=json.loads((BASE/"registry.json").read_text())
    public=json.loads((BASE/"status.json").read_text())
    mirror_paths=(Path("/var/www/crownsystem-v3/m1m6_status.json"),
                  Path("/opt/crown-radar-v2/data/m1m6_status.json"))
    mirrored=all(json.loads(p.read_text())==public for p in mirror_paths)
    active=[r for r in public["rules"] if r["active"]]
    invalid=[r["id"] for r in active if not r.get("gate",{}).get("pass") or
             not r["gate"].get("recent5",{}).get("pass")]
    with closing(sqlite3.connect(f"file:{BASE/'ledger.sqlite'}?mode=ro",uri=True)) as db:
        db.row_factory=sqlite3.Row
        new=[]
        for row in db.execute("SELECT * FROM items ORDER BY ko"):
            p=json.loads(row["payload"])
            ge=p.get("gate_evidence",{})
            if any(g.get("gate_version")==GATE_VERSION for g in ge.values()):
                new.append({"sid":row["sid"],"status":row["status"],"ack_at":row["ack_at"],
                            "ko":row["ko"],"message_id":row["message_id"],
                            "rules":p.get("rules"),"gates":ge})
        audit_count=db.execute("SELECT COUNT(*) FROM result_score_corrections").fetchone()[0]
    natural=[]
    logs=subprocess.run(["journalctl","-u","crown-m1m6.service","--since","10 minutes ago",
                         "-o","cat","--no-pager"],capture_output=True,text=True,timeout=10).stdout
    for line in logs.splitlines():
        try:
            record=json.loads(line)
        except ValueError:
            continue
        if isinstance(record,dict) and record.get("result_revisions"):
            natural.append(record)
    return {"summary":{"action":"recent_five_gate_verify","database_writes":0,"forced_sends":0,
                       "at_ms":int(time.time()*1000),"config_gate_version":config.get("gate_version"),
                       "registry_gate_version":registry.get("gate_version"),"public_gate_version":public.get("gate_version"),
                       "public_gate_text":public.get("gate_text"),"mirrors_equal":mirrored,
                       "active_strategies":len(active),"invalid_active_ids":invalid,
                       "cutover_pending":registry.get("qualification_cutover",{}).get("full_search_pending",False),
                       "runtime_policy_equal":(DEST/"policy.py").read_bytes()==(HERE/"policy.py").read_bytes()},
            "deployment":json.loads((BASE/"recent_five_gate_deploy_receipt.json").read_text()),
            "research":json.loads((BASE/"research_status.json").read_text()),
            "active":[{"id":r["id"],"gate":r["gate"]} for r in active],
            "new_notifications":new,"natural_ticks":natural,
            "correction_audit_count":audit_count,"performance_period":public.get("performance_period"),
            "strategy_pending_batches":public.get("strategy_pending_batches")}
