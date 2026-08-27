#!/usr/bin/env python3
"""Securely stream a bounded Footbreak ledger snapshot under its writer lock."""
from __future__ import annotations

import argparse
import fcntl
import hashlib
import os
import re
import stat
import subprocess
import sys
import time
from pathlib import Path
from typing import Callable


MAX_LEDGER_BYTES = 128 * 1024 * 1024
SHA256_RE = re.compile(r"[0-9a-f]{64}")
COMMIT_RE = re.compile(r"[0-9a-f]{40}")


class CaptureFailure(RuntimeError):
    pass


def _identity(info: os.stat_result) -> tuple[int, int]:
    return info.st_dev, info.st_ino


def _validate_regular_single_link(info: os.stat_result, code: str) -> None:
    if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1:
        raise CaptureFailure(code)


def _open_verified(path: Path, code: str) -> tuple[int, os.stat_result]:
    before = os.lstat(path)
    _validate_regular_single_link(before, code)
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC)
    try:
        opened = os.fstat(fd)
        _validate_regular_single_link(opened, code)
        if _identity(before) != _identity(opened):
            raise CaptureFailure(f"{code}_identity_changed")
        return fd, opened
    except Exception:
        os.close(fd)
        raise


def _verify_path_fd(path: Path, fd: int, expected: os.stat_result, code: str) -> None:
    current_fd = os.fstat(fd)
    current_path = os.lstat(path)
    _validate_regular_single_link(current_fd, code)
    _validate_regular_single_link(current_path, code)
    if (
        _identity(current_fd) != _identity(expected)
        or _identity(current_path) != _identity(expected)
    ):
        raise CaptureFailure(f"{code}_identity_changed")


def _read_fd_bounded(
    fd: int, expected: os.stat_result, maximum: int, code: str,
) -> bytes:
    if expected.st_size > maximum:
        raise CaptureFailure(f"{code}_size_exceeded")
    payload = bytearray()
    while len(payload) <= maximum:
        block = os.read(fd, min(1024 * 1024, maximum + 1 - len(payload)))
        if not block:
            break
        payload.extend(block)
    if len(payload) > maximum:
        raise CaptureFailure(f"{code}_size_exceeded")
    after = os.fstat(fd)
    if (
        _identity(after) != _identity(expected)
        or after.st_nlink != 1
        or after.st_size != expected.st_size
        or after.st_mtime_ns != expected.st_mtime_ns
        or after.st_ctime_ns != expected.st_ctime_ns
        or len(payload) != expected.st_size
    ):
        raise CaptureFailure(f"{code}_changed_during_read")
    return bytes(payload)


def _read_verified(path: Path, maximum: int, code: str) -> bytes:
    fd, opened = _open_verified(path, code)
    try:
        payload = _read_fd_bounded(fd, opened, maximum, code)
        _verify_path_fd(path, fd, opened, code)
        return payload
    finally:
        os.close(fd)


def verify_deployed_code(
    repo: Path, expected_commit: str, expected_validation_sha256: str,
) -> None:
    if (
        COMMIT_RE.fullmatch(expected_commit) is None
        or SHA256_RE.fullmatch(expected_validation_sha256) is None
    ):
        raise CaptureFailure("invalid_deployed_code_authority")
    result = subprocess.run(
        ["git", "-C", str(repo), "rev-parse", "HEAD"],
        check=False,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.DEVNULL,
        timeout=10,
    )
    if result.returncode != 0 or result.stdout.strip() != expected_commit:
        raise CaptureFailure("deployed_commit_mismatch")
    validation = repo / "analysis" / "wilson_validation.py"
    payload = _read_verified(validation, 8 * 1024 * 1024, "deployed_validation")
    if hashlib.sha256(payload).hexdigest() != expected_validation_sha256:
        raise CaptureFailure("deployed_validation_hash_mismatch")


def capture(
    lock_path: Path,
    ledger_path: Path,
    repo: Path,
    expected_commit: str,
    expected_validation_sha256: str,
    *,
    lock_timeout: float = 120.0,
    after_lock_hook: Callable[[], None] | None = None,
    after_read_hook: Callable[[], None] | None = None,
) -> bytes:
    lock_fd, lock_info = _open_verified(lock_path, "lock_path_invalid")
    acquired = False
    try:
        deadline = time.monotonic() + lock_timeout
        while True:
            try:
                fcntl.flock(lock_fd, fcntl.LOCK_SH | fcntl.LOCK_NB)
                acquired = True
                break
            except BlockingIOError:
                if time.monotonic() >= deadline:
                    raise CaptureFailure("lock_timeout")
                time.sleep(0.1)
        if after_lock_hook is not None:
            after_lock_hook()
        _verify_path_fd(lock_path, lock_fd, lock_info, "lock_path_invalid")
        verify_deployed_code(repo, expected_commit, expected_validation_sha256)

        ledger_fd, ledger_info = _open_verified(
            ledger_path, "ledger_path_invalid",
        )
        try:
            payload = _read_fd_bounded(
                ledger_fd, ledger_info, MAX_LEDGER_BYTES, "ledger",
            )
            if after_read_hook is not None:
                after_read_hook()
            _verify_path_fd(
                ledger_path, ledger_fd, ledger_info, "ledger_path_invalid",
            )
            _verify_path_fd(lock_path, lock_fd, lock_info, "lock_path_invalid")
            verify_deployed_code(repo, expected_commit, expected_validation_sha256)
            _verify_path_fd(
                ledger_path, ledger_fd, ledger_info, "ledger_path_invalid",
            )
            _verify_path_fd(lock_path, lock_fd, lock_info, "lock_path_invalid")
        finally:
            os.close(ledger_fd)
        return payload
    finally:
        if acquired:
            fcntl.flock(lock_fd, fcntl.LOCK_UN)
        os.close(lock_fd)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--lock", required=True, type=Path)
    parser.add_argument("--ledger", required=True, type=Path)
    parser.add_argument("--repo", required=True, type=Path)
    parser.add_argument("--expected-commit", required=True)
    parser.add_argument("--expected-validation-sha256", required=True)
    parser.add_argument("--lock-timeout", type=float, default=120.0)
    args = parser.parse_args(argv)
    try:
        payload = capture(
            args.lock,
            args.ledger,
            args.repo,
            args.expected_commit,
            args.expected_validation_sha256,
            lock_timeout=args.lock_timeout,
        )
    except Exception:
        return 1
    sys.stdout.buffer.write(payload)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
