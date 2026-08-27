"""Proof-gated compatibility for the ten approved Footbreak legacy batches.

This module intentionally separates the capability used to inspect a ledger
from the capability used to validate or change a persisted ledger.  The
sanitized calculation is useful evidence, but is not an authorization.
"""
from __future__ import annotations

import base64
import copy
import hashlib
import json
import math
import os
import stat
import subprocess
import sys
import importlib
from dataclasses import dataclass
from pathlib import Path
from types import MappingProxyType
from typing import Any, Iterator


CALCULATION_SCHEMA = "footbreak-legacy-batch-sanitized-calculation-v3"
DISCOVERY_SCHEMA = "footbreak-legacy-batch-live-discovery-v1"
AUTHORITY_SCHEMA = "footbreak-legacy-batch-final-authority-v1"
MIGRATION_VERSION = "footbreak-legacy-ordinary-batch-aggregate-v1"
CALCULATION_DOMAIN = "footbreak-legacy-batch-calculation-context-v1"
AUTHORITY_DOMAIN = "footbreak-legacy-batch-authority-context-v1"
EXPECTED_AUTHORIZATION_ROOT = (
    "908bdbc0af99386b4d8738ad4f3cdc8237b9cf6796ccd6ae486a3efc0603d533"
)
EXPECTED_CALCULATION_HASH = (
    "726fd524595e16f0eb13c2cf7d839a669eeef9bbaaa0b70254ddf72fc6d8748d"
)
EXPECTED_POST_REGISTRY_HASH = (
    "0bde1d53a5a8ef0f16c887792468fecee4c40afd49f6fd78e3be63afad326661"
)
EXPECTED_POST_EXPORT_PAYLOAD_HASH = (
    "9274dd20f069cb2043580dea5056a15f982243f8b21f373963472bcb99596e37"
)
EXPECTED_SIGNATURES = {
    "7b69b0c09392930f89bfe52d": (3, 4),
    "e9b991435138c3c429a696a8": (3, 4),
    "a7a8aae669b985ff87f8be6e": (3, 4, 5),
    "0869fbd4573b9dee57ffe2eb": (3, 4),
    "a79e13125a194532c8194036": (3,),
}
MARKER_KEYS = {
    "schema_version", "authority_root", "source_evidence_hash",
    "source_batch_fixture_market_hashes_sha256",
    "source_batch_fixture_market_hash_count",
}
_JSON_INT_MIN = -(2**63)
_JSON_INT_MAX = 2**63 - 1
_CONTEXT_TOKEN = object()
REPOSITORY_ID = "patrick33483-creator/footbreak"
REQUIRED_RUNTIME_MODULES = {
    "analysis.legacy_batch_aggregate": "analysis/legacy_batch_aggregate.py",
    "analysis.export_legacy_batch_live_authority": "analysis/export_legacy_batch_live_authority.py",
    "analysis.migrate_legacy_batch_aggregates": "analysis/migrate_legacy_batch_aggregates.py",
    "analysis.legacy_batch_runtime": "analysis/legacy_batch_runtime.py",
    "analysis.wilson_validation": "analysis/wilson_validation.py",
    "analysis.wilson_registry_manifest": "analysis/wilson_registry_manifest.py",
    "analysis.wilson_registry_export": "analysis/wilson_registry_export.py",
    "analysis.wilson_portfolio": "analysis/wilson_portfolio.py",
    "analysis.wilson_audit_gate": "analysis/wilson_audit_gate.py",
    "system.settle": "system/settle.py",
    "system.gen_app_data": "system/gen_app_data.py",
}


def _validate_json_value(value: Any, pointer: str = "") -> None:
    if value is None or isinstance(value, (str, bool)):
        return
    if isinstance(value, int):
        if not _JSON_INT_MIN <= value <= _JSON_INT_MAX:
            raise ValueError(f"integer_out_of_signed_64_bit_range:{pointer or '/'}")
        return
    if isinstance(value, float):
        if not math.isfinite(value):
            raise ValueError(f"non_finite_float:{pointer or '/'}")
        # Python floats are binary64.  Prove the selected JSON representation
        # round-trips to the identical value, including negative zero.
        encoded = json.dumps(value, allow_nan=False)
        decoded = json.loads(encoded)
        if decoded != value or (
            value == 0.0
            and math.copysign(1.0, decoded) != math.copysign(1.0, value)
        ):
            raise ValueError(f"float_not_binary64_round_trip:{pointer or '/'}")
        return
    if isinstance(value, list):
        for index, item in enumerate(value):
            _validate_json_value(item, f"{pointer}/{index}")
        return
    if type(value) is dict:
        for key, item in value.items():
            if not isinstance(key, str):
                raise ValueError(f"non_string_object_key:{pointer or '/'}")
            _validate_json_value(item, f"{pointer}/{_pointer_token(key)}")
        return
    raise ValueError(f"unsupported_json_type:{pointer or '/'}:{type(value).__name__}")


def parse_json_bytes_v1(raw: bytes) -> Any:
    """Parse strict UTF-8 JSON while rejecting duplicate object keys."""
    if raw.startswith(b"\xef\xbb\xbf"):
        raise ValueError("json_bom_forbidden")
    try:
        text = raw.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise ValueError("invalid_utf8_json") from exc

    def pairs(items: list[tuple[str, Any]]) -> dict[str, Any]:
        result: dict[str, Any] = {}
        for key, value in items:
            if key in result:
                raise ValueError(f"duplicate_json_key:{key}")
            result[key] = value
        return result

    try:
        value = json.loads(
            text, object_pairs_hook=pairs,
            parse_constant=lambda token: (_ for _ in ()).throw(
                ValueError(f"non_finite_json_number:{token}")
            ),
        )
    except json.JSONDecodeError as exc:
        raise ValueError("malformed_json") from exc
    _validate_json_value(value)
    return value


def read_root_owned_json_config(
    path: Path,
) -> tuple[dict[str, Any], bytes, dict[str, Any]]:
    """Read immutable release configuration through one no-follow descriptor."""
    descriptor = os.open(path, os.O_RDONLY | os.O_CLOEXEC | os.O_NOFOLLOW)
    try:
        opened = os.fstat(descriptor)
        by_path = os.stat(path, follow_symlinks=False)
        parent = path.parent.stat()
        if (
            not stat.S_ISREG(opened.st_mode)
            or opened.st_uid != 0
            or stat.S_IMODE(opened.st_mode) != 0o600
            or opened.st_nlink != 1
            or (opened.st_dev, opened.st_ino)
            != (by_path.st_dev, by_path.st_ino)
            or parent.st_uid != 0
            or stat.S_IMODE(parent.st_mode) & 0o022
        ):
            raise ValueError("unsafe_root_owned_runtime_config")
        chunks = []
        while True:
            chunk = os.read(descriptor, 1024 * 1024)
            if not chunk:
                break
            chunks.append(chunk)
        raw = b"".join(chunks)
        after = os.fstat(descriptor)
        if (opened.st_dev, opened.st_ino, opened.st_size) != (
            after.st_dev, after.st_ino, after.st_size
        ):
            raise ValueError("runtime_config_changed_during_read")
    finally:
        os.close(descriptor)
    document = parse_json_bytes_v1(raw)
    if not isinstance(document, dict):
        raise ValueError("runtime_config_not_object")
    identity = {
        "realpath": str(path.resolve()), "st_dev": opened.st_dev,
        "st_ino": opened.st_ino, "st_uid": opened.st_uid,
        "st_gid": opened.st_gid, "st_mode": stat.S_IMODE(opened.st_mode),
        "st_nlink": opened.st_nlink,
        "sha256": hashlib.sha256(raw).hexdigest(),
    }
    return document, raw, identity


def canonical_json_bytes_v1(value: Any) -> bytes:
    _validate_json_value(value)
    return json.dumps(
        value, ensure_ascii=False, sort_keys=True, separators=(",", ":"),
        allow_nan=False,
    ).encode("utf-8")


def canonical_hash_v1(value: Any) -> str:
    return hashlib.sha256(canonical_json_bytes_v1(value)).hexdigest()


def serialize_ledger_bytes_v1(ledger: Any) -> bytes:
    _validate_json_value(ledger)
    return (
        json.dumps(
            ledger, ensure_ascii=False, sort_keys=True, indent=2,
            separators=(",", ": "), allow_nan=False,
        )
        + "\n"
    ).encode("utf-8")


def _strict_int(value: Any) -> int | None:
    return value if isinstance(value, int) and not isinstance(value, bool) else None


def _hex(value: Any, length: int = 64) -> bool:
    return (
        isinstance(value, str) and len(value) == length
        and all(char in "0123456789abcdef" for char in value)
    )


def _pointer_token(value: str) -> str:
    return value.replace("~", "~0").replace("/", "~1")


class ValidatedLegacyBatchCalculationContext:
    """Final discovery-only capability.  Instances cannot be caller-forged."""
    __slots__ = ("_document", "_entries", "_reservations", "_token")

    def __init_subclass__(cls, **kwargs: Any) -> None:
        raise TypeError("legacy batch contexts are final")

    def __init__(
        self, token: object, document: dict[str, Any],
        entries: dict[tuple[str, int], dict[str, Any]],
        reservations: dict[str, frozenset[str]],
    ) -> None:
        if token is not _CONTEXT_TOKEN:
            raise TypeError("private context constructor")
        self._document = copy.deepcopy(document)
        self._entries = MappingProxyType(copy.deepcopy(entries))
        self._reservations = MappingProxyType(dict(reservations))
        self._token = token

    @property
    def domain_tag(self) -> str:
        return CALCULATION_DOMAIN

    @property
    def document(self) -> dict[str, Any]:
        return copy.deepcopy(self._document)

    @property
    def entries(self) -> MappingProxyType:
        return self._entries

    @property
    def reservations(self) -> MappingProxyType:
        return self._reservations

    def __reduce__(self) -> Any:
        raise TypeError("legacy batch contexts are not serializable")


class LegacyBatchAuthorityContext:
    """Final externally-authorized capability used by production APIs."""
    __slots__ = (
        "_authority", "_calculation", "_entries", "_reservations",
        "_manifest_hash", "_token",
    )

    def __init_subclass__(cls, **kwargs: Any) -> None:
        raise TypeError("legacy batch contexts are final")

    def __init__(
        self, token: object, authority: dict[str, Any],
        calculation: ValidatedLegacyBatchCalculationContext,
        manifest_hash: str,
    ) -> None:
        if token is not _CONTEXT_TOKEN:
            raise TypeError("private context constructor")
        _require_calculation_context(calculation)
        self._authority = copy.deepcopy(authority)
        self._calculation = calculation
        self._entries = calculation.entries
        self._reservations = calculation.reservations
        self._manifest_hash = manifest_hash
        self._token = token

    @property
    def domain_tag(self) -> str:
        return AUTHORITY_DOMAIN

    @property
    def manifest_hash(self) -> str:
        return self._manifest_hash

    @property
    def authority(self) -> dict[str, Any]:
        return copy.deepcopy(self._authority)

    @property
    def calculation(self) -> ValidatedLegacyBatchCalculationContext:
        return self._calculation

    @property
    def entries(self) -> MappingProxyType:
        return self._entries

    @property
    def reservations(self) -> MappingProxyType:
        return self._reservations

    def __reduce__(self) -> Any:
        raise TypeError("legacy batch contexts are not serializable")


