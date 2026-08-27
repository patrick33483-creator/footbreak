#!/usr/bin/python3 -I
"""Root-installed narrow wrapper for immutable offline-audit bundles.

Install this exact file outside the checkout as
``/usr/local/sbin/footbreak-audit-bundle``.  The workflow pins its bytes.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import pwd
import re
import signal
import stat
import tempfile

NAMES = (
    "ledger-sha256.txt",
    "wilson-production-audit-summary.json",
    "footbreak-wilson-registry-audit.json",
    "crown-wilson-registry-audit.json",
    "footbreak-wilson-registry-chains.json",
    "crown-wilson-registry-chains.json",
    "footbreak-legacy-batch-live-discovery.json",
)
ENTRY_KEYS = {"filename", "sha256", "size"}
HEX64 = re.compile(r"[0-9a-f]{64}")
RUN_KEY = re.compile(r"[1-9][0-9]*-[1-9][0-9]*")
POLICY_ROOT = re.compile(r"[0-9a-f]{64}")
PREFLIGHT_SCHEMA = "footbreak-audit-sudo-preflight-v1"
INSTALLED_WRAPPER = "/usr/local/sbin/footbreak-audit-bundle"


def _strict_json(raw: bytes):
    def pairs(items):
        result = {}
        for key, value in items:
            if key in result:
                raise ValueError("duplicate_json_key")
            result[key] = value
        return result
    return json.loads(raw.decode("utf-8"), object_pairs_hook=pairs)


def _read_single(path: str) -> bytes:
    descriptor = os.open(path, os.O_RDONLY | os.O_CLOEXEC | os.O_NOFOLLOW)
    try:
        info = os.fstat(descriptor)
        by_path = os.stat(path, follow_symlinks=False)
        if (
            not stat.S_ISREG(info.st_mode) or info.st_nlink != 1
            or (info.st_dev, info.st_ino) != (by_path.st_dev, by_path.st_ino)
        ):
            raise ValueError("unsafe_source")
        chunks = []
        while True:
            chunk = os.read(descriptor, 1024 * 1024)
            if not chunk:
                break
            chunks.append(chunk)
        after = os.fstat(descriptor)
        if (
            info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns
        ) != (
            after.st_dev, after.st_ino, after.st_size, after.st_mtime_ns
        ):
            raise ValueError("source_changed")
        return b"".join(chunks)
    finally:
        os.close(descriptor)


def _open_absolute(path: str, *, directory: bool = False) -> int:
    """Open an absolute path without following any component symlink."""
    if not path.startswith("/") or "\x00" in path:
        raise ValueError("absolute_path_required")
    components = [part for part in path.split("/") if part]
    if not components:
        raise ValueError("root_path_forbidden")
    current = os.open(
        "/", os.O_RDONLY | os.O_CLOEXEC | os.O_DIRECTORY | os.O_NOFOLLOW,
    )
    try:
        for index, component in enumerate(components):
            if component in {".", ".."}:
                raise ValueError("unsafe_path_component")
            last = index == len(components) - 1
            flags = os.O_RDONLY | os.O_CLOEXEC | os.O_NOFOLLOW
            if not last or directory:
                flags |= os.O_DIRECTORY
            following = os.open(component, flags, dir_fd=current)
            os.close(current)
            current = following
            info = os.fstat(current)
            if not last and (
                not stat.S_ISDIR(info.st_mode) or info.st_uid != 0
                or info.st_gid != 0 or stat.S_IMODE(info.st_mode) & 0o022
            ):
                raise ValueError("unsafe_policy_parent")
        return current
    except BaseException:
        os.close(current)
        raise


def _policy_file(
    path: str, *, required_uid: int = 0, required_gid: int = 0,
):
    descriptor = _open_absolute(path)
    try:
        before = os.fstat(descriptor)
        if (
            not stat.S_ISREG(before.st_mode) or before.st_nlink != 1
            or before.st_uid != required_uid or before.st_gid != required_gid
            or stat.S_IMODE(before.st_mode) & 0o022
        ):
            raise ValueError("unsafe_policy_file")
        chunks = []
        while True:
            chunk = os.read(descriptor, 1024 * 1024)
            if not chunk:
                break
            chunks.append(chunk)
        after = os.fstat(descriptor)
        if (
            before.st_dev, before.st_ino, before.st_size, before.st_mtime_ns
        ) != (
            after.st_dev, after.st_ino, after.st_size, after.st_mtime_ns
        ):
            raise ValueError("policy_changed_during_read")
        raw = b"".join(chunks)
        return raw, {
            "path": path, "sha256": hashlib.sha256(raw).hexdigest(),
            "size": len(raw), "uid": before.st_uid, "gid": before.st_gid,
            "mode": stat.S_IMODE(before.st_mode),
        }
    finally:
        os.close(descriptor)


def sudo_policy_inventory(
    root_path: str = "/etc/sudoers", *, required_uid: int = 0,
    required_gid: int = 0,
):
    """Return the canonical active include graph and its independent root."""
    pending = [root_path]
    visited = set()
    rows = []
    directive = re.compile(
        r"^[ \t]*(?:#include|@include)[ \t]+([^ \t#]+)[ \t]*$"
    )
    directory_directive = re.compile(
        r"^[ \t]*(?:#includedir|@includedir)[ \t]+([^ \t#]+)[ \t]*$"
    )
    include_prefix = re.compile(
        r"^[ \t]*(?:#include|#includedir|@include|@includedir)\b"
    )
    while pending:
        path = pending.pop(0)
        if path in visited:
            raise ValueError("sudo_policy_include_cycle")
        visited.add(path)
        raw, row = _policy_file(
            path, required_uid=required_uid, required_gid=required_gid,
        )
        rows.append(row)
        try:
            text = raw.decode("utf-8")
        except UnicodeDecodeError as exc:
            raise ValueError("sudo_policy_non_utf8") from exc
        for line in text.splitlines():
            match = directive.fullmatch(line)
            directory_match = directory_directive.fullmatch(line)
            if match:
                target = match.group(1)
                if not target.startswith("/"):
                    raise ValueError("relative_sudo_include")
                pending.append(target)
            elif directory_match:
                target = directory_match.group(1)
                directory_fd = _open_absolute(target, directory=True)
                try:
                    directory_info = os.fstat(directory_fd)
                    if (
                        directory_info.st_uid != required_uid
                        or directory_info.st_gid != required_gid
                        or stat.S_IMODE(directory_info.st_mode) & 0o022
                    ):
                        raise ValueError("unsafe_policy_include_directory")
                    names = sorted(os.listdir(directory_fd))
                    for name in names:
                        if name in {".", ".."} or "/" in name:
                            raise ValueError("unsafe_policy_include_name")
                        info = os.stat(
                            name, dir_fd=directory_fd, follow_symlinks=False,
                        )
                        if not stat.S_ISREG(info.st_mode):
                            raise ValueError("non_regular_policy_include")
                        pending.append(target.rstrip("/") + "/" + name)
                finally:
                    os.close(directory_fd)
            elif include_prefix.match(line):
                raise ValueError("unknown_sudo_include_syntax")
    rows.sort(key=lambda item: item["path"])
    document = {
        "schema": "footbreak-sudo-policy-inventory-v1",
        "files": rows,
    }
    raw = (
        json.dumps(document, sort_keys=True, separators=(",", ":")) + "\n"
    ).encode("utf-8")
    return document, hashlib.sha256(raw).hexdigest()


def preflight(
    policy_root: str, wrapper_sha256: str, runner_user: str, *,
    policy_path: str = "/etc/sudoers",
    wrapper_path: str = INSTALLED_WRAPPER,
    required_uid: int = 0,
    required_gid: int = 0,
):
    if os.geteuid() != 0:
        raise PermissionError("root_required")
    if (
        POLICY_ROOT.fullmatch(policy_root) is None
        or HEX64.fullmatch(wrapper_sha256) is None
        or not re.fullmatch(r"[a-z_][a-z0-9_-]{0,31}", runner_user)
    ):
        raise ValueError("invalid_preflight_argument")
    sudo_user = os.environ.get("SUDO_USER")
    sudo_uid = os.environ.get("SUDO_UID")
    if (
        sudo_user != runner_user or sudo_uid is None
        or pwd.getpwnam(runner_user).pw_uid != int(sudo_uid)
        or int(sudo_uid) == 0
    ):
        raise ValueError("runner_identity_mismatch")
    wrapper_raw, wrapper_row = _policy_file(
        wrapper_path, required_uid=required_uid, required_gid=required_gid,
    )
    if (
        wrapper_path != INSTALLED_WRAPPER
        and policy_path == "/etc/sudoers"
    ):
        raise ValueError("fixed_wrapper_path_required")
    actual_wrapper_hash = hashlib.sha256(wrapper_raw).hexdigest()
    if (
        actual_wrapper_hash != wrapper_sha256
        or wrapper_row["mode"] != 0o555
    ):
        raise ValueError("wrapper_identity_mismatch")
    inventory, actual_root = sudo_policy_inventory(
        policy_path, required_uid=required_uid, required_gid=required_gid,
    )
    if actual_root != policy_root:
        raise ValueError("sudo_policy_root_mismatch")
    receipt = {
        "schema": PREFLIGHT_SCHEMA,
        "policy_root": actual_root,
        "wrapper_sha256": actual_wrapper_hash,
        "runner_user": runner_user,
        "policy_file_count": len(inventory["files"]),
    }
    receipt["receipt_sha256"] = hashlib.sha256(
        json.dumps(
            receipt, sort_keys=True, separators=(",", ":"),
        ).encode("utf-8")
    ).hexdigest()
    return receipt


def _parse_expected(raw: bytes):
    value = _strict_json(raw)
    if (
        type(value) is not dict
        or set(value) != {"schema", "artifacts"}
        or value["schema"] != "footbreak-audit-bundle-expected-v1"
        or type(value["artifacts"]) is not list
        or len(value["artifacts"]) != 7
    ):
        raise ValueError("expected_manifest_shape")
    rows = value["artifacts"]
    for row in rows:
        if (
            type(row) is not dict or set(row) != ENTRY_KEYS
            or type(row["filename"]) is not str
            or type(row["sha256"]) is not str
            or HEX64.fullmatch(row["sha256"]) is None
            or type(row["size"]) is not int or row["size"] < 1
        ):
            raise ValueError("expected_manifest_entry")
    if tuple(row["filename"] for row in rows) != NAMES:
        raise ValueError("expected_manifest_names")
    return rows


def _parent(parent: str) -> int:
    if parent != "/var/tmp":
        raise ValueError("fixed_parent_required")
    descriptor = os.open(
        parent, os.O_RDONLY | os.O_CLOEXEC | os.O_DIRECTORY | os.O_NOFOLLOW,
    )
    info = os.fstat(descriptor)
    if info.st_uid != 0 or info.st_gid != 0 or stat.S_IMODE(info.st_mode) != 0o1777:
        os.close(descriptor)
        raise ValueError("unsafe_parent")
    return descriptor


def _safe_remove(path: str, expected_identity=None) -> None:
    try:
        info = os.stat(path, follow_symlinks=False)
    except FileNotFoundError:
        return
    if (
        not stat.S_ISDIR(info.st_mode) or info.st_uid != 0 or info.st_gid != 0
        or stat.S_IMODE(info.st_mode) not in {0o555, 0o700}
        or (
            expected_identity is not None
            and (info.st_dev, info.st_ino) != expected_identity
        )
    ):
        raise ValueError("unsafe_cleanup_root")
    descriptor = os.open(
        path, os.O_RDONLY | os.O_CLOEXEC | os.O_DIRECTORY | os.O_NOFOLLOW,
    )
    try:
        os.fchmod(descriptor, 0o700)
        for name in os.listdir(descriptor):
            entry = os.stat(name, dir_fd=descriptor, follow_symlinks=False)
            if (
                not stat.S_ISREG(entry.st_mode) or entry.st_uid != 0
                or entry.st_gid != 0 or entry.st_nlink != 1
            ):
                raise ValueError("unsafe_cleanup_entry")
            os.unlink(name, dir_fd=descriptor)
    finally:
        os.close(descriptor)
    os.rmdir(path)


def seal_bundle(
    expected_path: str, expected_hash: str, workspace: str, run_key: str,
    *, parent: str = "/var/tmp", fault_after_rename=None,
):
    if os.geteuid() != 0:
        raise PermissionError("root_required")
    if RUN_KEY.fullmatch(run_key) is None or HEX64.fullmatch(expected_hash) is None:
        raise ValueError("invalid_argument")
    parent_fd = _parent(parent)
    final = os.path.join(parent, "footbreak-audit-bundle-" + run_key)
    staging = None
    staging_identity = None
    renamed = False

    def cleanup():
        # Clean both predeclared identities.  This closes the signal window
        # between rename(2) returning and Python recording ``renamed``.
        if staging is not None:
            _safe_remove(staging, staging_identity)
        if staging_identity is not None:
            _safe_remove(final, staging_identity)
        os.fsync(parent_fd)

    previous = {}

    def interrupted(signum, _frame):
        try:
            cleanup()
        finally:
            os._exit(128 + signum)

    try:
        for signum in (signal.SIGHUP, signal.SIGINT, signal.SIGTERM):
            previous[signum] = signal.signal(signum, interrupted)
        if os.path.lexists(final):
            raise ValueError("final_exists")
        expected_raw = _read_single(expected_path)
        if hashlib.sha256(expected_raw).hexdigest() != expected_hash:
            raise ValueError("expected_manifest_hash")
        rows = _parse_expected(expected_raw)
        staging = tempfile.mkdtemp(
            prefix=".footbreak-audit-bundle." + run_key + ".", dir=parent,
        )
        os.chown(staging, 0, 0)
        staging_info = os.stat(staging, follow_symlinks=False)
        staging_identity = (staging_info.st_dev, staging_info.st_ino)
        produced = []
        for row in rows:
            raw = _read_single(os.path.join(workspace, row["filename"]))
            if (
                len(raw) != row["size"]
                or hashlib.sha256(raw).hexdigest() != row["sha256"]
            ):
                raise ValueError("source_changed")
            destination = os.path.join(staging, row["filename"])
            out = os.open(
                destination,
                os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_CLOEXEC
                | os.O_NOFOLLOW,
                0o444,
            )
            try:
                view = memoryview(raw)
                while view:
                    written = os.write(out, view)
                    view = view[written:]
                os.fsync(out)
                os.fchmod(out, 0o444)
                info = os.fstat(out)
            finally:
                os.close(out)
            produced.append({
                "filename": row["filename"], "sha256": row["sha256"],
                "size": row["size"], "mode": 0o444,
                "uid": info.st_uid, "gid": info.st_gid,
            })
        bundle = {
            "schema": "footbreak-root-audit-bundle-v1",
            "artifacts": produced,
        }
        bundle_raw = (
            json.dumps(bundle, sort_keys=True, separators=(",", ":")) + "\n"
        ).encode("utf-8")
        manifest_fd = os.open(
            os.path.join(staging, "bundle-manifest.json"),
            os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_CLOEXEC | os.O_NOFOLLOW,
            0o444,
        )
        try:
            os.write(manifest_fd, bundle_raw)
            os.fsync(manifest_fd)
            os.fchmod(manifest_fd, 0o444)
        finally:
            os.close(manifest_fd)
        staging_fd = os.open(
            staging,
            os.O_RDONLY | os.O_CLOEXEC | os.O_DIRECTORY | os.O_NOFOLLOW,
        )
        try:
            os.fchmod(staging_fd, 0o555)
            os.fsync(staging_fd)
        finally:
            os.close(staging_fd)
        os.rename(staging, final)
        renamed = True
        if fault_after_rename is not None:
            fault_after_rename()
        os.fsync(parent_fd)
        receipt = {
            "path": final,
            "manifest_sha256": hashlib.sha256(bundle_raw).hexdigest(),
        }
        print(json.dumps(receipt, sort_keys=True, separators=(",", ":")))
        return receipt
    except BaseException:
        cleanup()
        raise
    finally:
        os.close(parent_fd)
        for signum, handler in previous.items():
            signal.signal(signum, handler)


def cleanup_bundle(run_key: str, *, parent: str = "/var/tmp") -> None:
    if os.geteuid() != 0:
        raise PermissionError("root_required")
    if RUN_KEY.fullmatch(run_key) is None:
        raise ValueError("invalid_run_key")
    parent_fd = _parent(parent)
    try:
        _safe_remove(os.path.join(parent, "footbreak-audit-bundle-" + run_key))
        os.fsync(parent_fd)
    finally:
        os.close(parent_fd)


def main() -> int:
    parser = argparse.ArgumentParser()
    commands = parser.add_subparsers(dest="command", required=True)
    preflight_parser = commands.add_parser("preflight")
    preflight_parser.add_argument("--policy-root", required=True)
    preflight_parser.add_argument("--wrapper-sha256", required=True)
    preflight_parser.add_argument("--runner-user", required=True)
    seal = commands.add_parser("seal")
    seal.add_argument("--expected", required=True)
    seal.add_argument("--expected-sha256", required=True)
    seal.add_argument("--workspace", required=True)
    seal.add_argument("--run-key", required=True)
    seal.add_argument("--policy-root", required=True)
    seal.add_argument("--wrapper-sha256", required=True)
    seal.add_argument("--runner-user", required=True)
    cleanup = commands.add_parser("cleanup")
    cleanup.add_argument("--run-key", required=True)
    args = parser.parse_args()
    if os.geteuid() != 0:
        raise PermissionError("root_required")
    if args.command == "preflight":
        descriptor = _parent("/var/tmp")
        os.close(descriptor)
        receipt = preflight(
            args.policy_root, args.wrapper_sha256, args.runner_user,
        )
        print(json.dumps(receipt, sort_keys=True, separators=(",", ":")))
    elif args.command == "seal":
        preflight(
            args.policy_root, args.wrapper_sha256, args.runner_user,
        )
        seal_bundle(
            args.expected, args.expected_sha256, args.workspace, args.run_key,
        )
    else:
        cleanup_bundle(args.run_key)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
