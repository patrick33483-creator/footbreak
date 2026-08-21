"""Proof-gated recovery for legacy formal low-odds Wilson observations.

This is deliberately a one-purpose migration, not a backfill miner.  It only
repairs existing ``NO_BET_LOW_ODDS`` rows in the formal observation namespace
when their own pre-existing native T-5 and frozen-condition evidence prove
admission independently of a result.  Grades are read only after admission is
proved, solely to settle the recovered evidence row.
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
from pathlib import Path
from typing import Any, Iterable

from analysis.wilson_validation import (
    DECISION_STAGE, STRATEGY, _rollover_marker, _time, active_evidence_version,
    condition_signature, ensure_namespace, recompute_namespace,
)

SYSTEMS = ("footbreak", "crown")


def _hash(value: Any) -> str:
    return hashlib.sha256(json.dumps(
        value, ensure_ascii=False, sort_keys=True, separators=(",", ":"),
    ).encode("utf-8")).hexdigest()


def _number(value: Any) -> float | None:
    try:
        answer = float(value)
    except (TypeError, ValueError):
        return None
    return answer if math.isfinite(answer) else None


def _same_number(left: Any, right: Any) -> bool:
    a, b = _number(left), _number(right)
    return a is not None and b is not None and abs(a - b) <= 1e-8


def _stage_time(row: dict[str, Any]) -> str | None:
    for key in ("ts", "source_snapshot_at", "predicted_at", "created_at"):
        value = row.get(key)
        if _time(value) is not None:
            return str(value)
    return None


def _kickoff(row: dict[str, Any]) -> str | None:
    for key in ("kickoff", "kickoff_hkt"):
        value = row.get(key)
        if _time(value) is not None:
            return str(value)
    return None


def _history_rows(value: Any) -> list[dict[str, Any]]:
    """Return every persisted stage-shaped row without inventing a schema."""
    found: list[dict[str, Any]] = []
    seen: set[int] = set()

    def visit(item: Any) -> None:
        if isinstance(item, dict):
            marker = id(item)
            if marker in seen:
                return
            seen.add(marker)
            if item.get("match_id") is not None and item.get("stage") is not None:
                found.append(item)
            for child in item.values():
                visit(child)
        elif isinstance(item, list):
            for child in item:
                visit(child)

    visit(value)
    return found


def _selected_stage_item(
    stage: dict[str, Any], row: dict[str, Any], *, grade: bool,
) -> dict[str, Any] | None:
    source = stage.get("market_grades") if grade else stage.get("market_predictions")
    exact: list[dict[str, Any]] = []
    for item in source or []:
        if not isinstance(item, dict):
            continue
        if str(item.get("code") or "").upper() != str(row.get("code") or row.get("market") or "").upper():
            continue
        if str(item.get("side") or "").upper() != str(row.get("side") or "").upper():
            continue
        if not _same_number(item.get("line", item.get("condition")), row.get("line", row.get("condition"))):
            continue
        if not grade and not _same_number(item.get("odds"), row.get("odds")):
            continue
        exact.append(item)
    return exact[0] if len(exact) == 1 else None


def _frozen_proof(
    ledger: dict[str, Any], system: str, row: dict[str, Any], stage_at: str,
) -> tuple[dict[str, Any] | None, str | None]:
    ns = ensure_namespace(ledger, system)
    signature = str(row.get("frozen_condition_signature") or "")
    frozen = (ns.get("conditions") or {}).get(signature)
    if not signature or not isinstance(frozen, dict):
        return None, "frozen_condition_missing"
    if str(frozen.get("signature") or signature) != signature:
        return None, "frozen_condition_signature_conflict"
    try:
        if int(frozen.get("condition_number")) != int(row.get("condition_number")):
            return None, "frozen_condition_number_conflict"
    except (TypeError, ValueError):
        return None, "frozen_condition_number_unresolvable"
    definition = frozen.get("definition")
    history = frozen.get("historical_evidence")
    if not isinstance(definition, dict) or not isinstance(history, dict):
        return None, "frozen_condition_definition_or_history_missing"
    # Exact signature/version must be resolvable from immutable persisted
    # content.  A namespace rewrite is never repaired by guessing aliases.
    candidate = {**copy.deepcopy(definition), "key": copy.deepcopy(definition.get("miner_key"))}
    rebuilt, rebuilt_definition = condition_signature(system, candidate)
    if (
        rebuilt != signature
        or rebuilt_definition != definition
        or str(definition.get("system") or "") != system
        or _time(frozen.get("frozen_at")) is None
        or _time(frozen.get("frozen_at")) > _time(stage_at)
    ):
        return None, "frozen_condition_signature_or_time_unproven"
    try:
        if int(history.get("hits")) < 0 or int(history.get("decided")) < 50 or int(history["hits"]) > int(history["decided"]):
            return None, "frozen_historical_evidence_invalid"
    except (KeyError, TypeError, ValueError):
        return None, "frozen_historical_evidence_invalid"
    active = active_evidence_version(frozen, migration_boundary=ns["activation_at"])
    if not isinstance(active, dict):
        return None, "frozen_evidence_version_unavailable"
    # Rollover consumes only rows strictly after the frozen evidence boundary;
    # preserving this check avoids a retrospective learned threshold.
    boundary = _time(active.get("activation_boundary_at"))
    if boundary is None or _time(stage_at) is None or _time(stage_at) <= boundary:
        return None, "stage_not_after_frozen_evidence_boundary"
    return {"frozen": frozen, "active": active, "history": history}, None


def _native_t5_proof(
    history: Iterable[dict[str, Any]], system: str, row: dict[str, Any],
) -> tuple[dict[str, Any] | None, str | None]:
    fixture = str(row.get("match_id") or "")
    created_at = _time(row.get("created_at"))
    kickoff = _time(row.get("kickoff"))
    if not fixture or created_at is None or kickoff is None or created_at >= kickoff:
        return None, "legacy_row_not_provably_pre_kickoff"
    choices: list[tuple[dict[str, Any], dict[str, Any], str, str]] = []
    for stage in history:
        if str(stage.get("match_id") or "") != fixture or str(stage.get("stage") or "") != DECISION_STAGE:
            continue
        stage_at, stage_kickoff = _stage_time(stage), _kickoff(stage)
        if stage_at is None or stage_kickoff is None:
            continue
        if _time(stage_at) != created_at or _time(stage_kickoff) != kickoff or _time(stage_at) >= _time(stage_kickoff):
            continue
        if stage.get("post_hoc_backfill") or stage.get("exclude_from_simulation"):
            continue
        item = _selected_stage_item(stage, row, grade=False)
        if item is None:
            continue
        source = str(item.get("quote_source") or item.get("source") or "").strip().lower()
        if not source or source in {"none", "fallback", "model_only", "model-only", "unavailable"}:
            continue
        if _time(item.get("observed_at")) is None or _time(item.get("observed_at")) >= _time(stage_kickoff):
            continue
        choices.append((stage, item, stage_at, stage_kickoff))
    if len(choices) != 1:
        return None, "native_t5_missing_or_ambiguous"
    stage, item, stage_at, stage_kickoff = choices[0]
    return {
        "stage": stage, "item": item, "stage_at": stage_at,
        "kickoff": stage_kickoff,
        "source_hash": _hash({"stage": stage, "selected": item}),
    }, None


def _grade_proof(
    history: Iterable[dict[str, Any]], row: dict[str, Any], native: dict[str, Any],
) -> tuple[dict[str, Any] | None, str | None]:
    """Read a normal persisted grade only after independent admission proof."""
    fixture = str(row.get("match_id") or "")
    candidates: list[tuple[dict[str, Any], dict[str, Any]]] = []
    for stage in history:
        if (
            str(stage.get("match_id") or "") != fixture
            or str(stage.get("stage") or "") != DECISION_STAGE
            or _stage_time(stage) != native["stage_at"]
        ):
            continue
        grade = _selected_stage_item(stage, row, grade=True)
        if grade is None or str(grade.get("grade_status") or "") != "GRADED":
            continue
        if grade.get("hit") not in (True, False, None):
            continue
        candidates.append((stage, grade))
    if len(candidates) != 1:
        return None, "normal_result_grade_missing_or_ambiguous"
    stage, grade = candidates[0]
    result = "Won" if grade.get("hit") is True else "Lost" if grade.get("hit") is False else "Refunded"
    return {
        "result": result, "source_hash": _hash({"stage": stage, "grade": grade}),
        "grade": grade,
    }, None


def _conflict_reason(ns: dict[str, Any], row: dict[str, Any]) -> str | None:
    key = (
        str(row.get("match_id") or ""), str(row.get("code") or row.get("market") or ""),
        str(row.get("frozen_condition_signature") or ""),
    )
    same = []
    for item in ns.get("observations") or []:
        if not isinstance(item, dict):
            continue
        item_key = (
            str(item.get("match_id") or ""), str(item.get("code") or item.get("market") or ""),
            str(item.get("frozen_condition_signature") or ""),
        )
        if item_key == key:
            same.append(item)
    if len(same) != 1:
        return "duplicate_or_conflicting_observation"
    for item in ns.get("observations") or []:
        if not isinstance(item, dict) or item is row:
            continue
        if (
            str(item.get("match_id") or "") == key[0]
            and str(item.get("code") or item.get("market") or "") == key[1]
            and str(item.get("frozen_condition_signature") or "") != key[2]
        ):
            return "duplicate_or_conflicting_observation"
    for item in ledger_bets(ns):
        if (
            str(item.get("match_id") or "") == key[0]
            and str(item.get("code") or item.get("market") or "") == key[1]
            and str(item.get("frozen_condition_signature") or "") == key[2]
        ):
            return "already_counted_formal_bet"
    return None


def ledger_bets(ns: dict[str, Any]) -> list[dict[str, Any]]:
    # Kept as a helper to make the conflict call-site explicit: Wilson bets
    # live on the root ledger and are supplied by recovery_system below.
    return list(ns.get("__recovery_root_bets") or [])


def recover_system(
    ledger: dict[str, Any], history_document: Any, system: str, *, apply: bool,
) -> dict[str, Any]:
    ns = ensure_namespace(ledger, system)
    ns["__recovery_root_bets"] = [
        row for row in ledger.get("bets") or [] if isinstance(row, dict)
    ]
    history = _history_rows(history_document)
    report: dict[str, Any] = {
        "system": system, "mode": "apply" if apply else "audit",
        "accepted": 0, "rejected": 0, "skipped": 0, "reasons": Counter(),
        "rows": [],
    }
    for row in ns.get("observations") or []:
        if not isinstance(row, dict):
            continue
        if not (
            row.get("portfolio") == f"{system}_wilson_observations"
            and row.get("strategy") == STRATEGY
            and row.get("formal_bet") is False
            and row.get("bet_status") == "NO_BET_LOW_ODDS"
            and row.get("stage") == DECISION_STAGE
        ):
            continue
        identifier = str(row.get("observation_id") or "")
        if isinstance(row.get("rollover_provenance"), dict) and row.get("status") in {"PENDING", "SETTLED", "VOIDED"}:
            report["skipped"] += 1
            report["reasons"]["already_recovered_or_current"] += 1
            continue
        conflict = _conflict_reason(ns, row)
        native, native_reason = _native_t5_proof(history, system, row)
        frozen, frozen_reason = _frozen_proof(ledger, system, row, (native or {}).get("stage_at") or "")
        # Admission proof never reads grade/result data.
        reason = conflict or native_reason or frozen_reason
        grade = None
        if reason is None and native is not None:
            grade, reason = _grade_proof(history, row, native)
        if reason is not None:
            report["rejected"] += 1
            report["reasons"][reason] += 1
            report["rows"].append({"observation_id": identifier, "status": "REJECTED", "reason": reason})
            continue
        assert native is not None and frozen is not None and grade is not None
        audit_payload = {
            "schema_version": 1, "migration": "formal-low-odds-proof-gated-v1",
            "admission_proof_hash": _hash({
                "legacy_row": row, "native_t5_hash": native["source_hash"],
                "condition_signature": row.get("frozen_condition_signature"),
                "condition_number": row.get("condition_number"),
                "evidence_version": frozen["active"].get("version"),
            }),
            "native_t5_source_hash": native["source_hash"],
            "normal_grade_source_hash": grade["source_hash"],
            "admitted_without_result_input": True,
        }
        report["accepted"] += 1
        report["rows"].append({
            "observation_id": identifier, "status": "ACCEPTED",
            "condition_number": row.get("condition_number"),
            "condition_signature": row.get("frozen_condition_signature"),
            "result": grade["result"],
        })
        if not apply:
            continue
        row.update({
            "status": "SETTLED", "result": grade["result"],
            "first_native_pre_kickoff_t5": True,
            "recovered_formal_observation": audit_payload,
            "recovered_at": "proof-gated-one-time-migration",
            "settlement_source": "persisted_normal_market_grade",
            "settled_at": "proof-gated-one-time-migration",
            "rollover_provenance": _rollover_marker(
                system, str(row["match_id"]), str(row.get("code") or row.get("market")),
                str(row["frozen_condition_signature"]), native["stage_at"], frozen["active"],
            ),
        })
        row.pop("stake", None)
        row.pop("pnl", None)
        row.setdefault("history", []).append({
            "stage": "recovery", "action": "proof-gated formal low-odds recovery",
            "admitted_without_result_input": True, "result": grade["result"],
            "source_hash": audit_payload["admission_proof_hash"],
        })
    ns.pop("__recovery_root_bets", None)
    if apply:
        recompute_namespace(ledger, system)
        ns["formal_observation_recovery_v1"] = {
            "completed": True, "accepted": report["accepted"], "rejected": report["rejected"],
            "reason_counts": dict(report["reasons"]), "rows": copy.deepcopy(report["rows"]),
        }
    report["reasons"] = dict(report["reasons"])
    return report


def _read(path: Path) -> Any:
    return json.loads(path.read_text(encoding="utf-8"))


def _write_atomic(path: Path, payload: Any) -> None:
    fd, name = tempfile.mkstemp(prefix=".formal-observation-recovery-", dir=str(path.parent))
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(payload, handle, ensure_ascii=False, indent=2, sort_keys=True)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(name, path)
    finally:
        if os.path.exists(name):
            os.unlink(name)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--system", choices=SYSTEMS, required=True)
    parser.add_argument("--ledger", type=Path, required=True)
    parser.add_argument("--history", type=Path, required=True)
    parser.add_argument("--apply", action="store_true")
    args = parser.parse_args()
    ledger, history = _read(args.ledger), _read(args.history)
    if not isinstance(ledger, dict):
        raise SystemExit("ledger must be a JSON object")
    report = recover_system(ledger, history, args.system, apply=args.apply)
    if args.apply:
        _write_atomic(args.ledger, ledger)
    print(json.dumps(report, ensure_ascii=False, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