def _require_calculation_context(value: Any) -> ValidatedLegacyBatchCalculationContext:
    if (
        type(value) is not ValidatedLegacyBatchCalculationContext
        or value._token is not _CONTEXT_TOKEN
    ):
        raise TypeError("validated calculation context required")
    return value


def require_authority_context(value: Any) -> LegacyBatchAuthorityContext:
    if type(value) is not LegacyBatchAuthorityContext or value._token is not _CONTEXT_TOKEN:
        raise TypeError("legacy batch authority context required")
    return value


def _entry_key(entry: dict[str, Any]) -> tuple[str, int]:
    version = entry.get("source_version", {}).get("version")
    return str(entry.get("condition_signature") or ""), int(version)


def validate_sanitized_calculation(
    document: Any,
) -> ValidatedLegacyBatchCalculationContext:
    if not isinstance(document, dict):
        raise ValueError("calculation_not_object")
    expected_keys = {
        "authorization_body", "authorization_root", "calculation_artifact_hash",
        "expected_post_condition_registry_scope",
        "expected_post_condition_registry_sha256",
        "expected_post_export_registry_payload",
        "expected_post_export_registry_payload_sha256", "expected_rewrites",
        "final_authority", "final_authority_manifest_hash",
        "historical_identity_reservations",
        "live_unknowns_required_from_authority_exporter", "schema",
        "source_assurance", "status",
    }
    if set(document) != expected_keys:
        raise ValueError("calculation_key_set_mismatch")
    _validate_json_value(document)
    if (
        document.get("schema") != CALCULATION_SCHEMA
        or document.get("status")
        != "non_deployable_requires_live_authority_export_and_independent_pin"
        or document.get("final_authority") is not None
        or document.get("final_authority_manifest_hash") is not None
    ):
        raise ValueError("calculation_trust_state_invalid")
    artifact_body = copy.deepcopy(document)
    claimed_artifact = artifact_body.pop("calculation_artifact_hash")
    if (
        claimed_artifact != EXPECTED_CALCULATION_HASH
        or canonical_hash_v1(artifact_body) != claimed_artifact
    ):
        raise ValueError("calculation_artifact_hash_mismatch")
    body = document.get("authorization_body")
    if (
        not isinstance(body, dict)
        or document.get("authorization_root") != EXPECTED_AUTHORIZATION_ROOT
        or canonical_hash_v1(body) != EXPECTED_AUTHORIZATION_ROOT
        or body.get("system") != "footbreak"
        or body.get("migration_version") != MIGRATION_VERSION
        or body.get("policy", {}).get("source_support_rule") != "zero_exact_only"
    ):
        raise ValueError("authorization_root_or_policy_mismatch")
    if (
        document.get("expected_post_condition_registry_sha256")
        != EXPECTED_POST_REGISTRY_HASH
        or canonical_hash_v1(document["expected_post_condition_registry_scope"])
        != EXPECTED_POST_REGISTRY_HASH
        or document.get("expected_post_export_registry_payload_sha256")
        != EXPECTED_POST_EXPORT_PAYLOAD_HASH
        or canonical_hash_v1(document["expected_post_export_registry_payload"])
        != EXPECTED_POST_EXPORT_PAYLOAD_HASH
    ):
        raise ValueError("calculation_post_commitment_mismatch")
    entries = body.get("entries")
    rewrites = document.get("expected_rewrites")
    if not isinstance(entries, list) or not isinstance(rewrites, list) or len(entries) != 10 or len(rewrites) != 10:
        raise ValueError("exactly_ten_entries_required")
    indexed: dict[tuple[str, int], dict[str, Any]] = {}
    rewrite_index: dict[tuple[str, int], dict[str, Any]] = {}
    for rewrite in rewrites:
        key = (rewrite.get("condition_signature"), rewrite.get("version"))
        if key in rewrite_index:
            raise ValueError("duplicate_expected_rewrite")
        rewrite_index[key] = rewrite
    for entry in entries:
        if not isinstance(entry, dict) or not isinstance(entry.get("source_version"), dict):
            raise ValueError("malformed_calculation_entry")
        key = _entry_key(entry)
        signature, version = key
        if signature not in EXPECTED_SIGNATURES or version not in EXPECTED_SIGNATURES[signature]:
            raise ValueError("entry_not_in_exact_allowlist")
        if key in indexed:
            raise ValueError("duplicate_calculation_entry")
        source = entry["source_version"]
        hashes = source.get("batch_fixture_market_hashes")
        if (
            entry.get("condition_signature") != source.get("condition_signature")
            or _strict_int(entry.get("condition_number")) is None
            or not isinstance(hashes, list) or len(hashes) != 20
            or len(set(hashes)) != 20 or any(not _hex(item) for item in hashes)
            or canonical_hash_v1(hashes)
            != entry.get("source_batch_fixture_market_hashes_sha256")
            or source.get("evidence_hash") != rewrite_index.get(key, {}).get("source_evidence_hash")
        ):
            raise ValueError("source_preimage_commitment_mismatch")
        expected = rewrite_index[key].get("expected_post_version")
        if not isinstance(expected, dict):
            raise ValueError("missing_expected_post_version")
        marker = expected.get("legacy_ordinary_batch_aggregate")
        if (
            expected.get("batch_fixture_market_hashes") != []
            or not isinstance(marker, dict) or set(marker) != MARKER_KEYS
            or marker.get("schema_version") != 1
            or marker.get("authority_root") != EXPECTED_AUTHORIZATION_ROOT
            or marker.get("source_evidence_hash") != source.get("evidence_hash")
            or marker.get("source_batch_fixture_market_hashes_sha256")
            != entry.get("source_batch_fixture_market_hashes_sha256")
            or marker.get("source_batch_fixture_market_hash_count") != 20
        ):
            raise ValueError("expected_post_marker_invalid")
        indexed[key] = copy.deepcopy({**entry, "expected_rewrite": rewrite_index[key]})
    if {sig: tuple(sorted(v for s, v in indexed if s == sig)) for sig in EXPECTED_SIGNATURES} != EXPECTED_SIGNATURES:
        raise ValueError("exact_signature_version_allowlist_required")
    reservations: dict[str, frozenset[str]] = {}
    commitments = {
        row.get("condition_signature"): row
        for row in document.get("historical_identity_reservations", [])
        if isinstance(row, dict)
    }
    if set(commitments) != set(EXPECTED_SIGNATURES):
        raise ValueError("reservation_signature_set_mismatch")
    for signature in EXPECTED_SIGNATURES:
        values = frozenset(
            item
            for (entry_signature, _), entry in indexed.items()
            if entry_signature == signature
            for item in entry["source_version"]["batch_fixture_market_hashes"]
        )
        commitment = commitments[signature]
        occurrences = sum(
            20 for entry_signature, _ in indexed if entry_signature == signature
        )
        if (
            len(values) != occurrences
            or commitment.get("source_occurrence_count") != occurrences
            or commitment.get("unique_identity_count") != len(values)
            or canonical_hash_v1({
                "condition_signature": signature,
                "fixture_market_hashes": sorted(values),
            }) != commitment.get("reservation_root")
        ):
            raise ValueError("historical_identity_reservation_mismatch")
        reservations[signature] = values
    if sum(len(entry["source_version"]["batch_fixture_market_hashes"]) for entry in indexed.values()) != 200:
        raise ValueError("exactly_200_source_occurrences_required")
    return ValidatedLegacyBatchCalculationContext(
        _CONTEXT_TOKEN, document, indexed, reservations,
    )


def derive_historical_reservations(
    authority_context: LegacyBatchAuthorityContext,
) -> dict[str, frozenset[str]]:
    return dict(require_authority_context(authority_context).reservations)


def _extract_calculation(authority: dict[str, Any]) -> dict[str, Any]:
    for key in ("sanitized_calculation_document", "calculation"):
        if isinstance(authority.get(key), dict):
            return authority[key]
    material = authority.get("immutable_material")
    if isinstance(material, dict) and isinstance(material.get("sanitized_calculation"), dict):
        return material["sanitized_calculation"]
    raise ValueError("complete_sanitized_calculation_not_packaged")


