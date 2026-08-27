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
MAX_REPOSITORY_BYTES = 512 * 1024 * 1024
MAX_TRACKED_FILE_BYTES = 128 * 1024 * 1024
SHA256_RE = re.compile(r"[0-9a-f]{64}")
COMMIT_RE = re.compile(r"[0-9a-f]{40}")
TREE_RE = re.compile(r"[0-9a-f]{40}")
SOURCE_SUFFIXES = (".py", ".pyc", ".so", ".pyd", ".pth", ".egg-link")
PUBLIC_FAILURE_CODES = frozenset({
    "condition17_already_activated",
    "deployed_commit_mismatch",
    "deployed_commit_unavailable",
    "deployed_git_dir_unavailable",
    "deployed_git_object_integrity_failed",
    "deployed_git_paths_mismatch",
    "deployed_import_origin_mismatch",
    "deployed_index_dirty",
    "deployed_repository_identity_changed",
    "deployed_repository_path_invalid",
    "deployed_repository_too_large",
    "deployed_repository_top_unavailable",
    "deployed_tracked_content_mismatch",
    "deployed_tracked_file_changed_during_read",
    "deployed_tracked_file_identity_changed",
    "deployed_tracked_file_invalid",
    "deployed_tracked_file_size_exceeded",
    "deployed_tracked_file_too_large",
    "deployed_tracked_identity_changed",
    "deployed_tracked_mode_mismatch",
    "deployed_tree_empty",
    "deployed_tree_entry_unsupported",
    "deployed_tree_listing_failed",
    "deployed_tree_listing_invalid",
    "deployed_tree_mismatch",
    "deployed_tree_path_invalid",
    "deployed_tree_unavailable",
    "deployed_untracked_listing_failed",
    "deployed_untracked_path_invalid",
    "deployed_untracked_source_shadow",
    "deployed_validation_dependency_missing",
    "deployed_validation_hash_mismatch",
    "deployed_worktree_dirty",
    "invalid_deployed_code_authority",
    "ledger_changed_during_read",
    "ledger_path_invalid",
    "ledger_path_invalid_identity_changed",
    "ledger_size_exceeded",
    "lock_path_invalid",
    "lock_path_invalid_identity_changed",
    "lock_timeout",
})


class CaptureFailure(RuntimeError):
    pass


def _public_failure_code(exc: CaptureFailure) -> str:
    reason = str(exc)
    if reason in PUBLIC_FAILURE_CODES:
        return reason
    return "capture_invariant_failure"


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


def _git_environment() -> dict[str, str]:
    env = {
        key: value for key, value in os.environ.items()
        if not key.startswith("GIT_")
    }
    env.update({
        "GIT_CONFIG_NOSYSTEM": "1",
        "GIT_CONFIG_GLOBAL": "/dev/null",
        "GIT_OPTIONAL_LOCKS": "0",
        "HOME": "/nonexistent",
        "XDG_CONFIG_HOME": "/nonexistent",
        "PATH": "/usr/bin:/bin",
    })
    return env


def _git(
    repo: Path, arguments: list[str], *, timeout: float = 30,
) -> subprocess.CompletedProcess[bytes]:
    return subprocess.run(
        [
            "git",
            "--git-dir", str(repo / ".git"),
            "--work-tree", str(repo),
            "-c", "core.fsmonitor=false",
            "-c", "core.untrackedCache=false",
            "-c", "core.hooksPath=/dev/null",
            *arguments,
        ],
        check=False,
        stdout=subprocess.PIPE,
        stderr=subprocess.DEVNULL,
        timeout=timeout,
        env=_git_environment(),
        cwd="/",
    )


def _require_git_success(
    result: subprocess.CompletedProcess[bytes], code: str,
) -> bytes:
    if result.returncode != 0:
        raise CaptureFailure(code)
    return result.stdout


def _open_repository_root(repo: Path) -> tuple[int, os.stat_result, os.stat_result]:
    before = os.lstat(repo)
    git_before = os.lstat(repo / ".git")
    if (
        not stat.S_ISDIR(before.st_mode)
        or not stat.S_ISDIR(git_before.st_mode)
    ):
        raise CaptureFailure("deployed_repository_path_invalid")
    fd = os.open(
        repo, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC,
    )
    opened = os.fstat(fd)
    if _identity(opened) != _identity(before):
        os.close(fd)
        raise CaptureFailure("deployed_repository_identity_changed")
    return fd, opened, git_before


def _verify_repository_identity(
    repo: Path, repo_fd: int, expected: os.stat_result,
    git_expected: os.stat_result,
) -> None:
    current_fd = os.fstat(repo_fd)
    current_path = os.lstat(repo)
    current_git = os.lstat(repo / ".git")
    if (
        not stat.S_ISDIR(current_fd.st_mode)
        or not stat.S_ISDIR(current_path.st_mode)
        or not stat.S_ISDIR(current_git.st_mode)
        or _identity(current_fd) != _identity(expected)
        or _identity(current_path) != _identity(expected)
        or _identity(current_git) != _identity(git_expected)
    ):
        raise CaptureFailure("deployed_repository_identity_changed")


