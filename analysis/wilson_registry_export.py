"""Read-only sanitizer for production Wilson registry evidence chains."""
from __future__ import annotations

import argparse
import copy
import hashlib
import json
import os
import tempfile
from pathlib import Path
from typing import Any

from analysis import wilson_validation as wv


SCHEMA = "wilson-sanitized-registry-chains-v1"
TOP_LEVEL_KEYS = {
    "schema", "system", "source_ledger_sha256", "namespace_metadata",
    "condition_order", "conditions", "production_identity_manifest",
    "export_digest",
}
NAMESPACE_KEYS = {
    "schema_version", "system", "activation_at", "cutover_at",
    "rollover_migration_at", "granular_ranking_initial_migration_completed_at",
    "granular_ranking_initial_migration_version",
    "quarter_settlement_activation_at",
}
SOURCE_NAMESPACE_KEYS = NAMESPACE_KEYS | {
    "display_name", "starting_bankroll", "fixed_stake", "fixture_stake_cap",
    "fixture_market_cap", "minimum_decided", "edge_buffer", "conditions",
    "condition_order", "audit", "notifications", "retired_v1", "observations",
    "stats", "production_identity_manifest", "condition_identity_migrations",
    "formal_binding_omissions_migration_v1", "formal_observation_recovery_v1",
    "bilateral_schema_version", "counterpart_attempts", "decisions",
    "decision_outbox", "historical_discovery_archive", "audit_retention",
}
TIMESTAMP_NAMESPACE_KEYS = {
    "activation_at", "cutover_at", "rollover_migration_at",
    "granular_ranking_initial_migration_completed_at",
    "quarter_settlement_activation_at",
}
CONDITION_KEYS = {
    "signature", "condition_number", "definition", "historical_evidence",
    "evidence_versions", "active_evidence_version", "active_evidence_hash",
    "active_evidence",
}
HISTORICAL_KEYS = {"hits", "decided", "pushes", "artifact"}
HISTORICAL_SOURCE_KEYS = HISTORICAL_KEYS | {"label"}
ARTIFACT_KEYS = {"hash", "version", "as_of"}
ACTIVE_KEYS = {
    "version", "cumulative_hits", "cumulative_decided",
    "wilson95_lower_raw", "minimum_acceptable_odds_raw",
    "minimum_acceptable_odds_display", "activation_boundary_at", "created_at",
    "evidence_hash",
}
EVIDENCE_BASE_KEYS = {
    "condition_signature", "version", "prior_version", "prior_evidence_hash",
    "batch_fixture_market_hashes", "batch_hits", "batch_decided",
    "cumulative_hits", "cumulative_decided", "wilson95_lower_raw",
    "minimum_acceptable_odds_raw", "minimum_acceptable_odds_display",
    "activation_boundary_at", "created_at", "evidence_hash",
}
EVIDENCE_V1_KEYS = EVIDENCE_BASE_KEYS | {"migration_baseline"}
EVIDENCE_MIGRATION_KEYS = EVIDENCE_BASE_KEYS | {
    "batch_fixture_market_ids_unavailable_from_legacy_aggregate",
    "initial_migration_full_cohort", "legacy_prospective_cohort",
}
LEGACY_COHORT_KEYS = {"hits", "decided", "pushes"}


def _digest(value: Any) -> str:
    return wv._canonical_hash(value)


def _only(source: dict[str, Any], keys: set[str]) -> dict[str, Any]:
    return {
        key: copy.deepcopy(source[key])
        for key in keys if key in source
    }


def _evidence_version(row: Any) -> dict[str, Any]:
    if not isinstance(row, dict):
        raise ValueError("evidence version must be an object")
    keys = set(row)
    if frozenset(keys) not in {
        frozenset(EVIDENCE_BASE_KEYS),
        frozenset(EVIDENCE_V1_KEYS),
        frozenset(EVIDENCE_MIGRATION_KEYS),
    }:
        raise ValueError("evidence version contains unknown or missing fields")
    if keys == EVIDENCE_MIGRATION_KEYS:
        cohort = row.get("legacy_prospective_cohort")
        if not isinstance(cohort, dict) or set(cohort) != LEGACY_COHORT_KEYS:
            raise ValueError("legacy evidence cohort shape invalid")
    return _only(row, keys)


