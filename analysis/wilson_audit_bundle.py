"""Descriptor-bound validation and commitment of offline audit artifacts."""
from __future__ import annotations

import argparse
import hashlib
import os
import stat
from datetime import datetime
from pathlib import Path
from typing import Any

from analysis.legacy_batch_aggregate import (
    parse_json_bytes_v1,
    serialize_ledger_bytes_v1,
    validate_live_discovery,
    validate_sanitized_calculation,
)
from analysis.legacy_batch_runtime import load_production_legacy_batch_authority
from analysis.wilson_audit_gate import (
    EXPECTED_RELEASE,
    HEX64,
    _verify_manifest_hash,
    _verify_release_shape,
    summary_projection,
)
from analysis.wilson_registry_export import export_registry, verify_export
from analysis.wilson_registry_manifest import build_manifest


ARTIFACT_NAMES = (
    "ledger-sha256.txt",
    "wilson-production-audit-summary.json",
    "footbreak-wilson-registry-audit.json",
    "crown-wilson-registry-audit.json",
    "footbreak-wilson-registry-chains.json",
    "crown-wilson-registry-chains.json",
    "footbreak-legacy-batch-live-discovery.json",
)
EXPECTED_SCHEMA = "footbreak-audit-bundle-expected-v1"
EXPECTED_KEYS = {"schema", "artifacts"}
ENTRY_KEYS = {"filename", "sha256", "size"}
SUMMARY_KEYS = {
    "schema", "audited_commit", "captured_at_utc", "capture_outcome",
    "capture_exit_codes", "ledger_sha256", "exit_codes", "systems",
    "production_mutation", "recovery_enabled",
}
Identity = tuple[int, int]


def _read_once(path: Path) -> tuple[bytes, os.stat_result]:
    descriptor = os.open(path, os.O_RDONLY | os.O_CLOEXEC | os.O_NOFOLLOW)
    try:
        before = os.fstat(descriptor)
        by_path = os.stat(path, follow_symlinks=False)
        if (
            not stat.S_ISREG(before.st_mode)
            or before.st_nlink != 1
            or (before.st_dev, before.st_ino) != (by_path.st_dev, by_path.st_ino)
        ):
            raise ValueError(f"unsafe_source:{path.name}")
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
            raise ValueError(f"source_changed_during_read:{path.name}")
        final_path = os.stat(path, follow_symlinks=False)
        if (after.st_dev, after.st_ino) != (
            final_path.st_dev, final_path.st_ino,
        ):
            raise ValueError(f"source_path_changed:{path.name}")
        raw = b"".join(chunks)
        if len(raw) != before.st_size:
            raise ValueError(f"source_size_changed:{path.name}")
        return raw, before
    finally:
        os.close(descriptor)


def _object(raw: bytes, name: str) -> dict[str, Any]:
    value = parse_json_bytes_v1(raw)
    if type(value) is not dict:
        raise ValueError(f"{name}:expected_object")
    return value


def _ledger_hashes(raw: bytes) -> dict[str, str]:
    try:
        lines = raw.decode("ascii").splitlines()
    except UnicodeDecodeError as exc:
        raise ValueError("ledger_hash_non_ascii") from exc
    if len(lines) != 2 or not raw.endswith(b"\n"):
        raise ValueError("ledger_hash_cardinality")
    result: dict[str, str] = {}
    for line in lines:
        parts = line.split("  ")
        if len(parts) != 2:
            raise ValueError("malformed_ledger_hash_line")
        digest, path = parts
        expected_path = f"audit-input/{Path(path).name}"
        if (
            path != expected_path or HEX64.fullmatch(digest) is None
            or Path(path).name in result
        ):
            raise ValueError("invalid_ledger_hash_entry")
        result[Path(path).name] = digest
    if set(result) != {"footbreak-ledger.json", "crown-ledger.json"}:
        raise ValueError("ledger_hash_set")
    return result


