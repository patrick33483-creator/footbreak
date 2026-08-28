#!/usr/bin/env python3
"""Recover missed Crown condition #5 T-30 observations exactly once."""
from __future__ import annotations

import argparse
import copy
import hashlib
import json
import os
import tempfile
from collections import Counter
from datetime import datetime
from pathlib import Path
from typing import Any

from analysis.audit_crown_condition5_history import (
    SYSTEM, _condition, _matched_panels, _raw_stage_map, _rows, _stamp,
)
from analysis.legacy_batch_runtime import load_production_legacy_batch_authority
from analysis.recover_crown_condition2_history import (
    _grade, _hash, _selected, _watch, _with_quarter_line_profile,
)
from analysis.wilson_validation import (
    _canonical_hash, _time, active_evidence_version, apply_active_evidence,
    formal_registry_candidates, match_formal_registry, matching_admissions,
    recompute_namespace, record_match_observation, validate_formal_row,
)


SIGNATURE = "7d990edf3c230cf5a21ead44"
MIGRATION = "crown-condition5-t30-missed-admission-v1"
_CROWN_SNAPSHOT_MUTABLE = {
    "collection_attempts",
    "formal_admission_pending",
    "formal_admission_snapshot_id",
    "formal_admission_snapshot_hash",
    "formal_admission_watch_context_hash",
    "formal_admission_status",
    "formal_admission_reason",
    "formal_admission_completed_at",
    "wilson_validation",
}


def _read(path: Path) -> dict[str, Any]:
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise ValueError(f"{path} must contain one JSON object")
    return value


def _write_atomic(path: Path, payload: dict[str, Any]) -> None:
    fd, temporary = tempfile.mkstemp(
        prefix=f".{path.name}.condition5.", dir=str(path.parent),
    )
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(payload, handle, ensure_ascii=False, indent=2, sort_keys=True)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
    finally:
        try:
            os.unlink(temporary)
        except FileNotFoundError:
            pass


def _candidate_matches(
    history_rows: list[dict[str, Any]], definition: dict[str, Any],
    boundary: str,
) -> list[dict[str, Any]]:
    target = tuple(definition.get("miner_key") or [])
    raw = _raw_stage_map(history_rows)
    output: list[dict[str, Any]] = []
    for item in _matched_panels(history_rows, target, settled_only=False):
        panel, path = item["panel"], item["path"]
        fixture = str(panel.get("fixture") or "")
        source = raw.get((fixture, "T-30"))
        stage_at = _stamp(source or {})
        if (
            not fixture or not isinstance(source, dict) or stage_at is None
            or _time(boundary) is None or stage_at <= _time(boundary)
        ):
            continue
        kickoff = panel.get("kickoff")
        output.append({
            "fixture": fixture,
            "kickoff": kickoff.isoformat() if hasattr(kickoff, "isoformat") else None,
            "stage_at": stage_at.isoformat(),
            "source": source,
            "terminal": path[-1],
        })
    return output


def _deduplicate_condition_rows(
    namespace: dict[str, Any],
) -> tuple[list[dict[str, Any]], int]:
    rows = namespace.get("observations") or []
    if not isinstance(rows, list):
        raise ValueError("Wilson observations must be an array")
    kept: list[dict[str, Any]] = []
    identities: dict[str, dict[str, Any]] = {}
    removed = 0
    for row in rows:
        if not isinstance(row, dict):
            kept.append(row)
            continue
        is_target = (
            str(row.get("frozen_condition_signature") or "") == SIGNATURE
            and str(row.get("stage") or "") == "T-30"
            and str(row.get("code") or row.get("market") or "") == "HIL"
        )
        if not is_target:
            kept.append(row)
            continue
        identity = str(row.get("observation_id") or "")
        if not identity:
            raise ValueError("condition #5 observation has no identity")
        prior = identities.get(identity)
        if prior is None:
            identities[identity] = row
            kept.append(row)
            continue
        if _canonical_hash(prior) != _canonical_hash(row):
            raise ValueError(f"conflicting duplicate observation: {identity}")
        removed += 1
    namespace["observations"] = kept
    return kept, removed