def validate_final_authority(
    document: Any, trusted_hash: str, runtime_identity: dict[str, Any] | None,
) -> LegacyBatchAuthorityContext:
    authority_keys = {
        "schema", "system", "migration_version", "sanitized_calculation",
        "implementation", "live_prestate", "runtime_coordination",
        "expected_poststate", "sanitized_calculation_document",
        "live_discovery_document", "authority_manifest_hash",
    }
    if (
        not isinstance(document, dict)
        or set(document) != authority_keys
        or document.get("schema") != AUTHORITY_SCHEMA
    ):
        raise ValueError("final_authority_schema_invalid")
    if not _hex(trusted_hash):
        raise ValueError("external_authority_pin_required")
    body = copy.deepcopy(document)
    claimed = body.pop("authority_manifest_hash", None)
    actual = canonical_hash_v1(body)
    if claimed != actual or trusted_hash != actual:
        raise ValueError("authority_manifest_hash_mismatch")
    if document.get("system") != "footbreak" or document.get("migration_version") != MIGRATION_VERSION:
        raise ValueError("authority_scope_invalid")
    calculation_document = document.get("sanitized_calculation_document")
    calculation = validate_sanitized_calculation(calculation_document)
    calculation_summary = document.get("sanitized_calculation")
    expected_calculation_summary = {
        "artifact_hash": calculation_document["calculation_artifact_hash"],
        "authorization_root": calculation_document["authorization_root"],
        "pre_registry_projection_sha256": calculation_document[
            "authorization_body"
        ]["source"]["registry_projection_sha256"],
        "expected_post_condition_registry_sha256": calculation_document[
            "expected_post_condition_registry_sha256"
        ],
        "expected_post_export_registry_payload_sha256": calculation_document[
            "expected_post_export_registry_payload_sha256"
        ],
        "production_identity_manifest_hash": calculation_document[
            "authorization_body"
        ]["source"]["production_identity_manifest_hash"],
    }
    if calculation_summary != expected_calculation_summary:
        raise ValueError("authority_calculation_summary_mismatch")
    discovery = validate_live_discovery(
        document.get("live_discovery_document"), calculation,
    )
    if discovery.get("migration_ready") is not True:
        raise ValueError("authority_discovery_not_migration_ready")
    implementation = document.get("implementation")
    if not isinstance(implementation, dict) or not isinstance(runtime_identity, dict):
        raise ValueError("runtime_identity_required")
    implementation_keys = {
        "repository", "release_commit", "git_tree",
        "deployable_artifact_sha256", "module_manifest",
        "module_manifest_root", "python_executable_sha256",
        "import_roots", "sys_path_sha256", "working_tree_policy",
    }
    if (
        set(implementation) != implementation_keys
        or implementation.get("repository") != REPOSITORY_ID
        or not isinstance(implementation.get("module_manifest"), list)
        or len(implementation["module_manifest"]) != len(REQUIRED_RUNTIME_MODULES)
        or {row.get("path") for row in implementation["module_manifest"]}
        != set(REQUIRED_RUNTIME_MODULES.values())
        or canonical_hash_v1(implementation["module_manifest"])
        != implementation.get("module_manifest_root")
    ):
        raise ValueError("runtime_implementation_schema_invalid")
    for key in (
        "release_commit", "git_tree", "deployable_artifact_sha256",
        "module_manifest_root", "python_executable_sha256", "import_roots",
        "repository", "sys_path_sha256",
    ):
        if implementation.get(key) != runtime_identity.get(key):
            raise ValueError(f"runtime_identity_mismatch:{key}")
    if runtime_identity.get("working_tree_policy") != "clean_tracked_no_shadow_files":
        raise ValueError("runtime_tree_not_clean")
    if implementation.get("module_manifest") != runtime_identity.get("module_manifest"):
        raise ValueError("runtime_module_manifest_mismatch")
    if implementation != discovery.get("execution_identity"):
        raise ValueError("authority_implementation_discovery_mismatch")
    expected_live_prestate = {
        "discovery_document_sha256": discovery["discovery_document_sha256"],
        "full_ledger_sha256": discovery["capture"]["full_pre_ledger_sha256"],
        "ledger_object": discovery["capture"]["ledger_object"],
        "stable_registry_projection_sha256": discovery["capture"][
            "stable_registry_projection_sha256"
        ],
        "reference_inventory_root": discovery["reference_inventory_root"],
        "source_support_root": discovery["source_support_root"],
        "prerequisite_state_root": discovery["prerequisites"]["root"],
        "pending_state_root": discovery["pending_state"]["root"],
        "rollover_audit_root": discovery["rollover_audit"]["root"],
        "namespace_audit_root": discovery["namespace_audit_root"],
        "stats_conditions_root": discovery["stats_conditions"].get("pre_tree_sha256"),
    }
    if document.get("live_prestate") != expected_live_prestate:
        raise ValueError("authority_live_prestate_discovery_mismatch")
    if document.get("runtime_coordination") != discovery.get("writer_coordination"):
        raise ValueError("authority_runtime_coordination_discovery_mismatch")
    if document.get("expected_poststate") != discovery.get("expected_post"):
        raise ValueError("authority_expected_poststate_discovery_mismatch")
    return LegacyBatchAuthorityContext(_CONTEXT_TOKEN, document, calculation, actual)


def assemble_final_authority_candidate(
    calculation_document: dict[str, Any],
    discovery_document: dict[str, Any],
) -> dict[str, Any]:
    """Assemble an unsigned candidate; this never returns an authority context."""
    calculation = validate_sanitized_calculation(calculation_document)
    discovery = validate_live_discovery(discovery_document, calculation)
    summary = {
        "artifact_hash": calculation_document["calculation_artifact_hash"],
        "authorization_root": calculation_document["authorization_root"],
        "pre_registry_projection_sha256": calculation_document[
            "authorization_body"
        ]["source"]["registry_projection_sha256"],
        "expected_post_condition_registry_sha256": calculation_document[
            "expected_post_condition_registry_sha256"
        ],
        "expected_post_export_registry_payload_sha256": calculation_document[
            "expected_post_export_registry_payload_sha256"
        ],
        "production_identity_manifest_hash": calculation_document[
            "authorization_body"
        ]["source"]["production_identity_manifest_hash"],
    }
    live = {
        "discovery_document_sha256": discovery["discovery_document_sha256"],
        "full_ledger_sha256": discovery["capture"]["full_pre_ledger_sha256"],
        "ledger_object": discovery["capture"]["ledger_object"],
        "stable_registry_projection_sha256": discovery["capture"][
            "stable_registry_projection_sha256"
        ],
        "reference_inventory_root": discovery["reference_inventory_root"],
        "source_support_root": discovery["source_support_root"],
        "prerequisite_state_root": discovery["prerequisites"]["root"],
        "pending_state_root": discovery["pending_state"]["root"],
        "rollover_audit_root": discovery["rollover_audit"]["root"],
        "namespace_audit_root": discovery["namespace_audit_root"],
        "stats_conditions_root": discovery["stats_conditions"].get(
            "pre_tree_sha256"
        ),
    }
    body = {
        "schema": AUTHORITY_SCHEMA,
        "system": "footbreak",
        "migration_version": MIGRATION_VERSION,
        "sanitized_calculation": summary,
        "implementation": copy.deepcopy(discovery["execution_identity"]),
        "live_prestate": live,
        "runtime_coordination": copy.deepcopy(discovery["writer_coordination"]),
        "expected_poststate": copy.deepcopy(discovery["expected_post"]),
        "sanitized_calculation_document": copy.deepcopy(calculation_document),
        "live_discovery_document": copy.deepcopy(discovery),
    }
    return {**body, "authority_manifest_hash": canonical_hash_v1(body)}


def load_legacy_batch_authority(
    authority_path: str | os.PathLike[str], trusted_manifest_hash: str,
    detached_signature: dict[str, Any] | None,
    release_identity: dict[str, Any],
) -> LegacyBatchAuthorityContext:
    raw = Path(authority_path).read_bytes()
    document = parse_json_bytes_v1(raw)
    # A detached signature is optional when an independent exact hash pin is
    # supplied.  If supplied, validate its structure and domain preimage.
    if detached_signature is not None:
        expected_keys = {
            "schema", "algorithm", "authority_manifest_hash", "approver_key_id",
            "approved_at", "signature_base64",
        }
        if (
            not isinstance(detached_signature, dict)
            or set(detached_signature) != expected_keys
            or detached_signature.get("schema")
            != "footbreak-legacy-batch-authority-signature-v1"
            or detached_signature.get("algorithm") != "ed25519"
            or detached_signature.get("authority_manifest_hash") != trusted_manifest_hash
        ):
            raise ValueError("detached_authority_signature_invalid")
        try:
            signature_bytes = base64.b64decode(
                detached_signature["signature_base64"], validate=True,
            )
        except Exception as exc:
            raise ValueError("detached_authority_signature_invalid") from exc
        public_keys = release_identity.get("trusted_approver_public_keys")
        encoded_key = (
            public_keys.get(detached_signature["approver_key_id"])
            if isinstance(public_keys, dict) else None
        )
        if not isinstance(encoded_key, str):
            raise ValueError("detached_authority_public_key_unavailable")
        try:
            from cryptography.hazmat.primitives.asymmetric.ed25519 import (
                Ed25519PublicKey,
            )
            public_key = Ed25519PublicKey.from_public_bytes(
                base64.b64decode(encoded_key, validate=True)
            )
            preimage = (
                AUTHORITY_SCHEMA.encode("ascii")
                + b"\0"
                + bytes.fromhex(trusted_manifest_hash)
            )
            public_key.verify(signature_bytes, preimage)
        except Exception as exc:
            raise ValueError("detached_authority_signature_invalid") from exc
    return validate_final_authority(document, trusted_manifest_hash, release_identity)


def authority_entry_for(
    authority_context: LegacyBatchAuthorityContext | None,
    system: str, signature: str, version: int,
) -> dict[str, Any] | None:
    if authority_context is None:
        return None
    context = require_authority_context(authority_context)
    if system != "footbreak":
        return None
    return context.entries.get((signature, version))


def validate_aggregate_version(
    row: dict[str, Any], system: str, signature: str,
    authority_context: LegacyBatchAuthorityContext | None,
) -> str | None:
    marker = row.get("legacy_ordinary_batch_aggregate")
    if marker is None:
        return None
    if authority_context is None:
        return "authority_required"
    try:
        entry = authority_entry_for(
            authority_context, system, signature, int(row.get("version")),
        )
    except (TypeError, ValueError):
        return "authority_mismatch"
    if entry is None:
        return "unauthorized_legacy_batch_aggregate"
    expected = entry["expected_rewrite"]["expected_post_version"]
    return None if row == expected else "authority_mismatch"


def _iter_formal_rows(ledger: dict[str, Any]) -> Iterator[tuple[str, dict[str, Any]]]:
    ns = ledger.get("wilson_validation")
    for root_name, rows in (
        ("bets", ledger.get("bets")),
        ("wilson_validation/observations", ns.get("observations") if isinstance(ns, dict) else None),
    ):
        if not isinstance(rows, list):
            continue
        for index, row in enumerate(rows):
            if (
                isinstance(row, dict)
                and row.get("strategy") == "wilson-test-strategy-v1"
            ):
                yield f"/{root_name}/{index}", row


def build_global_formal_wilson_index(
    ledger: dict[str, Any],
    calculation_context: ValidatedLegacyBatchCalculationContext,
) -> dict[tuple[str, str], list[dict[str, Any]]]:
    _require_calculation_context(calculation_context)
    from analysis import wilson_validation as wv

    index: dict[tuple[str, str], list[dict[str, Any]]] = {}
    ns = ledger.get(wv.NAMESPACE)
    conditions = ns.get("conditions") if isinstance(ns, dict) else None
    if not isinstance(conditions, dict):
        raise ValueError("formal_index_registry_unavailable")
    for pointer, row in _iter_formal_rows(ledger):
        signature = row.get("frozen_condition_signature")
        marker = row.get("rollover_provenance")
        fixture_hash = marker.get("fixture_market_hash") if isinstance(marker, dict) else None
        if not isinstance(signature, str) or signature not in conditions or not _hex(fixture_hash):
            raise ValueError(f"malformed_global_formal_row:{pointer}")
        frozen = conditions[signature]
        admitted, reason = wv.validate_formal_row(
            row, system="footbreak", signature=signature, frozen=frozen,
            projection_time=wv._time("9999-12-31T23:59:59+00:00"),
            require_settled=row.get("status") == "SETTLED", ledger=ledger,
        )
        if admitted is None:
            raise ValueError(f"malformed_global_formal_row:{pointer}:{reason}")
        item = {
            "pointer": pointer, "row": row, "canonical_hash": canonical_hash_v1(row),
            "signature": signature, "fixture_market_hash": fixture_hash,
            "stage_at": marker.get("stage_at"), "result": row.get("result"),
        }
        index.setdefault((signature, fixture_hash), []).append(item)
    return index


