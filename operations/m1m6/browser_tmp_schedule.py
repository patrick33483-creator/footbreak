"""Server-native two-hour browser-temp maintenance with explicit capacity gate."""
import json
import os
from pathlib import Path
import shutil
import time
from browser_tmp_cleanup import cleanup

TARGET = 32 * 1024**3
STATE = Path("/var/lib/crown-m1m6/browser_tmp_schedule_status.json")
VERSION = "browser-temp-7day-32GiB-2hour-v1"


def run():
    started = int(time.time()*1000)
    disk = shutil.disk_usage("/")
    result = {"version": VERSION, "started_at_ms": started, "threshold_bytes": TARGET,
              "free_before": disk.free, "deleted_directories": 0, "deleted_allocated_bytes": 0,
              "database_writes": 0, "service_restarts": 0, "telegram_sends": 0}
    if disk.free >= TARGET:
        result["status"] = "skipped_capacity_sufficient"
    else:
        receipt = cleanup()["summary"]
        result.update({k: receipt[k] for k in (
            "deleted_directories", "deleted_allocated_bytes", "stop_reason",
            "audit_path", "protected_config_unchanged") if k in receipt})
        result["status"] = ("skipped_other_run" if receipt.get("stop_reason") ==
                            "another_cleanup_running" else "completed")
    result["free_after"] = shutil.disk_usage("/").free
    result["below_target_after"] = result["free_after"] < TARGET
    result["completed_at_ms"] = int(time.time()*1000)
    temp = STATE.with_name(STATE.name+f".{os.getpid()}.tmp")
    with temp.open("x") as f:
        json.dump(result, f, ensure_ascii=False, indent=2)
        f.flush()
        os.fsync(f.fileno())
    os.replace(temp, STATE)
    return result


if __name__ == "__main__":
    # Only a compact result enters journald; deletion proofs remain in audit files.
    print(json.dumps(run(), ensure_ascii=False), flush=True)