def _bind_recovered_quarter_snapshot(
    ledger: dict[str, Any], match: dict[str, Any], selected: dict[str, Any],
) -> None:
    line = float(selected.get("line", selected.get("condition")))
    if abs((abs(line) % 1) - 0.25) > 1e-8 and abs(
        (abs(line) % 1) - 0.75
    ) > 1e-8:
        return
    fixture = str(match["fixture"])
    watch = (ledger.get("watch") or {}).get(fixture)
    if not isinstance(watch, dict):
        raise ValueError(f"quarter-line fixture missing from watch: {fixture}")
    stage_matches = [
        row for row in watch.get("stages") or []
        if isinstance(row, dict)
        and row.get("stage") == "T-30"
        and _time(row.get("ts")) == _time(match["stage_at"])
        and not str(row.get("formal_admission_snapshot_id") or "").startswith(
            "recovered:"
        )
    ]
    if len(stage_matches) != 1:
        raise ValueError(f"quarter-line T-30 stage is ambiguous: {fixture}")
    stage = copy.deepcopy(stage_matches[0])
    quotes = [
        row for row in stage.get("market_predictions") or []
        if isinstance(row, dict)
        and str(row.get("code") or "").upper() == "HIL"
        and str(row.get("side") or "").upper()
        == str(selected.get("side") or "").upper()
        and abs(float(row.get("line", row.get("condition"))) - line) <= 1e-8
        and abs(float(row.get("odds")) - float(selected.get("odds"))) <= 1e-8
    ]
    if len(quotes) != 1:
        raise ValueError(f"quarter-line selected quote is ambiguous: {fixture}")
    profile = selected.get("quarter_line_settlement")
    if not isinstance(profile, dict):
        raise ValueError(f"quarter-line settlement profile unavailable: {fixture}")
    existing_profile = quotes[0].get("quarter_line_settlement")
    if existing_profile not in (None, profile):
        raise ValueError(f"quarter-line settlement profile conflicts: {fixture}")
    quotes[0]["quarter_line_settlement"] = copy.deepcopy(profile)
    for key in _CROWN_SNAPSHOT_MUTABLE:
        stage.pop(key, None)
    payload = {
        key: value for key, value in stage.items()
        if key not in _CROWN_SNAPSHOT_MUTABLE
    }
    snapshot_hash = hashlib.sha256(json.dumps(
        payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"),
        default=str,
    ).encode()).hexdigest()
    snapshot_id = f"recovered:{fixture}:T-30:{stage['ts']}"
    stage["formal_admission_snapshot_id"] = snapshot_id
    stage["formal_admission_snapshot_hash"] = snapshot_hash
    stage["formal_admission_pending"] = False
    stage["formal_admission_status"] = "COMPLETED"
    stage["formal_admission_reason"] = "condition5_missed_admission_recovery"
    existing_recovery = [
        row for row in watch.get("stages") or []
        if isinstance(row, dict)
        and row.get("formal_admission_snapshot_id") == snapshot_id
    ]
    if existing_recovery:
        if (
            len(existing_recovery) != 1
            or existing_recovery[0].get("formal_admission_snapshot_hash")
            != snapshot_hash
        ):
            raise ValueError(f"recovered snapshot conflicts: {fixture}")
    else:
        watch.setdefault("stages", []).append(stage)
    selected["native_snapshot_binding"] = {
        "schema_version": 1,
        "system": SYSTEM,
        "snapshot_id": snapshot_id,
        "snapshot_hash": snapshot_hash,
    }


