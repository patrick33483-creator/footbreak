"""Disable only the identified R-Pin2 HKJC Telegram channel."""
import hashlib
import json
from pathlib import Path
import shutil
import sqlite3
import subprocess
import time
from ops import run, atomic

RADAR = Path("/opt/crown-radar-v2")
FLAG = RADAR / "data/HKJC_STRATEGY_TG_DISABLED"
RECEIPT = Path("/var/lib/crown-m1m6/hkjc-notify-disabled.json")
MARKER = "HKJC_STRATEGY_TG_DISABLED"


def stats():
    db = sqlite3.connect(f"file:{RADAR}/data/crown.db?mode=ro", uri=True)
    row = db.execute("SELECT COUNT(*),MAX(notified_at) FROM r_pin2_notified").fetchone()
    db.close()
    return {"count": row[0], "last_notified_at": row[1]}


def verify_hkjc():
    report = json.loads(RECEIPT.read_text())
    src = (RADAR / "server.js").read_text()
    ledger = stats()
    return {"summary": {
        "action": "verify_hkjc_notifications_disabled",
        "disabled_at": report["disabled_at"],
        "flag_exists": FLAG.exists(),
        "function_guard_present": MARKER in src,
        "send_loop_guard_present": f'if (fs.existsSync("./data/{MARKER}")) break;' in src,
        "receipts_at_disable": report["before"], "receipts_now": ledger,
        "no_new_receipts": ledger == report["before"],
        "collector_running": run(["docker","inspect","--format","{{.State.Status}}",
                                  "crown-radar-v2"])["stdout"].strip(),
        "m1m6_timer": run(["systemctl","is-active","crown-m1m6.timer"])["stdout"].strip(),
        "m1m6_policy_unchanged": hashlib.sha256(Path("/opt/crown-m1m6/policy.py").read_bytes()).hexdigest()
                                  == report["m1m6_policy_sha"],
    }}


def stop_hkjc():
    if RECEIPT.exists():
        return verify_hkjc()
    server = RADAR / "server.js"
    old = server.read_text()
    anchor = "async function checkRPin2AndNotify() {\n"
    loop = "    for (const f of fires) {\n"
    if old.count(anchor) != 1 or old.count(loop) != 1:
        raise RuntimeError("Unexpected notifier source, no changes made")
    new = old.replace(anchor, anchor +
        '  if (fs.existsSync("./data/HKJC_STRATEGY_TG_DISABLED")) {\n'
        '    log("r_pin2_notify_disabled", { reason: "user_requested" }); return;\n'
        '  }\n', 1).replace(loop, loop +
        '      if (fs.existsSync("./data/HKJC_STRATEGY_TG_DISABLED")) break;\n', 1)
    check = subprocess.run(["docker","exec","-i","crown-radar-v2","node",
                            "--input-type=module","--check"],
                           input=new, text=True, capture_output=True, timeout=20)
    if check.returncode:
        raise RuntimeError("JavaScript syntax check failed, no runtime changes made")
    backup = Path("/var/lib/crown-m1m6/legacy-backup/server-before-hkjc-stop.js")
    if not backup.exists():
        shutil.copy2(server, backup)
    before = stats()
    at = int(time.time()*1000)
    FLAG.write_text(json.dumps({"disabled_at": at, "reason": "user_requested"}))
    server.write_text(new)  # preserve bind-mounted inode
    run(["docker","exec","crown-radar-v2","node","--check","server.js"], True)
    run(["docker","restart","crown-radar-v2"], True)
    atomic(RECEIPT, {"disabled_at": at, "before": before,
        "m1m6_policy_sha": hashlib.sha256(Path("/opt/crown-m1m6/policy.py").read_bytes()).hexdigest(),
        "source_before_sha": hashlib.sha256(old.encode()).hexdigest(),
        "source_after_sha": hashlib.sha256(new.encode()).hexdigest(),
        "history_deleted": False})
    return verify_hkjc()
