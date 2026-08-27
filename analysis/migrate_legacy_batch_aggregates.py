"""Offline, externally-authorized legacy ordinary-batch migration."""
from __future__ import annotations

import argparse
import copy
import fcntl
import hashlib
import json
import os
import stat
import tempfile
from pathlib import Path
from typing import Any, Callable

from .legacy_batch_aggregate import (
    load_legacy_batch_authority, parse_json_bytes_v1,
    plan_legacy_batch_migration, prove_legacy_batch_poststate,
    REQUIRED_RUNTIME_MODULES, runtime_identity_from_checkout,
    read_root_owned_json_config, serialize_ledger_bytes_v1,
)


CONFIRMATION = "APPLY_FOOTBREAK_LEGACY_BATCH_AGGREGATES"


class MigrationRollback(Exception):
    pass


def _identity(path: Path, descriptor: int) -> tuple[int, int, int, int]:
    opened = os.fstat(descriptor)
    current = os.stat(path, follow_symlinks=False)
    if (
        not stat.S_ISREG(opened.st_mode)
        or (opened.st_dev, opened.st_ino) != (current.st_dev, current.st_ino)
        or opened.st_nlink != 1 or opened.st_mode & 0o022
    ):
        raise ValueError("unsafe_or_replaced_file_object")
    return opened.st_dev, opened.st_ino, opened.st_uid, stat.S_IMODE(opened.st_mode)


def _identity_document(path: Path, descriptor: int) -> dict[str, Any]:
    _identity(path, descriptor)
    opened = os.fstat(descriptor)
    return {
        "realpath": str(path.resolve()), "st_dev": opened.st_dev,
        "st_ino": opened.st_ino, "st_uid": opened.st_uid,
        "st_gid": opened.st_gid, "st_mode": stat.S_IMODE(opened.st_mode),
        "st_nlink": opened.st_nlink,
    }


def _fsync_directory(path: Path) -> None:
    descriptor = os.open(path, os.O_RDONLY | os.O_CLOEXEC)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def _read_regular_nofollow(path: Path) -> bytes:
    descriptor = os.open(path, os.O_RDONLY | os.O_CLOEXEC | os.O_NOFOLLOW)
    try:
        _identity(path, descriptor)
        size = os.fstat(descriptor).st_size
        chunks = []
        while sum(map(len, chunks)) <= size:
            chunk = os.read(descriptor, min(1024 * 1024, size + 1))
            if not chunk:
                break
            chunks.append(chunk)
        return b"".join(chunks)
    finally:
        os.close(descriptor)


def _exclusive_file(path: Path, data: bytes, mode: int, uid: int, gid: int) -> None:
    descriptor = os.open(
        path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_CLOEXEC | os.O_NOFOLLOW,
        mode,
    )
    try:
        os.fchmod(descriptor, mode)
        try:
            os.fchown(descriptor, uid, gid)
        except PermissionError:
            if os.geteuid() == 0:
                raise
        view = memoryview(data)
        while view:
            written = os.write(descriptor, view)
            view = view[written:]
        os.fsync(descriptor)
    finally:
        os.close(descriptor)
    if _read_regular_nofollow(path) != data:
        raise OSError("durable_file_readback_mismatch")


def _replace_bytes(
    target: Path, data: bytes, *, mode: int, uid: int, gid: int,
    prefix: str,
) -> None:
    descriptor, temporary_name = tempfile.mkstemp(
        prefix=prefix, dir=target.parent,
    )
    temporary = Path(temporary_name)
    try:
        os.fchmod(descriptor, mode)
        try:
            os.fchown(descriptor, uid, gid)
        except PermissionError:
            if os.geteuid() == 0:
                raise
        with os.fdopen(descriptor, "wb") as handle:
            handle.write(data)
            handle.flush()
            os.fsync(handle.fileno())
        if _read_regular_nofollow(temporary) != data:
            raise OSError("replacement_temp_readback_mismatch")
        os.replace(temporary, target)
        _fsync_directory(target.parent)
    finally:
        if temporary.exists():
            temporary.unlink()


def _durable_report(path: Path, report: dict[str, Any]) -> None:
    data = serialize_ledger_bytes_v1(report)
    descriptor, name = tempfile.mkstemp(prefix=path.name + ".", dir=path.parent)
    temporary = Path(name)
    try:
        with os.fdopen(descriptor, "wb") as handle:
            handle.write(data)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
        _fsync_directory(path.parent)
        if _read_regular_nofollow(path) != data:
            raise OSError("migration_report_readback_mismatch")
    finally:
        if temporary.exists():
            temporary.unlink()


