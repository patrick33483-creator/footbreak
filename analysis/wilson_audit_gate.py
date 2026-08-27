"""Executable, fail-closed release gate for Wilson offline audit artifacts."""
from __future__ import annotations

import argparse
import hashlib
import json
import re
from datetime import datetime
from pathlib import Path
from typing import Any

from analysis.wilson_registry_manifest import canonical_hash as _canonical_hash
from analysis.wilson_registry_manifest import build_manifest
from analysis.wilson_registry_export import export_registry, verify_export


EXPECTED_RELEASE = {
    "footbreak": {
        "historical": 17, "active": 15, "retired": 2,
        "retired_successors": {1: 7, 2: 14},
    },
    "crown": {
        "historical": 20, "active": 20, "retired": 0,
        "retired_successors": {},
    },
}
HEX64 = re.compile(r"[0-9a-f]{64}")


def _load_object(path: Path) -> dict[str, Any]:
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise ValueError(f"{path.name}: expected JSON object")
    return value


def _verify_manifest_hash(data: dict[str, Any], system: str) -> None:
    supplied = data.get("manifest_hash")
    body = {key: value for key, value in data.items() if key != "manifest_hash"}
    if not isinstance(supplied, str) or supplied != _canonical_hash(body):
        raise ValueError(f"{system}: manifest hash mismatch")


def _verify_release_shape(data: dict[str, Any], system: str) -> None:
    expected = EXPECTED_RELEASE[system]
    if data.get("schema") != "wilson-registry-manifest-v2":
        raise ValueError(f"{system}: unexpected manifest schema")
    if data.get("system") != system:
        raise ValueError(f"{system}: manifest system mismatch")
    shape = {
        "historical": data.get("historical_condition_count"),
        "active": data.get("active_condition_count"),
        "retired": data.get("retired_duplicate_count"),
    }
    expected_shape = {
        key: expected[key] for key in ("historical", "active", "retired")
    }
    if shape != expected_shape:
        raise ValueError(
            f"{system}: expected release shape {expected_shape}, found {shape}"
        )
    if data.get("condition_count") != expected["historical"]:
        raise ValueError(f"{system}: historical condition count alias mismatch")
    if data.get("valid") is not True:
        raise ValueError(f"{system}: manifest is not valid")
    conditions = data.get("conditions")
    if (
        not isinstance(conditions, list)
        or len(conditions) != expected["historical"]
    ):
        raise ValueError(f"{system}: malformed condition manifest rows")
    for position, condition in enumerate(conditions, start=1):
        if not isinstance(condition, dict):
            raise ValueError(f"{system}: malformed condition row {position}")
        if condition.get("condition_number") != position:
            raise ValueError(f"{system}: condition order mismatch at {position}")
        if condition.get("valid") is not True:
            raise ValueError(f"{system}: condition {position} is not valid")
        retired = position in expected["retired_successors"]
        expected_status = "retired_duplicate" if retired else "active"
        if condition.get("identity_status") != expected_status:
            raise ValueError(
                f"{system}: condition {position} identity status mismatch"
            )
        if retired:
            if (
                condition.get("canonical_successor_condition_number")
                != expected["retired_successors"][position]
                or condition.get("future_admission") != "target_only"
                or condition.get("prospective_x20", {}).get("decided") != 0
            ):
                raise ValueError(
                    f"{system}: retired condition {position} is not terminal"
                )
        elif condition.get("own_stage_matcher_can_structurally_admit") is not True:
            raise ValueError(
                f"{system}: condition {position} cannot be structurally admitted"
            )
    recovery = data.get("recovery")
    if (
        not isinstance(recovery, dict)
        or recovery.get("implemented") is not False
    ):
        raise ValueError(f"{system}: recovery isolation is not proven")


def summary_projection(data: dict[str, Any]) -> dict[str, Any]:
    """Return the exact non-sensitive per-system workflow summary."""
    return {
        "valid": data.get("valid") is True,
        "condition_count": data.get("condition_count"),
        "historical_condition_count": data.get("historical_condition_count"),
        "active_condition_count": data.get("active_condition_count"),
        "retired_duplicate_count": data.get("retired_duplicate_count"),
        "decision_stage_counts": data.get("decision_stage_counts"),
        "rejection_reasons": data.get("rejection_reasons"),
        "recovery": data.get("recovery"),
    }


