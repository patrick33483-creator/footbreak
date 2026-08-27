"""Manual proof-gated migration for two legacy Wilson binding omissions.

The command is read-only unless both ``--apply`` and the exact confirmation
phrase are supplied.  It is intentionally not imported by any runtime path.
"""
from __future__ import annotations

import argparse
import copy
import fcntl
import json
import os
import stat
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .wilson_validation import (
    BINARY_DECIDED_RESULTS,
    NAMESPACE,
    SCHEMA_VERSION,
    STRATEGY,
    _canonical_hash,
    _expected_production_identity_manifest,
    _legacy_formal_binding_repair_copy,
    _prove_explicit_rollover_batch,
    _prove_pending_rollover_cohort,
    _rollover_condition,
    _signature_rows_for_rollover,
    _time,
    portfolio_name,
    recompute_namespace,
    validate_formal_row,
)

MIGRATION_NAME = "wilson-formal-binding-omissions-v1"
MIGRATION_AUDIT_VERSION = 1
MIGRATION_AUDIT_KEY = "formal_binding_omissions_migration_v1"
CONFIRMATION = "APPLY_WILSON_FORMAL_BINDING_OMISSIONS_V1"


def _fail(reason: str) -> None:
    raise ValueError(reason)


def _located_formal_rows(
    ledger: dict[str, Any], system: str,
) -> list[dict[str, Any]]:
    ns = ledger.get(NAMESPACE)
    bets = ledger.get("bets")
    observations = ns.get("observations", []) if isinstance(ns, dict) else None
    if not isinstance(bets, list) or not isinstance(observations, list):
        _fail("formal_evidence_containers_malformed")
    if any(not isinstance(row, dict) for row in bets + observations):
        _fail("formal_evidence_rows_malformed")
    located: list[dict[str, Any]] = []
    for container, rows in (("bets", bets), ("observations", observations)):
        for index, row in enumerate(rows):
            claimed = (
                row.get("portfolio") == portfolio_name(system)
                and row.get("strategy") == STRATEGY
                if container == "bets"
                else row.get("portfolio") == f"{system}_wilson_observations"
                and row.get("strategy") == STRATEGY
                and row.get("formal_bet") is False
            )
            if claimed:
                located.append({
                    "container": container, "index": index, "row": row,
                })
    return located


def _migration_hash_scope(
    ledger: dict[str, Any], system: str,
) -> dict[str, Any]:
    ns = ledger[NAMESPACE]
    conditions = ns["conditions"]
    return {
        "system": system,
        "formal_rows": [
            {
                "container": item["container"], "index": item["index"],
                "row": copy.deepcopy(item["row"]),
            }
            for item in _located_formal_rows(ledger, system)
        ],
        "conditions": {
            signature: {
                key: copy.deepcopy(frozen.get(key))
                for key in (
                    "definition", "evidence_versions", "rollover_audit",
                    "active_evidence_version", "active_evidence_hash",
                    "pending_rollover_progress",
                )
            }
            for signature, frozen in sorted(conditions.items())
        },
    }


def _validate_existing_audit(audit: Any, system: str) -> None:
    required = {
        "audit_version", "migration", "system", "applied_at",
        "pre_state_hash", "post_state_hash", "repair_count", "rows",
        "pending_proofs", "merged_batch_proofs",
        "strict_validation_after", "audit_hash",
    }
    if (
        not isinstance(audit, dict) or set(audit) != required
        or audit.get("audit_version") != MIGRATION_AUDIT_VERSION
        or audit.get("migration") != MIGRATION_NAME
        or audit.get("system") != system
        or _time(audit.get("applied_at")) is None
        or not isinstance(audit.get("rows"), list)
        or audit.get("repair_count") != len(audit["rows"])
        or audit.get("strict_validation_after") is not True
        or audit.get("audit_hash") != _canonical_hash({
            key: value for key, value in audit.items() if key != "audit_hash"
        })
    ):
        _fail("invalid_existing_migration_audit")


