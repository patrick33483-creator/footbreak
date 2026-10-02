"""Remove admission locks only; retain rules, gates, receipts and settlement."""
import fcntl
import hashlib
import json
from pathlib import Path
import shutil
import sqlite3
import subprocess
import time

from ops import BASE, atomic, run
from install import PAGES, render_page, verify
from policy import VERSION
from upgrade_batch import semantic_nodes, constants

HERE=Path(__file__).parent
DEST=Path("/opt/crown-m1m6")
CONFIG=Path("/etc/crown-m1m6.json")


def upgrade():
    old=(DEST/"policy.py").read_text()
    new=(HERE/"policy.py").read_text()
    if semantic_nodes(old)!=semantic_nodes(new) or constants(old)!=constants(new):
        raise RuntimeError("Fixed rules, settlement or gates changed; refusing upgrade")
    test=subprocess.run(["python3","-m","unittest","discover","-s",str(HERE),"-p","test_*.py"],
                        capture_output=True,text=True,timeout=30)
    if test.returncode:
        raise RuntimeError("No-lock upgrade tests failed")
    panel=(HERE/"panel.html").read_text()
    pages={p:render_page(p.read_text(),panel) for p in PAGES}
    cfg=json.loads(CONFIG.read_text())
    backup=BASE/"nolock-v3-backup"
    backup.mkdir(exist_ok=True)
    run(["systemctl","stop","crown-m1m6.timer"],True)
    try:
        with (BASE/"run.lock").open("a") as lock:
            fcntl.flock(lock,fcntl.LOCK_EX)
            paths=[CONFIG,*PAGES,*[DEST/x for x in ("policy.py","notifier.py","panel.html","test_policy.py")]]
            originals={p:p.read_bytes() for p in paths}
            for p in paths:
                target=backup/str(p).lstrip("/").replace("/","__")
                if not target.exists():
                    shutil.copy2(p,target)
            db=sqlite3.connect(BASE/"ledger.sqlite")
            snap=sqlite3.connect(backup/"ledger.sqlite")
            db.backup(snap)
            snap.close()
            before=list(db.execute("SELECT * FROM items ORDER BY bet_key"))
            try:
                for name in ("policy.py","notifier.py","panel.html","test_policy.py"):
                    shutil.copy2(HERE/name,DEST/name)
                for p,text in pages.items():
                    p.write_text(text)
                cfg.update(version=VERSION,mode="rolling_OR_no_batch_lock",
                           batch_lock_enabled=False,batch_scope="accounting_only_no_notification_lock",
                           batch_append="any qualified kickoff; deduplicate each bet",
                           batch_release="accounting closes after all official results; never gates new sends",
                           no_result_policy="pending_accounting_only_no_lock",
                           no_lock_at=int(time.time()*1000))
                atomic(CONFIG,cfg)
                if before!=list(db.execute("SELECT * FROM items ORDER BY bet_key")):
                    raise RuntimeError("Historical notification items changed during upgrade")
                atomic(BASE/"nolock-v3-upgrade.json",{
                    "at_ms":cfg["no_lock_at"],"version":VERSION,
                    "fixed_rule_and_gate_semantics_unchanged":True,
                    "original_activated_at":cfg["activated_at"],
                    "old_policy_sha":hashlib.sha256(old.encode()).hexdigest(),
                    "new_policy_sha":hashlib.sha256(new.encode()).hexdigest(),
                    "historical_items_unchanged":True,
                    "tests":test.stderr.strip()})
            except Exception:
                for p,raw in originals.items():
                    p.write_bytes(raw)
                raise
            finally:
                db.close()
    finally:
        run(["systemctl","start","crown-m1m6.timer"],True)
    result=verify()
    result["summary"].update(upgrade_version=VERSION,tests=test.stderr.strip(),
                             fixed_rule_and_gate_semantics_unchanged=True,
                             historical_items_unchanged=True,batch_lock_enabled=False,
                             awaiting_next_natural_tick=True)
    return result