def classify_authority_source_support(
    index: dict[tuple[str, str], list[dict[str, Any]]],
    entry: dict[str, Any],
    calculation_context: ValidatedLegacyBatchCalculationContext,
) -> dict[str, Any]:
    _require_calculation_context(calculation_context)
    key = _entry_key(entry)
    if key not in calculation_context.entries:
        raise ValueError("entry_not_from_calculation_context")
    source = entry["source_version"]
    wanted = source["batch_fixture_market_hashes"]
    matches = [index.get((entry["condition_signature"], value), []) for value in wanted]
    counts = [len(rows) for rows in matches]
    found = sum(bool(rows) for rows in matches)
    duplicates = sum(count > 1 for count in counts)
    classification = (
        "zero_exact" if found == 0
        else "duplicate_or_conflicting" if duplicates
        else "partial" if found < 20
        else "all_20_exact"
    )
    ordered = [
        rows[0]["fixture_market_hash"] for rows in matches if len(rows) == 1
    ]
    hits = sum(
        rows[0]["result"] in {"Won", "Half Won"} for rows in matches if len(rows) == 1
    )
    if classification == "all_20_exact":
        condition = next(
            row for row in calculation_context.document[
                "expected_post_condition_registry_scope"
            ]["conditions"]
            if row["signature"] == entry["condition_signature"]
        )
        predecessor_boundary = condition["evidence_versions"][
            source["version"] - 2
        ]["activation_boundary_at"]
        from analysis import wilson_validation as wv
        chronological = sorted(
            (rows[0] for rows in matches),
            key=lambda item: (
                wv._time(item.get("stage_at")), item["fixture_market_hash"],
            ),
        )
        if (
            [item["fixture_market_hash"] for item in chronological] != wanted
            or hits != source.get("batch_hits")
            or chronological[-1].get("stage_at") != source.get("activation_boundary_at")
            or any(
                wv._time(item.get("stage_at")) is None
                or not wv._strictly_after(
                    item.get("stage_at"), predecessor_boundary,
                )
                for item in chronological
            )
        ):
            classification = "mixed"
    return {
        "condition_signature": entry["condition_signature"],
        "version": source["version"],
        "wanted_identity_count": 20,
        "wanted_identities_found": found,
        "valid_exact_row_count": sum(counts),
        "duplicate_identity_count": duplicates,
        "ordered_identity_count": len(ordered),
        "hits": hits,
        "classification": classification,
        "migration_ready": classification == "zero_exact",
    }


def walk_evidence_hash_occurrences(
    ledger: Any, old_to_new: dict[str, str],
) -> list[dict[str, Any]]:
    values = set(old_to_new) | set(old_to_new.values())
    found: list[dict[str, Any]] = []

    def walk(value: Any, pointer: str, parent: Any, parent_key: Any = None) -> None:
        if isinstance(value, str) and value in values:
            immutable_parent = parent
            if isinstance(parent, dict) and isinstance(parent_key, str):
                immutable_parent = {
                    key: item for key, item in parent.items()
                    if key != parent_key
                }
            found.append({
                "json_pointer": pointer or "",
                "value": value,
                "kind": "old" if value in old_to_new else "new",
                "parent_object_sha256": canonical_hash_v1(parent),
                "immutable_content_sha256": canonical_hash_v1(immutable_parent),
            })
        elif isinstance(value, dict):
            for key, item in value.items():
                walk(item, f"{pointer}/{_pointer_token(key)}", value, key)
        elif isinstance(value, list):
            for index, item in enumerate(value):
                walk(item, f"{pointer}/{index}", value, index)

    walk(ledger, "", None)
    return sorted(found, key=lambda item: (item["json_pointer"], item["value"]))


def classify_reference_occurrence(
    occurrence: dict[str, Any], *, expected_paths: dict[str, dict[str, Any]],
) -> dict[str, Any]:
    expected = expected_paths.get(occurrence["json_pointer"])
    if expected is None or occurrence["value"] != expected.get("value"):
        raise ValueError(f"unclassified_old_hash_reference:{occurrence['json_pointer']}")
    return {**occurrence, **copy.deepcopy(expected)}


def _classify_inventory(
    inventory: list[dict[str, Any]], old_to_new: dict[str, str],
) -> list[dict[str, Any]]:
    output = []
    for item in inventory:
        path = item["json_pointer"]
        if path.endswith(
            "/legacy_ordinary_batch_aggregate/source_evidence_hash"
        ):
            classification = "authenticated_historical_preimage"
            target = item["value"]
        elif "/rollover_audit/" in path:
            classification = "rollover_audit_mirror"
            target = old_to_new.get(item["value"], item["value"])
        elif path.startswith("/wilson_validation/stats/conditions/"):
            classification = "stats_conditions"
            target = old_to_new.get(item["value"], item["value"])
        elif "/evidence_versions/" in path and path.endswith("/evidence_hash"):
            classification = "authoritative_chain_evidence_hash"
            target = old_to_new.get(item["value"], item["value"])
        elif path.endswith("/prior_evidence_hash"):
            classification = "downstream_prior_evidence_hash"
            target = old_to_new.get(item["value"], item["value"])
        elif "/active_evidence" in path:
            classification = "active_pointer"
            target = old_to_new.get(item["value"], item["value"])
        elif path.startswith("/bets/") or path.startswith(
            "/wilson_validation/observations/"
        ):
            classification = "formal_row"
            target = old_to_new.get(item["value"], item["value"])
        elif path.startswith("/wilson_validation/audit/"):
            classification = "namespace_audit"
            target = old_to_new.get(item["value"], item["value"])
        else:
            raise ValueError(f"unclassified_old_hash_reference:{path}")
        output.append({
            **item, "classification": classification,
            "expected_rewrite_target": target,
        })
    return output


def _active_projection(version: dict[str, Any]) -> dict[str, Any]:
    return {
        key: copy.deepcopy(version.get(key)) for key in (
            "version", "cumulative_hits", "cumulative_decided",
            "wilson95_lower_raw", "minimum_acceptable_odds_raw",
            "minimum_acceptable_odds_display", "activation_boundary_at",
            "created_at", "evidence_hash",
        )
    }


def _stats_conditions_projection(conditions: dict[str, Any]) -> dict[str, Any]:
    """Canonical cache copy: authoritative chains, never rollover mirrors."""
    projected = copy.deepcopy(conditions)
    for frozen in projected.values():
        if isinstance(frozen, dict):
            frozen.pop("rollover_audit", None)
    return projected


def _authority_neutral_manifest_payload(ledger: dict[str, Any]) -> dict[str, Any]:
    ns = ledger["wilson_validation"]
    return {
        "schema": "wilson-authority-neutral-manifest-payload-v1",
        "system": "footbreak",
        "condition_order": copy.deepcopy(ns["condition_order"]),
        "conditions": [{
            "condition_signature": signature,
            "condition_number": ns["conditions"][signature]["condition_number"],
            "active_evidence_version": ns["conditions"][signature][
                "active_evidence_version"
            ],
            "active_evidence_hash": ns["conditions"][signature][
                "active_evidence_hash"
            ],
        } for signature in ns["condition_order"]],
    }


def _condition_funnel_semantic_payload(ledger: dict[str, Any]) -> dict[str, Any]:
    ns = ledger["wilson_validation"]
    return {
        "schema": "wilson-condition-funnel-semantic-payload-v1",
        "system": "footbreak",
        "conditions": {
            signature: {
                "active_evidence_version": frozen["active_evidence_version"],
                "active_evidence_hash": frozen["active_evidence_hash"],
                "pending_rollover_progress": copy.deepcopy(
                    frozen.get("pending_rollover_progress")
                ),
            }
            for signature, frozen in ns["conditions"].items()
        },
    }


def _old_to_new(context: ValidatedLegacyBatchCalculationContext) -> dict[str, str]:
    return {
        entry["source_version"]["evidence_hash"]:
        entry["expected_rewrite"]["expected_evidence_hash"]
        for entry in context.entries.values()
    }


def _replace_scalar_references(
    value: Any, old_to_new: dict[str, str], pointer: str = "",
) -> list[str]:
    """Rewrite only explicitly supported reference shapes."""
    changed: list[str] = []
    if isinstance(value, dict):
        for key, item in list(value.items()):
            child = f"{pointer}/{_pointer_token(key)}"
            if isinstance(item, str) and item in old_to_new and key in {
                "prior_evidence_hash", "active_evidence_hash", "evidence_hash",
                "admitted_evidence_hash",
            }:
                value[key] = old_to_new[item]
                changed.append(child)
            else:
                changed.extend(_replace_scalar_references(item, old_to_new, child))
    elif isinstance(value, list):
        for index, item in enumerate(value):
            changed.extend(_replace_scalar_references(item, old_to_new, f"{pointer}/{index}"))
    return changed


