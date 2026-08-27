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
PAYLOAD_V2_SCHEMA = "wilson-registry-export-payload-v2"
EXPORT_V2_SCHEMA = "wilson-sanitized-registry-chains-v2"
EVIDENCE_AGGREGATE_KEYS = EVIDENCE_BASE_KEYS | {
    "legacy_ordinary_batch_aggregate",
}


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


def _evidence_version_v2(row: Any) -> dict[str, Any]:
    if not isinstance(row, dict):
        raise ValueError("evidence version must be an object")
    if set(row) == EVIDENCE_AGGREGATE_KEYS:
        marker = row.get("legacy_ordinary_batch_aggregate")
        from analysis.legacy_batch_aggregate import MARKER_KEYS
        if not isinstance(marker, dict) or set(marker) != MARKER_KEYS:
            raise ValueError("aggregate marker shape invalid")
        return copy.deepcopy(row)
    return _evidence_version(row)


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
    expected_manifest, _validated, manifest_reason = (
        wv._expected_production_identity_manifest(ns, system)
    )
    if expected_manifest is None:
        raise ValueError(
            manifest_reason or "production identity manifest unavailable"
        )
    stored_manifest = ns.get("production_identity_manifest")
    if (
        stored_manifest is not None
        and (
            not isinstance(stored_manifest, dict)
            or json.dumps(
                stored_manifest, ensure_ascii=False, sort_keys=True,
                separators=(",", ":"),
            )
            != json.dumps(
                expected_manifest, ensure_ascii=False, sort_keys=True,
                separators=(",", ":"),
            )
        )
    ):
        raise ValueError("stored production identity manifest mismatch")
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
        "production_identity_manifest": copy.deepcopy(expected_manifest),
    }
    result = verify_export({**body, "export_digest": _digest(body)})
    if ledger != before:
        raise RuntimeError("sanitized export mutated source ledger")
    return result


def _registry_payload_v2(
    ledger: dict[str, Any], system: str, *,
    authority_context: Any = None, calculation_context: Any = None,
) -> dict[str, Any]:
    """Build the authority-neutral v2 payload without envelope metadata."""
    from analysis.legacy_batch_aggregate import (
        _require_calculation_context, require_authority_context,
    )
    if (authority_context is None) == (calculation_context is None):
        raise TypeError("supply exactly one legacy batch context")
    authority = None
    if authority_context is not None:
        authority = require_authority_context(authority_context)
        calculation = authority.calculation
    else:
        calculation = _require_calculation_context(calculation_context)
    if system != "footbreak":
        raise ValueError("aggregate v2 export is Footbreak-only")
    ns = ledger.get(wv.NAMESPACE)
    if not isinstance(ns, dict):
        raise ValueError("validated Wilson namespace required")
    metadata = _namespace_metadata(_only(ns, NAMESPACE_KEYS), system)
    order, conditions = ns.get("condition_order"), ns.get("conditions")
    if not isinstance(order, list) or not isinstance(conditions, dict):
        raise ValueError("frozen condition registry unavailable")
    identity, reason = wv.validate_production_identity_manifest_v1(ns, system)
    if identity is None:
        raise ValueError(reason or "production identity v1 invalid")
    rows = []
    for signature in order:
        frozen = conditions.get(signature)
        if not isinstance(frozen, dict):
            raise ValueError("frozen condition unavailable")
        historical = frozen.get("historical_evidence")
        artifact = (
            historical.get("artifact") if isinstance(historical, dict) else None
        )
        active = frozen.get("active_evidence")
        versions = frozen.get("evidence_versions")
        if (
            not isinstance(historical, dict)
            or not HISTORICAL_KEYS.issubset(historical)
            or not isinstance(artifact, dict) or set(artifact) != ARTIFACT_KEYS
            or not isinstance(active, dict) or set(active) != ACTIVE_KEYS
            or not isinstance(versions, list)
        ):
            raise ValueError("v2 registry projection invalid")
        projected = [_evidence_version_v2(row) for row in versions]
        if authority is not None:
            definition, validated, chain_reason = (
                wv._validate_frozen_identity_and_chain(
                    frozen, signature, system,
                    authority_context=authority,
                )
            )
            if definition is None or validated is None or chain_reason is not None:
                raise ValueError(chain_reason or "aggregate evidence chain invalid")
        rows.append({
            "signature": signature,
            "condition_number": frozen.get("condition_number"),
            "definition": copy.deepcopy(frozen.get("definition")),
            "historical_evidence": {
                **_only(historical, HISTORICAL_KEYS - {"artifact"}),
                "artifact": _only(artifact, ARTIFACT_KEYS),
            },
            "evidence_versions": projected,
            "active_evidence_version": frozen.get("active_evidence_version"),
            "active_evidence_hash": frozen.get("active_evidence_hash"),
            "active_evidence": _only(active, ACTIVE_KEYS),
        })
    payload = {
        "schema": PAYLOAD_V2_SCHEMA,
        "system": system,
        "namespace_metadata": metadata,
        "condition_order": copy.deepcopy(order),
        "conditions": rows,
        "production_identity_manifest": copy.deepcopy(identity),
    }
    # Calculation-context use is discovery-only and must be the exact planned
    # sanitized payload, not a caller-invented aggregate ledger.
    if calculation_context is not None and _digest(payload) != (
        calculation.document["expected_post_export_registry_payload_sha256"]
    ):
        raise ValueError("discovery post export payload commitment mismatch")
    return payload


