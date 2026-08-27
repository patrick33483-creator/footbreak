#!/usr/bin/env python3
"""Proof-gated recovery of Crown Wilson condition #2 first-look evidence.

The migration has two deliberately separate proofs:

1. prove that the legacy 44/71 "prospective" cohort is the latest 30% subset
   of the already-counted 141/231 discovery total, then supersede only that
   duplicate evidence version;
2. match later fixtures from immutable pre-kickoff first-look predictions
   without reading outcomes, create ordinary non-betting observations, and
   only then use the persisted normal grade to settle them.

Audit is the default.  ``--apply`` atomically replaces only the Crown ledger.
"""
from __future__ import annotations

import argparse
import copy
import hashlib
import json
import math
import os
import tempfile
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable

from analysis.granular_conditions import (
    MARKET_LABELS, _descriptor, _paths, canonical_panels,
)
from analysis.legacy_batch_runtime import load_production_legacy_batch_authority
from analysis.wilson_validation import (
    _canonical_hash, _time, _version_hash, active_evidence_version,
    apply_active_evidence, formal_registry_candidates, matching_admissions,
    recompute_namespace, record_match_observation, validate_formal_row,
)

SYSTEM = "crown"
CONDITION_NUMBER = 2
SIGNATURE = "8c240fb81cb9dda4fb50ecf7"
EXPECTED_AXES = {
    "system": "crown",
    "market": "HIL",
    "path": "首預",
    "stage": "首預",
    "odds_tier": "≥1.70",
    "direction": "A",
    "role": "大",
    "line_bucket": "2.75–3.0",
    "movement": "不變",
}
EXPECTED_BASELINE = {"hits": 141, "decided": 231, "pushes": 17, "settled": 248}
EXPECTED_DUPLICATE = {"hits": 44, "decided": 71, "pushes": 4, "settled": 75}
MIGRATION = "crown-condition2-first-look-history-v1"


def _hash(value: Any) -> str:
    return hashlib.sha256(json.dumps(
        value, ensure_ascii=False, sort_keys=True, separators=(",", ":"),
        default=str,
    ).encode()).hexdigest()


def _read(path: Path) -> dict[str, Any]:
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise ValueError(f"{path} must contain one JSON object")
    return value


