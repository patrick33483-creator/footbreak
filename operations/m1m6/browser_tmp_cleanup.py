"""User-approved old, unused Snap browser temporary cleanup."""
import collections
import fcntl
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import sqlite3
import stat
import subprocess
import time

ROOT = Path("/tmp/snap-private-tmp/snap.chromium/tmp")
NAME = re.compile(r"(?:playwright_chromiumdev_profile-|org\.chromium\.Chromium\.chromium_chrome_url_fetcher_\.)[A-Za-z0-9_-]+")
RECEIPT = Path("/var/lib/crown-m1m6/browser_tmp_cleanup_receipt.json")
LOCK = Path("/var/lib/crown-m1m6/browser_tmp_cleanup.lock")
from disk_readonly import command


def names_in(text):
    return set(NAME.findall(text))


def active_names():
    """Read references in every process, including Snap namespace aliases."""
    used = set()
    scanned = 0
    for proc in Path("/proc").iterdir():
        if not proc.name.isdigit():
            continue
        try:
            used.update(names_in((proc/"cmdline").read_bytes().decode("utf-8", "replace")))
            for key in ("cwd", "exe"):
                try:
                    used.update(names_in(os.readlink(proc/key)))
                except FileNotFoundError:
                    pass
            for fd in (proc/"fd").iterdir():
                try:
                    used.update(names_in(os.readlink(fd)))
                except FileNotFoundError:
                    pass
            used.update(names_in((proc/"maps").read_text(errors="replace")))
            scanned += 1
        except (FileNotFoundError, ProcessLookupError):
            continue
        except PermissionError as exc:
            raise RuntimeError("Cannot establish complete process reference inventory") from exc
    return used, scanned


def eligible(path, cutoff, used, device):
    if not NAME.fullmatch(path.name):
        return None, "unapproved_name"
    if path.name in used:
        return None, "active_process"
    try:
        top = path.lstat()
        if not stat.S_ISDIR(top.st_mode) or top.st_dev != device:
            return None, "not_plain_same_device_directory"
        allocated = 0
        count = 0
        stack = [path]
        while stack:
            current = stack.pop()
            info = current.lstat()
            if info.st_dev != device:
                return None, "mount_boundary"
            if max(info.st_mtime, info.st_ctime) >= cutoff:
                return None, "recent_content"
            if not (stat.S_ISDIR(info.st_mode) or stat.S_ISREG(info.st_mode) or stat.S_ISLNK(info.st_mode)):
                return None, "special_file"
            allocated += info.st_blocks*512
            count += 1
            if stat.S_ISDIR(info.st_mode):
                with os.scandir(current) as entries:
                    stack.extend(Path(e.path) for e in entries)
            elif current.name == "SingletonLock" and stat.S_ISLNK(info.st_mode):
                target = os.readlink(current)
                pid = target.rsplit("-", 1)[-1]
                if pid.isdigit() and Path("/proc", pid).exists():
                    return None, "live_singleton_pid"
        return {"inode": top.st_ino, "mtime": top.st_mtime_ns, "ctime": top.st_ctime_ns,
                "allocated_bytes": allocated, "entries": count}, None
    except (OSError, RuntimeError):
        return None, "unverifiable_or_changed"


def health():
    data = {"at_ms": int(time.time()*1000), "disk": shutil.disk_usage("/")._asdict()}
    data["services"] = {u: command(["systemctl", "show", u+".service", "-p", "ActiveState",
                                    "-p", "Result", "-p", "NRestarts", "-p", "ExecMainExitTimestamp"])
                        for u in ("crown-m1m6", "crown-tick", "crown-sweep", "crown-strategy-results")}
    data["databases"] = {}
    for p, table, cols in (
        ("/opt/crown-radar-v2/data/crown.db", "crown_snapshots", "sid,captured_at"),
        ("/var/lib/crown-m1m6/ledger.sqlite", "items", "sid,status,attempt_at,ack_at,message_id")):
        db = sqlite3.connect(f"file:{p}?mode=ro", uri=True, timeout=3)
        try:
            db.execute("PRAGMA query_only=ON")
            row = db.execute(f"SELECT {cols} FROM {table} ORDER BY rowid DESC LIMIT 1").fetchone()
            data["databases"][p] = {"inode": os.stat(p).st_ino, "latest": dict(zip(cols.split(","), row))}
            if table == "crown_snapshots":
                deadline = time.monotonic()+5
                db.set_progress_handler(lambda: int(time.monotonic() > deadline), 10000)
                try:
                    data["databases"][p]["max_captured_at"] = db.execute(
                        "SELECT MAX(captured_at) FROM crown_snapshots").fetchone()[0]
                except sqlite3.OperationalError:
                    data["databases"][p]["max_captured_at_check"] = "bounded_query_timeout"
        finally:
            db.close()
    p = Path("/var/lib/crown-m1m6/status.json")
    data["public_status_mtime"] = p.stat().st_mtime
    return data