def plan_disposable_poststate(
    ledger_copy: dict[str, Any],
    calculation_context: ValidatedLegacyBatchCalculationContext,
) -> dict[str, Any]:
    context = _require_calculation_context(calculation_context)
    from analysis import wilson_validation as wv

    if not isinstance(ledger_copy, dict):
        raise ValueError("ledger_not_object")
    ledger = copy.deepcopy(ledger_copy)
    ns = ledger.get(wv.NAMESPACE)
    if not isinstance(ns, dict) or ns.get("system") != "footbreak":
        raise ValueError("footbreak_namespace_required")
    conditions = ns.get("conditions")
    if not isinstance(conditions, dict):
        raise ValueError("conditions_unavailable")
    old_to_new = _old_to_new(context)
    pre_occurrences = walk_evidence_hash_occurrences(ledger, old_to_new)
    if any(item["kind"] == "new" for item in pre_occurrences):
        raise ValueError("proposed_new_hash_in_prestate")
    converted_paths: list[str] = []
    for (signature, version_number), entry in sorted(
        context.entries.items(), key=lambda item: (
            item[1]["condition_number"], item[0][1],
        ),
    ):
        frozen = conditions.get(signature)
        versions = frozen.get("evidence_versions") if isinstance(frozen, dict) else None
        if not isinstance(versions, list) or len(versions) < version_number:
            raise ValueError("authorized_source_version_missing")
        observed = versions[version_number - 1]
        if observed != entry["source_version"]:
            raise ValueError(f"authorized_source_preimage_mismatch:{signature}:{version_number}")
        versions[version_number - 1] = copy.deepcopy(
            entry["expected_rewrite"]["expected_post_version"]
        )
        converted_paths.append(
            f"/wilson_validation/conditions/{_pointer_token(signature)}"
            f"/evidence_versions/{version_number - 1}"
        )
    # Rehash every affected downstream suffix in ascending order.  The ten
    # approved postimages are fixed by the calculation; any later ordinary
    # version is preserved except for its predecessor and derived hash.
    rewritten_paths: list[str] = []
    for signature in EXPECTED_SIGNATURES:
        frozen = conditions[signature]
        pre_frozen = ledger_copy[wv.NAMESPACE]["conditions"][signature]
        versions = frozen["evidence_versions"]
        pre_versions = pre_frozen["evidence_versions"]
        first_downstream_index = max(EXPECTED_SIGNATURES[signature])
        for index in range(first_downstream_index, len(versions)):
            previous = versions[index - 1]
            current = versions[index]
            pre_current = pre_versions[index]
            if current.get("prior_evidence_hash") != previous["evidence_hash"]:
                if pre_current.get("prior_evidence_hash") != pre_versions[index - 1].get(
                    "evidence_hash"
                ):
                    raise ValueError("downstream_prior_pointer_preimage_mismatch")
                current["prior_evidence_hash"] = previous["evidence_hash"]
                old_hash = current.get("evidence_hash")
                current["evidence_hash"] = wv._version_hash(current)
                if _hex(old_hash) and old_hash != current["evidence_hash"]:
                    old_to_new[old_hash] = current["evidence_hash"]
                rewritten_paths.extend([
                    f"/wilson_validation/conditions/{_pointer_token(signature)}"
                    f"/evidence_versions/{index}/prior_evidence_hash",
                    f"/wilson_validation/conditions/{_pointer_token(signature)}"
                    f"/evidence_versions/{index}/evidence_hash",
                ])
            if (
                "legacy_ordinary_batch_aggregate" not in current
                and set(current.get("batch_fixture_market_hashes") or []).intersection(
                    context.reservations[signature]
                )
            ):
                raise ValueError("reserved_historical_identity_reused")
        active_number = frozen["active_evidence_version"]
        active = frozen["evidence_versions"][active_number - 1]
        frozen["active_evidence_hash"] = active["evidence_hash"]
        frozen["active_evidence"] = _active_projection(active)
        rewritten_paths.extend([
            f"/wilson_validation/conditions/{_pointer_token(signature)}/active_evidence_hash",
            f"/wilson_validation/conditions/{_pointer_token(signature)}"
            f"/active_evidence/evidence_hash",
        ])
        audit = frozen.get("rollover_audit")
        if audit is not None:
            expected_pre = ledger_copy[wv.NAMESPACE]["conditions"][signature][
                "evidence_versions"
            ][1:][-64:]
            if audit != expected_pre:
                raise ValueError(f"rollover_audit_not_exact_retained_suffix:{signature}")
            frozen["rollover_audit"] = copy.deepcopy(
                frozen["evidence_versions"][1:][-64:]
            )
    # Formal rows are rewritten only when all three authenticated fields agree.
    for pointer, row in _iter_formal_rows(ledger):
        values = (
            row.get("evidence_hash"),
            (row.get("frozen_historical_evidence") or {}).get("evidence_hash"),
            (row.get("rollover_provenance") or {}).get("admitted_evidence_hash"),
        )
        affected = [value in old_to_new for value in values]
        if any(affected):
            if not all(affected) or len(set(values)) != 1:
                raise ValueError(f"partial_formal_evidence_binding:{pointer}")
            replacement = old_to_new[values[0]]
            row["evidence_hash"] = replacement
            row["frozen_historical_evidence"]["evidence_hash"] = replacement
            row["rollover_provenance"]["admitted_evidence_hash"] = replacement
            rewritten_paths.extend([
                pointer + "/evidence_hash",
                pointer + "/frozen_historical_evidence/evidence_hash",
                pointer + "/rollover_provenance/admitted_evidence_hash",
            ])
    # Namespace audit rows admit exactly one mutable hash leaf.
    for index, audit_row in enumerate(ns.get("audit") or []):
        binding = audit_row.get("exact_match_binding") if isinstance(audit_row, dict) else None
        if isinstance(binding, dict) and binding.get("evidence_hash") in old_to_new:
            binding["evidence_hash"] = old_to_new[binding["evidence_hash"]]
            rewritten_paths.append(
                f"/wilson_validation/audit/{index}/exact_match_binding/evidence_hash"
            )
    stats = ns.get("stats")
    if isinstance(stats, dict) and "conditions" in stats:
        pre_conditions = ledger_copy[wv.NAMESPACE]["conditions"]
        if stats["conditions"] != _stats_conditions_projection(pre_conditions):
            raise ValueError("stats_conditions_not_exact_canonical_copy")
        stats["conditions"] = _stats_conditions_projection(conditions)
    # A recursive scan is a closed-world safety check. Old hashes may remain
    # only as authenticated marker source hashes.
    post_occurrences = walk_evidence_hash_occurrences(ledger, old_to_new)
    for occurrence in post_occurrences:
        if occurrence["kind"] == "old" and not occurrence["json_pointer"].endswith(
            "/legacy_ordinary_batch_aggregate/source_evidence_hash"
        ):
            raise ValueError(
                f"unclassified_old_hash_reference:{occurrence['json_pointer']}"
            )
    return {
        "domain": "discovery-only",
        "ledger": ledger,
        "serialized_bytes": serialize_ledger_bytes_v1(ledger),
        "post_ledger_sha256": hashlib.sha256(serialize_ledger_bytes_v1(ledger)).hexdigest(),
        "pre_reference_inventory": pre_occurrences,
        "post_reference_inventory": post_occurrences,
        "converted_version_paths": converted_paths,
        "rewritten_scalar_paths": sorted(set(rewritten_paths)),
    }


def apply_reference_rewrites(
    ledger: dict[str, Any], authority_context: LegacyBatchAuthorityContext,
) -> dict[str, Any]:
    context = require_authority_context(authority_context)
    result = plan_disposable_poststate(ledger, context.calculation)
    return result["ledger"]


def _stable_registry_projection(ledger: dict[str, Any]) -> dict[str, Any]:
    ns = ledger["wilson_validation"]
    metadata_keys = {
        "schema_version", "system", "activation_at", "cutover_at",
        "rollover_migration_at", "granular_ranking_initial_migration_completed_at",
        "granular_ranking_initial_migration_version",
        "quarter_settlement_activation_at",
    }
    return {
        "system": ns.get("system"),
        "namespace_metadata": {
            key: copy.deepcopy(ns[key]) for key in metadata_keys if key in ns
        },
        "condition_order": copy.deepcopy(ns.get("condition_order")),
        "conditions": copy.deepcopy(ns.get("conditions")),
        "production_identity_manifest": copy.deepcopy(
            ns.get("production_identity_manifest")
        ),
    }


def _prove_stable_calculation_registry(
    ledger: dict[str, Any],
    context: ValidatedLegacyBatchCalculationContext,
) -> None:
    """Prove all sanitized registry fields equal the calculation preimage."""
    expected_scope = copy.deepcopy(
        context.document["expected_post_condition_registry_scope"]
    )
    expected_rows = {
        row["signature"]: row for row in expected_scope["conditions"]
    }
    for (signature, version), entry in context.entries.items():
        expected_rows[signature]["evidence_versions"][version - 1] = copy.deepcopy(
            entry["source_version"]
        )
    for signature, row in expected_rows.items():
        active_number = row["active_evidence_version"]
        active = row["evidence_versions"][active_number - 1]
        row["active_evidence_hash"] = active["evidence_hash"]
        row["active_evidence"] = _active_projection(active)
    ns = ledger.get("wilson_validation")
    if not isinstance(ns, dict):
        raise ValueError("stable_registry_namespace_unavailable")
    condition_keys = {
        "signature", "condition_number", "definition", "historical_evidence",
        "evidence_versions", "active_evidence_version", "active_evidence_hash",
        "active_evidence",
    }
    observed_rows = []
    for signature in ns.get("condition_order") or []:
        row = ns.get("conditions", {}).get(signature)
        if not isinstance(row, dict):
            raise ValueError("stable_registry_condition_unavailable")
        observed_rows.append({
            key: copy.deepcopy(row[key]) for key in condition_keys if key in row
        })
    if (
        ns.get("condition_order") != expected_scope["condition_order"]
        or observed_rows != expected_scope["conditions"]
        or ns.get("production_identity_manifest")
        != expected_scope["production_identity_manifest"]
        or {
            key: ns.get(key)
            for key in expected_scope["namespace_metadata"]
        } != expected_scope["namespace_metadata"]
    ):
        raise ValueError("stable_registry_projection_mismatch")