def _proof_summary(proof: dict[str, Any]) -> dict[str, Any]:
    return {
        "expected_decided": proof["expected_decided"],
        "expected_hits": proof["expected_hits"],
        "excluded": copy.deepcopy(proof.get("excluded", {})),
        "ordered_fixture_market_hashes_hash": _canonical_hash(
            proof["ordered_fixture_market_hashes"],
        ),
    }


def _batch_summary(signature: str, proof: dict[str, Any]) -> dict[str, Any]:
    return {
        "condition_signature": signature,
        "version": proof["version"],
        "batch_decided": proof["expected_decided"],
        "batch_hits": proof["expected_hits"],
        "batch_fixture_market_hashes_hash": _canonical_hash(
            proof["ordered_fixture_market_hashes"],
        ),
    }


def _captured_rollover_state(ledger: dict[str, Any]) -> dict[str, Any]:
    return {
        signature: {
            key: copy.deepcopy(frozen.get(key))
            for key in (
                "evidence_versions", "rollover_audit",
                "active_evidence_version", "active_evidence_hash",
                "active_evidence", "pending_rollover_progress",
            )
        }
        for signature, frozen in ledger[NAMESPACE]["conditions"].items()
    }


def migrate_legacy_formal_bindings(
    ledger: dict[str, Any], system: str, *, now: str, apply: bool,
) -> dict[str, Any]:
    """Audit or transactionally apply the exact two-field migration."""
    if system not in {"footbreak", "crown"}:
        _fail("unsupported_system")
    projection_time = _time(now)
    if projection_time is None:
        _fail("invalid_now")
    if not isinstance(ledger, dict):
        _fail("ledger_must_be_object")
    proposed = copy.deepcopy(ledger)
    ns = proposed.get(NAMESPACE)
    if (
        not isinstance(ns, dict)
        or ns.get("schema_version") != SCHEMA_VERSION
        or ns.get("system") != system
        or not isinstance(ns.get("conditions"), dict)
        or not isinstance(ns.get("condition_order"), list)
    ):
        _fail("validated_wilson_namespace_required")
    _manifest, validated_registry, registry_reason = (
        _expected_production_identity_manifest(ns, system)
    )
    if validated_registry is None:
        _fail(registry_reason or "validated_frozen_registry_required")

    located = _located_formal_rows(proposed, system)
    plans: list[dict[str, Any]] = []
    rejected: list[dict[str, Any]] = []
    for item in located:
        row = item["row"]
        signature = str(row.get("frozen_condition_signature") or "")
        frozen = ns["conditions"].get(signature)
        if not isinstance(frozen, dict):
            rejected.append({**item, "reason": "unknown_condition_signature"})
            continue
        admitted, reason = validate_formal_row(
            row, system=system, signature=signature, frozen=frozen,
            projection_time=projection_time,
            require_settled=row.get("status") == "SETTLED", ledger=proposed,
        )
        if admitted is not None:
            continue
        repaired, fields, repair_reason = _legacy_formal_binding_repair_copy(
            row, system=system, signature=signature, frozen=frozen,
            projection_time=projection_time,
            require_settled=row.get("status") == "SETTLED", ledger=proposed,
            require_absent_native_stage_key=True,
        )
        if repaired is None:
            rejected.append({
                **item, "reason": repair_reason or reason or "invalid_formal_row",
            })
        else:
            plans.append({**item, "repaired": repaired, "fields": fields})

    existing_audit = ns.get(MIGRATION_AUDIT_KEY)
    if existing_audit is not None:
        _validate_existing_audit(existing_audit, system)
        if plans:
            _fail("new_legacy_candidate_after_completed_migration")
        if rejected:
            _fail("strict_validation_failed_after_completed_migration")
        return {
            "status": "already_applied", "apply": apply, "repair_count": 0,
            "audit": copy.deepcopy(existing_audit),
        }
    if rejected:
        _fail("unrepairable_formal_rows:" + ",".join(
            f"{item['container']}[{item['index']}]:{item['reason']}"
            for item in rejected
        ))
    if not plans:
        return {
            "status": "nothing_to_migrate", "apply": apply,
            "repair_count": 0, "audit": None,
        }

    pre_state_hash = _canonical_hash(_migration_hash_scope(proposed, system))
    affected = sorted({
        str(item["row"]["frozen_condition_signature"]) for item in plans
    })
    planned_rows = {id(item["row"]) for item in plans}
    for signature in affected:
        frozen = ns["conditions"][signature]
        for row in _signature_rows_for_rollover(proposed, signature):
            if id(row) in planned_rows:
                continue
            admitted, reason = validate_formal_row(
                row, system=system, signature=signature, frozen=frozen,
                projection_time=projection_time,
                require_settled=row.get("status") == "SETTLED",
                ledger=proposed,
            )
            if admitted is None:
                _fail(
                    f"unrepairable_same_signature_activity:{signature}:{reason}"
                )
    pre_pending: dict[str, dict[str, Any]] = {}
    pre_batches: dict[tuple[str, int], dict[str, Any]] = {}
    for signature in affected:
        frozen = ns["conditions"][signature]
        versions = validated_registry[signature][1]
        active = versions[-1]
        pending = frozen.get("pending_rollover_progress")
        if not isinstance(pending, dict):
            _fail(f"pending_summary_unavailable:{signature}")
        pending_proof = _prove_pending_rollover_cohort(
            proposed, system, signature, frozen, active, pending,
            projection_time=projection_time, allow_legacy_omissions=True,
            require_absent_native_stage_key=True,
        )
        if not pending_proof["complete"]:
            _fail(f"pending_preproof_failed:{signature}:{pending_proof['reason']}")
        pre_pending[signature] = pending_proof
        for version in versions:
            if not version.get("batch_fixture_market_hashes"):
                continue
            proof = _prove_explicit_rollover_batch(
                proposed, system, signature, frozen, version,
                projection_time=projection_time, allow_legacy_omissions=True,
                require_absent_native_stage_key=True,
            )
            if not proof["complete"]:
                _fail(
                    f"batch_preproof_failed:{signature}:"
                    f"{version.get('version')}:{proof['reason']}"
                )
            pre_batches[(signature, int(version["version"]))] = proof

        pending_ids = set(pending_proof["ordered_fixture_market_hashes"])
        batch_memberships: dict[str, int] = {}
        for (batch_signature, _version), proof in pre_batches.items():
            if batch_signature == signature:
                for fixture_hash in proof["ordered_fixture_market_hashes"]:
                    batch_memberships[fixture_hash] = (
                        batch_memberships.get(fixture_hash, 0) + 1
                    )
        active_boundary = _time(active.get("activation_boundary_at"))
        for plan in plans:
            row = plan["repaired"]
            if (
                row.get("frozen_condition_signature") != signature
                or row.get("status") != "SETTLED"
                or row.get("result") not in BINARY_DECIDED_RESULTS
            ):
                continue
            marker = row["rollover_provenance"]
            fixture_hash = marker["fixture_market_hash"]
            stage_at = _time(marker["stage_at"])
            if stage_at is None or active_boundary is None:
                _fail(f"candidate_time_unavailable:{signature}")
            if stage_at > active_boundary:
                if fixture_hash not in pending_ids:
                    _fail(f"unclassified_pending_candidate:{signature}")
            elif batch_memberships.get(fixture_hash) != 1:
                _fail(f"unclassified_merged_candidate:{signature}")

    row_audits: list[dict[str, Any]] = []
    for plan in plans:
        row = plan["row"]
        pre_hash = _canonical_hash(row)
        for field in plan["fields"]:
            row[field] = copy.deepcopy(plan["repaired"][field])
        marker = row.get("rollover_provenance") or {}
        row_audits.append({
            "container": plan["container"], "index": plan["index"],
            "bet_id": row.get("bet_id"),
            "observation_id": row.get("observation_id"),
            "condition_signature": row.get("frozen_condition_signature"),
            "fixture_market_hash": marker.get("fixture_market_hash"),
            "omissions": list(plan["fields"]), "pre_hash": pre_hash,
            "post_hash": _canonical_hash(row),
        })

    for signature in affected:
        frozen = ns["conditions"][signature]
        versions = frozen["evidence_versions"]
        active = versions[-1]
        post_pending = _prove_pending_rollover_cohort(
            proposed, system, signature, frozen, active,
            frozen["pending_rollover_progress"],
            projection_time=projection_time, allow_legacy_omissions=False,
        )
        if not post_pending["complete"]:
            _fail(f"pending_postproof_failed:{signature}:{post_pending['reason']}")
        before = pre_pending[signature]
        parity_keys = (
            "expected_decided", "expected_hits", "excluded",
            "ordered_fixture_market_hashes",
        )
        if any(post_pending[key] != before[key] for key in parity_keys):
            _fail(f"pending_pre_post_mismatch:{signature}")
        for version in versions:
            key = (signature, int(version["version"]))
            if key not in pre_batches:
                continue
            post_batch = _prove_explicit_rollover_batch(
                proposed, system, signature, frozen, version,
                projection_time=projection_time, allow_legacy_omissions=False,
            )
            if not post_batch["complete"]:
                _fail(f"batch_postproof_failed:{signature}:{version['version']}")
            for field in (
                "expected_decided", "expected_hits",
                "ordered_fixture_market_hashes",
            ):
                if post_batch[field] != pre_batches[key][field]:
                    _fail(f"batch_pre_post_mismatch:{signature}:{version['version']}")

    for signature in affected:
        frozen = ns["conditions"][signature]
        for row in _signature_rows_for_rollover(proposed, signature):
            admitted, reason = validate_formal_row(
                row, system=system, signature=signature, frozen=frozen,
                projection_time=projection_time,
                require_settled=row.get("status") == "SETTLED",
                ledger=proposed,
            )
            if admitted is None:
                _fail(f"strict_postvalidation_failed:{signature}:{reason}")

    verification = copy.deepcopy(proposed)
    before_recompute = _captured_rollover_state(verification)
    recompute_namespace(verification, system)
    verification_ns = verification[NAMESPACE]
    migration_boundary = str(
        verification_ns.get("rollover_migration_at")
        or verification_ns["activation_at"]
    )
    for signature in affected:
        if not _rollover_condition(
            verification_ns["conditions"][signature],
            _signature_rows_for_rollover(verification, signature),
            system, signature, now=now,
            migration_boundary=migration_boundary, ledger=verification,
        ):
            _fail(f"ordinary_rollover_validation_failed:{signature}")
    if _captured_rollover_state(verification) != before_recompute:
        _fail("ordinary_recompute_would_change_rollover_state")

    post_state_hash = _canonical_hash(_migration_hash_scope(proposed, system))
    pending_summaries = {
        signature: _proof_summary(pre_pending[signature])
        for signature in affected
    }
    batch_summaries = [
        _batch_summary(signature, proof)
        for (signature, _version), proof in sorted(pre_batches.items())
    ]
    audit = {
        "audit_version": MIGRATION_AUDIT_VERSION,
        "migration": MIGRATION_NAME, "system": system, "applied_at": now,
        "pre_state_hash": pre_state_hash, "post_state_hash": post_state_hash,
        "repair_count": len(row_audits),
        "rows": sorted(
            row_audits, key=lambda item: (item["container"], item["index"]),
        ),
        "pending_proofs": pending_summaries,
        "merged_batch_proofs": batch_summaries,
        "strict_validation_after": True,
    }
    audit["audit_hash"] = _canonical_hash(audit)
    proposed[NAMESPACE][MIGRATION_AUDIT_KEY] = audit
    if apply:
        ledger.clear()
        ledger.update(proposed)
    return {
        "status": "applied" if apply else "ready",
        "apply": apply, "repair_count": len(row_audits),
        "audit": copy.deepcopy(audit),
    }