def _open_relative_file(
    root_fd: int, relative: str,
) -> tuple[int, os.stat_result]:
    parts = relative.split("/")
    if (
        not relative
        or relative.startswith("/")
        or any(part in {"", ".", ".."} for part in parts)
    ):
        raise CaptureFailure("deployed_tree_path_invalid")
    directory_fd = os.dup(root_fd)
    try:
        for part in parts[:-1]:
            next_fd = os.open(
                part,
                os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC,
                dir_fd=directory_fd,
            )
            os.close(directory_fd)
            directory_fd = next_fd
        before = os.stat(
            parts[-1], dir_fd=directory_fd, follow_symlinks=False,
        )
        if not stat.S_ISREG(before.st_mode) or before.st_nlink != 1:
            raise CaptureFailure("deployed_tracked_file_invalid")
        fd = os.open(
            parts[-1], os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC,
            dir_fd=directory_fd,
        )
        opened = os.fstat(fd)
        if (
            not stat.S_ISREG(opened.st_mode)
            or opened.st_nlink != 1
            or _identity(opened) != _identity(before)
        ):
            os.close(fd)
            raise CaptureFailure("deployed_tracked_file_identity_changed")
        return fd, opened
    finally:
        os.close(directory_fd)


def _read_relative_file(
    root_fd: int, relative: str, *, executable: bool,
) -> tuple[bytes, tuple[int, int]]:
    fd, opened = _open_relative_file(root_fd, relative)
    try:
        if opened.st_size > MAX_TRACKED_FILE_BYTES:
            raise CaptureFailure("deployed_tracked_file_too_large")
        payload = _read_fd_bounded(
            fd, opened, MAX_TRACKED_FILE_BYTES, "deployed_tracked_file",
        )
        if bool(opened.st_mode & 0o111) != executable:
            raise CaptureFailure("deployed_tracked_mode_mismatch")
        check_fd, current = _open_relative_file(root_fd, relative)
        os.close(check_fd)
        if _identity(current) != _identity(opened):
            raise CaptureFailure("deployed_tracked_file_identity_changed")
        return payload, _identity(opened)
    finally:
        os.close(fd)


def _git_blob_oid(payload: bytes) -> str:
    digest = hashlib.sha1()
    digest.update(f"blob {len(payload)}\0".encode("ascii"))
    digest.update(payload)
    return digest.hexdigest()


def _verify_tracked_tree(
    repo: Path, repo_fd: int, expected_commit: str, expected_tree: str,
) -> tuple[dict[str, str], dict[str, tuple[int, int]]]:
    head = _require_git_success(
        _git(repo, ["rev-parse", "--verify", "HEAD^{commit}"]),
        "deployed_commit_unavailable",
    ).decode("ascii").strip()
    if head != expected_commit:
        raise CaptureFailure("deployed_commit_mismatch")
    tree = _require_git_success(
        _git(repo, ["rev-parse", "--verify", f"{expected_commit}^{{tree}}"]),
        "deployed_tree_unavailable",
    ).decode("ascii").strip()
    if tree != expected_tree:
        raise CaptureFailure("deployed_tree_mismatch")
    top = _require_git_success(
        _git(repo, ["rev-parse", "--show-toplevel"]),
        "deployed_repository_top_unavailable",
    ).decode("utf-8").strip()
    git_dir = _require_git_success(
        _git(repo, ["rev-parse", "--absolute-git-dir"]),
        "deployed_git_dir_unavailable",
    ).decode("utf-8").strip()
    if Path(top).resolve() != repo.resolve() or Path(git_dir).resolve() != (
        repo / ".git"
    ).resolve():
        raise CaptureFailure("deployed_git_paths_mismatch")
    _require_git_success(
        _git(
            repo,
            [
                "fsck", "--strict", "--no-dangling", "--no-reflogs",
                expected_tree,
            ],
            timeout=60,
        ),
        "deployed_git_object_integrity_failed",
    )
    _require_git_success(
        _git(repo, ["diff-index", "--cached", "--quiet", expected_commit, "--"]),
        "deployed_index_dirty",
    )
    _require_git_success(
        _git(repo, ["diff-files", "--quiet", "--"]),
        "deployed_worktree_dirty",
    )

    listing = _require_git_success(
        _git(repo, ["ls-tree", "-rz", "--full-tree", expected_commit]),
        "deployed_tree_listing_failed",
    )
    hashes: dict[str, str] = {}
    identities: dict[str, tuple[int, int]] = {}
    total = 0
    for record in listing.split(b"\0"):
        if not record:
            continue
        try:
            metadata, raw_path = record.split(b"\t", 1)
            mode, object_type, oid = metadata.decode("ascii").split()
            relative = raw_path.decode("utf-8", "strict")
        except (ValueError, UnicodeDecodeError):
            raise CaptureFailure("deployed_tree_listing_invalid") from None
        if object_type != "blob" or mode not in {"100644", "100755"}:
            raise CaptureFailure("deployed_tree_entry_unsupported")
        payload, identity = _read_relative_file(
            repo_fd, relative, executable=mode == "100755",
        )
        total += len(payload)
        if total > MAX_REPOSITORY_BYTES:
            raise CaptureFailure("deployed_repository_too_large")
        actual_oid = _git_blob_oid(payload)
        if actual_oid != oid:
            raise CaptureFailure("deployed_tracked_content_mismatch")
        hashes[relative] = hashlib.sha256(payload).hexdigest()
        identities[relative] = identity
    if not hashes:
        raise CaptureFailure("deployed_tree_empty")
    return hashes, identities