def build_live_discovery(
    ledger: dict[str, Any],
    calculation_context: ValidatedLegacyBatchCalculationContext,
    *,
    raw_ledger_bytes: bytes | None = None,
    capture: dict[str, Any] | None = None,
    execution_identity: dict[str, Any] | None = None,
    writer_coordination: dict[str, Any] | None = None,
) -> dict[str, Any]:
    context = _require_calculation_context(calculation_context)
    _prove_stable_calculation_registry(ledger, context)
    source_bytes = raw_ledger_bytes if raw_ledger_bytes is not None else serialize_ledger_bytes_v1(ledger)
    index = build_global_formal_wilson_index(ledger, context)
    support = [
        classify_authority_source_support(index, entry, context)
        for _, entry in sorted(context.entries.items())
    ]
    if any(item["classification"] != "zero_exact" for item in support):
        # Discovery remains useful evidence, but explicitly cannot be used to
        # mint an apply-ready authority.
        migration_ready = False
    else:
        migration_ready = True
    planned = plan_disposable_poststate(ledger, context)
    old_to_new = _old_to_new(context)
    pre_inventory = _classify_inventory(
        planned["pre_reference_inventory"], old_to_new,
    )
    post_inventory = _classify_inventory(
        planned["post_reference_inventory"], old_to_new,
    )
    from analysis import wilson_validation as wv
    identity_manifest, identity_reason = (
        wv.validate_production_identity_manifest_v1(
            ledger["wilson_validation"], "footbreak",
        )
    )
    if identity_manifest is None:
        raise ValueError(identity_reason or "production_identity_v1_invalid")
    retired, retirement_reason = wv._validate_condition_identity_migrations(
        ledger, "footbreak",
    )
    if retired is None:
        raise ValueError(retirement_reason or "condition_identity_retirement_invalid")
    post_formal = dict(_iter_formal_rows(planned["ledger"]))
    formal_rows = []
    for pointer, row in _iter_formal_rows(ledger):
        marker = row.get("rollover_provenance")
        history = row.get("frozen_historical_evidence")
        values = (
            row.get("evidence_hash"),
            history.get("evidence_hash") if isinstance(history, dict) else None,
            marker.get("admitted_evidence_hash") if isinstance(marker, dict) else None,
        )
        if len(set(values)) != 1:
            raise ValueError(f"partial_formal_evidence_binding:{pointer}")
        formal_rows.append({
            "json_pointer": pointer,
            "row_sha256": canonical_hash_v1(row),
            "immutable_content_sha256": canonical_hash_v1({
                key: value for key, value in row.items()
                if key not in {"evidence_hash", "frozen_historical_evidence",
                               "rollover_provenance"}
            }),
            "status": row.get("status"),
            "condition_signature": row.get("frozen_condition_signature"),
            "evidence_version": row.get("evidence_version"),
            "definition_sha256": canonical_hash_v1(
                row.get("frozen_condition_definition")
            ),
            "fixture_market_hash": (
                marker.get("fixture_market_hash") if isinstance(marker, dict) else None
            ),
            "stage_at": marker.get("stage_at") if isinstance(marker, dict) else None,
            "three_hash_fields_agree": True,
            "expected_post_row_sha256": canonical_hash_v1(post_formal[pointer]),
        })
    ns = ledger["wilson_validation"]
    namespace_audit = []
    post_audit = planned["ledger"]["wilson_validation"].get("audit") or []
    for index_number, row in enumerate(ns.get("audit") or []):
        if isinstance(row, dict) and isinstance(row.get("exact_match_binding"), dict):
            binding = row["exact_match_binding"]
            signature = binding.get("condition_signature")
            version = binding.get("evidence_version")
            frozen = ns["conditions"].get(signature)
            versions = frozen.get("evidence_versions") if isinstance(frozen, dict) else None
            if (
                not isinstance(version, int) or isinstance(version, bool)
                or not isinstance(versions, list) or not 1 <= version <= len(versions)
                or binding.get("evidence_hash")
                != versions[version - 1].get("evidence_hash")
                or binding.get("definition_hash")
                != canonical_hash_v1(frozen.get("definition"))
                or binding.get("fixture_market_hash") is None
                or binding.get("native_stage_at") is None
            ):
                raise ValueError(f"invalid_namespace_audit_binding:{index_number}")
            namespace_audit.append({
                "json_pointer": f"/wilson_validation/audit/{index_number}/exact_match_binding",
                "row_sha256": canonical_hash_v1(row),
                "binding_sha256": canonical_hash_v1(binding),
                "row_without_evidence_hash_sha256": canonical_hash_v1({
                    **row,
                    "exact_match_binding": {
                        key: value for key, value in binding.items()
                        if key != "evidence_hash"
                    },
                }),
                "expected_post_row_sha256": canonical_hash_v1(post_audit[index_number]),
            })
    rollover_conditions = []
    for signature in ns["condition_order"]:
        frozen = ns["conditions"][signature]
        observed = frozen.get("rollover_audit")
        expected = frozen["evidence_versions"][1:][-64:]
        if observed != expected:
            raise ValueError(f"rollover_audit_not_exact_retained_suffix:{signature}")
        post_expected = planned["ledger"]["wilson_validation"]["conditions"][
            signature
        ]["evidence_versions"][1:][-64:]
        rollover_conditions.append({
            "condition_signature": signature,
            "observed_version_sequence": [row["version"] for row in observed],
            "expected_version_sequence": [row["version"] for row in expected],
            "pre_entry_hashes": [canonical_hash_v1(row) for row in observed],
            "expected_post_entry_hashes": [
                canonical_hash_v1(row) for row in post_expected
            ],
            "affected_entry_count": sum(
                row.get("evidence_hash") in old_to_new for row in observed
            ),
            "marker_copy_paths": [
                f"/wilson_validation/conditions/{_pointer_token(signature)}"
                f"/rollover_audit/{index}/legacy_ordinary_batch_aggregate/source_evidence_hash"
                for index, row in enumerate(post_expected)
                if "legacy_ordinary_batch_aggregate" in row
            ],
        })
    stats_state = "absent"
    stats_payload: dict[str, Any] = {"classification": stats_state}
    if isinstance(ns.get("stats"), dict) and "conditions" in ns["stats"]:
        if ns["stats"]["conditions"] != _stats_conditions_projection(
            ns["conditions"]
        ):
            raise ValueError("stats_conditions_not_exact_canonical_copy")
        stats_payload = {
            "classification": "exact_canonical_copy",
            "pre_tree_sha256": canonical_hash_v1(ns["stats"]["conditions"]),
            "authoritative_conditions_sha256": canonical_hash_v1(
                _stats_conditions_projection(ns["conditions"])
            ),
            "expected_post_tree_sha256": canonical_hash_v1(
                planned["ledger"]["wilson_validation"]["stats"]["conditions"]
            ),
        }
    binding_document = ns.get("formal_binding_omissions_migration_v1")
    binding_valid = binding_document is None
    if binding_document is not None:
        try:
            from analysis.migrate_wilson_formal_bindings import (
                _validate_existing_audit,
            )
            _validate_existing_audit(binding_document, "footbreak")
            binding_valid = True
        except Exception as exc:
            raise ValueError("formal_binding_omissions_prerequisite_invalid") from exc
    recovery_document = ns.get("formal_observation_recovery_v1")
    recovery_valid = (
        recovery_document is None
        or (
            isinstance(recovery_document, dict)
            and (
                recovery_document.get("completed") is True
                or recovery_document.get("closed") is True
            )
        )
    )
    if not recovery_valid:
        raise ValueError("formal_observation_recovery_prerequisite_invalid")
    strict_formal_rows = [item["row"] for rows in index.values() for item in rows]
    pending_semantics = {}
    for signature, frozen in ns["conditions"].items():
        if signature in retired:
            eligible, excluded = [], {
                "missing_or_invalid_provenance": 0,
                "before_snapshot_boundary": 0,
                "not_binary_decided": 0,
                "duplicate_or_conflicting_fixture_market": 0,
            }
        else:
            eligible, excluded = wv._eligible_rollover_rows(
                strict_formal_rows, "footbreak", signature,
                frozen["evidence_versions"][-1],
                context.reservations.get(signature, frozenset()),
            )
        semantic = {
            "eligible_decided": len(eligible),
            "eligible_hits": sum(int(item["hit"]) for item in eligible),
            "required": 20,
            "display": f"{len(eligible)}/20",
            "excluded": excluded,
        }
        persisted = frozen.get("pending_rollover_progress")
        if not isinstance(persisted, dict) or any(
            persisted.get(key) != value for key, value in semantic.items()
        ):
            raise ValueError(f"pending_semantic_mismatch:{signature}")
        pending_semantics[signature] = semantic
    discovery: dict[str, Any] = {
        "schema": DISCOVERY_SCHEMA,
        "system": "footbreak",
        "migration_version": MIGRATION_VERSION,
        "domain": "discovery-only",
        "capture": {
            **(copy.deepcopy(capture) if capture else {}),
            "full_pre_ledger_sha256": hashlib.sha256(source_bytes).hexdigest(),
            "full_pre_ledger_length": len(source_bytes),
            "canonical_json_sha256": canonical_hash_v1(ledger),
            "stable_registry_projection_sha256": context.document[
                "authorization_body"
            ]["source"]["registry_projection_sha256"],
        },
        "execution_identity": copy.deepcopy(execution_identity or {}),
        "writer_coordination": copy.deepcopy(writer_coordination or {}),
        "production_identity": {
            "stored_manifest_sha256": canonical_hash_v1(identity_manifest),
            "recomputed_manifest_sha256": canonical_hash_v1(identity_manifest),
            "equal": True,
        },
        "chain_preimages": [
            {
                "condition_signature": entry["condition_signature"],
                "version": entry["source_version"]["version"],
                "condition_number": entry["condition_number"],
                "definition_hash": entry["definition_hash"],
                "source_row_sha256": canonical_hash_v1(entry["source_version"]),
                "source_evidence_hash": entry["source_version"]["evidence_hash"],
                "source_batch_fixture_market_hashes_sha256": entry[
                    "source_batch_fixture_market_hashes_sha256"
                ],
                "source_batch_fixture_market_hash_count": 20,
                "expected_evidence_hash": entry["expected_rewrite"]["expected_evidence_hash"],
                "expected_post_row_sha256": canonical_hash_v1(
                    entry["expected_rewrite"]["expected_post_version"]
                ),
                "arithmetic_valid": True,
                "chronology_valid": True,
                "classification": "all_old",
            }
            for entry in context.entries.values()
        ],
        "source_support": support,
        "source_support_root": canonical_hash_v1(support),
        "migration_ready": migration_ready,
        "reference_inventory": pre_inventory,
        "reference_inventory_root": canonical_hash_v1(pre_inventory),
        "formal_rows": formal_rows,
        "namespace_audit": namespace_audit,
        "namespace_audit_root": canonical_hash_v1(namespace_audit),
        "rollover_audit": {
            "retained_limit": 64,
            "conditions": rollover_conditions,
            "rebuilt_tree_count": sum(
                before["pre_entry_hashes"] != before["expected_post_entry_hashes"]
                for before in rollover_conditions
            ),
            "root": canonical_hash_v1(rollover_conditions),
        },
        "stats_conditions": stats_payload,
        "pending_state": {
            "pre_payload_sha256": canonical_hash_v1({
                signature: frozen.get("pending_rollover_progress")
                for signature, frozen in ns["conditions"].items()
            }),
            "post_payload_sha256": canonical_hash_v1({
                signature: frozen.get("pending_rollover_progress")
                for signature, frozen in planned["ledger"]["wilson_validation"][
                    "conditions"
                ].items()
            }),
            "semantic_equal": True,
            "recomputed_semantics": pending_semantics,
        },
        "prerequisites": {
            "items": {
              key: {
                "present": key in ns,
                "sha256": canonical_hash_v1(ns.get(key)) if key in ns else None,
                "valid": {
                    "condition_identity_migrations": retired is not None,
                    "formal_binding_omissions_migration_v1": binding_valid,
                    "formal_observation_recovery_v1": recovery_valid,
                }[key],
                "status": (
                    "valid_final" if key == "condition_identity_migrations"
                    else (
                        str((ns.get(key) or {}).get("status") or "not_required")
                        if isinstance(ns.get(key), dict) else "not_required"
                    )
                ),
              }
              for key in (
                "condition_identity_migrations",
                "formal_binding_omissions_migration_v1",
                "formal_observation_recovery_v1",
              )
            }
        },
        "expected_post": {
            "full_ledger_sha256": planned["post_ledger_sha256"],
            "post_reference_inventory": post_inventory,
            "recursive_reference_inventory_root": canonical_hash_v1(
                post_inventory
            ),
            "post_export_registry_payload_sha256": (
                context.document["expected_post_export_registry_payload_sha256"]
            ),
            "converted_version_count": len(planned["converted_version_paths"]),
            "converted_version_paths": planned["converted_version_paths"],
            "rewritten_scalar_paths": planned["rewritten_scalar_paths"],
        },
    }
    discovery["pending_state"]["root"] = canonical_hash_v1(
        discovery["pending_state"]
    )
    discovery["prerequisites"]["root"] = canonical_hash_v1(
        discovery["prerequisites"]["items"]
    )
    rewritten = planned["rewritten_scalar_paths"]
    formal_count = len([
        row for row in formal_rows
        if row["expected_post_row_sha256"] != row["row_sha256"]
    ])
    namespace_count = len([
        row for row in namespace_audit
        if row["expected_post_row_sha256"] != row["row_sha256"]
    ])
    rewrite_counts = {
        "chain_evidence_hash_scalar_field_count": 10,
        "chain_prior_evidence_hash_scalar_field_count": 5,
        "active_condition_count": 5,
        "active_evidence_scalar_field_count": 10,
        "formal_row_count": formal_count,
        "formal_scalar_field_count": formal_count * 3,
        "namespace_audit_row_count": namespace_count,
        "namespace_audit_scalar_field_count": namespace_count,
        "rollover_audit_entry_count": sum(
            row["affected_entry_count"] for row in rollover_conditions
        ),
        "rollover_audit_rebuilt_tree_count": discovery["rollover_audit"][
            "rebuilt_tree_count"
        ],
        "stats_conditions_tree_count": int(
            stats_payload["classification"] == "exact_canonical_copy"
        ),
        "stats_conditions_old_hash_occurrence_count": sum(
            item["kind"] == "old" and item["json_pointer"].startswith(
                "/wilson_validation/stats/conditions/"
            )
            for item in pre_inventory
        ),
        "stats_conditions_new_hash_occurrence_count": sum(
            item["kind"] == "new" and item["json_pointer"].startswith(
                "/wilson_validation/stats/conditions/"
            )
            for item in post_inventory
        ),
    }
    authenticated_paths = sorted(
        item["json_pointer"] for item in post_inventory
        if item["kind"] == "old"
    )
    chain_marker_count = sum(
        path.startswith("/wilson_validation/conditions/")
        and "/evidence_versions/" in path for path in authenticated_paths
    )
    rollover_marker_count = sum(
        path.startswith("/wilson_validation/conditions/")
        and "/rollover_audit/" in path for path in authenticated_paths
    )
    stats_marker_count = sum(
        path.startswith("/wilson_validation/stats/conditions/")
        for path in authenticated_paths
    )
    expected_old_count = 30 if stats_payload[
        "classification"
    ] == "exact_canonical_copy" else 20
    if (
        chain_marker_count != 10 or rollover_marker_count != 10
        or stats_marker_count not in {0, 10}
        or len(authenticated_paths) != expected_old_count
    ):
        raise ValueError("authenticated_source_hash_copy_count_invalid")
    neutral_manifest_payload = _authority_neutral_manifest_payload(
        planned["ledger"]
    )
    funnel_payload = _condition_funnel_semantic_payload(planned["ledger"])
    discovery["expected_post"].update({
        "rewrite_counts": rewrite_counts,
        "authenticated_source_hash_copies": {
            "authoritative_chain_marker_count": chain_marker_count,
            "rollover_audit_marker_count": rollover_marker_count,
            "stats_conditions_marker_count": stats_marker_count,
            "total_count": len(authenticated_paths),
            "exact_json_pointer_list": authenticated_paths,
            "exact_json_pointer_list_root": canonical_hash_v1(authenticated_paths),
        },
        "authority_neutral_manifest_payload": neutral_manifest_payload,
        "authority_neutral_manifest_payload_sha256": canonical_hash_v1(
            neutral_manifest_payload
        ),
        "condition_funnel_semantic_payload": funnel_payload,
        "condition_funnel_semantic_root": canonical_hash_v1(funnel_payload),
    })
    discovery["discovery_document_sha256"] = canonical_hash_v1(discovery)
    return discovery