def _atomic_write_json(
    path: Path, payload: dict[str, Any], system: str,
) -> list[str]:
    """Commit JSON atomically and report, never raise, post-replace warnings."""
    original = path.stat()
    temporary_name: str | None = None
    committed = False
    warnings: list[str] = []
    try:
        descriptor, temporary_name = tempfile.mkstemp(
            prefix=f".{path.name}.", suffix=".tmp", dir=path.parent,
        )
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            json.dump(payload, handle, ensure_ascii=False, indent=2)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.chmod(temporary_name, stat.S_IMODE(original.st_mode))
        try:
            os.chown(temporary_name, original.st_uid, original.st_gid)
        except PermissionError:
            pass
        os.replace(temporary_name, path)
        committed = True
        temporary_name = None
        try:
            directory_fd = os.open(path.parent, os.O_RDONLY)
            try:
                os.fsync(directory_fd)
            finally:
                os.close(directory_fd)
        except Exception as exc:
            warnings.append(f"parent_directory_fsync_failed:{exc}")
        try:
            persisted = json.loads(path.read_text(encoding="utf-8"))
            if persisted != payload:
                warnings.append("post_write_readback_mismatch")
            else:
                audit = persisted[NAMESPACE][MIGRATION_AUDIT_KEY]
                _validate_existing_audit(audit, system)
        except Exception as exc:
            warnings.append(
                f"post_write_readback_validation_failed:"
                f"{type(exc).__name__}:{exc}"
            )
        return warnings
    finally:
        if temporary_name is not None:
            try:
                os.unlink(temporary_name)
            except OSError:
                pass
        # Once replacement succeeds, no later durability/readback problem is
        # raised as a false "unchanged" result.  The caller receives warnings
        # with an otherwise successful committed status.
        if committed:
            temporary_name = None


