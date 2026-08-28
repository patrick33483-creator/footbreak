"""Strictly read-only live discovery for the legacy-batch migration."""
from __future__ import annotations

import argparse
import base64
import copy
import fcntl
import hashlib
import json
import os
import stat
import tempfile
import subprocess
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .legacy_batch_aggregate import (
    build_live_discovery, canonical_hash_v1, parse_json_bytes_v1,
    REQUIRED_RUNTIME_MODULES, runtime_identity_from_checkout,
    read_root_owned_json_config, serialize_ledger_bytes_v1,
    validate_sanitized_calculation,
)

CAPTURE_ENVELOPE_SCHEMA = "footbreak-legacy-batch-private-capture-v1"
CAPTURE_ENVELOPE_KEYS = {
    "schema",
    "discovery_base64", "discovery_length", "discovery_sha256",
    "footbreak_ledger_base64", "footbreak_ledger_length",
    "footbreak_ledger_sha256",
}
MAX_LEDGER_CAPTURE_BYTES = 128 * 1024 * 1024
MAX_DISCOVERY_CAPTURE_BYTES = 16 * 1024 * 1024
MAX_CAPTURE_ENVELOPE_BYTES = (
    4 * ((MAX_LEDGER_CAPTURE_BYTES + 2) // 3)
    + 4 * ((MAX_DISCOVERY_CAPTURE_BYTES + 2) // 3)
    + 4096
)


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _file_identity(path: Path, descriptor: int, *, regular: bool = True) -> dict[str, Any]:
    current = os.fstat(descriptor)
    by_path = os.stat(path, follow_symlinks=False)
    if (
        (regular and not stat.S_ISREG(current.st_mode))
        or (current.st_dev, current.st_ino) != (by_path.st_dev, by_path.st_ino)
        or current.st_nlink != 1
        or current.st_mode & 0o022
    ):
        raise ValueError("unsafe_or_replaced_file_object")
    return {
        "realpath": str(path.resolve()),
        "st_dev": current.st_dev, "st_ino": current.st_ino,
        "st_uid": current.st_uid, "st_gid": current.st_gid,
        "st_mode": stat.S_IMODE(current.st_mode), "st_nlink": current.st_nlink,
    }


def _read_fd(descriptor: int) -> bytes:
    os.lseek(descriptor, 0, os.SEEK_SET)
    chunks = []
    while True:
        chunk = os.read(descriptor, 1024 * 1024)
        if not chunk:
            return b"".join(chunks)
        chunks.append(chunk)


def build_private_capture_envelope(
    discovery: dict[str, Any], raw_ledger_bytes: bytes,
) -> dict[str, Any]:
    """Bind sanitized discovery and the exact private audit input bytes."""
    discovery_bytes = serialize_ledger_bytes_v1(discovery)
    if len(raw_ledger_bytes) > MAX_LEDGER_CAPTURE_BYTES:
        raise ValueError("ledger_exceeds_capture_bound")
    if len(discovery_bytes) > MAX_DISCOVERY_CAPTURE_BYTES:
        raise ValueError("discovery_exceeds_capture_bound")
    ledger_digest = hashlib.sha256(raw_ledger_bytes).hexdigest()
    if discovery["capture"]["full_pre_ledger_sha256"] != ledger_digest:
        raise ValueError("capture_envelope_ledger_binding_mismatch")
    return {
        "schema": CAPTURE_ENVELOPE_SCHEMA,
        "discovery_base64": base64.b64encode(discovery_bytes).decode("ascii"),
        "discovery_length": len(discovery_bytes),
        "discovery_sha256": hashlib.sha256(discovery_bytes).hexdigest(),
        "footbreak_ledger_base64": base64.b64encode(raw_ledger_bytes).decode("ascii"),
        "footbreak_ledger_length": len(raw_ledger_bytes),
        "footbreak_ledger_sha256": ledger_digest,
    }


def _atomic_private_write(path: Path, data: bytes) -> None:
    """Create or replace one private single-link regular output."""
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    descriptor, temporary_name = tempfile.mkstemp(
        prefix=path.name + ".", suffix=".tmp", dir=path.parent,
    )
    temporary = Path(temporary_name)
    try:
        os.fchmod(descriptor, 0o600)
        with os.fdopen(descriptor, "wb") as handle:
            handle.write(data)
            handle.flush()
            os.fsync(handle.fileno())
        if path.exists() or path.is_symlink():
            opened = os.lstat(path)
            if (
                not stat.S_ISREG(opened.st_mode)
                or opened.st_nlink != 1
                or opened.st_mode & 0o022
            ):
                raise ValueError("unsafe_existing_private_output")
        os.replace(temporary, path)
        final_fd = os.open(
            path, os.O_RDONLY | os.O_CLOEXEC | os.O_NOFOLLOW,
        )
        try:
            info = os.fstat(final_fd)
            by_path = os.stat(path, follow_symlinks=False)
            if (
                not stat.S_ISREG(info.st_mode)
                or info.st_nlink != 1
                or stat.S_IMODE(info.st_mode) != 0o600
                or (info.st_dev, info.st_ino) != (by_path.st_dev, by_path.st_ino)
                or _read_fd(final_fd) != data
            ):
                raise ValueError("private_output_verification_failed")
        finally:
            os.close(final_fd)
        directory_fd = os.open(path.parent, os.O_RDONLY | os.O_DIRECTORY)
        try:
            os.fsync(directory_fd)
        finally:
            os.close(directory_fd)
    finally:
        if temporary.exists():
            temporary.unlink()


def _verify_writer_inventory(
    config: dict[str, Any], ledger_identity: dict[str, Any],
    state_probe: Any = None,
) -> list[dict[str, Any]]:
    inventory = config.get("writer_inventory")
    required_names = {"tick", "sweep", "settle", "t30", "result_reconciliation"}
    if (
        not isinstance(inventory, list)
        or {row.get("name") for row in inventory if isinstance(row, dict)}
        != required_names
    ):
        raise ValueError("writer_inventory_incomplete")
    for row in inventory:
        if (
            set(row) != {
                "name", "unit_name", "state", "unit_sha256", "config_sha256",
            }
            or row["state"] not in {"inactive", "stopped", "masked"}
            or not all(
                isinstance(row[key], str) and len(row[key]) == 64
                and all(char in "0123456789abcdef" for char in row[key])
                for key in ("unit_sha256", "config_sha256")
            )
        ):
            raise ValueError("writer_inventory_not_quiesced_or_unattested")
        probe = state_probe or (
            lambda unit: subprocess.check_output(
                [
                    "systemctl", "show", unit,
                    "--property=ActiveState", "--value",
                ],
                text=True, stderr=subprocess.DEVNULL,
            ).strip()
        )
        try:
            actual_state = probe(row["unit_name"])
        except Exception as exc:
            raise ValueError("writer_service_state_unavailable") from exc
        if actual_state not in {"inactive", "failed"}:
            raise ValueError("writer_service_is_active")
    # Linux deployment check: no process other than this read-only exporter may
    # hold the authoritative ledger open with a writable access mode.
    proc = Path("/proc")
    if proc.exists():
        for fdinfo in proc.glob("[0-9]*/fdinfo/*"):
            try:
                info = fdinfo.read_text()
                flags_line = next(
                    line for line in info.splitlines() if line.startswith("flags:")
                )
                flags = int(flags_line.split()[1], 8)
                target = fdinfo.parent.parent / "fd" / fdinfo.name
                opened = target.stat()
            except (OSError, StopIteration, ValueError):
                continue
            if (
                (opened.st_dev, opened.st_ino)
                == (ledger_identity["st_dev"], ledger_identity["st_ino"])
                and flags & os.O_ACCMODE in {os.O_WRONLY, os.O_RDWR}
            ):
                raise ValueError("unlisted_process_has_ledger_open_writable")
    return copy.deepcopy(inventory)


def export_live_authority(
    ledger_path: Path, runtime_config_path: Path, calculation_path: Path,
    output_path: Path, *, require_quiesced: bool,
    capture_envelope_path: Path | None = None,
) -> dict[str, Any]:
    config, _config_bytes, config_identity = read_root_owned_json_config(
        runtime_config_path
    )
    if set(config) != {
        "ledger_path", "canonical_lock_path", "canonical_lock_identity",
        "all_writers_quiesced", "repository_root", "writer_inventory",
        "service_configuration", "authority_path",
    }:
        raise ValueError("runtime_config_schema_invalid")
    configured_ledger = Path(str(config.get("ledger_path") or ""))
    lock_path = Path(str(config.get("canonical_lock_path") or ""))
    if configured_ledger.resolve() != ledger_path.resolve():
        raise ValueError("ledger_path_not_runtime_configured")
    if config.get("all_writers_quiesced") is not True:
        raise ValueError("all_writers_not_quiesced")
    if not lock_path.is_absolute() or not ledger_path.is_absolute():
        raise ValueError("runtime_paths_must_be_absolute")
    output_path.parent.mkdir(parents=True, exist_ok=True)
    if (
        output_path.resolve() == ledger_path.resolve()
        or output_path.exists() and os.path.samefile(output_path, ledger_path)
    ):
        raise ValueError("output_aliases_ledger")
    calculation = validate_sanitized_calculation(
        parse_json_bytes_v1(calculation_path.read_bytes())
    )
    lock_fd = os.open(
        lock_path,
        os.O_RDWR | os.O_CREAT | os.O_CLOEXEC | os.O_NOFOLLOW,
        0o600,
    )
    try:
        try:
            fcntl.flock(lock_fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            raise ValueError("canonical_writer_lock_busy") from exc
        lock_identity = _file_identity(lock_path, lock_fd)
        expected_lock = config.get("canonical_lock_identity")
        if isinstance(expected_lock, dict) and any(
            lock_identity.get(key) != expected_lock.get(key)
            for key in ("st_dev", "st_ino")
        ):
            raise ValueError("canonical_lock_identity_mismatch")
        ledger_fd = os.open(
            ledger_path, os.O_RDONLY | os.O_CLOEXEC | os.O_NOFOLLOW,
        )
        try:
            ledger_identity = _file_identity(ledger_path, ledger_fd)
            writer_inventory = _verify_writer_inventory(
                config, ledger_identity,
            )
            started = _now()
            raw = _read_fd(ledger_fd)
            if len(raw) > MAX_LEDGER_CAPTURE_BYTES:
                raise ValueError("ledger_exceeds_capture_bound")
            ledger = parse_json_bytes_v1(raw)
            repository_root = Path(str(config.get("repository_root") or "")).resolve()
            if not repository_root.is_absolute():
                raise ValueError("runtime_identity_configuration_invalid")
            execution = runtime_identity_from_checkout(
                repository_root, sorted(REQUIRED_RUNTIME_MODULES.values()),
            )
            if execution["working_tree_policy"] != "clean_tracked_no_shadow_files":
                raise ValueError("runtime_tree_not_clean")
            discovery = build_live_discovery(
                ledger, calculation, raw_ledger_bytes=raw,
                capture={
                    "started_at": started,
                    "ledger_object": ledger_identity,
                },
                execution_identity=execution,
                writer_coordination={
                    "all_writers_quiesced": config.get("all_writers_quiesced"),
                    "canonical_lock": lock_identity,
                    "writer_inventory_root": canonical_hash_v1(writer_inventory),
                    "writer_count": len(writer_inventory)
                    if isinstance(writer_inventory, list) else 0,
                    "service_configuration_sha256": canonical_hash_v1(
                        config.get("service_configuration")
                    ),
                    "runtime_config": config_identity,
                },
            )
            _file_identity(ledger_path, ledger_fd)
            final_raw = _read_fd(ledger_fd)
            if final_raw != raw:
                raise ValueError("ledger_changed_during_discovery")
            discovery["capture"]["ended_at"] = _now()
            # The timestamp is part of the canonical discovery document.
            discovery.pop("discovery_document_sha256")
            discovery["discovery_document_sha256"] = canonical_hash_v1(discovery)
            envelope = (
                build_private_capture_envelope(discovery, raw)
                if capture_envelope_path is not None else None
            )
            _file_identity(lock_path, lock_fd)
            if ledger_path.read_bytes() != raw:
                raise ValueError("ledger_changed_before_unlock")
            _file_identity(ledger_path, ledger_fd)
        finally:
            os.close(ledger_fd)
        _atomic_private_write(output_path, serialize_ledger_bytes_v1(discovery))
        if capture_envelope_path is not None:
            if (
                capture_envelope_path.resolve() == ledger_path.resolve()
                or capture_envelope_path.resolve() == output_path.resolve()
                or capture_envelope_path.exists()
                and (
                    os.path.samefile(capture_envelope_path, ledger_path)
                    or os.path.samefile(capture_envelope_path, output_path)
                )
            ):
                raise ValueError("capture_envelope_output_alias")
            assert envelope is not None
            _atomic_private_write(
                capture_envelope_path, serialize_ledger_bytes_v1(envelope),
            )
        return discovery
    finally:
        try:
            fcntl.flock(lock_fd, fcntl.LOCK_UN)
        finally:
            os.close(lock_fd)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--ledger", required=True, type=Path)
    parser.add_argument("--runtime-config", required=True, type=Path)
    parser.add_argument("--calculation", required=True, type=Path)
    parser.add_argument("--output", required=True, type=Path)
    parser.add_argument("--capture-envelope", type=Path)
    parser.add_argument("--require-quiesced", action="store_true")
    args = parser.parse_args()
    try:
        discovery = export_live_authority(
            args.ledger, args.runtime_config, args.calculation, args.output,
            require_quiesced=args.require_quiesced,
            capture_envelope_path=args.capture_envelope,
        )
        print(json.dumps({
            "valid": True,
            "discovery_document_sha256": discovery["discovery_document_sha256"],
            "migration_ready": discovery["migration_ready"],
        }, sort_keys=True))
        return 0
    except Exception as exc:
        print(json.dumps({"valid": False, "error": str(exc)}, sort_keys=True))
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
