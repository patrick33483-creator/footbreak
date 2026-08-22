#!/usr/bin/env python3
"""Read-only Crown formal-observation and evidence-version integrity audit.

This program is deliberately streamed by a non-deploy workflow.  It reads the
durable ledger and local systemd status only: it makes no provider request,
writes no ledger/dashboard data, and never invokes a result or tick worker.
"""
from __future__ import annotations

import hashlib
import json
import subprocess
from collections import Counter, defaultdict
from datetime import datetime
from pathlib import Path
from typing import Any

from crown.common import HKT
from analysis.wilson_validation import (
    BINARY_DECIDED_RESULTS,
    DECISION_STAGE,
    ROLLOVER_BATCH_SIZE,
    STRATEGY,
    _eligible_rollover_rows,
    _version_hash,
)


LEDGER = Path("/var/lib/footbreak/crown/ledger.json")


def read_json(path: Path) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    return value if isinstance(value, dict) else {}


def systemctl(units: list[str]) -> str:
    try:
        completed = subprocess.run(
            [
                "systemctl", "show", *units, "--no-pager",
                "-p", "Id", "-p", "LoadState", "-p", "UnitFileState",
                "-p", "ActiveState", "-p", "SubState", "-p", "Result",
                "-p", "LastTriggerUSec", "-p", "NextElapseUSecRealtime",
                "-p", "TimeoutStartUSec", "-p", "ExecMainStatus",
            ],
            text=True, capture_output=True, check=False, timeout=20,
        )
        return completed.stdout[-12000:]
    except (OSError, subprocess.TimeoutExpired):
        return ""


def condition_key(signature: str) -> str:
    """Expose only a short hash prefix in public check output."""
    return signature[:24]


def formal_rows(ns: dict[str, Any]) -> list[dict[str, Any]]:
    rows = ns.get("observations")
    return [row for row in rows if isinstance(row, dict)] if isinstance(rows, list) else []


def condition_rows(ledger: dict[str, Any]) -> list[dict[str, Any]]:
    # Formal evidence comes from both simulated executions and the segregated
    # low-odds observation ledger.  This audit never projects research rows.
    output: list[dict[str, Any]] = []
    for row in ledger.get("bets") or []:
        if isinstance(row, dict):
            output.append(row)
    output.extend(formal_rows((ledger.get("wilson_validation") or {})))
    return output


def valid_formal_row(row: dict[str, Any], signature: str) -> bool:
    marker = row.get("rollover_provenance")
    return (
        str(row.get("frozen_condition_signature") or "") == signature
        and row.get("stage") == DECISION_STAGE
        and row.get("first_native_pre_kickoff_t5") is True
        and isinstance(marker, dict)
        and marker.get("schema_version") == 1
        and marker.get("system") == "crown"
        and marker.get("condition_signature") == signature
        and marker.get("native_pre_kickoff_t5") is True
        and not row.get("post_hoc_backfill")
        and not row.get("exclude_from_simulation")
    )


def evidence_chain(
    signature: str, frozen: dict[str, Any], all_rows: list[dict[str, Any]],
) -> dict[str, Any]:
    versions = frozen.get("evidence_versions")
    versions = versions if isinstance(versions, list) else []
    well_formed = True
    failures: list[str] = []
    batch_hashes: list[str] = []
    previous: dict[str, Any] | None = None
    rendered_versions: list[dict[str, Any]] = []
    for index, version in enumerate(versions, start=1):
        if not isinstance(version, dict):
            well_formed = False
            failures.append("non_object_evidence_version")
            continue
        try:
            version_number = int(version.get("version"))
        except (TypeError, ValueError):
            version_number = -1
        if version_number != index:
            well_formed = False
            failures.append("non_contiguous_version_number")
        if str(version.get("condition_signature") or "") != signature:
            well_formed = False
            failures.append("signature_mismatch")
        if str(version.get("evidence_hash") or "") != _version_hash(version):
            well_formed = False
            failures.append("evidence_hash_mismatch")
        if previous is not None:
            if version.get("prior_version") != previous.get("version"):
                well_formed = False
                failures.append("prior_version_mismatch")
            if version.get("prior_evidence_hash") != previous.get("evidence_hash"):
                well_formed = False
                failures.append("prior_hash_mismatch")
            batch = version.get("batch_fixture_market_hashes")
            if not isinstance(batch, list) or len(batch) != ROLLOVER_BATCH_SIZE:
                well_formed = False
                failures.append("non_exact_rollover_batch")
            else:
                batch_hashes.extend(str(item) for item in batch)
            if version.get("batch_decided") != ROLLOVER_BATCH_SIZE:
                well_formed = False
                failures.append("batch_decided_not_20")
        rendered_versions.append({
            "version": version.get("version"),
            "cumulative_hits": version.get("cumulative_hits"),
            "cumulative_decided": version.get("cumulative_decided"),
            "activation_boundary_at": version.get("activation_boundary_at"),
            "evidence_hash_prefix": str(version.get("evidence_hash") or "")[:24],
            "batch_decided": version.get("batch_decided"),
        })
        previous = version
    if len(batch_hashes) != len(set(batch_hashes)):
        well_formed = False
        failures.append("duplicate_hash_across_rollovers")

    active = versions[-1] if versions and isinstance(versions[-1], dict) else {}
    try:
        eligible, excluded = _eligible_rollover_rows(
            all_rows, "crown", signature, active,
        ) if active else ([], {})
    except Exception as exc:  # malformed persisted evidence must stay visible
        eligible, excluded = [], {"audit_exception": type(exc).__name__}
        well_formed = False
        failures.append("eligible_rollover_audit_exception")

    admitted = [row for row in all_rows if valid_formal_row(row, signature)]
    statuses = Counter(str(row.get("status") or "") for row in admitted)
    invalid_same_signature = sum(
        str(row.get("frozen_condition_signature") or "") == signature
        and not valid_formal_row(row, signature)
        for row in all_rows
    )
    invalid_research = sum(
        str(row.get("strategy") or "") != STRATEGY
        and str(row.get("frozen_condition_signature") or "") == signature
        for row in all_rows
    )
    return {
        "condition_number": frozen.get("condition_number"),
        "signature_prefix": condition_key(signature),
        "frozen_active": frozen.get("active") is True,
        "active_evidence_version": frozen.get("active_evidence_version"),
        "active_evidence_hash_prefix": str(frozen.get("active_evidence_hash") or "")[:24],
        "rollover_status": frozen.get("rollover_status"),
        "versions": rendered_versions,
        "chain_valid": well_formed,
        "chain_failures": sorted(set(failures)),
        "settlement_lifecycle": dict(sorted(statuses.items())),
        "admitted_formal_rows": len(admitted),
        "invalid_provenance_rows_same_signature": invalid_same_signature,
        "research_strategy_rows_same_signature": invalid_research,
        "eligible_decided_after_active_boundary": len(eligible),
        "eligible_progress": f"{len(eligible)}/{ROLLOVER_BATCH_SIZE}",
        "eligible_excluded": excluded,
        "stored_pending_rollover_progress": frozen.get("pending_rollover_progress"),
        "stored_rollover_audit_count": len(
            frozen.get("rollover_audit") if isinstance(frozen.get("rollover_audit"), list) else []
        ),
    }


