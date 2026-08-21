#!/usr/bin/env python3
"""Bounded local disk protection for the Footbreak/Crown runtime.

Only derived public history generations and abandoned atomic-write temporary
files are eligible for deletion. Authoritative ledgers, prediction history,
learning databases and current dashboard artifacts are never cleanup targets.
"""
from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import stat
import subprocess
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable


GIB = 1024**3
MIB = 1024**2
DEFAULT_WARNING_FREE_BYTES = 4 * GIB
DEFAULT_CRITICAL_FREE_BYTES = 2 * GIB
DEFAULT_MINIMUM_WRITE_FREE_BYTES = 256 * MIB
DEFAULT_RESERVE_BYTES = 256 * MIB
DEFAULT_STALE_SECONDS = 60 * 60
DEFAULT_HISTORY_GENERATIONS = 5
HISTORY_NAME = re.compile(r"^history-[0-9a-f]{20}\.json$")
TEMP_PREFIXES = (
    ".ledger.json.",
    ".predictions.json.",
    ".prediction_history.json.",
    ".notify_state.json.",
    ".pinnapi_live.json.",
    ".footbreak-execution-evidence.json.",
    ".data.json.",
    ".dashboard-data-",
    ".prediction-history.",
    ".sim-ledger-",
    ".settle-",
    ".accuracy.",
    ".incident-alert-",
    ".tmp-",
)


@dataclass(frozen=True)
class DiskStatus:
    total: int
    used: int
    free: int

    @property
    def used_percent(self) -> float:
        return (self.used / self.total * 100.0) if self.total else 100.0


@dataclass(frozen=True)
class MaintenanceResult:
    status: DiskStatus
    released_reserve: bool
    recreated_reserve: bool
    removed_temp_files: int
    removed_temp_bytes: int
    removed_history_files: int
    removed_history_bytes: int
    pruned_old_build_cache: bool


def _env_int(name: str, default: int, minimum: int = 1) -> int:
    try:
        return max(minimum, int(os.environ.get(name, default)))
    except (TypeError, ValueError):
        return default


def disk_status(path: Path = Path("/")) -> DiskStatus:
    usage = shutil.disk_usage(path)
    return DiskStatus(total=usage.total, used=usage.used, free=usage.free)


def warning_free_bytes() -> int:
    return _env_int("DISK_GUARD_WARNING_FREE_BYTES", DEFAULT_WARNING_FREE_BYTES)


def critical_free_bytes() -> int:
    return _env_int("DISK_GUARD_CRITICAL_FREE_BYTES", DEFAULT_CRITICAL_FREE_BYTES)


def minimum_write_free_bytes() -> int:
    return _env_int(
        "DISK_GUARD_MINIMUM_WRITE_FREE_BYTES", DEFAULT_MINIMUM_WRITE_FREE_BYTES,
    )


def reserve_path() -> Path:
    return Path(os.environ.get(
        "DISK_GUARD_RESERVE_PATH", "/var/lib/footbreak/.disk-reserve",
    ))


def _regular_file(path: Path) -> os.stat_result | None:
    try:
        value = path.lstat()
    except OSError:
        return None
    return value if stat.S_ISREG(value.st_mode) and not stat.S_ISLNK(value.st_mode) else None


def release_reserve(path: Path | None = None) -> bool:
    target = path or reserve_path()
    info = _regular_file(target)
    if info is None:
        return False
    try:
        target.unlink()
        return True
    except OSError:
        return False


def ensure_reserve(
    status: DiskStatus,
    path: Path | None = None,
    size: int | None = None,
) -> bool:
    target = path or reserve_path()
    wanted = size or _env_int("DISK_GUARD_RESERVE_BYTES", DEFAULT_RESERVE_BYTES)
    current = _regular_file(target)
    if current is not None and current.st_size == wanted:
        return False
    # Do not consume the last safety margin merely to recreate the reserve.
    if status.free < warning_free_bytes() + wanted:
        return False
    target.parent.mkdir(parents=True, exist_ok=True)
    temporary = target.with_name(f".{target.name}.{os.getpid()}")
    try:
        fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        try:
            if hasattr(os, "posix_fallocate"):
                os.posix_fallocate(fd, 0, wanted)
            else:
                os.lseek(fd, wanted - 1, os.SEEK_SET)
                os.write(fd, b"\0")
            os.fsync(fd)
        finally:
            os.close(fd)
        os.replace(temporary, target)
        os.chmod(target, 0o600)
        return True
    finally:
        try:
            temporary.unlink()
        except FileNotFoundError:
            pass


def _is_known_temp(path: Path) -> bool:
    return (
        any(path.name.startswith(prefix) for prefix in TEMP_PREFIXES)
        or (
            path.name.startswith(".history-")
            and ".json." in path.name
        )
    )