def cleanup():
    """Serialize scheduled and manually dispatched runs, fail closed on overlap."""
    with LOCK.open("a") as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            return {"summary": {"action": "approved_browser_tmp_cleanup",
                                "stop_reason": "another_cleanup_running",
                                "deleted_directories": 0, "deleted_allocated_bytes": 0,
                                "database_writes": 0, "service_restarts": 0}}
        return _cleanup_unlocked()


def _cleanup_unlocked():
    assert os.geteuid() == 0
    assert ROOT.resolve() == ROOT and ROOT.is_dir()
    assert shutil.rmtree.avoids_symlink_attacks
    os.nice(10)
    command(["ionice", "-c", "3", "-p", str(os.getpid())])
    started = time.time()
    cutoff = started-7*86400
    before = health()
    protected = [Path("/etc/crown-m1m6.json"), Path("/opt/crown-m1m6/policy.py"),
                 Path("/var/lib/crown-m1m6/performance_epoch.json")]
    hashes = {str(p): hashlib.sha256(p.read_bytes()).hexdigest() for p in protected}
    used, process_count = active_names()
    refreshed = time.monotonic()
    device = ROOT.stat().st_dev
    candidates = []
    with os.scandir(ROOT) as entries:
        for entry in entries:
            if NAME.fullmatch(entry.name) and entry.is_dir(follow_symlinks=False):
                s = entry.stat(follow_symlinks=False)
                if max(s.st_mtime, s.st_ctime) < cutoff:
                    candidates.append((s.st_mtime, entry.name))
    candidates.sort()
    audit_path = RECEIPT.with_name(f"browser-tmp-cleanup-{int(started)}.jsonl")
    skipped = collections.Counter()
    deleted = allocated = file_entries = 0
    stop = "all_candidates_checked"
    with audit_path.open("x") as audit:
        for _, name in candidates:
            if time.time()-started > 360:
                stop = "time_bounded_batch"
                break
            if shutil.disk_usage("/").free >= 32*1024**3:
                stop = "free_space_safety_target_reached"
                break
            if time.monotonic()-refreshed >= 2:
                used, process_count = active_names()
                refreshed = time.monotonic()
            path = ROOT/name
            proof, reason = eligible(path, cutoff, used, device)
            if reason:
                skipped[reason] += 1
                continue
            # Recheck references after a potentially long subtree traversal.
            if time.monotonic()-refreshed >= 2:
                used, process_count = active_names()
                refreshed = time.monotonic()
            if name in used:
                skipped["active_process"] += 1
                continue
            s = path.lstat()
            if (s.st_ino, s.st_mtime_ns, s.st_ctime_ns) != (proof["inode"], proof["mtime"], proof["ctime"]):
                skipped["changed_before_delete"] += 1
                continue
            audit.write(json.dumps({"name": name, "proof": proof, "state": "approved_for_delete",
                                    "at": time.time()})+"\n")
            audit.flush()
            # Anchored parent descriptor and symlink-resistant stdlib deletion.
            parent = os.open(ROOT, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
            try:
                shutil.rmtree(name, dir_fd=parent)
            finally:
                os.close(parent)
            deleted += 1
            allocated += proof["allocated_bytes"]
            file_entries += proof["entries"]
            audit.write(json.dumps({"name": name, "state": "deleted"})+"\n")
            if deleted % 100 == 0:
                audit.flush()
                os.fsync(audit.fileno())
                time.sleep(0.1)
    after = health()
    assert hashes == {str(p): hashlib.sha256(p.read_bytes()).hexdigest() for p in protected}
    receipt = {"action": "approved_browser_tmp_cleanup", "started_at": started, "cutoff": cutoff,
               "root": str(ROOT), "candidate_count": len(candidates), "deleted_directories": deleted,
               "deleted_allocated_bytes": allocated, "deleted_tree_entries": file_entries,
               "skipped": dict(skipped), "stop_reason": stop, "audit_path": str(audit_path),
               "last_process_inventory_count": process_count, "before": before, "after": after,
               "database_writes": 0, "service_restarts": 0, "telegram_sends": 0,
               "protected_config_unchanged": True}
    RECEIPT.write_text(json.dumps(receipt, ensure_ascii=False, indent=2))
    return {"summary": receipt}


def verify():
    return {"summary": {"action": "browser_cleanup_verify", "deletes": 0},
            "receipt": json.loads(RECEIPT.read_text()), "health": health(),
            "write_error_check": command(["journalctl", "-u", "crown-m1m6.service",
                "-u", "crown-tick.service", "-u", "crown-sweep.service", "--since", "15 minutes ago",
                "--no-pager", "--case-sensitive=no", "--grep",
                "no space left|database or disk is full|sqlite_full|disk i/o error"], 30)}