def main() -> None:
    ledger = read_json(LEDGER)
    ns = ledger.get("wilson_validation")
    ns = ns if isinstance(ns, dict) else {}
    conditions = ns.get("conditions")
    conditions = conditions if isinstance(conditions, dict) else {}
    all_rows = condition_rows(ledger)
    per_condition = [
        evidence_chain(signature, frozen, all_rows)
        for signature, frozen in conditions.items()
        if isinstance(signature, str) and isinstance(frozen, dict)
    ]
    per_condition.sort(
        key=lambda row: (
            int(row["condition_number"]) if str(row["condition_number"]).isdigit() else 10**9,
            row["signature_prefix"],
        )
    )
    known_signatures = set(conditions)
    unknown_signature_rows = sum(
        bool(str(row.get("frozen_condition_signature") or ""))
        and str(row.get("frozen_condition_signature") or "") not in known_signatures
        for row in all_rows
    )
    formal_observation_rows = formal_rows(ns)
    return_payload = {
        "mode": "provider_free_read_only",
        "generated_at_hkt": datetime.now(HKT).isoformat(),
        "server_sha": subprocess.run(
            ["git", "-C", "/opt/footbreak", "rev-parse", "HEAD"],
            text=True, capture_output=True, check=False, timeout=10,
        ).stdout.strip(),
        "registry": {
            "schema_version": ns.get("schema_version"),
            "system": ns.get("system"),
            "activation_at": ns.get("activation_at"),
            "condition_count": len(per_condition),
            "active_condition_count": sum(row["frozen_active"] for row in per_condition),
        },
        "formal_row_inventory": {
            "condition_bet_rows": sum(isinstance(row, dict) for row in ledger.get("bets") or []),
            "observation_rows": len(formal_observation_rows),
            "all_formal_evidence_rows": len(all_rows),
            "unknown_signature_rows": unknown_signature_rows,
            "observation_statuses": dict(sorted(
                Counter(str(row.get("status") or "") for row in formal_observation_rows).items()
            )),
            "research_rows_in_formal_observation_namespace": sum(
                str(row.get("strategy") or "") != STRATEGY for row in formal_observation_rows
            ),
        },
        "conditions": per_condition,
        "integrity": {
            "chains_valid": sum(row["chain_valid"] for row in per_condition),
            "chains_invalid": sum(not row["chain_valid"] for row in per_condition),
            "conditions_with_invalid_provenance_rows": sum(
                bool(row["invalid_provenance_rows_same_signature"]) for row in per_condition
            ),
            "conditions_with_research_contamination": sum(
                bool(row["research_strategy_rows_same_signature"]) for row in per_condition
            ),
            "conditions_with_due_full_rollover_batch": sum(
                row["eligible_decided_after_active_boundary"] >= ROLLOVER_BATCH_SIZE
                for row in per_condition
            ),
        },
        "timers": systemctl([
            "crown-tick.timer", "crown-tick.service",
            "crown-round-update.timer", "crown-round-update.service",
            "crown-first-look-reconcile.timer", "crown-first-look-reconcile.service",
        ]),
    }
    print(json.dumps(return_payload, ensure_ascii=False, sort_keys=True, indent=2))


if __name__ == "__main__":
    main()
