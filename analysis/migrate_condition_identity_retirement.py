"""Offline, proof-gated installation of the Footbreak duplicate-retirement root."""
from __future__ import annotations

import argparse
import copy
import fcntl
import json
import os
import stat
import subprocess
import tempfile
from pathlib import Path
from typing import Any

from .wilson_validation import (
    NAMESPACE,
    _validate_condition_identity_migrations,
    apply_condition_identity_migration,
    plan_condition_identity_migration,
)
from .wilson_registry_manifest import build_manifest

CONFIRMATION = "APPLY_FOOTBREAK_RETIRED_DUPLICATE_IDENTITY_V1"


def _read_object(path: Path, label: str) -> dict[str, Any]:
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise ValueError(f"{label}_must_be_object")
    return value


def _checked_out_commit() -> str:
    root = Path(__file__).resolve().parents[1]
    value = subprocess.run(
        ["git", "-C", str(root), "rev-parse", "HEAD"],
        check=True, capture_output=True, text=True,
    ).stdout.strip()
    if len(value) != 40 or any(char not in "0123456789abcdef" for char in value):
        raise ValueError("checked_out_release_commit_invalid")
    return value


def _atomic_write_json(path: Path, payload: dict[str, Any]) -> list[str]:
    """Durably replace one ledger, preserving its existing ownership and mode."""
    original = path.stat()
    temporary: str | None = None
    warnings: list[str] = []
    try:
        descriptor, temporary = tempfile.mkstemp(
            prefix=f".{path.name}.", suffix=".tmp", dir=path.parent,
        )
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            json.dump(payload, handle, ensure_ascii=False, indent=2)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.chmod(temporary, stat.S_IMODE(original.st_mode))
        try:
            os.chown(temporary, original.st_uid, original.st_gid)
        except PermissionError:
            pass
        os.replace(temporary, path)
        temporary = None
        try:
            directory_descriptor = os.open(path.parent, os.O_RDONLY)
            try:
                os.fsync(directory_descriptor)
            finally:
                os.close(directory_descriptor)
        except Exception as exc:
            warnings.append(
                f"parent_directory_fsync_failed:{type(exc).__name__}:{exc}",
            )
        return warnings
    finally:
        if temporary is not None:
            try:
                os.unlink(temporary)
            except OSError:
                pass


def migrate_file(
    ledger_path: Path, authorized_manifest_path: Path, *, lock_path: Path,
    apply: bool, confirmation: str | None,
    trusted_manifest_hash: str | None = None,
) -> dict[str, Any]:
    """Take the shared writer lock, reload, prove, and optionally replace."""
    if apply and confirmation != CONFIRMATION:
        raise ValueError("exact_apply_confirmation_required")
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    with lock_path.open("a+", encoding="utf-8") as lock:
        try:
            fcntl.flock(lock.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            raise RuntimeError("migration_lock_unavailable") from exc
        try:
            ledger = _read_object(ledger_path, "ledger")
            authorized = _read_object(
                authorized_manifest_path, "authorized_manifest",
            )
            # CLI authority is always bound to the code actually checked out.
            # A caller may not self-authorize a different commit.
            release_commit = _checked_out_commit()
            existing = (
                ledger.get(NAMESPACE, {}).get("condition_identity_migrations")
                if isinstance(ledger.get(NAMESPACE), dict) else None
            )
            if existing is None:
                proposed = copy.deepcopy(ledger)
                if trusted_manifest_hash is None:
                    apply_condition_identity_migration(
                        proposed, "footbreak", authorized_manifest=authorized,
                        expected_release_commit=release_commit,
                    )
                else:
                    apply_condition_identity_migration(
                        proposed, "footbreak",
                        trusted_manifest_hash=trusted_manifest_hash,
                        candidate_manifest=authorized,
                        expected_release_commit=release_commit,
                    )
                pre_replace_manifest = build_manifest(proposed, "footbreak")
                if not pre_replace_manifest["valid"]:
                    raise ValueError(
                        "candidate_registry_manifest_invalid:"
                        + json.dumps(
                            pre_replace_manifest["rejection_reasons"],
                            ensure_ascii=False, sort_keys=True,
                        )
                    )
                result = plan_condition_identity_migration(
                    ledger, "footbreak", authorized,
                    expected_release_commit=release_commit,
                )
                result["apply"] = apply
                expected_document = authorized
                result["pre_replace_manifest_hash"] = pre_replace_manifest[
                    "manifest_hash"
                ]
                if apply:
                    ledger = proposed
                    result["status"] = "applied"
                    result["committed"] = True
                    warnings = _atomic_write_json(ledger_path, ledger)
                    if warnings:
                        result["durability_warnings"] = warnings
            else:
                installed = (
                    apply_condition_identity_migration(
                        ledger, "footbreak",
                        trusted_manifest_hash=trusted_manifest_hash,
                    )
                    if trusted_manifest_hash is not None
                    else apply_condition_identity_migration(
                        ledger, "footbreak", authorized_manifest=authorized,
                    )
                )
                result = {
                    "status": "already_applied",
                    "apply": apply,
                    "condition_identity_migrations": installed,
                }
                expected_document = installed
            if apply:
                try:
                    persisted = _read_object(ledger_path, "ledger_readback")
                    retired, reason = _validate_condition_identity_migrations(
                        persisted, "footbreak",
                    )
                    post_manifest = build_manifest(persisted, "footbreak")
                    if persisted != ledger:
                        raise RuntimeError("post_write_exact_payload_mismatch")
                    if retired is None:
                        raise RuntimeError(
                            "post_write_migration_validation_failed:"
                            + (reason or "unknown"),
                        )
                    if (
                        persisted[NAMESPACE]["condition_identity_migrations"]
                        != expected_document
                    ):
                        raise RuntimeError("post_write_manifest_mismatch")
                    if not post_manifest["valid"]:
                        raise RuntimeError(
                            "post_write_registry_manifest_invalid:"
                            + json.dumps(
                                post_manifest["rejection_reasons"],
                                ensure_ascii=False, sort_keys=True,
                            )
                        )
                    result["readback_verified"] = True
                    result["post_replace_manifest_hash"] = post_manifest[
                        "manifest_hash"
                    ]
                except Exception as exc:
                    result.setdefault("durability_warnings", []).append(
                        "post_write_readback_validation_failed:"
                        f"{type(exc).__name__}:{exc}"
                    )
                    result["readback_verified"] = False
            return result
        finally:
            fcntl.flock(lock.fileno(), fcntl.LOCK_UN)


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Install the immutable Footbreak #1/#2 identity retirement",
    )
    parser.add_argument("--ledger", required=True, type=Path)
    parser.add_argument("--authorized-manifest", required=True, type=Path)
    parser.add_argument("--lock", required=True, type=Path)
    parser.add_argument("--apply", action="store_true")
    parser.add_argument("--confirm")
    parser.add_argument("--trusted-manifest-hash")
    args = parser.parse_args()
    try:
        result = migrate_file(
            args.ledger, args.authorized_manifest, lock_path=args.lock,
            apply=args.apply, confirmation=args.confirm,
            trusted_manifest_hash=args.trusted_manifest_hash,
        )
        print(json.dumps(result, ensure_ascii=False, indent=2, sort_keys=True))
        return 0
    except Exception as exc:
        print(json.dumps({
            "status": "failed",
            "error": f"{type(exc).__name__}:{exc}",
        }, ensure_ascii=False, sort_keys=True))
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
