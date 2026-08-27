"""Single trusted production loader for legacy-batch aggregate authority."""
from __future__ import annotations

import copy
import os
import stat
from pathlib import Path
from typing import Any

from .legacy_batch_aggregate import (
    load_legacy_batch_authority, parse_json_bytes_v1,
    read_root_owned_json_config, REQUIRED_RUNTIME_MODULES,
    runtime_identity_from_checkout,
)


def ledger_has_legacy_batch_aggregates(ledger: dict[str, Any]) -> bool:
    namespace = ledger.get("wilson_validation")
    return any(
        isinstance(version, dict)
        and "legacy_ordinary_batch_aggregate" in version
        for frozen in (
            namespace.get("conditions", {}).values()
            if isinstance(namespace, dict) else []
        )
        if isinstance(frozen, dict)
        for version in (frozen.get("evidence_versions") or [])
    )


def load_production_legacy_batch_authority(
    ledger: dict[str, Any],
) -> Any:
    """Return None for ordinary ledgers; require root-owned authority otherwise."""
    if not ledger_has_legacy_batch_aggregates(ledger):
        return None
    configured = os.environ.get("FOOTBREAK_LEGACY_BATCH_RUNTIME_CONFIG")
    if not configured:
        raise ValueError("legacy_batch_runtime_authority_config_required")
    config_path = Path(configured)
    config, _raw, _identity = read_root_owned_json_config(config_path)
    required = {
        "ledger_path", "canonical_lock_path", "canonical_lock_identity",
        "all_writers_quiesced", "repository_root", "writer_inventory",
        "service_configuration", "authority_path",
    }
    if not isinstance(config, dict) or set(config) != required:
        raise ValueError("legacy_batch_runtime_authority_config_invalid")
    runtime = runtime_identity_from_checkout(
        Path(config["repository_root"]),
        sorted(REQUIRED_RUNTIME_MODULES.values()),
    )
    if config.get("trusted_approver_public_keys") is not None:
        runtime["trusted_approver_public_keys"] = copy.deepcopy(
            config["trusted_approver_public_keys"]
        )
    trusted_hash = os.environ.get("FOOTBREAK_LEGACY_BATCH_AUTHORITY_HASH")
    if not trusted_hash:
        raise ValueError("independent_legacy_batch_authority_hash_required")
    context = load_legacy_batch_authority(
        config["authority_path"],
        trusted_hash,
        config.get("detached_signature"),
        runtime,
    )
    if context.authority["runtime_coordination"]["runtime_config"] != _identity:
        raise ValueError("legacy_batch_runtime_config_identity_mismatch")
    return context
