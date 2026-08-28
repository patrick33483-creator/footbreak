#!/usr/bin/env python3
"""Proof-gated independent-history recovery for Crown Wilson condition #6."""
from __future__ import annotations

import argparse
import copy
import hashlib
import json
import math
import os
import tempfile
from collections import Counter
from datetime import datetime
from pathlib import Path
from typing import Any

from analysis.audit_crown_condition6_history import (
    CONDITION_NUMBER, EXPECTED_BASELINE, EXPECTED_DUPLICATE, SIGNATURE,
    SYSTEM, _condition, _kickoff, _matches, _raw_stage_map, _read, _rows,
    _stamp, audit,
)
from analysis.legacy_batch_runtime import load_production_legacy_batch_authority
from analysis.wilson_validation import (
    _canonical_hash, _evidence_values, _time, _version_hash,
    active_evidence_version, formal_registry_candidates, match_formal_registry,
    matching_admissions, recompute_namespace,
)


MIGRATION = "crown-condition6-independent-history-v1"
MIGRATION_FIELD = "condition6_independent_history_recovery_v1"
EXPECTED_RECOVERY = {
    "accepted": 105,
    "settled": 47,
    "hits": 26,
    "losses": 21,
    "pushes": 0,
    "pending": 58,
    "final_hits": 87,
    "final_decided": 144,
}


def _hash(value: Any) -> str:
    return hashlib.sha256(json.dumps(
        value, ensure_ascii=False, sort_keys=True, separators=(",", ":"),
        default=str,
    ).encode()).hexdigest()