def _verify_no_source_shadows(repo: Path) -> None:
    candidates: set[str] = set()
    for arguments in (
        ["ls-files", "--others", "--exclude-standard", "-z"],
        ["ls-files", "--others", "--ignored", "--exclude-standard", "-z"],
    ):
        raw = _require_git_success(
            _git(repo, arguments),
            "deployed_untracked_listing_failed",
        )
        try:
            candidates.update(
                item.decode("utf-8", "strict")
                for item in raw.split(b"\0") if item
            )
        except UnicodeDecodeError:
            raise CaptureFailure("deployed_untracked_path_invalid") from None
    for relative in candidates:
        normalized = relative.replace("\\", "/")
        if normalized == ".venv" or normalized.startswith(".venv/"):
            continue
        if "/__pycache__/" in f"/{normalized}/" and normalized.endswith(".pyc"):
            continue
        if normalized.endswith(SOURCE_SUFFIXES):
            raise CaptureFailure("deployed_untracked_source_shadow")


def verify_deployed_code(
    repo: Path,
    expected_commit: str,
    expected_tree: str,
    expected_validation_sha256: str,
    activation_marker: Path,
    expected_identities: dict[str, tuple[int, int]] | None = None,
) -> dict[str, tuple[int, int]]:
    if (
        COMMIT_RE.fullmatch(expected_commit) is None
        or TREE_RE.fullmatch(expected_tree) is None
        or SHA256_RE.fullmatch(expected_validation_sha256) is None
    ):
        raise CaptureFailure("invalid_deployed_code_authority")
    if os.path.lexists(activation_marker):
        raise CaptureFailure("condition17_already_activated")
    repo_fd, repo_info, git_info = _open_repository_root(repo)
    try:
        hashes, identities = _verify_tracked_tree(
            repo, repo_fd, expected_commit, expected_tree,
        )
        if expected_identities is not None and identities != expected_identities:
            raise CaptureFailure("deployed_tracked_identity_changed")
        _verify_no_source_shadows(repo)
        validation_hash = hashes.get("analysis/wilson_validation.py")
        quarter_line_hash = hashes.get("analysis/quarter_line.py")
        if validation_hash != expected_validation_sha256:
            raise CaptureFailure("deployed_validation_hash_mismatch")
        if not isinstance(quarter_line_hash, str):
            raise CaptureFailure("deployed_validation_dependency_missing")
        # Never import or execute code from the production worktree.  The
        # capture process verifies every tracked blob and streams ledger bytes
        # only.  Interpretation happens later in the exact reviewed checkout.
        _verify_repository_identity(repo, repo_fd, repo_info, git_info)
        if os.path.lexists(activation_marker):
            raise CaptureFailure("condition17_already_activated")
        return identities
    finally:
        os.close(repo_fd)


def capture(
    lock_path: Path,
    ledger_path: Path,
    repo: Path,
    expected_commit: str,
    expected_tree: str,
    expected_validation_sha256: str,
    activation_marker: Path,
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
        deployed_identities = verify_deployed_code(
            repo, expected_commit, expected_tree,
            expected_validation_sha256, activation_marker,
        )

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
            verify_deployed_code(
                repo, expected_commit, expected_tree,
                expected_validation_sha256, activation_marker,
                expected_identities=deployed_identities,
            )
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
    parser.add_argument("--expected-tree", required=True)
    parser.add_argument("--expected-validation-sha256", required=True)
    parser.add_argument("--activation-marker", required=True, type=Path)
    parser.add_argument("--lock-timeout", type=float, default=120.0)
    args = parser.parse_args(argv)
    try:
        payload = capture(
            args.lock,
            args.ledger,
            args.repo,
            args.expected_commit,
            args.expected_tree,
            args.expected_validation_sha256,
            args.activation_marker,
            lock_timeout=args.lock_timeout,
        )
    except CaptureFailure as exc:
        reason = _public_failure_code(exc)
        print(f"condition17_capture_failure={reason}", file=sys.stderr)
        return 1
    except Exception:
        print(
            "condition17_capture_failure=capture_unexpected_failure",
            file=sys.stderr,
        )
        return 1
    sys.stdout.buffer.write(payload)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