def _namespace_metadata(value: Any, system: str) -> dict[str, Any]:
    if not isinstance(value, dict) or not set(value).issubset(NAMESPACE_KEYS):
        raise ValueError("sanitized namespace metadata contains unknown fields")
    if not {"schema_version", "system", "activation_at"}.issubset(value):
        raise ValueError("sanitized namespace metadata missing required fields")
    if (
        type(value.get("schema_version")) is not int
        or value["schema_version"] != wv.SCHEMA_VERSION
        or type(value.get("system")) is not str
        or value["system"] != system
    ):
        raise ValueError("sanitized namespace scalar invalid")
    for key in TIMESTAMP_NAMESPACE_KEYS:
        if key in value and (
            type(value[key]) is not str or wv._time(value[key]) is None
        ):
            raise ValueError(f"sanitized namespace timestamp invalid: {key}")
    if "granular_ranking_initial_migration_version" in value and (
        type(value["granular_ranking_initial_migration_version"]) is not int
        or value["granular_ranking_initial_migration_version"] != 1
    ):
        raise ValueError("sanitized namespace migration version invalid")
    return _only(value, NAMESPACE_KEYS)


def verify_export(document: Any) -> dict[str, Any]:
    if not isinstance(document, dict) or set(document) != TOP_LEVEL_KEYS:
        raise ValueError("sanitized export shape invalid")
    body = {key: value for key, value in document.items() if key != "export_digest"}
    if (
        document.get("schema") != SCHEMA
        or document.get("system") not in {"footbreak", "crown"}
        or not isinstance(document.get("source_ledger_sha256"), str)
        or len(document["source_ledger_sha256"]) != 64
        or any(
            char not in "0123456789abcdef"
            for char in document["source_ledger_sha256"]
        )
        or document.get("export_digest") != _digest(body)
    ):
        raise ValueError("sanitized export digest invalid")
    metadata = _namespace_metadata(
        document.get("namespace_metadata"), document["system"],
    )
    order, rows = document.get("condition_order"), document.get("conditions")
    if (
        not isinstance(order, list) or not order
        or len(order) != len(set(order))
        or not isinstance(rows, list) or len(rows) != len(order)
    ):
        raise ValueError("sanitized condition registry invalid")
    conditions = {}
    for position, row in enumerate(rows, start=1):
        if not isinstance(row, dict) or set(row) != CONDITION_KEYS:
            raise ValueError("sanitized condition shape invalid")
        signature = row.get("signature")
        historical = row.get("historical_evidence")
        artifact = (
            historical.get("artifact") if isinstance(historical, dict) else None
        )
        active = row.get("active_evidence")
        versions = row.get("evidence_versions")
        if (
            signature != order[position - 1]
            or row.get("condition_number") != position
            or not isinstance(historical, dict)
            or set(historical) != HISTORICAL_KEYS
            or not isinstance(artifact, dict)
            or set(artifact) != ARTIFACT_KEYS
            or not isinstance(active, dict)
            or set(active) != ACTIVE_KEYS
            or not isinstance(versions, list)
        ):
            raise ValueError("sanitized condition binding invalid")
        projected_versions = [_evidence_version(version) for version in versions]
        if versions != projected_versions:
            raise ValueError("sanitized evidence version projection mismatch")
        frozen = copy.deepcopy(row)
        definition, versions, reason = wv._validate_frozen_identity_and_chain(
            frozen, signature, document["system"],
        )
        if reason is not None or definition is None or versions is None:
            raise ValueError(reason or "sanitized evidence chain invalid")
        first, tail = versions[0], versions[-1]
        if (
            first.get("cumulative_hits") != historical.get("hits")
            or first.get("cumulative_decided") != historical.get("decided")
            or wv._time(first.get("activation_boundary_at"))
            != wv._time(artifact.get("as_of"))
            or row.get("active_evidence_version") != tail.get("version")
            or row.get("active_evidence_hash") != tail.get("evidence_hash")
            or active != {
                key: tail.get(key) for key in ACTIVE_KEYS
            }
        ):
            raise ValueError("sanitized evidence projection invalid")
        conditions[signature] = frozen
    ns = {
        **copy.deepcopy(metadata),
        "condition_order": copy.deepcopy(order),
        "conditions": conditions,
    }
    expected, _validated, reason = wv._expected_production_identity_manifest(
        ns, document["system"],
    )
    if (
        reason is not None or expected is None
        or document.get("production_identity_manifest") != expected
    ):
        raise ValueError(reason or "production identity manifest mismatch")
    return copy.deepcopy(document)