def _write_atomic(path: Path, payload: dict[str, Any]) -> None:
    fd, temporary = tempfile.mkstemp(
        prefix=f".{path.name}.condition6.", dir=str(path.parent),
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


def _number(value: Any) -> float | None:
    try:
        answer = float(value)
    except (TypeError, ValueError):
        return None
    return answer if math.isfinite(answer) else None


def _same_number(left: Any, right: Any) -> bool:
    a, b = _number(left), _number(right)
    return a is not None and b is not None and abs(a - b) <= 1e-8


def _selected(match: dict[str, Any]) -> dict[str, Any] | None:
    terminal, source = match["path"][-1], match["source"]
    exact = [
        row for row in source.get("market_predictions") or []
        if isinstance(row, dict)
        and str(row.get("code") or "").upper() == "HDC"
        and str(row.get("side") or "").upper()
        == str(terminal.get("side") or "").upper()
        and _same_number(
            row.get("line", row.get("condition")),
            terminal.get("selected_line"),
        )
        and _same_number(row.get("odds"), terminal.get("odds"))
    ]
    return copy.deepcopy(exact[0]) if len(exact) == 1 else None


def _grade(match: dict[str, Any]) -> tuple[str, str, str] | None:
    terminal, source = match["path"][-1], match["source"]
    exact = [
        row for row in source.get("market_grades") or []
        if isinstance(row, dict)
        and str(row.get("code") or "").upper() == "HDC"
        and str(row.get("side") or "").upper()
        == str(terminal.get("side") or "").upper()
        and _same_number(
            row.get("line", row.get("condition")),
            terminal.get("selected_line"),
        )
        and str(row.get("grade_status") or "") == "GRADED"
    ]
    if len(exact) != 1:
        return None
    grade = exact[0]
    raw = str(grade.get("settlement") or "").strip().lower()
    normal = {
        "won": "Won",
        "half won": "Half Won",
        "lost": "Lost",
        "half lost": "Half Lost",
        "refunded": "Refunded",
        "push": "Refunded",
        "void": "Refunded",
    }.get(raw)
    if normal is None:
        hit = grade.get("hit")
        normal = (
            "Won" if hit is True
            else "Lost" if hit is False
            else "Refunded" if hit is None
            else None
        )
    if normal is None:
        return None
    kickoff = match.get("panel", {}).get("kickoff") or _kickoff(source)
    settled_at = next((
        str(value) for value in (
            grade.get("result_recorded_at"),
            grade.get("settled_at"),
            source.get("verified_at"),
            source.get("result_recorded_at"),
        )
        if _time(value) is not None
        and _time(kickoff) is not None
        and _time(value) >= _time(kickoff)
    ), None)
    if settled_at is None:
        return None
    return normal, settled_at, _hash({"source": source, "grade": grade})


def _candidates(
    history_rows: list[dict[str, Any]], definition: dict[str, Any],
    boundary: Any,
) -> list[dict[str, Any]]:
    raw = _raw_stage_map(history_rows)
    boundary_at = _time(boundary)
    if boundary_at is None:
        raise ValueError("condition #6 discovery boundary is unavailable")
    baseline_rows = [
        row for row in history_rows
        if _kickoff(row) is not None
        and _kickoff(row) <= boundary_at
        and _time(row.get("verified_at")) is not None
        and _time(row.get("verified_at")) <= boundary_at
    ]
    baseline_fixtures = {
        str(item["panel"].get("fixture") or "").strip()
        for item in _matches(baseline_rows, definition, settled_only=True)
    }
    if len(baseline_fixtures) != EXPECTED_BASELINE["decided"]:
        raise ValueError(
            "condition #6 reconstructed baseline fixture count changed: "
            f"{len(baseline_fixtures)}"
        )
    output: list[dict[str, Any]] = []
    for item in _matches(history_rows, definition, settled_only=False):
        fixture = str(item["panel"].get("fixture") or "")
        source = raw.get((fixture, "T-30"))
        stage_at = _stamp(source or {})
        if (
            not fixture
            or fixture in baseline_fixtures
            or not isinstance(source, dict)
            or stage_at is None
        ):
            continue
        output.append({
            **item,
            "fixture": fixture,
            "source": source,
            "stage_at": stage_at.isoformat(),
            "kickoff": (
                item["panel"]["kickoff"].isoformat()
                if hasattr(item["panel"].get("kickoff"), "isoformat") else None
            ),
        })
    return output


def _merge_independent_v2(
    frozen: dict[str, Any], recovered: list[dict[str, Any]],
    migration_at: str,
) -> dict[str, int]:
    versions = frozen["evidence_versions"]
    v1 = copy.deepcopy(versions[0])
    original_v2 = copy.deepcopy(versions[1])
    hits = sum(row["result"] in {"Won", "Half Won"} for row in recovered)
    losses = sum(row["result"] in {"Lost", "Half Lost"} for row in recovered)
    pushes = sum(row["result"] == "Refunded" for row in recovered)
    pending = sum(row["result"] == "PENDING" for row in recovered)
    decided = hits + losses
    cumulative_hits = EXPECTED_BASELINE["hits"] + hits
    cumulative_decided = EXPECTED_BASELINE["decided"] + decided
    values = _evidence_values(cumulative_hits, cumulative_decided)
    row_hashes = sorted(_hash(row) for row in recovered)
    v2 = {
        **original_v2,
        "batch_fixture_market_hashes": [],
        "batch_fixture_market_ids_unavailable_from_legacy_aggregate": True,
        "batch_hits": hits,
        "batch_decided": decided,
        "cumulative_hits": cumulative_hits,
        "cumulative_decided": cumulative_decided,
        "wilson95_lower_raw": values["wilson95_lower_raw"],
        "minimum_acceptable_odds_raw": values["minimum_acceptable_odds_raw"],
        "minimum_acceptable_odds_display": values["display"][
            "minimum_acceptable_odds"
        ],
        "activation_boundary_at": migration_at,
        "created_at": migration_at,
        "initial_migration_full_cohort": False,
        "legacy_prospective_cohort": {
            "hits": hits, "decided": decided, "pushes": pushes,
        },
        "condition6_independent_history_recovery": {
            "schema_version": 1,
            "migration": MIGRATION,
            "independent_starting_cohort": copy.deepcopy(EXPECTED_BASELINE),
            "removed_duplicate_holdout": copy.deepcopy(EXPECTED_DUPLICATE),
            "recovered": {
                "hits": hits, "losses": losses, "decided": decided,
                "pushes": pushes, "pending": pending,
            },
            "fixture_rows_root_hash": _hash(row_hashes),
            "fixture_row_hashes": row_hashes,
            "superseded_v2_evidence_hash": original_v2["evidence_hash"],
        },
    }
    v2["evidence_hash"] = _version_hash(v2)
    frozen["evidence_versions"] = [v1, v2]
    frozen["active_evidence_version"] = 2
    frozen["active_evidence_hash"] = v2["evidence_hash"]
    frozen["active_evidence"] = {
        key: copy.deepcopy(v2.get(key)) for key in (
            "version", "cumulative_hits", "cumulative_decided",
            "wilson95_lower_raw", "minimum_acceptable_odds_raw",
            "minimum_acceptable_odds_display", "activation_boundary_at",
            "created_at", "evidence_hash",
        )
    }
    frozen["superseded_pre_history_recovery_v2"] = [{
        "migration": MIGRATION,
        "superseded_at": migration_at,
        "reason": "duplicate_holdout_removed_and_independent_history_merged",
        "version": original_v2,
    }]
    frozen["rollover_audit"] = [copy.deepcopy(v2)]
    frozen["prospective"] = {}
    frozen["prospective_observations"] = {}
    frozen["pending_rollover_progress"] = {
        "eligible_decided": 0,
        "eligible_hits": 0,
        "accuracy": None,
        "required": 20,
        "display": "0/20",
        "excluded": {},
    }
    frozen["rollover_status"] = "active"
    frozen["historical_recovery_rows"] = copy.deepcopy(recovered)
    return {
        "hits": hits, "losses": losses, "decided": decided,
        "pushes": pushes, "pending": pending,
    }


def recover(
    ledger: dict[str, Any], history: dict[str, Any], *, apply: bool,
) -> dict[str, Any]:
    before_hash = _canonical_hash(ledger)
    namespace, frozen = _condition(ledger)
    existing = namespace.get(MIGRATION_FIELD)
    if isinstance(existing, dict) and existing.get("completed") is True:
        return {
            "mode": "apply" if apply else "audit",
            "status": "already_completed",
            "stored": copy.deepcopy(existing),
            "ledger_hash": before_hash,
        }

    proof = audit(ledger, history)
    duplicate = proof["legacy_duplicate_proof"]
    if (
        duplicate.get("baseline_matches") is not True
        or duplicate.get("v2_is_duplicate_holdout") is not True
    ):
        raise ValueError("condition #6 duplicate proof failed")
    history_rows = _rows(history)
    artifact = (frozen.get("historical_evidence") or {}).get("artifact") or {}
    boundary = artifact.get("as_of") or frozen.get("frozen_at")
    candidates = _candidates(history_rows, frozen["definition"], boundary)
    migration_at = datetime.now().astimezone().isoformat()
    authority = load_production_legacy_batch_authority(ledger)
    registry = formal_registry_candidates(
        ledger, SYSTEM, now=migration_at, authority_context=authority,
    )
    formal_matches = match_formal_registry(
        history_rows, registry, system=SYSTEM, decision_stage="T-30",
    )
    result: dict[str, Any] = {
        "mode": "apply" if apply else "audit",
        "status": "ready",
        "migration": MIGRATION,
        "condition_number": CONDITION_NUMBER,
        "condition_signature": SIGNATURE,
        "starting_proof": {
            "stored_active": {"hits": 80, "decided": 127},
            "independent_baseline": copy.deepcopy(EXPECTED_BASELINE),
            "duplicate_holdout_removed": copy.deepcopy(EXPECTED_DUPLICATE),
        },
        "matched_outside_independent_baseline": len(candidates),
        "accepted": 0,
        "settled": 0,
        "pending_result": 0,
        "rejected": 0,
        "reasons": Counter(),
        "fixtures": [],
    }
    for match in candidates:
        selected = _selected(match)
        if selected is None:
            result["rejected"] += 1
            result["reasons"]["selected_prediction_missing_or_ambiguous"] += 1
            continue
        exact_registry = [
            row for row in formal_matches.get(match["fixture"], [])
            if row.get("__formal_frozen_signature") == SIGNATURE
        ]
        if len(exact_registry) != 1:
            result["rejected"] += 1
            result["reasons"][
                "exact_formal_registry_match_missing_or_ambiguous"
            ] += 1
            continue
        admissions, reason = matching_admissions(
            SYSTEM, "HDC", selected, exact_registry,
            stage_at=match["stage_at"],
        )
        exact = [row for row in admissions if row.get("signature") == SIGNATURE]
        if len(exact) != 1:
            result["rejected"] += 1
            result["reasons"][reason or "exact_condition_admission_missing"] += 1
            continue
        grade = _grade(match)
        fixture_result = "PENDING"
        settled_at = None
        grade_hash = None
        if grade is None:
            result["pending_result"] += 1
        else:
            fixture_result, settled_at, grade_hash = grade
            result["settled"] += 1
        result["accepted"] += 1
        result["fixtures"].append({
            "match_id": match["fixture"],
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
            "admission_source_hash": _hash({
                "source": match["source"],
                "selected": selected,
                "condition_signature": SIGNATURE,
            }),
            "normal_grade_source_hash": grade_hash,
            "matched_without_result_input": True,
        })

    counts = _merge_independent_v2(frozen, result["fixtures"], migration_at)
    result["recovered_counts"] = counts
    actual = {
        "accepted": result["accepted"],
        "settled": result["settled"],
        "hits": counts["hits"],
        "losses": counts["losses"],
        "pushes": counts["pushes"],
        "pending": counts["pending"],
        "final_hits": EXPECTED_BASELINE["hits"] + counts["hits"],
        "final_decided": EXPECTED_BASELINE["decided"] + counts["decided"],
    }
    if actual != EXPECTED_RECOVERY or result["rejected"] != 0:
        raise ValueError(
            f"condition #6 recovery proof mismatch: actual={actual}, "
            f"rejected={result['rejected']}, reasons={dict(result['reasons'])}"
        )
    recompute_namespace(ledger, SYSTEM, authority_context=authority)
    active = active_evidence_version(
        frozen, migration_boundary=namespace["activation_at"],
        authority_context=authority,
    )
    if not isinstance(active, dict):
        raise ValueError("condition #6 active evidence unavailable after recovery")
    after = {
        "active_version": active.get("version"),
        "hits": active.get("cumulative_hits"),
        "decided": active.get("cumulative_decided"),
        "wilson95_lower_raw": active.get("wilson95_lower_raw"),
        "minimum_acceptable_odds_raw": active.get(
            "minimum_acceptable_odds_raw"
        ),
        "minimum_acceptable_odds_display": active.get(
            "minimum_acceptable_odds_display"
        ),
        "pending_rollover_progress": copy.deepcopy(
            frozen.get("pending_rollover_progress") or {}
        ),
    }
    if (
        after["active_version"] != 2
        or after["hits"] != 87
        or after["decided"] != 144
        or after["pending_rollover_progress"].get("display") != "0/20"
    ):
        raise ValueError(f"condition #6 post-recovery state mismatch: {after}")
    result["after_recovery"] = after
    result["reasons"] = dict(result["reasons"])
    result["before_ledger_hash"] = before_hash
    result["status"] = "applied" if apply else "audit_ready"
    if apply:
        namespace[MIGRATION_FIELD] = {
            "completed": True,
            "migration": MIGRATION,
            "completed_at": migration_at,
            "proof_hash": _hash(result["starting_proof"]),
            "matched_outside_independent_baseline": result[
                "matched_outside_independent_baseline"
            ],
            "accepted": result["accepted"],
            "settled": result["settled"],
            "pending_result": result["pending_result"],
            "rejected": result["rejected"],
            "after_recovery": copy.deepcopy(after),
        }
    result["after_ledger_hash"] = _canonical_hash(ledger)
    return result


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--ledger", type=Path, required=True)
    parser.add_argument("--history", type=Path, required=True)
    parser.add_argument("--apply", action="store_true")
    args = parser.parse_args()
    ledger = _read(args.ledger)
    history = _read(args.history)
    working = ledger if args.apply else copy.deepcopy(ledger)
    report = recover(working, history, apply=args.apply)
    if args.apply and report.get("status") == "applied":
        _write_atomic(args.ledger, working)
    print(json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