def export_registry_payload_v2_for_discovery(
    ledger: dict[str, Any], calculation_context: Any,
) -> dict[str, Any]:
    return _registry_payload_v2(
        ledger, "footbreak", calculation_context=calculation_context,
    )


def export_registry_v2(
    ledger: dict[str, Any], *, authority_context: Any,
) -> dict[str, Any]:
    from analysis.legacy_batch_aggregate import (
        canonical_hash_v1, require_authority_context,
    )
    context = require_authority_context(authority_context)
    before = copy.deepcopy(ledger)
    payload = _registry_payload_v2(
        ledger, "footbreak", authority_context=context,
    )
    body = {
        "schema": EXPORT_V2_SCHEMA,
        "trusted_authority_manifest_hash": context.manifest_hash,
        "payload_sha256": canonical_hash_v1(payload),
        "payload": payload,
        "authority_material": context.authority,
    }
    result = {**body, "export_digest": canonical_hash_v1(body)}
    if ledger != before:
        raise RuntimeError("v2 export mutated source ledger")
    return result


def verify_export_v2(
    document: Any, *, trusted_authority_hash: str | None = None,
    trusted_public_key: str | bytes | None = None,
    detached_signature: dict[str, Any] | None = None,
    trusted_public_key_id: str | None = None,
) -> dict[str, Any]:
    """Verify payload, independent authority pin, and archival envelope."""
    from analysis.legacy_batch_aggregate import (
        AUTHORITY_SCHEMA, _extract_calculation, canonical_hash_v1,
        validate_final_authority, validate_sanitized_calculation,
    )
    keys = {
        "schema", "trusted_authority_manifest_hash", "payload_sha256", "payload",
        "authority_material", "export_digest",
    }
    if not isinstance(document, dict) or set(document) != keys:
        raise ValueError("v2 export envelope shape invalid")
    if document.get("schema") != EXPORT_V2_SCHEMA:
        raise ValueError("v2 export schema invalid")
    if trusted_authority_hash is None and (
        trusted_public_key is None or detached_signature is None
        or not trusted_public_key_id
    ):
        raise ValueError("external authority trust required")
    authority_hash = document.get("trusted_authority_manifest_hash")
    if trusted_authority_hash is not None and authority_hash != trusted_authority_hash:
        raise ValueError("trusted authority hash mismatch")
    authority = document.get("authority_material")
    if not isinstance(authority, dict):
        raise ValueError("complete authority material required")
    authority_body = copy.deepcopy(authority)
    claimed_authority_hash = authority_body.pop("authority_manifest_hash", None)
    if claimed_authority_hash != canonical_hash_v1(authority_body):
        raise ValueError("embedded authority hash invalid")
    if authority_hash != claimed_authority_hash:
        raise ValueError("export authority binding mismatch")
    if trusted_public_key is not None or detached_signature is not None:
        if not isinstance(detached_signature, dict) or set(detached_signature) != {
            "schema", "algorithm", "authority_manifest_hash", "approver_key_id",
            "approved_at", "signature_base64",
        }:
            raise ValueError("detached authority signature invalid")
        if (
            detached_signature["schema"]
            != "footbreak-legacy-batch-authority-signature-v1"
            or detached_signature["algorithm"] != "ed25519"
            or detached_signature["authority_manifest_hash"] != authority_hash
            or detached_signature["approver_key_id"] != trusted_public_key_id
        ):
            raise ValueError("detached authority signature invalid")
        import base64
        from cryptography.hazmat.primitives.asymmetric.ed25519 import (
            Ed25519PublicKey,
        )
        try:
            key_bytes = (
                trusted_public_key if isinstance(trusted_public_key, bytes)
                else base64.b64decode(trusted_public_key, validate=True)
            )
            Ed25519PublicKey.from_public_bytes(key_bytes).verify(
                base64.b64decode(
                    detached_signature["signature_base64"], validate=True,
                ),
                AUTHORITY_SCHEMA.encode("ascii")
                + b"\0" + bytes.fromhex(authority_hash),
            )
        except Exception as exc:
            raise ValueError("detached authority signature invalid") from exc
    validate_final_authority(
        authority, authority_hash, authority.get("implementation"),
    )
    calculation = validate_sanitized_calculation(_extract_calculation(authority))
    payload = document.get("payload")
    if (
        not isinstance(payload, dict)
        or set(payload) != {
            "schema", "system", "namespace_metadata", "condition_order",
            "conditions", "production_identity_manifest",
        }
        or payload.get("schema") != PAYLOAD_V2_SCHEMA
        or canonical_hash_v1(payload) != document.get("payload_sha256")
        or document.get("payload_sha256")
        != authority.get("expected_poststate", {}).get(
            "post_export_registry_payload_sha256",
            calculation.document["expected_post_export_registry_payload_sha256"],
        )
    ):
        raise ValueError("v2 registry payload hash mismatch")
    if payload != calculation.document["expected_post_export_registry_payload"]:
        raise ValueError("v2 payload does not reconstruct approved postimages")
    body = {key: value for key, value in document.items() if key != "export_digest"}
    if canonical_hash_v1(body) != document.get("export_digest"):
        raise ValueError("v2 export envelope digest invalid")
    # Re-validating calculation commits every old preimage, postimage, and
    # reservation set independently of the embedded authority summary.
    return copy.deepcopy(document)


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