def migrate(
    ledger_path: Path, authority_path: Path, runtime_config_path: Path, *,
    trusted_manifest_hash: str, apply: bool = False,
    confirmation: str | None = None,
    fault_hook: Callable[[str], None] | None = None,
    runtime_identity_collector: Callable[[Path, list[str]], dict[str, Any]]
    | None = None,
    config_loader: Callable[[Path], tuple[dict[str, Any], bytes, dict[str, Any]]]
    | None = None,
    writer_state_probe: Callable[[str], str] | None = None,
) -> tuple[int, dict[str, Any]]:
    """Plan by default; apply only with the exact explicit confirmation."""
    hook = fault_hook or (lambda _point: None)
    loader = config_loader or read_root_owned_json_config
    config, _config_bytes, config_identity = loader(runtime_config_path)
    if set(config) != {
        "ledger_path", "canonical_lock_path", "canonical_lock_identity",
        "all_writers_quiesced", "repository_root", "writer_inventory",
        "service_configuration", "authority_path",
    }:
        return 2, {"committed": False, "error": "runtime_config_schema_invalid"}
    if Path(config["authority_path"]).resolve() != authority_path.resolve():
        return 2, {"committed": False, "error": "authority_path_not_runtime_configured"}
    if Path(str(config.get("ledger_path") or "")).resolve() != ledger_path.resolve():
        raise ValueError("ledger_path_not_runtime_configured")
    if config.get("all_writers_quiesced") is not True:
        return 3, {"committed": False, "error": "all_writers_not_quiesced"}
    lock_path = Path(str(config.get("canonical_lock_path") or ""))
    if not lock_path.is_absolute():
        return 3, {"committed": False, "error": "canonical_lock_not_configured"}
    if apply and confirmation != CONFIRMATION:
        return 2, {"committed": False, "error": "exact_apply_confirmation_required"}
    untrusted_authority = parse_json_bytes_v1(authority_path.read_bytes())
    module_paths = sorted(REQUIRED_RUNTIME_MODULES.values())
    repository_root = Path(str(config.get("repository_root") or "")).resolve()
    collector = runtime_identity_collector or runtime_identity_from_checkout
    actual_runtime = collector(repository_root, module_paths)
    if config.get("trusted_approver_public_keys") is not None:
        actual_runtime["trusted_approver_public_keys"] = copy.deepcopy(
            config["trusted_approver_public_keys"]
        )
    authority = load_legacy_batch_authority(
        authority_path, trusted_manifest_hash,
        config.get("detached_signature"),
        actual_runtime,
    )
    if authority.authority["runtime_coordination"]["runtime_config"] != config_identity:
        return 2, {"committed": False, "error": "authority_runtime_config_mismatch"}
    suffix = authority.manifest_hash[:16]
    backup = ledger_path.with_name(f"{ledger_path.name}.{suffix}.legacy-batch.backup")
    report_path = ledger_path.with_name(
        f"{ledger_path.name}.{suffix}.legacy-batch-report.json"
    )
    sentinel = runtime_config_path.with_name(
        f"legacy-batch-{suffix}.EMERGENCY"
    )
    if sentinel.exists():
        return 5, {"committed_unknown": True, "error": "emergency_sentinel_present"}
    lock_fd = os.open(
        lock_path,
        os.O_RDWR | os.O_CREAT | os.O_CLOEXEC | os.O_NOFOLLOW,
        0o600,
    )
    try:
        try:
            fcntl.flock(lock_fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            return 3, {"committed": False, "error": "canonical_writer_lock_busy"}
        lock_object = _identity(lock_path, lock_fd)
        lock_document = _identity_document(lock_path, lock_fd)
        expected_lock = config.get("canonical_lock_identity")
        if isinstance(expected_lock, dict) and (
            lock_object[0], lock_object[1]
        ) != (expected_lock.get("st_dev"), expected_lock.get("st_ino")):
            return 3, {"committed": False, "error": "canonical_lock_identity_mismatch"}
        authority_lock = authority.authority.get(
            "runtime_coordination", {}
        ).get("canonical_lock")
        if authority_lock != lock_document:
            return 3, {"committed": False, "error": "authority_lock_identity_mismatch"}
        descriptor = os.open(
            ledger_path, os.O_RDONLY | os.O_CLOEXEC | os.O_NOFOLLOW,
        )
        try:
            pre_object = _identity(ledger_path, descriptor)
            pre_document = _identity_document(ledger_path, descriptor)
            from .export_legacy_batch_live_authority import (
                _verify_writer_inventory,
            )
            inventory = _verify_writer_inventory(
                config, pre_document, writer_state_probe,
            )
            approved_coordination = authority.authority[
                "runtime_coordination"
            ]
            from .legacy_batch_aggregate import canonical_hash_v1
            if (
                canonical_hash_v1(inventory)
                != approved_coordination["writer_inventory_root"]
                or len(inventory) != approved_coordination["writer_count"]
                or canonical_hash_v1(config.get("service_configuration"))
                != approved_coordination["service_configuration_sha256"]
            ):
                raise ValueError("authority_writer_inventory_mismatch")
            os.lseek(descriptor, 0, os.SEEK_SET)
            chunks = []
            while True:
                chunk = os.read(descriptor, 1024 * 1024)
                if not chunk:
                    break
                chunks.append(chunk)
            raw = b"".join(chunks)
            if os.fstat(descriptor).st_size != len(raw):
                raise ValueError("ledger_changed_while_loading")
            authority_ledger = authority.authority["live_prestate"]["ledger_object"]
            if (
                hashlib.sha256(raw).hexdigest()
                == authority.authority["live_prestate"]["full_ledger_sha256"]
                and pre_document != authority_ledger
            ):
                raise ValueError("authority_ledger_object_identity_mismatch")
            ledger = parse_json_bytes_v1(raw)
        finally:
            os.close(descriptor)
        if report_path.exists():
            report_fd = os.open(
                report_path, os.O_RDONLY | os.O_CLOEXEC | os.O_NOFOLLOW,
            )
            try:
                _identity(report_path, report_fd)
                os.lseek(report_fd, 0, os.SEEK_SET)
                prior_report = parse_json_bytes_v1(
                    os.read(report_fd, os.fstat(report_fd).st_size + 1)
                )
            finally:
                os.close(report_fd)
            raw_hash = hashlib.sha256(raw).hexdigest()
            if (
                not isinstance(prior_report, dict)
                or prior_report.get("authority_manifest_hash")
                != authority.manifest_hash
                or prior_report.get("committed_unknown") is True
                or (
                    prior_report.get("committed") is True
                    and raw_hash
                    != authority.authority["expected_poststate"][
                        "full_ledger_sha256"
                    ]
                )
            ):
                return 5, {
                    "committed_unknown": True,
                    "error": "ambiguous_prior_migration_report",
                }
        plan = plan_legacy_batch_migration(ledger, raw, authority)
        if plan["status"] == "already_applied":
            return 0, {
                "committed": False, "already_applied": True,
                "authority_manifest_hash": authority.manifest_hash,
                "post_ledger_sha256": hashlib.sha256(raw).hexdigest(),
            }
        post_bytes = plan["plan"]["serialized_bytes"]
        summary = {
            "committed": False, "dry_run": not apply, "already_applied": False,
            "authority_manifest_hash": authority.manifest_hash,
            "pre_ledger_sha256": hashlib.sha256(raw).hexdigest(),
            "post_ledger_sha256": hashlib.sha256(post_bytes).hexdigest(),
            "converted_version_count": 10,
            "backup_path": str(backup),
            "report_path": str(report_path),
        }
        if not apply:
            return 0, summary
        if backup.exists():
            backup_fd = os.open(
                backup, os.O_RDONLY | os.O_CLOEXEC | os.O_NOFOLLOW,
            )
            try:
                _identity(backup, backup_fd)
                os.lseek(backup_fd, 0, os.SEEK_SET)
                backup_bytes = os.read(backup_fd, len(raw) + 1)
            finally:
                os.close(backup_fd)
            if backup_bytes != raw:
                return 2, {**summary, "error": "existing_backup_mismatch"}
        else:
            _exclusive_file(
                backup, raw, pre_object[3], pre_object[2], os.stat(ledger_path).st_gid,
            )
            hook("after_backup_fsync")
            _fsync_directory(ledger_path.parent)
        # Construct and fsync the deterministic replacement before the final
        # compare-and-swap check.
        temp_fd, temp_name = tempfile.mkstemp(
            prefix=ledger_path.name + ".legacy-batch.", dir=ledger_path.parent,
        )
        temporary = Path(temp_name)
        replaced = False
        try:
            os.fchmod(temp_fd, pre_object[3])
            try:
                os.fchown(temp_fd, pre_object[2], os.stat(ledger_path).st_gid)
            except PermissionError:
                if os.geteuid() == 0:
                    raise
            with os.fdopen(temp_fd, "wb") as handle:
                handle.write(post_bytes)
                handle.flush()
                os.fsync(handle.fileno())
            hook("after_temp_fsync")
            if _read_regular_nofollow(temporary) != post_bytes:
                raise OSError("post_temp_readback_mismatch")
            check_fd = os.open(
                ledger_path, os.O_RDONLY | os.O_CLOEXEC | os.O_NOFOLLOW,
            )
            try:
                if _identity(ledger_path, check_fd) != pre_object:
                    raise ValueError("ledger_object_changed_before_replace")
            finally:
                os.close(check_fd)
            check_fd = os.open(
                ledger_path, os.O_RDONLY | os.O_CLOEXEC | os.O_NOFOLLOW,
            )
            try:
                os.lseek(check_fd, 0, os.SEEK_SET)
                unchanged = os.read(check_fd, len(raw) + 1)
            finally:
                os.close(check_fd)
            if unchanged != raw:
                raise ValueError("ledger_bytes_changed_before_replace")
            _identity(lock_path, lock_fd)
            hook("before_replace")
            os.replace(temporary, ledger_path)
            replaced = True
            hook("after_replace")
            _fsync_directory(ledger_path.parent)
            hook("after_directory_fsync")
            readback_fd = os.open(
                ledger_path, os.O_RDONLY | os.O_CLOEXEC | os.O_NOFOLLOW,
            )
            try:
                readback_object = _identity(ledger_path, readback_fd)
                if (
                    readback_object[0], readback_object[2], readback_object[3]
                ) != (pre_object[0], pre_object[2], pre_object[3]):
                    raise OSError("post_replace_file_metadata_mismatch")
                os.lseek(readback_fd, 0, os.SEEK_SET)
                readback = os.read(readback_fd, len(post_bytes) + 1)
            finally:
                os.close(readback_fd)
            hook("after_readback")
            post_ledger = parse_json_bytes_v1(readback)
            prove_legacy_batch_poststate(post_ledger, readback, authority)
            _identity(lock_path, lock_fd)
            hook("after_postproof")
            committed_report = {
                **summary, "committed": True, "dry_run": False,
                "backup_sha256": hashlib.sha256(raw).hexdigest(),
                "rolled_back": False,
            }
            _durable_report(report_path, committed_report)
            hook("after_report_fsync")
            return 0, committed_report
        except Exception as failure:
            if not replaced:
                return 2, {**summary, "error": str(failure)}
            try:
                hook("before_rollback_replace")
                _replace_bytes(
                    ledger_path, raw, mode=pre_object[3], uid=pre_object[2],
                    gid=os.stat(backup).st_gid,
                    prefix=ledger_path.name + ".legacy-batch.rollback.",
                )
                hook("after_rollback_fsync")
                if _read_regular_nofollow(ledger_path) != raw:
                    raise OSError("rollback_readback_mismatch")
                # Full preproof is rerun, not merely a hash comparison.
                restored = parse_json_bytes_v1(raw)
                plan_legacy_batch_migration(restored, raw, authority)
                rollback_report = {
                    **summary, "committed": False, "dry_run": False,
                    "rolled_back": True, "error": str(failure),
                }
                _durable_report(report_path, rollback_report)
                return 4, rollback_report
            except Exception as rollback_failure:
                emergency = {
                    **summary, "committed": False, "committed_unknown": True,
                    "rollback_failed": True, "error": str(failure),
                    "rollback_error": str(rollback_failure),
                }
                try:
                    _durable_report(sentinel, emergency)
                except Exception:
                    pass
                return 5, emergency
        finally:
            if temporary.exists():
                temporary.unlink()
    except Exception as exc:
        return 2, {"committed": False, "error": str(exc)}
    finally:
        try:
            fcntl.flock(lock_fd, fcntl.LOCK_UN)
        finally:
            os.close(lock_fd)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--ledger", required=True, type=Path)
    parser.add_argument("--authority", required=True, type=Path)
    parser.add_argument("--runtime-config", required=True, type=Path)
    parser.add_argument("--trusted-authority-hash", required=True)
    parser.add_argument("--apply", action="store_true")
    parser.add_argument("--confirm")
    args = parser.parse_args()
    code, report = migrate(
        args.ledger, args.authority, args.runtime_config,
        trusted_manifest_hash=args.trusted_authority_hash,
        apply=args.apply, confirmation=args.confirm,
    )
    print(json.dumps(report, ensure_ascii=False, sort_keys=True))
    return code


if __name__ == "__main__":
    raise SystemExit(main())