def _validate_summary(
    value: dict[str, Any], commit: str, hashes: dict[str, str],
    manifests: dict[str, dict[str, Any]],
) -> None:
    if set(value) != SUMMARY_KEYS:
        raise ValueError("summary_fields")
    try:
        datetime.fromisoformat(value["captured_at_utc"])
    except (KeyError, TypeError, ValueError) as exc:
        raise ValueError("summary_timestamp") from exc
    if (
        value["schema"] != "wilson-production-offline-audit-v1"
        or value["audited_commit"] != commit
        or value["capture_outcome"] != "success"
        or value["capture_exit_codes"] != {"footbreak": 0, "crown": 0}
        or value["exit_codes"] != {"footbreak": 0, "crown": 0}
        or value["ledger_sha256"] != hashes
        or value["systems"] != {
            system: summary_projection(manifests[system])
            for system in EXPECTED_RELEASE
        }
        or value["production_mutation"] is not False
        or value["recovery_enabled"] is not False
    ):
        raise ValueError("summary_binding")


def validate_and_commit(
    base: Path, calculation_path: Path, output: Path, *, audited_commit: str,
) -> tuple[str, Identity]:
    """Validate exact source bytes and exclusively commit seven rows."""
    source_raw = {name: _read_once(base / name)[0] for name in ARTIFACT_NAMES}
    ledger_raw = {
        system: _read_once(base / "audit-input" / f"{system}-ledger.json")[0]
        for system in EXPECTED_RELEASE
    }
    calculation = validate_sanitized_calculation(
        _object(_read_once(calculation_path)[0], calculation_path.name)
    )
    ledgers = {
        system: _object(raw, f"{system}-ledger.json")
        for system, raw in ledger_raw.items()
    }
    actual_hashes = {
        f"{system}-ledger.json": hashlib.sha256(raw).hexdigest()
        for system, raw in ledger_raw.items()
    }
    declared = _ledger_hashes(source_raw["ledger-sha256.txt"])
    if declared != actual_hashes:
        raise ValueError("captured_ledger_hash_mismatch")

    manifests: dict[str, dict[str, Any]] = {}
    for system in EXPECTED_RELEASE:
        supplied = _object(
            source_raw[f"{system}-wilson-registry-audit.json"],
            f"{system}-manifest",
        )
        rebuilt = build_manifest(
            ledgers[system], system,
            authority_context=load_production_legacy_batch_authority(
                ledgers[system],
            ),
        )
        if supplied != rebuilt:
            raise ValueError(f"{system}:manifest_source_mismatch")
        _verify_manifest_hash(supplied, system)
        _verify_release_shape(supplied, system)
        manifests[system] = supplied
        supplied_export = verify_export(_object(
            source_raw[f"{system}-wilson-registry-chains.json"],
            f"{system}-export",
        ))
        rebuilt_export = export_registry(
            ledgers[system], system,
            source_ledger_sha256=actual_hashes[f"{system}-ledger.json"],
        )
        if supplied_export != rebuilt_export:
            raise ValueError(f"{system}:export_source_mismatch")

    _validate_summary(
        _object(
            source_raw["wilson-production-audit-summary.json"], "summary",
        ),
        audited_commit, actual_hashes, manifests,
    )
    discovery = validate_live_discovery(
        _object(
            source_raw["footbreak-legacy-batch-live-discovery.json"],
            "discovery",
        ),
        calculation,
    )
    if discovery["execution_identity"]["release_commit"] != audited_commit:
        raise ValueError("discovery_commit_mismatch")
    if (
        discovery["capture"]["full_pre_ledger_sha256"]
        != actual_hashes["footbreak-ledger.json"]
    ):
        raise ValueError("discovery_ledger_hash_mismatch")
    if (
        serialize_ledger_bytes_v1(discovery)
        != source_raw["footbreak-legacy-batch-live-discovery.json"]
    ):
        raise ValueError("discovery_not_canonical")

    manifest = {
        "schema": EXPECTED_SCHEMA,
        "artifacts": [
            {
                "filename": name,
                "sha256": hashlib.sha256(source_raw[name]).hexdigest(),
                "size": len(source_raw[name]),
            }
            for name in ARTIFACT_NAMES
        ],
    }
    raw = serialize_ledger_bytes_v1(manifest)
    flags = (
        os.O_RDWR | os.O_CREAT | os.O_EXCL | os.O_CLOEXEC | os.O_NOFOLLOW
    )
    descriptor = os.open(output, flags, 0o600)
    identity: Identity | None = None
    try:
        opened = os.fstat(descriptor)
        identity = (opened.st_dev, opened.st_ino)
        if (
            not stat.S_ISREG(opened.st_mode)
            or opened.st_nlink != 1
            or stat.S_IMODE(opened.st_mode) != 0o600
        ):
            raise ValueError("unsafe_expected_manifest")
        view = memoryview(raw)
        while view:
            count = os.write(descriptor, view)
            view = view[count:]
        os.fsync(descriptor)
        os.lseek(descriptor, 0, os.SEEK_SET)
        if os.read(descriptor, len(raw) + 1) != raw:
            raise ValueError("expected_manifest_readback")
        by_path = os.stat(output, follow_symlinks=False)
        after = os.fstat(descriptor)
        if (
            identity != (by_path.st_dev, by_path.st_ino)
            or identity != (after.st_dev, after.st_ino)
            or after.st_nlink != 1
        ):
            raise ValueError("expected_manifest_identity")
        directory = os.open(
            output.parent,
            os.O_RDONLY | os.O_CLOEXEC | os.O_DIRECTORY | os.O_NOFOLLOW,
        )
        try:
            os.fsync(directory)
        finally:
            os.close(directory)
        return hashlib.sha256(raw).hexdigest(), identity
    except BaseException:
        if identity is not None:
            try:
                current = os.stat(output, follow_symlinks=False)
                if (
                    current.st_dev, current.st_ino, current.st_nlink
                ) == (identity[0], identity[1], 1):
                    os.unlink(output)
            except FileNotFoundError:
                pass
        raise
    finally:
        os.close(descriptor)