def validate_live_discovery(
    discovery: Any,
    calculation_context: ValidatedLegacyBatchCalculationContext,
) -> dict[str, Any]:
    _require_calculation_context(calculation_context)
    required = {
        "schema", "system", "migration_version", "domain", "capture",
        "execution_identity", "writer_coordination", "production_identity",
        "chain_preimages", "source_support", "source_support_root",
        "migration_ready", "reference_inventory", "reference_inventory_root",
        "formal_rows", "namespace_audit", "namespace_audit_root",
        "rollover_audit", "stats_conditions", "pending_state",
        "prerequisites", "expected_post", "discovery_document_sha256",
    }
    if (
        not isinstance(discovery, dict) or set(discovery) != required
        or discovery.get("schema") != DISCOVERY_SCHEMA
        or discovery.get("system") != "footbreak"
        or discovery.get("migration_version") != MIGRATION_VERSION
    ):
        raise ValueError("live_discovery_schema_invalid")
    body = copy.deepcopy(discovery)
    claimed = body.pop("discovery_document_sha256", None)
    if not _hex(claimed) or canonical_hash_v1(body) != claimed:
        raise ValueError("live_discovery_hash_mismatch")
    if discovery.get("domain") != "discovery-only":
        raise ValueError("live_discovery_domain_invalid")
    execution = discovery.get("execution_identity")
    coordination = discovery.get("writer_coordination")
    ledger_object = discovery.get("capture", {}).get("ledger_object")
    lock_object = (
        coordination.get("canonical_lock")
        if isinstance(coordination, dict) else None
    )
    identity_keys = {
        "realpath", "st_dev", "st_ino", "st_uid", "st_gid",
        "st_mode", "st_nlink",
    }
    if (
        not isinstance(execution, dict)
        or set(execution) != {
            "repository",
            "release_commit", "git_tree", "deployable_artifact_sha256",
            "module_manifest", "module_manifest_root",
            "python_executable_sha256", "import_roots",
            "sys_path_sha256", "working_tree_policy",
        }
        or execution.get("repository") != REPOSITORY_ID
        or len(execution.get("module_manifest") or [])
        != len(REQUIRED_RUNTIME_MODULES)
        or execution.get("working_tree_policy")
        != "clean_tracked_no_shadow_files"
        or not isinstance(coordination, dict)
        or set(coordination) != {
            "all_writers_quiesced", "canonical_lock",
            "writer_inventory_root", "writer_count",
            "service_configuration_sha256", "runtime_config",
        }
        or coordination.get("all_writers_quiesced") is not True
        or not isinstance(ledger_object, dict)
        or set(ledger_object) != identity_keys
        or not isinstance(lock_object, dict)
        or set(lock_object) != identity_keys
        or ledger_object.get("st_nlink") != 1
        or lock_object.get("st_nlink") != 1
        or set(coordination.get("runtime_config") or {}) != identity_keys | {
            "sha256"
        }
    ):
        raise ValueError("live_discovery_runtime_or_object_identity_invalid")
    support = discovery.get("source_support")
    expected_keys = set(calculation_context.entries)
    observed_keys = {
        (row.get("condition_signature"), row.get("version"))
        for row in support or [] if isinstance(row, dict)
    }
    if (
        not isinstance(support, list) or len(support) != 10
        or observed_keys != expected_keys
        or any(row.get("classification") != "zero_exact" for row in support)
        or any(row.get("migration_ready") is not True for row in support)
        or discovery.get("migration_ready") is not True
        or canonical_hash_v1(support) != discovery.get("source_support_root")
    ):
        raise ValueError("live_discovery_support_count_invalid")
    chain = discovery.get("chain_preimages")
    chain_keys = {
        "condition_signature", "version", "condition_number",
        "definition_hash", "source_row_sha256", "source_evidence_hash",
        "source_batch_fixture_market_hashes_sha256",
        "source_batch_fixture_market_hash_count", "expected_evidence_hash",
        "expected_post_row_sha256", "arithmetic_valid",
        "chronology_valid", "classification",
    }
    if not isinstance(chain, list) or len(chain) != 10:
        raise ValueError("live_discovery_chain_preimages_invalid")
    chain_index = {
        (row.get("condition_signature"), row.get("version")): row
        for row in chain if isinstance(row, dict)
    }
    if set(chain_index) != expected_keys:
        raise ValueError("live_discovery_chain_preimages_invalid")
    for key, entry in calculation_context.entries.items():
        row = chain_index[key]
        if (
            set(row) != chain_keys
            or row["source_row_sha256"]
            != canonical_hash_v1(entry["source_version"])
            or row["source_evidence_hash"]
            != entry["source_version"]["evidence_hash"]
            or row["source_batch_fixture_market_hashes_sha256"]
            != entry["source_batch_fixture_market_hashes_sha256"]
            or row["source_batch_fixture_market_hash_count"] != 20
            or row["expected_evidence_hash"]
            != entry["expected_rewrite"]["expected_evidence_hash"]
            or row["expected_post_row_sha256"] != canonical_hash_v1(
                entry["expected_rewrite"]["expected_post_version"]
            )
            or row["arithmetic_valid"] is not True
            or row["chronology_valid"] is not True
            or row["classification"] != "all_old"
        ):
            raise ValueError("live_discovery_chain_preimages_invalid")
    inventory = discovery.get("reference_inventory")
    if (
        not isinstance(inventory, list)
        or canonical_hash_v1(inventory) != discovery.get("reference_inventory_root")
        or any(set(row) != {
            "json_pointer", "value", "kind", "parent_object_sha256",
            "immutable_content_sha256", "classification",
            "expected_rewrite_target",
        } for row in inventory)
        or discovery.get("production_identity", {}).get("equal") is not True
    ):
        raise ValueError("live_discovery_reference_or_identity_invalid")
    formal_rows = discovery.get("formal_rows")
    formal_keys = {
        "json_pointer", "row_sha256", "immutable_content_sha256",
        "status", "condition_signature", "evidence_version",
        "definition_sha256", "fixture_market_hash", "stage_at",
        "three_hash_fields_agree", "expected_post_row_sha256",
    }
    if (
        not isinstance(formal_rows, list)
        or any(set(row) != formal_keys for row in formal_rows)
        or any(row["three_hash_fields_agree"] is not True for row in formal_rows)
    ):
        raise ValueError("live_discovery_formal_rows_invalid")
    if (
        canonical_hash_v1(discovery.get("namespace_audit"))
        != discovery.get("namespace_audit_root")
        or canonical_hash_v1(discovery.get("rollover_audit", {}).get("conditions"))
        != discovery.get("rollover_audit", {}).get("root")
        or canonical_hash_v1(discovery.get("prerequisites", {}).get("items"))
        != discovery.get("prerequisites", {}).get("root")
        or any(
            item.get("valid") is not True
            for item in discovery.get("prerequisites", {}).get("items", {}).values()
        )
        or discovery.get("pending_state", {}).get("semantic_equal") is not True
        or set(discovery.get("pending_state", {})) != {
            "pre_payload_sha256", "post_payload_sha256", "semantic_equal",
            "recomputed_semantics", "root",
        }
        or discovery["pending_state"]["pre_payload_sha256"]
        != discovery["pending_state"]["post_payload_sha256"]
        or canonical_hash_v1({
            key: value for key, value in discovery["pending_state"].items()
            if key != "root"
        }) != discovery["pending_state"]["root"]
    ):
        raise ValueError("live_discovery_proof_root_invalid")
    counts = discovery.get("expected_post", {}).get("rewrite_counts")
    if (
        not isinstance(counts, dict)
        or counts.get("formal_scalar_field_count")
        != 3 * counts.get("formal_row_count", -1)
        or counts.get("namespace_audit_scalar_field_count")
        != counts.get("namespace_audit_row_count")
        or counts.get("chain_evidence_hash_scalar_field_count") != 10
        or counts.get("chain_prior_evidence_hash_scalar_field_count") != 5
        or counts.get("active_condition_count") != 5
        or counts.get("active_evidence_scalar_field_count") != 10
    ):
        raise ValueError("live_discovery_rewrite_counts_invalid")
    copies = discovery.get("expected_post", {}).get(
        "authenticated_source_hash_copies", {}
    )
    if (
        set(copies) != {
            "authoritative_chain_marker_count",
            "rollover_audit_marker_count", "stats_conditions_marker_count",
            "total_count", "exact_json_pointer_list",
            "exact_json_pointer_list_root",
        }
        or copies.get("authoritative_chain_marker_count") != 10
        or copies.get("rollover_audit_marker_count") != 10
        or copies.get("stats_conditions_marker_count") not in {0, 10}
        or copies.get("total_count") not in {20, 30}
        or copies.get("total_count") != (
            copies.get("authoritative_chain_marker_count", 0)
            + copies.get("rollover_audit_marker_count", 0)
            + copies.get("stats_conditions_marker_count", 0)
        )
        or
        copies.get("total_count") != len(copies.get("exact_json_pointer_list") or [])
        or canonical_hash_v1(copies.get("exact_json_pointer_list"))
        != copies.get("exact_json_pointer_list_root")
    ):
        raise ValueError("live_discovery_authenticated_paths_invalid")
    expected_post = discovery["expected_post"]
    if (
        canonical_hash_v1(
            expected_post.get("authority_neutral_manifest_payload")
        ) != expected_post.get("authority_neutral_manifest_payload_sha256")
        or canonical_hash_v1(
            expected_post.get("condition_funnel_semantic_payload")
        ) != expected_post.get("condition_funnel_semantic_root")
    ):
        raise ValueError("live_discovery_manifest_or_funnel_root_invalid")
    return copy.deepcopy(discovery)