def _declared_ledger_hashes(path: Path) -> dict[str, str]:
    hashes: dict[str, str] = {}
    for line in path.read_text(encoding="utf-8").splitlines():
        parts = line.split()
        if len(parts) != 2:
            raise ValueError("malformed ledger hash line")
        digest, raw_path = parts
        expected_path = f"audit-input/{Path(raw_path).name}"
        if raw_path != expected_path or HEX64.fullmatch(digest) is None:
            raise ValueError("invalid ledger hash entry")
        basename = Path(raw_path).name
        if basename in hashes:
            raise ValueError(f"duplicate ledger hash: {basename}")
        hashes[basename] = digest
    expected = {"footbreak-ledger.json", "crown-ledger.json"}
    if set(hashes) != expected:
        raise ValueError("ledger hash set mismatch")
    return hashes


def enforce(base: Path, *, audited_commit: str) -> None:
    supplied_manifests = {
        system: _load_object(base / f"{system}-wilson-registry-audit.json")
        for system in EXPECTED_RELEASE
    }
    manifests = {}
    ledgers = {}
    actual = {}
    for system, supplied in supplied_manifests.items():
        _verify_manifest_hash(supplied, system)
        ledger_path = base / "audit-input" / f"{system}-ledger.json"
        ledger = _load_object(ledger_path)
        ledgers[system] = ledger
        actual[ledger_path.name] = hashlib.sha256(
            ledger_path.read_bytes(),
        ).hexdigest()
        from analysis.legacy_batch_runtime import (
            load_production_legacy_batch_authority,
        )
        rebuilt = build_manifest(
            ledger, system,
            authority_context=load_production_legacy_batch_authority(ledger),
        )
        if supplied != rebuilt:
            raise ValueError(f"{system}: supplied manifest does not match ledger")
        manifests[system] = rebuilt
    for system, data in manifests.items():
        _verify_manifest_hash(data, system)
        _verify_release_shape(data, system)
    totals = tuple(
        sum(
            int(manifests[system][field])
            for system in EXPECTED_RELEASE
        )
        for field in (
            "historical_condition_count",
            "active_condition_count",
            "retired_duplicate_count",
        )
    )
    if totals != (37, 35, 2):
        raise ValueError(
            "expected historical/active/retired totals 37/35/2, found "
            f"{totals[0]}/{totals[1]}/{totals[2]}"
        )

    declared = _declared_ledger_hashes(base / "ledger-sha256.txt")
    if actual != declared:
        raise ValueError("captured ledger SHA-256 mismatch")
    for system in EXPECTED_RELEASE:
        supplied = verify_export(_load_object(
            base / f"{system}-wilson-registry-chains.json",
        ))
        rebuilt = export_registry(
            ledgers[system], system,
            source_ledger_sha256=actual[f"{system}-ledger.json"],
        )
        if (
            supplied != rebuilt
            or _canonical_hash(supplied) != _canonical_hash(rebuilt)
        ):
            raise ValueError(
                f"{system}: supplied sanitized export does not match ledger"
            )

    summary = _load_object(base / "wilson-production-audit-summary.json")
    if summary.get("ledger_sha256") != declared:
        raise ValueError("summary ledger SHA-256 mismatch")
    if summary.get("systems") != {
        system: summary_projection(manifests[system])
        for system in EXPECTED_RELEASE
    }:
        raise ValueError("summary systems projection mismatch")
    try:
        datetime.fromisoformat(str(summary.get("captured_at_utc") or ""))
    except ValueError as exc:
        raise ValueError("summary capture timestamp invalid") from exc
    if (
        summary.get("schema") != "wilson-production-offline-audit-v1"
        or summary.get("audited_commit") != audited_commit
        or summary.get("capture_outcome") != "success"
        or summary.get("capture_exit_codes") != {"footbreak": 0, "crown": 0}
        or summary.get("exit_codes") != {"footbreak": 0, "crown": 0}
        or summary.get("production_mutation") is not False
        or summary.get("recovery_enabled") is not False
    ):
        raise ValueError("summary audit binding mismatch")


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--base-dir", type=Path, default=Path("."))
    parser.add_argument("--audited-commit", required=True)
    args = parser.parse_args()
    try:
        enforce(args.base_dir, audited_commit=args.audited_commit)
    except Exception as exc:
        print(json.dumps({"valid": False, "error": str(exc)}, sort_keys=True))
        return 2
    print(json.dumps({"valid": True}, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