def validate_expected_document(raw: bytes) -> dict[str, Any]:
    value = _object(raw, "expected_manifest")
    if set(value) != EXPECTED_KEYS or value["schema"] != EXPECTED_SCHEMA:
        raise ValueError("expected_manifest_fields")
    rows = value["artifacts"]
    if type(rows) is not list or len(rows) != len(ARTIFACT_NAMES):
        raise ValueError("expected_manifest_cardinality")
    seen = []
    for row in rows:
        if (
            type(row) is not dict or set(row) != ENTRY_KEYS
            or type(row["filename"]) is not str
            or type(row["sha256"]) is not str
            or HEX64.fullmatch(row["sha256"]) is None
            or type(row["size"]) is not int or row["size"] < 1
        ):
            raise ValueError("expected_manifest_entry")
        seen.append(row["filename"])
    if tuple(seen) != ARTIFACT_NAMES or len(set(seen)) != len(seen):
        raise ValueError("expected_manifest_names")
    if serialize_ledger_bytes_v1(value) != raw:
        raise ValueError("expected_manifest_not_canonical")
    return value


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--base-dir", type=Path, default=Path("."))
    parser.add_argument("--calculation", required=True, type=Path)
    parser.add_argument("--output", required=True, type=Path)
    parser.add_argument("--audited-commit", required=True)
    args = parser.parse_args()
    digest, identity = validate_and_commit(
        args.base_dir, args.calculation, args.output,
        audited_commit=args.audited_commit,
    )
    print(
        f"manifest_sha256={digest}\n"
        f"manifest_identity={identity[0]}:{identity[1]}"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