def cleanup_stale_atomic_temps(
    roots: Iterable[Path],
    *,
    now: float | None = None,
    stale_seconds: int | None = None,
) -> tuple[int, int]:
    cutoff = (now if now is not None else time.time()) - (
        stale_seconds or _env_int("DISK_GUARD_STALE_SECONDS", DEFAULT_STALE_SECONDS)
    )
    removed = reclaimed = 0
    for root in roots:
        try:
            candidates = list(root.iterdir())
        except OSError:
            continue
        for path in candidates:
            info = _regular_file(path)
            if info is None or info.st_mtime > cutoff or not _is_known_temp(path):
                continue
            try:
                path.unlink()
            except OSError:
                continue
            removed += 1
            reclaimed += info.st_size
    return removed, reclaimed


def _current_history_name(web_root: Path) -> str | None:
    try:
        payload = json.loads((web_root / "data.json").read_text(encoding="utf-8"))
    except (OSError, ValueError, TypeError):
        return None
    value = str(payload.get("history_data_url") or "") if isinstance(payload, dict) else ""
    return value if HISTORY_NAME.fullmatch(value) else None


def prune_crown_history_sidecars(
    web_root: Path,
    *,
    keep_generations: int | None = None,
) -> tuple[int, int]:
    keep_count = keep_generations or _env_int(
        "DISK_GUARD_HISTORY_GENERATIONS", DEFAULT_HISTORY_GENERATIONS,
    )
    current = _current_history_name(web_root)
    # If the public pointer is unavailable, retain every generation. Deleting
    # a possibly referenced sidecar under ambiguous state is not safe.
    if current is None:
        return 0, 0
    try:
        rows = [
            (path, info)
            for path in web_root.iterdir()
            if HISTORY_NAME.fullmatch(path.name)
            and (info := _regular_file(path)) is not None
        ]
    except OSError:
        return 0, 0
    rows.sort(key=lambda item: (item[1].st_mtime_ns, item[0].name), reverse=True)
    keep = {path.name for path, _info in rows[:keep_count]}
    if current:
        keep.add(current)
    removed = reclaimed = 0
    for path, info in rows:
        if path.name in keep:
            continue
        try:
            path.unlink()
        except OSError:
            continue
        removed += 1
        reclaimed += info.st_size
    return removed, reclaimed


def prune_old_docker_build_cache(*, timeout_seconds: int = 60) -> bool:
    """Prune only unused build cache older than seven days.

    Running containers, images and volumes are deliberately outside this
    command's scope.
    """
    docker = shutil.which("docker")
    if not docker:
        return False
    try:
        completed = subprocess.run(
            [docker, "builder", "prune", "-af", "--filter", "until=168h"],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            check=False,
            timeout=timeout_seconds,
        )
    except (OSError, subprocess.SubprocessError):
        return False
    return completed.returncode == 0


def run_maintenance(
    *,
    filesystem: Path = Path("/"),
    crown_state: Path = Path("/var/lib/footbreak/crown"),
    shared_state: Path = Path("/var/lib/footbreak"),
    footbreak_state: Path = Path("/opt/footbreak/system"),
    crown_web: Path = Path("/var/www/crown"),
    footbreak_web: Path = Path("/var/www/footbreak"),
    reserve: Path | None = None,
    preflight: bool = False,
) -> MaintenanceResult:
    status = disk_status(filesystem)
    released = False
    recreated = False
    pruned_build_cache = False
    temp_count = temp_bytes = history_count = history_bytes = 0
    if status.free < critical_free_bytes():
        released = release_reserve(reserve)
        temp_count, temp_bytes = cleanup_stale_atomic_temps(
            (shared_state, crown_state, footbreak_state, crown_web, footbreak_web),
        )
        history_count, history_bytes = prune_crown_history_sidecars(crown_web)
        status = disk_status(filesystem)
    elif not preflight:
        temp_count, temp_bytes = cleanup_stale_atomic_temps(
            (shared_state, crown_state, footbreak_state, crown_web, footbreak_web),
        )
        history_count, history_bytes = prune_crown_history_sidecars(crown_web)
        status = disk_status(filesystem)
    if not preflight and status.free < warning_free_bytes():
        pruned_build_cache = prune_old_docker_build_cache()
        status = disk_status(filesystem)
    if not preflight:
        recreated = ensure_reserve(status, reserve)
        status = disk_status(filesystem)
    return MaintenanceResult(
        status=status,
        released_reserve=released,
        recreated_reserve=recreated,
        removed_temp_files=temp_count,
        removed_temp_bytes=temp_bytes,
        removed_history_files=history_count,
        removed_history_bytes=history_bytes,
        pruned_old_build_cache=pruned_build_cache,
    )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--preflight", action="store_true")
    args = parser.parse_args(argv)
    result = run_maintenance(preflight=args.preflight)
    print(json.dumps({
        "free_bytes": result.status.free,
        "used_percent": round(result.status.used_percent, 1),
        "released_reserve": result.released_reserve,
        "recreated_reserve": result.recreated_reserve,
        "removed_temp_files": result.removed_temp_files,
        "removed_temp_bytes": result.removed_temp_bytes,
        "removed_history_files": result.removed_history_files,
        "removed_history_bytes": result.removed_history_bytes,
        "pruned_old_build_cache": result.pruned_old_build_cache,
    }, sort_keys=True))
    return 0 if result.status.free >= minimum_write_free_bytes() else 75


if __name__ == "__main__":
    raise SystemExit(main())
