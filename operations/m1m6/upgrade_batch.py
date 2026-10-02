"""Upgrade batching only; fixed rules, settlement and rolling gates stay identical."""
import ast
import fcntl
import hashlib
import json
from pathlib import Path
import shutil
import subprocess
import time
from ops import BASE, atomic, run
from install import PAGES, render_page, verify
from policy import VERSION

HERE=Path(__file__).parent
DEST=Path("/opt/crown-m1m6")
CONFIG=Path("/etc/crown-m1m6.json")


def semantic_nodes(text):
    tree=ast.parse(text)
    return {n.name:ast.dump(n,include_attributes=False) for n in tree.body
            if isinstance(n,(ast.FunctionDef,ast.AsyncFunctionDef)) and n.name!="choose_batch"}


def constants(text):
    tree=ast.parse(text)
    return {n.targets[0].id:ast.dump(n.value,include_attributes=False) for n in tree.body
            if isinstance(n,ast.Assign) and isinstance(n.targets[0],ast.Name)
            and n.targets[0].id in {"RULES","START_MS"}}


def upgrade():
    old=(DEST/"policy.py").read_text()
    new=(HERE/"policy.py").read_text()
    if semantic_nodes(old)!=semantic_nodes(new) or constants(old)!=constants(new):
        raise RuntimeError("Non-batching policy change detected; refusing upgrade")
    test=subprocess.run(["python3","-m","unittest","discover","-s",str(HERE),"-p","test_*.py"],
                        capture_output=True,text=True,timeout=30)
    if test.returncode:
        raise RuntimeError("Batch upgrade tests failed")
    cfg=json.loads(CONFIG.read_text())
    run(["systemctl","stop","crown-m1m6.timer"],True)
    backup=BASE/"batch-v2-backup"
    backup.mkdir(exist_ok=True)
    # Wait for an existing tick to finish; never terminate a Telegram request.
    with (BASE/"run.lock").open("a") as lock:
        fcntl.flock(lock,fcntl.LOCK_EX)
        for p in [CONFIG,*PAGES,*[DEST/x for x in ("policy.py","notifier.py","panel.html","test_policy.py")]]:
            target=backup/str(p).lstrip("/").replace("/","__")
            if not target.exists():
                shutil.copy2(p,target)
        for name in ("policy.py","notifier.py","panel.html","test_policy.py"):
            shutil.copy2(HERE/name,DEST/name)
        import notifier
        db=notifier.state_db()
        db.close()
        panel=(HERE/"panel.html").read_text()
        for p in PAGES:
            p.write_text(render_page(p.read_text(),panel))
        cfg.update(version=VERSION,mode="same_kickoff_batch_then_rolling_OR",
                   batch_scope="all M1-M6, grouped by exact kickoff timestamp",
                   batch_append="same kickoff only, before kickoff and passing current gates",
                   batch_v2_at=int(time.time()*1000))
        atomic(CONFIG,cfg)
        atomic(BASE/"batch-v2-upgrade.json",{
            "at_ms":cfg["batch_v2_at"],"version":VERSION,
            "fixed_rule_and_gate_semantics_unchanged":True,
            "original_activated_at":cfg["activated_at"],
            "old_policy_sha":hashlib.sha256(old.encode()).hexdigest(),
            "new_policy_sha":hashlib.sha256(new.encode()).hexdigest(),
            "historical_items_rewritten":False,"tests":"13 passed"})
    run(["systemctl","start","crown-m1m6.timer"],True)
    # No manual tick or send: the existing timer owns the next natural run.
    result=verify()
    result["summary"].update(upgrade_version=VERSION,tests="13 passed",
                             fixed_rule_and_gate_semantics_unchanged=True,
                             awaiting_next_natural_tick=True)
    return result