def migrate_file(
    ledger_path: Path, system: str, *, lock_path: Path, now: str,
    apply: bool, confirmation: str | None,
) -> dict[str, Any]:
    """Lock, reload, prove, and atomically replace only on successful apply."""
    if apply and confirmation != CONFIRMATION:
        _fail("exact_apply_confirmation_required")
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    with lock_path.open("a+", encoding="utf-8") as lock:
        try:
            fcntl.flock(lock.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            raise RuntimeError("migration_lock_unavailable") from exc
        try:
            payload = json.loads(ledger_path.read_text(encoding="utf-8"))
            if not isinstance(payload, dict):
                _fail("ledger_must_be_object")
            result = migrate_legacy_formal_bindings(
                payload, system, now=now, apply=apply,
            )
            if apply and result["status"] == "applied":
                warnings = _atomic_write_json(ledger_path, payload, system)
                if warnings:
                    result["durability_warnings"] = warnings
            return result
        finally:
            fcntl.flock(lock.fileno(), fcntl.LOCK_UN)


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Audit/apply exact legacy Wilson formal binding omissions",
    )
    parser.add_argument("--ledger", required=True, type=Path)
    parser.add_argument("--system", required=True, choices=("footbreak", "crown"))
    parser.add_argument("--lock", required=True, type=Path)
    parser.add_argument("--now")
    parser.add_argument("--apply", action="store_true")
    parser.add_argument("--confirm")
    args = parser.parse_args()
    result = migrate_file(
        args.ledger, args.system, lock_path=args.lock,
        now=args.now or datetime.now(timezone.utc).astimezone().isoformat(
            timespec="seconds",
        ),
        apply=args.apply, confirmation=args.confirm,
    )
    print(json.dumps(result, ensure_ascii=False, indent=2, sort_keys=True))


if __name__ == "__main__":
    main()
