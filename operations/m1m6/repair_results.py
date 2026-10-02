"""Scoped importer wiring fix; preserve exact-ID parser and settlement writes."""
import ast
import hashlib
import json
from pathlib import Path
import shutil
import subprocess
import time
from ops import atomic, run

HERE=Path(__file__).parent
LIVE=Path("/opt/crown-strategy-results/sync_results.py")


def invariant_functions(text):
    return {n.name:ast.dump(n,include_attributes=False) for n in ast.parse(text).body
            if isinstance(n,ast.FunctionDef) and n.name!="candidates"}


def preflight():
    if invariant_functions(LIVE.read_text())!=invariant_functions((HERE/"sync_results.py").read_text()):
        raise RuntimeError("Result parsing/write semantics changed")
    r=subprocess.run(["python3","-I",str(HERE/"sync_results.py")],
                     capture_output=True,text=True,timeout=105)
    evidence=json.loads(r.stdout)
    return {"summary":{"action":"results_preflight","returncode":r.returncode,
                       "dry_run":evidence["dry_run"],"candidates":evidence["candidates"],
                       "accepted":len(evidence["accepted"]),
                       "errors":evidence["errors"],"skipped":evidence["skipped"]},
            "evidence":evidence}


def install_fix():
    if invariant_functions(LIVE.read_text())!=invariant_functions((HERE/"sync_results.py").read_text()):
        raise RuntimeError("Result parsing/write semantics changed")
    run(["python3","-m","py_compile",str(HERE/"sync_results.py")],True)
    backup=Path("/var/lib/crown-m1m6/legacy-backup/result-sync-before-m1m6.py")
    if not backup.exists():
        shutil.copy2(LIVE,backup)
    before=hashlib.sha256(LIVE.read_bytes()).hexdigest()
    temp=LIVE.with_suffix(".py.tmp")
    shutil.copy2(HERE/"sync_results.py",temp)
    temp.replace(LIVE)
    atomic(Path("/var/lib/crown-m1m6/result-sync-repair.json"),{
        "at_ms":int(time.time()*1000),"old_sha":before,
        "new_sha":hashlib.sha256(LIVE.read_bytes()).hexdigest(),
        "change":"Add M1-M6 observations and prioritize unresolved open-batch IDs",
        "parser_and_write_semantics_unchanged":True})
    # Use the existing result-only service, never force a notifier tick or unlock.
    run(["systemctl","start","--no-block","crown-strategy-results.service"],True)
    return {"summary":{"action":"repair_result_importer",
                       "parser_and_write_semantics_unchanged":True,
                       "result_service_requested":True,"manual_unlock":False}}