def export_registry(
    ledger: dict[str, Any], system: str, *, source_ledger_sha256: str,
) -> dict[str, Any]:
    before = copy.deepcopy(ledger)
    ns = ledger.get(wv.NAMESPACE)
    if not isinstance(ns, dict):
        raise ValueError("validated Wilson namespace required")
    if not set(ns).issubset(SOURCE_NAMESPACE_KEYS):
        raise ValueError("Wilson namespace contains unknown source fields")
    metadata = _namespace_metadata(_only(ns, NAMESPACE_KEYS), system)
    order = ns.get("condition_order")
    conditions = ns.get("conditions")
    if not isinstance(order, list) or not isinstance(conditions, dict):
        raise ValueError("frozen condition registry unavailable")
    rows = []
    for signature in order:
        frozen = conditions.get(signature)
        if not isinstance(frozen, dict):
            raise ValueError("frozen condition unavailable")
        historical = frozen.get("historical_evidence")
        artifact = (
            historical.get("artifact") if isinstance(historical, dict) else {}
        )
        if (
            not isinstance(historical, dict)
            or not set(historical).issubset(HISTORICAL_SOURCE_KEYS)
            or not HISTORICAL_KEYS.issubset(historical)
            or not isinstance(artifact, dict)
            or set(artifact) != ARTIFACT_KEYS
        ):
            raise ValueError("historical evidence contains unknown fields")
        active = frozen.get("active_evidence")
        if not isinstance(active, dict) or set(active) != ACTIVE_KEYS:
            raise ValueError("active evidence contains unknown fields")
        versions = frozen.get("evidence_versions")
        if not isinstance(versions, list):
            raise ValueError("evidence versions unavailable")
        projected_versions = [_evidence_version(version) for version in versions]
        rows.append({
            "signature": signature,
            "condition_number": frozen.get("condition_number"),
            "definition": copy.deepcopy(frozen.get("definition")),
            "historical_evidence": {
                **_only(historical or {}, HISTORICAL_KEYS - {"artifact"}),
                "artifact": _only(artifact, ARTIFACT_KEYS),
            },
            "evidence_versions": projected_versions,
            "active_evidence_version": frozen.get("active_evidence_version"),
            "active_evidence_hash": frozen.get("active_evidence_hash"),
            "active_evidence": _only(active, ACTIVE_KEYS),
        })
    body = {
        "schema": SCHEMA,
        "system": system,
        "source_ledger_sha256": source_ledger_sha256,
        "namespace_metadata": metadata,
        "condition_order": copy.deepcopy(order),
        "conditions": rows,
        "production_identity_manifest": copy.deepcopy(
            ns.get("production_identity_manifest"),
        ),
    }
    result = verify_export({**body, "export_digest": _digest(body)})
    if ledger != before:
        raise RuntimeError("sanitized export mutated source ledger")
    return result


def _same_file(left: Path, right: Path) -> bool:
    try:
        return (
            left.resolve() == right.resolve()
            or left.exists() and right.exists() and os.path.samefile(left, right)
        )
    except OSError:
        return False


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--system", required=True, choices=("footbreak", "crown"))
    parser.add_argument("--ledger", required=True, type=Path)
    parser.add_argument("--output", required=True, type=Path)
    args = parser.parse_args()
    try:
        if _same_file(args.ledger, args.output):
            raise ValueError("output aliases input ledger")
        before = args.ledger.read_bytes()
        ledger = json.loads(before)
        result = export_registry(
            ledger, args.system,
            source_ledger_sha256=hashlib.sha256(before).hexdigest(),
        )
        if args.ledger.read_bytes() != before:
            raise RuntimeError("source ledger changed during export")
        text = json.dumps(
            result, ensure_ascii=False, sort_keys=True, indent=2,
        ) + "\n"
        args.output.parent.mkdir(parents=True, exist_ok=True)
        descriptor, temporary = tempfile.mkstemp(
            prefix=args.output.name + ".", dir=args.output.parent,
        )
        try:
            with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
                handle.write(text)
                handle.flush()
                os.fsync(handle.fileno())
            if _same_file(args.ledger, args.output):
                raise ValueError("output aliases input ledger")
            os.replace(temporary, args.output)
        finally:
            if os.path.exists(temporary):
                os.unlink(temporary)
        return 0
    except Exception as exc:
        print(json.dumps({"valid": False, "error": str(exc)}, sort_keys=True))
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