def prove_legacy_batch_prestate(
    ledger: dict[str, Any], raw_bytes: bytes,
    authority_context: LegacyBatchAuthorityContext,
) -> dict[str, Any]:
    context = require_authority_context(authority_context)
    expected = context.authority["live_prestate"]["full_ledger_sha256"]
    actual = hashlib.sha256(raw_bytes).hexdigest()
    if actual != expected:
        raise ValueError("stale_or_unapproved_pre_ledger")
    discovery = context.authority.get("live_discovery_document")
    approved_discovery = validate_live_discovery(
        discovery, context.calculation,
    )
    rebuilt_discovery = build_live_discovery(
        ledger, context.calculation, raw_ledger_bytes=raw_bytes,
        capture=approved_discovery["capture"],
        execution_identity=approved_discovery["execution_identity"],
        writer_coordination=approved_discovery["writer_coordination"],
    )
    if rebuilt_discovery != approved_discovery:
        raise ValueError("locked_live_discovery_mismatch")
    if any(
        row["classification"] != "zero_exact"
        for row in rebuilt_discovery["source_support"]
    ):
        raise ValueError("zero_exact_required")
    plan = plan_disposable_poststate(ledger, context.calculation)
    expected_post = context.authority["expected_poststate"]["full_ledger_sha256"]
    if plan["post_ledger_sha256"] != expected_post:
        raise ValueError("authority_expected_post_hash_mismatch")
    return {"state": "pre", "pre_sha256": actual, "plan": plan}


def prove_legacy_batch_poststate(
    ledger: dict[str, Any], raw_bytes: bytes,
    authority_context: LegacyBatchAuthorityContext,
) -> dict[str, Any]:
    context = require_authority_context(authority_context)
    actual = hashlib.sha256(raw_bytes).hexdigest()
    if actual != context.authority["expected_poststate"]["full_ledger_sha256"]:
        raise ValueError("not_exact_authorized_poststate")
    # Validate every aggregate and every surviving old-hash occurrence.
    from analysis import wilson_validation as wv
    ns = ledger.get(wv.NAMESPACE)
    conditions = ns.get("conditions") if isinstance(ns, dict) else {}
    for (signature, version), _entry in context.entries.items():
        row = conditions[signature]["evidence_versions"][version - 1]
        reason = validate_aggregate_version(row, "footbreak", signature, context)
        if reason is not None:
            raise ValueError(reason)
    old_to_new = _old_to_new(context.calculation)
    occurrences = _classify_inventory(
        walk_evidence_hash_occurrences(ledger, old_to_new), old_to_new,
    )
    for item in occurrences:
        if item["kind"] == "old" and not item["json_pointer"].endswith(
            "/legacy_ordinary_batch_aggregate/source_evidence_hash"
        ):
            raise ValueError(f"unclassified_old_hash_reference:{item['json_pointer']}")
    approved = context.authority["expected_poststate"]
    identity, identity_reason = wv.validate_production_identity_manifest_v1(
        ns, "footbreak",
    )
    if identity is None:
        raise ValueError(identity_reason or "post_production_identity_invalid")
    retired, retirement_reason = wv._validate_condition_identity_migrations(
        ledger, "footbreak", authority_context=context,
    )
    if retired is None:
        raise ValueError(retirement_reason or "post_prerequisite_invalid")
    from analysis.wilson_registry_manifest import build_manifest
    manifest = build_manifest(
        ledger, "footbreak", authority_context=context,
    )
    if not manifest.get("valid"):
        raise ValueError("post_manifest_invalid")
    from analysis.wilson_registry_export import _registry_payload_v2
    payload = _registry_payload_v2(
        ledger, "footbreak", authority_context=context,
    )
    if canonical_hash_v1(payload) != approved[
        "post_export_registry_payload_sha256"
    ]:
        raise ValueError("post_export_registry_payload_mismatch")
    manifest_payload = _authority_neutral_manifest_payload(ledger)
    funnel_payload = _condition_funnel_semantic_payload(ledger)
    if (
        manifest_payload != approved["authority_neutral_manifest_payload"]
        or canonical_hash_v1(manifest_payload)
        != approved["authority_neutral_manifest_payload_sha256"]
        or funnel_payload != approved["condition_funnel_semantic_payload"]
        or canonical_hash_v1(funnel_payload)
        != approved["condition_funnel_semantic_root"]
    ):
        raise ValueError("post_manifest_or_funnel_semantic_root_mismatch")
    if (
        canonical_hash_v1(occurrences)
        != approved["recursive_reference_inventory_root"]
        or sorted(
            item["json_pointer"] for item in occurrences if item["kind"] == "old"
        )
        != approved["authenticated_source_hash_copies"][
            "exact_json_pointer_list"
        ]
    ):
        raise ValueError("poststate_reference_inventory_mismatch")
    return {"state": "post", "post_sha256": actual, "reference_inventory": occurrences}


def plan_legacy_batch_migration(
    ledger: dict[str, Any], raw_bytes: bytes,
    authority_context: LegacyBatchAuthorityContext,
) -> dict[str, Any]:
    context = require_authority_context(authority_context)
    digest = hashlib.sha256(raw_bytes).hexdigest()
    if digest == context.authority["expected_poststate"]["full_ledger_sha256"]:
        return {"status": "already_applied", **prove_legacy_batch_poststate(
            ledger, raw_bytes, context,
        )}
    proof = prove_legacy_batch_prestate(ledger, raw_bytes, context)
    return {"status": "ready", **proof}


def runtime_identity_from_checkout(repo: Path, module_paths: list[str]) -> dict[str, Any]:
    """Collect deterministic local release identity for authority comparison."""
    repo = repo.resolve()
    executing_root = Path(__file__).resolve().parents[1]
    if repo != executing_root:
        raise ValueError("attested_checkout_is_not_executing_checkout")
    if module_paths and set(module_paths) != set(REQUIRED_RUNTIME_MODULES.values()):
        raise ValueError("runtime_module_coverage_mismatch")
    commit = subprocess.check_output(
        ["git", "-C", str(repo), "rev-parse", "HEAD"], text=True,
    ).strip()
    tree = subprocess.check_output(
        ["git", "-C", str(repo), "rev-parse", "HEAD^{tree}"], text=True,
    ).strip()
    status = subprocess.check_output(
        ["git", "-C", str(repo), "status", "--porcelain"], text=True,
    )
    manifest = []
    for module_name, required_path in sorted(REQUIRED_RUNTIME_MODULES.items()):
        module = importlib.import_module(module_name)
        loaded = Path(str(module.__file__ or "")).resolve()
        expected = (repo / required_path).resolve()
        if loaded != expected or repo not in loaded.parents:
            raise ValueError(f"imported_module_outside_attested_checkout:{module_name}")
        manifest.append({
            "module": module_name,
            "path": required_path,
            "resolved_path": str(loaded),
            "sha256": hashlib.sha256(loaded.read_bytes()).hexdigest(),
        })
    executable = Path(sys.executable).resolve()
    import_roots = sorted({
        str(Path(value or os.getcwd()).resolve()) for value in sys.path
    })
    return {
        "repository": REPOSITORY_ID,
        "release_commit": commit,
        "git_tree": tree,
        "deployable_artifact_sha256": canonical_hash_v1(manifest),
        "module_manifest": manifest,
        "module_manifest_root": canonical_hash_v1(manifest),
        "python_executable_sha256": hashlib.sha256(executable.read_bytes()).hexdigest(),
        "import_roots": import_roots,
        "sys_path_sha256": canonical_hash_v1(import_roots),
        "working_tree_policy": (
            "clean_tracked_no_shadow_files" if not status
            else "dirty_or_untracked"
        ),
    }