def recover(
    ledger: dict[str, Any], history: dict[str, Any], *, apply: bool,
) -> dict[str, Any]:
    before_hash = _canonical_hash(ledger)
    now = datetime.now().astimezone().isoformat()
    namespace, frozen = _condition(ledger)
    if str(frozen.get("signature") or "") != SIGNATURE:
        raise ValueError("condition #5 signature changed")
    existing_migration = namespace.get("condition5_history_recovery_v1")
    if isinstance(existing_migration, dict) and existing_migration.get("completed"):
        return {
            "mode": "apply" if apply else "audit",
            "status": "already_completed",
            "stored": copy.deepcopy(existing_migration),
            "ledger_hash": before_hash,
        }

    observations, duplicates_removed = _deduplicate_condition_rows(namespace)
    history_rows = _rows(history)
    boundary = str(
        (frozen.get("active_evidence") or {}).get("activation_boundary_at") or ""
    )
    candidates = _candidate_matches(history_rows, frozen["definition"], boundary)
    authority = load_production_legacy_batch_authority(ledger)
    registry = formal_registry_candidates(
        ledger, SYSTEM, now=now, authority_context=authority,
    )
    formal_matches = match_formal_registry(
        history_rows, registry, system=SYSTEM, decision_stage="T-30",
    )
    existing_ids = {
        str(row.get("observation_id") or "")
        for row in observations if isinstance(row, dict)
    }
    existing_fixture_ids = {
        str(row.get("match_id") or "")
        for row in list(ledger.get("bets") or []) + observations
        if isinstance(row, dict)
        and str(row.get("frozen_condition_signature") or "") == SIGNATURE
        and str(row.get("stage") or "") == "T-30"
        and str(row.get("code") or row.get("market") or "") == "HIL"
    }
    result: dict[str, Any] = {
        "mode": "apply" if apply else "audit",
        "status": "ready",
        "migration": MIGRATION,
        "condition_signature": SIGNATURE,
        "matched_after_boundary": len(candidates),
        "accepted": 0,
        "settled": 0,
        "pending_result": 0,
        "existing_skipped": 0,
        "duplicates_removed": duplicates_removed,
        "rejected": 0,
        "reasons": Counter(),
        "fixtures": [],
    }
    for match in candidates:
        fixture = match["fixture"]
        identity = f"{fixture}|HIL|T-30|{SIGNATURE}|formal-observation"
        if identity in existing_ids or fixture in existing_fixture_ids:
            result["existing_skipped"] += 1
            continue
        selected = _selected(match)
        if selected is None:
            result["rejected"] += 1
            result["reasons"]["selected_prediction_missing_or_ambiguous"] += 1
            continue
        selected = _with_quarter_line_profile(selected, match["source"])
        _bind_recovered_quarter_snapshot(ledger, match, selected)
        exact_registry = [
            row for row in formal_matches.get(fixture, [])
            if row.get("__formal_frozen_signature") == SIGNATURE
        ]
        if len(exact_registry) != 1:
            result["rejected"] += 1
            result["reasons"][
                "exact_formal_registry_match_missing_or_ambiguous"
            ] += 1
            continue
        admissions, reason = matching_admissions(
            SYSTEM, "HIL", selected, exact_registry, stage_at=match["stage_at"],
        )
        exact = [row for row in admissions if row.get("signature") == SIGNATURE]
        if len(exact) != 1:
            result["rejected"] += 1
            result["reasons"][reason or "exact_condition_admission_missing"] += 1
            continue
        adjusted, binding_reason = apply_active_evidence(
            ledger, SYSTEM, exact[0], stage_at=match["stage_at"],
            now=match["stage_at"], authority_context=authority,
        )
        if adjusted is None:
            result["rejected"] += 1
            result["reasons"][
                binding_reason or "active_evidence_binding_failed"
            ] += 1
            continue
        row = record_match_observation(
            ledger, SYSTEM, _watch(match), "HIL", selected, adjusted,
            now=match["stage_at"], market_label="入球大細", selected_role="大",
            selected_line=float(match["terminal"]["selected_line"]),
            decision_stage="T-30", authority_context=authority,
        )
        if not isinstance(row, dict):
            result["rejected"] += 1
            result["reasons"]["observation_writer_rejected"] += 1
            continue
        grade = _grade(match)
        fixture_result = "PENDING"
        settled_at = None
        grade_hash = None
        if grade is None:
            result["pending_result"] += 1
        else:
            fixture_result, settled_at, grade_hash = grade
            row["status"] = "SETTLED"
            row["result"] = fixture_result
            row["settled_at"] = settled_at
            row.setdefault("history", []).append({
                "ts": settled_at,
                "stage": "SETTLED",
                "action": "條件 #5 漏入組修復：套用既有正常賽果",
                "result": fixture_result,
            })
            result["settled"] += 1
        result["accepted"] += 1
        existing_ids.add(identity)
        existing_fixture_ids.add(fixture)
        result["fixtures"].append({
            "match_id": fixture,
            "kickoff": match["kickoff"],
            "stage_at": match["stage_at"],
            "league": match["source"].get("league"),
            "home": match["source"].get("home"),
            "away": match["source"].get("away"),
            "side": selected.get("side"),
            "line": selected.get("line", selected.get("condition")),
            "odds": selected.get("odds"),
            "result": fixture_result,
            "settled_at": settled_at,
            "normal_grade_source_hash": grade_hash,
            "matched_without_result_input": True,
        })

    validation_reasons: Counter[str] = Counter()
    validation_samples: list[dict[str, Any]] = []
    projection_time = _time(now)
    if projection_time is None:
        raise ValueError("recovery projection time is invalid")
    for row in namespace.get("observations") or []:
        if (
            not isinstance(row, dict)
            or str(row.get("frozen_condition_signature") or "") != SIGNATURE
        ):
            continue
        admitted, validation_reason = validate_formal_row(
            row, system=SYSTEM, signature=SIGNATURE, frozen=frozen,
            projection_time=projection_time,
            require_settled=row.get("status") == "SETTLED",
            ledger=ledger, authority_context=authority,
        )
        key = "VALID" if admitted is not None else (
            validation_reason or "UNKNOWN_INVALID"
        )
        validation_reasons[key] += 1
        if admitted is None and len(validation_samples) < 10:
            validation_samples.append({
                "observation_id": row.get("observation_id"),
                "status": row.get("status"),
                "result": row.get("result"),
                "reason": key,
            })
    result["formal_validation_counts"] = dict(validation_reasons)
    result["formal_validation_invalid_samples"] = validation_samples
    recompute_namespace(ledger, SYSTEM, authority_context=authority)
    active = active_evidence_version(
        frozen, migration_boundary=namespace["activation_at"],
        authority_context=authority,
    )
    if not isinstance(active, dict):
        raise ValueError("condition #5 active evidence unavailable after recovery")
    after = {
        "active_version": active.get("version"),
        "hits": active.get("cumulative_hits"),
        "decided": active.get("cumulative_decided"),
        "wilson95_lower_raw": active.get("wilson95_lower_raw"),
        "minimum_acceptable_odds_raw": active.get("minimum_acceptable_odds_raw"),
        "pending_rollover_progress": copy.deepcopy(
            frozen.get("pending_rollover_progress") or {}
        ),
    }
    result["after_recovery"] = after
    result["reasons"] = dict(result["reasons"])
    result["before_ledger_hash"] = before_hash
    if apply:
        namespace["condition5_history_recovery_v1"] = {
            "completed": True,
            "migration": MIGRATION,
            "completed_at": now,
            "matched_after_boundary": result["matched_after_boundary"],
            "accepted": result["accepted"],
            "settled": result["settled"],
            "pending_result": result["pending_result"],
            "existing_skipped": result["existing_skipped"],
            "duplicates_removed": result["duplicates_removed"],
            "rejected": result["rejected"],
            "reason_counts": copy.deepcopy(result["reasons"]),
            "after_recovery": copy.deepcopy(after),
            "fixture_proof_root_hash": _hash(result["fixtures"]),
        }
        result["status"] = "applied"
    else:
        result["status"] = "audit_ready"
    result["after_ledger_hash"] = _canonical_hash(ledger)
    return result


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--ledger", type=Path, required=True)
    parser.add_argument("--history", type=Path, required=True)
    parser.add_argument("--apply", action="store_true")
    args = parser.parse_args()
    ledger = _read(args.ledger)
    result = recover(ledger, _read(args.history), apply=args.apply)
    if args.apply and result.get("status") == "applied":
        _write_atomic(args.ledger, ledger)
    print(json.dumps(result, ensure_ascii=False, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