def _write_atomic(path: Path, payload: dict[str, Any]) -> None:
    fd, temporary = tempfile.mkstemp(
        prefix=f".{path.name}.condition2.", dir=str(path.parent),
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


def _stamp(row: dict[str, Any]) -> str | None:
    for key in ("predicted_at", "ts", "source_snapshot_at", "created_at"):
        if _time(row.get(key)) is not None:
            return str(row[key])
    return None


def _kickoff(row: dict[str, Any]) -> str | None:
    for key in ("kickoff", "kickoff_hkt"):
        if _time(row.get(key)) is not None:
            return str(row[key])
    return None


def _number(value: Any) -> float | None:
    try:
        answer = float(value)
    except (TypeError, ValueError):
        return None
    return answer if math.isfinite(answer) else None


def _same_number(left: Any, right: Any) -> bool:
    a, b = _number(left), _number(right)
    return a is not None and b is not None and abs(a - b) <= 1e-8


def _target_key(definition: dict[str, Any]) -> tuple[str, ...]:
    values = (
        ("system", SYSTEM),
        ("market", definition.get("market")),
        ("path", definition.get("path")),
        ("decision", definition.get("stage")),
        ("tier", definition.get("odds_tier")),
        ("direction", definition.get("direction")),
        ("role", definition.get("role")),
        ("bucket", definition.get("line_bucket")),
        ("movement", definition.get("movement")),
    )
    return tuple(f"{key}={value}" for key, value in values if value not in (None, ""))


def _rows(document: dict[str, Any]) -> list[dict[str, Any]]:
    value = document.get("rows")
    if not isinstance(value, list):
        raise ValueError("prediction history rows are unavailable")
    return [row for row in value if isinstance(row, dict)]


def _raw_stage_map(
    rows: Iterable[dict[str, Any]],
) -> dict[tuple[str, str], dict[str, Any]]:
    output: dict[tuple[str, str], dict[str, Any]] = {}
    for row in rows:
        fixture = str(row.get("match_id") or row.get("history_key") or "").strip()
        stage = str(row.get("stage") or "").strip()
        if not fixture or not stage:
            continue
        key = fixture, stage
        previous = output.get(key)
        if previous is None or str(_stamp(row) or "") >= str(_stamp(previous) or ""):
            output[key] = row
    return output


def _matching_rows(
    rows: list[dict[str, Any]], definition: dict[str, Any], *,
    settled_only: bool,
) -> list[dict[str, Any]]:
    """Match the exact immutable axes; results are not read in admission mode."""
    wanted = _target_key(definition)
    raw = _raw_stage_map(rows)
    matches: dict[str, dict[str, Any]] = {}
    for panel in canonical_panels(rows, settled_only=settled_only):
        for path in _paths(panel, "首預"):
            if path[-1].get("stage") != "首預":
                continue
            key, _label, _specificity = _descriptor(SYSTEM, path, 2)
            if key != wanted:
                continue
            terminal = path[-1]
            fixture = str(panel.get("fixture") or "")
            source = raw.get((fixture, "首預"))
            if not fixture or not isinstance(source, dict):
                continue
            matches[fixture] = {
                "fixture": fixture,
                "panel": panel,
                "terminal": terminal,
                "source": source,
                "stage_at": _stamp(source),
                "kickoff": _kickoff(source),
            }
    return sorted(matches.values(), key=lambda item: (
        _time(item.get("stage_at")) or datetime.min.replace(tzinfo=timezone.utc),
        item["fixture"],
    ))


def _metrics(matches: Iterable[dict[str, Any]]) -> dict[str, int]:
    materialized = list(matches)
    hits = sum(item["terminal"].get("hit") is True for item in materialized)
    losses = sum(item["terminal"].get("hit") is False for item in materialized)
    pushes = sum(item["terminal"].get("hit") is None for item in materialized)
    return {
        "settled": len(materialized), "hits": hits, "losses": losses,
        "pushes": pushes, "decided": hits + losses,
    }


def _condition(ledger: dict[str, Any]) -> tuple[dict[str, Any], dict[str, Any]]:
    namespace = ledger.get("wilson_validation")
    if not isinstance(namespace, dict) or namespace.get("system") != SYSTEM:
        raise ValueError("Crown Wilson namespace is unavailable")
    conditions = namespace.get("conditions")
    if not isinstance(conditions, dict):
        raise ValueError("Crown frozen condition registry is unavailable")
    numbered = [
        row for row in conditions.values()
        if isinstance(row, dict)
        and int(row.get("condition_number") or 0) == CONDITION_NUMBER
    ]
    if len(numbered) != 1:
        raise ValueError(f"expected exactly one condition #2, found {len(numbered)}")
    frozen = numbered[0]
    if str(frozen.get("signature") or "") != SIGNATURE:
        raise ValueError("condition #2 signature changed")
    definition = frozen.get("definition")
    if not isinstance(definition, dict) or any(
        definition.get(key) != value for key, value in EXPECTED_AXES.items()
    ):
        raise ValueError("condition #2 immutable axes changed")
    return namespace, frozen


def _prove_duplicate(
    ledger: dict[str, Any], history_rows: list[dict[str, Any]],
) -> dict[str, Any]:
    namespace, frozen = _condition(ledger)
    historical = frozen.get("historical_evidence")
    if not isinstance(historical, dict):
        raise ValueError("condition #2 historical baseline is unavailable")
    artifact = historical.get("artifact")
    boundary_text = (
        artifact.get("as_of") if isinstance(artifact, dict) else None
    ) or frozen.get("frozen_at")
    boundary = _time(boundary_text)
    if boundary is None:
        raise ValueError("condition #2 discovery boundary is invalid")
    if (
        int(historical.get("hits") or -1) != EXPECTED_BASELINE["hits"]
        or int(historical.get("decided") or -1) != EXPECTED_BASELINE["decided"]
    ):
        raise ValueError("condition #2 stored baseline is not 141/231")

    eligible_history = [
        row for row in history_rows
        if (
            _time(_kickoff(row)) is not None
            and _time(_kickoff(row)) <= boundary
            and _time(row.get("verified_at")) is not None
            and _time(row.get("verified_at")) <= boundary
        )
    ]
    settled = _matching_rows(
        eligible_history, frozen["definition"], settled_only=True,
    )
    baseline = _metrics(settled)
    if any(baseline.get(key) != value for key, value in EXPECTED_BASELINE.items()):
        raise ValueError(f"reconstructed baseline mismatch: {baseline}")
    holdout_size = max(1, math.ceil(len(settled) * 0.30))
    holdout = settled[-holdout_size:]
    duplicate = _metrics(holdout)
    if any(duplicate.get(key) != value for key, value in EXPECTED_DUPLICATE.items()):
        raise ValueError(f"reconstructed duplicate holdout mismatch: {duplicate}")

    versions = frozen.get("evidence_versions")
    if not isinstance(versions, list) or len(versions) != 2:
        raise ValueError("condition #2 does not have the expected two-version chain")
    v1, v2 = versions
    if (
        not isinstance(v1, dict) or not isinstance(v2, dict)
        or v1.get("migration_baseline") is not True
        or int(v1.get("cumulative_hits") or -1) != 141
        or int(v1.get("cumulative_decided") or -1) != 231
        or v1.get("evidence_hash") != _version_hash(v1)
        or v2.get("initial_migration_full_cohort") is not True
        or v2.get("legacy_prospective_cohort") != {
            "hits": 44, "decided": 71, "pushes": 4,
        }
        or int(v2.get("cumulative_hits") or -1) != 185
        or int(v2.get("cumulative_decided") or -1) != 302
        or v2.get("prior_evidence_hash") != v1.get("evidence_hash")
        or v2.get("evidence_hash") != _version_hash(v2)
    ):
        raise ValueError("condition #2 duplicate evidence chain proof failed")
    return {
        "namespace": namespace,
        "frozen": frozen,
        "boundary": str(boundary_text),
        "baseline": baseline,
        "holdout": duplicate,
        "holdout_fixtures": [item["fixture"] for item in holdout],
        "v1": copy.deepcopy(v1),
        "v2": copy.deepcopy(v2),
    }


def _supersede_duplicate(proof: dict[str, Any], migration_at: str) -> None:
    frozen = proof["frozen"]
    v1, v2 = proof["v1"], proof["v2"]
    frozen["evidence_versions"] = [copy.deepcopy(v1)]
    frozen["active_evidence_version"] = 1
    frozen["active_evidence_hash"] = v1["evidence_hash"]
    frozen["active_evidence"] = {
        key: copy.deepcopy(v1.get(key)) for key in (
            "version", "cumulative_hits", "cumulative_decided",
            "wilson95_lower_raw", "minimum_acceptable_odds_raw",
            "minimum_acceptable_odds_display", "activation_boundary_at",
            "created_at", "evidence_hash",
        )
    }
    frozen["superseded_duplicate_evidence_versions"] = [{
        "migration": MIGRATION,
        "superseded_at": migration_at,
        "reason": "discovery_holdout_is_subset_of_historical_total",
        "proof_hash": _hash({
            "baseline": proof["baseline"],
            "holdout": proof["holdout"],
            "holdout_fixtures": proof["holdout_fixtures"],
            "version": v2,
        }),
        "version": copy.deepcopy(v2),
    }]
    frozen["rollover_audit"] = [
        row for row in frozen.get("rollover_audit") or []
        if not (isinstance(row, dict) and row.get("evidence_hash") == v2["evidence_hash"])
    ]
    frozen["prospective"] = {}
    frozen["prospective_observations"] = {}
    frozen["pending_rollover_progress"] = {
        "eligible_decided": 0, "eligible_hits": 0, "accuracy": None,
        "required": 20, "display": "0/20", "excluded": {},
    }
    frozen["rollover_status"] = "active"


def _selected(match: dict[str, Any]) -> dict[str, Any] | None:
    terminal, source = match["terminal"], match["source"]
    exact = [
        row for row in source.get("market_predictions") or []
        if isinstance(row, dict)
        and str(row.get("code") or "").upper() == "HIL"
        and str(row.get("side") or "").upper() == str(terminal.get("side") or "").upper()
        and _same_number(row.get("line", row.get("condition")), terminal.get("selected_line"))
        and _same_number(row.get("odds"), terminal.get("odds"))
    ]
    return copy.deepcopy(exact[0]) if len(exact) == 1 else None


def _grade(match: dict[str, Any]) -> tuple[str, str, str] | None:
    """Return result, settlement timestamp, and immutable result proof hash."""
    terminal, source = match["terminal"], match["source"]
    exact = [
        row for row in source.get("market_grades") or []
        if isinstance(row, dict)
        and str(row.get("code") or "").upper() == "HIL"
        and str(row.get("side") or "").upper() == str(terminal.get("side") or "").upper()
        and _same_number(row.get("line", row.get("condition")), terminal.get("selected_line"))
        and str(row.get("grade_status") or "") == "GRADED"
    ]
    if len(exact) != 1:
        return None
    grade = exact[0]
    raw_result = str(grade.get("settlement") or "").strip()
    normal = {
        "won": "Won", "half won": "Half Won", "lost": "Lost",
        "half lost": "Half Lost", "refunded": "Refunded",
        "push": "Refunded", "void": "Refunded",
    }.get(raw_result.lower())
    if normal is None:
        hit = grade.get("hit")
        normal = "Won" if hit is True else "Lost" if hit is False else (
            "Refunded" if hit is None else None
        )
    if normal is None:
        return None
    kickoff = _time(match.get("kickoff"))
    settled_at = next((
        str(value) for value in (
            grade.get("result_recorded_at"), grade.get("settled_at"),
            source.get("verified_at"), source.get("result_recorded_at"),
        )
        if _time(value) is not None
        and kickoff is not None
        and _time(value) >= kickoff
    ), None)
    if settled_at is None:
        return None
    return normal, settled_at, _hash({"source": source, "grade": grade})


def _watch(match: dict[str, Any]) -> dict[str, Any]:
    source = copy.deepcopy(match["source"])
    return {
        "match_id": match["fixture"],
        "league": source.get("league"),
        "home": source.get("home"),
        "away": source.get("away"),
        "kickoff": source.get("kickoff") or source.get("kickoff_hkt"),
        "kickoff_hkt": source.get("kickoff_hkt") or source.get("kickoff"),
        "hkjc_match_id": source.get("hkjc_match_id"),
        "native_fixture_id": source.get("native_fixture_id"),
        "titan_match_id": source.get("titan_match_id") or match["fixture"],
        "pinnapi_event_id": source.get("pinnapi_event_id"),
        "matching_version": source.get("matching_version"),
        "prediction_era": source.get("prediction_era"),
        "stages": [source],
    }


def recover(
    ledger: dict[str, Any], history: dict[str, Any], *, apply: bool,
) -> dict[str, Any]:
    history_rows = _rows(history)
    original_hash = _canonical_hash(ledger)
    migration_at = datetime.now().astimezone().isoformat()
    namespace, frozen = _condition(ledger)
    existing = namespace.get("condition2_history_recovery_v1")
    if isinstance(existing, dict) and existing.get("completed") is True:
        return {
            "mode": "apply" if apply else "audit",
            "status": "already_completed",
            "migration": MIGRATION,
            "stored": copy.deepcopy(existing),
            "ledger_hash": original_hash,
        }

    proof = _prove_duplicate(ledger, history_rows)
    _supersede_duplicate(proof, migration_at)
    authority = load_production_legacy_batch_authority(ledger)
    registry = formal_registry_candidates(
        ledger, SYSTEM, now=migration_at, authority_context=authority,
    )
    candidates = _matching_rows(
        history_rows, frozen["definition"], settled_only=False,
    )
    candidates = [
        item for item in candidates
        if _time(item.get("stage_at")) is not None
        and _time(item["stage_at"]) > _time(proof["boundary"])
    ]
    result: dict[str, Any] = {
        "mode": "apply" if apply else "audit",
        "status": "ready",
        "migration": MIGRATION,
        "condition_number": CONDITION_NUMBER,
        "condition_signature": SIGNATURE,
        "duplicate_proof": {
            "baseline": proof["baseline"],
            "duplicate_holdout": proof["holdout"],
            "all_duplicate_fixtures_are_in_baseline": True,
            "superseded_active": {"hits": 185, "decided": 302},
            "correct_active_before_new_history": {"hits": 141, "decided": 231},
        },
        "matched_after_boundary": len(candidates),
        "accepted": 0, "settled": 0, "pending_result": 0,
        "rejected": 0, "reasons": Counter(), "fixtures": [],
    }
    observations = namespace.setdefault("observations", [])
    if not isinstance(observations, list):
        raise ValueError("Crown Wilson observations container is malformed")
    if len(observations) + len(candidates) > 1600:
        raise ValueError(
            "recovery could exceed the 1600-observation retention limit; "
            "refusing to evict existing evidence"
        )

    for match in candidates:
        selected = _selected(match)
        if selected is None:
            result["rejected"] += 1
            result["reasons"]["selected_prediction_missing_or_ambiguous"] += 1
            continue
        stage_at = str(match["stage_at"])
        admissions, reason = matching_admissions(
            SYSTEM, "HIL", selected, registry, stage_at=stage_at,
        )
        exact = [row for row in admissions if row.get("signature") == SIGNATURE]
        if len(exact) != 1:
            result["rejected"] += 1
            result["reasons"][reason or "exact_condition_admission_missing"] += 1
            continue
        admission, reason = apply_active_evidence(
            ledger, SYSTEM, exact[0], stage_at=stage_at, now=stage_at,
            authority_context=authority,
        )
        if admission is None:
            result["rejected"] += 1
            result["reasons"][reason or "active_evidence_unavailable"] += 1
            continue
        observation_id = (
            f"{match['fixture']}|HIL|首預|{SIGNATURE}|formal-observation"
        )
        prior = [
            row for row in observations
            if isinstance(row, dict) and row.get("observation_id") == observation_id
        ]
        if prior:
            result["rejected"] += 1
            result["reasons"]["existing_observation_requires_manual_review"] += 1
            continue
        row = record_match_observation(
            ledger, SYSTEM, _watch(match), "HIL", selected, admission,
            now=stage_at, market_label=MARKET_LABELS["HIL"],
            selected_role="大", selected_line=float(match["terminal"]["selected_line"]),
            decision_stage="首預", authority_context=authority,
        )
        if row is None:
            result["rejected"] += 1
            result["reasons"]["canonical_observation_creation_failed"] += 1
            continue
        row["historical_recovery"] = {
            "schema_version": 1,
            "migration": MIGRATION,
            "recovered_at": migration_at,
            "admission_source_hash": _hash({
                "source": match["source"], "selected": selected,
                "condition_signature": SIGNATURE,
            }),
            "matched_without_result_input": True,
        }
        admitted, validation_reason = validate_formal_row(
            row, system=SYSTEM, signature=SIGNATURE, frozen=frozen,
            projection_time=datetime.now().astimezone(), require_settled=False,
            ledger=ledger, authority_context=authority,
        )
        if admitted is None:
            observations.remove(row)
            result["rejected"] += 1
            result["reasons"][validation_reason or "formal_row_validation_failed"] += 1
            continue
        grade = _grade(match)
        fixture_result = "PENDING"
        if grade is None:
            result["pending_result"] += 1
        else:
            settlement, settled_at, grade_hash = grade
            row.update({
                "status": "SETTLED",
                "result": settlement,
                "settled_at": settled_at,
                "settlement_source": "persisted_normal_market_grade",
            })
            row["historical_recovery"]["normal_grade_source_hash"] = grade_hash
            row.setdefault("history", []).append({
                "ts": migration_at, "stage": "歷史修復",
                "action": "以既有首預決策及正常賽果補回正式觀察",
                "result": settlement, "source_hash": grade_hash,
            })
            admitted, validation_reason = validate_formal_row(
                row, system=SYSTEM, signature=SIGNATURE, frozen=frozen,
                projection_time=datetime.now().astimezone(), require_settled=True,
                ledger=ledger, authority_context=authority,
            )
            if admitted is None:
                observations.remove(row)
                result["rejected"] += 1
                result["reasons"][
                    validation_reason or "settled_row_validation_failed"
                ] += 1
                continue
            fixture_result = settlement
            result["settled"] += 1
        result["accepted"] += 1
        result["fixtures"].append({
            "match_id": match["fixture"],
            "kickoff": match["kickoff"],
            "stage_at": stage_at,
            "league": match["source"].get("league"),
            "home": match["source"].get("home"),
            "away": match["source"].get("away"),
            "side": selected.get("side"),
            "line": selected.get("line", selected.get("condition")),
            "odds": selected.get("odds"),
            "result": fixture_result,
            "evidence_version_at_admission": row.get("evidence_version"),
        })

    recompute_namespace(ledger, SYSTEM, authority_context=authority)
    active = active_evidence_version(
        frozen, migration_boundary=namespace["activation_at"],
        authority_context=authority,
    )
    if not isinstance(active, dict):
        raise ValueError("condition #2 active evidence is unavailable after recovery")
    progress = copy.deepcopy(frozen.get("pending_rollover_progress") or {})
    result["after_recovery"] = {
        "active_version": active.get("version"),
        "hits": active.get("cumulative_hits"),
        "decided": active.get("cumulative_decided"),
        "wilson95_lower_raw": active.get("wilson95_lower_raw"),
        "minimum_acceptable_odds_raw": active.get("minimum_acceptable_odds_raw"),
        "pending_rollover_progress": progress,
    }
    result["reasons"] = dict(result["reasons"])
    result["before_ledger_hash"] = original_hash
    result["status"] = "applied" if apply else "audit_ready"
    if apply:
        namespace["condition2_history_recovery_v1"] = {
            "completed": True,
            "migration": MIGRATION,
            "completed_at": migration_at,
            "proof_hash": _hash(result["duplicate_proof"]),
            "matched_after_boundary": result["matched_after_boundary"],
            "accepted": result["accepted"],
            "settled": result["settled"],
            "pending_result": result["pending_result"],
            "rejected": result["rejected"],
            "reason_counts": copy.deepcopy(result["reasons"]),
            "after_recovery": copy.deepcopy(result["after_recovery"]),
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
