#!/usr/bin/env python3
"""Exhaustively recheck Crown condition #2 pending results on a copied state.

The caller must provide a disposable Crown state directory.  This tool filters
that copied prediction history to the exact fixtures already admitted to
condition #2, runs the normal strict-ID result pipeline with a larger bounded
manual-recovery budget, and reports every row.  It never writes the ledger and
disables learning-store persistence.
"""
from __future__ import annotations

import argparse
import copy
import json
import os
from collections import Counter
from pathlib import Path
from typing import Any

from crown.config import settings
from crown import prediction_history
from analysis.recover_crown_condition2_history import (
    _condition,
    _grade,
    _hash,
    _matching_rows,
    _number,
    _read,
    _rows,
    _same_number,
    _time,
    _write_atomic,
)


TRUSTED_TERMINAL_SOURCES = {
    "hkjc_official_exact_id_terminal_status",
    "titan_exact_id_terminal_status",
}


def _pending_rows(ledger: dict[str, Any]) -> list[dict[str, Any]]:
    _namespace, frozen = _condition(ledger)
    recovered = frozen.get("historical_recovery_rows")
    if not isinstance(recovered, list):
        raise ValueError("condition #2 historical recovery rows are unavailable")
    return [
        copy.deepcopy(row)
        for row in recovered
        if isinstance(row, dict) and row.get("result") == "PENDING"
    ]


def _same_identity(
    recovered: dict[str, Any], match: dict[str, Any],
) -> tuple[bool, str | None]:
    selected = match.get("terminal") or {}
    checks = (
        (str(recovered.get("match_id") or "") == str(match.get("fixture") or ""),
         "match_id_mismatch"),
        (_time(recovered.get("kickoff")) == _time(match.get("kickoff")),
         "kickoff_mismatch"),
        (_time(recovered.get("stage_at")) == _time(match.get("stage_at")),
         "stage_at_mismatch"),
        (str(recovered.get("side") or "").upper()
         == str(selected.get("side") or "").upper(), "side_mismatch"),
        (_same_number(recovered.get("line"), selected.get("selected_line")),
         "line_mismatch"),
        (_same_number(recovered.get("odds"), selected.get("odds")),
         "odds_mismatch"),
    )
    for valid, reason in checks:
        if not valid:
            return False, reason
    return True, None


def _trusted_terminal(match: dict[str, Any]) -> tuple[str, str, str] | None:
    source = match.get("source") or {}
    detail = source.get("result_detail")
    result_source = str(source.get("result_source") or "")
    terminal_status = (
        str(detail.get("terminal_status") or "").strip()
        if isinstance(detail, dict) else ""
    )
    kickoff = _time(match.get("kickoff"))
    verified_at = _time(source.get("verified_at") or source.get("result_recorded_at"))
    if (
        source.get("result_status") == "不計"
        and result_source in TRUSTED_TERMINAL_SOURCES
        and terminal_status
        and kickoff is not None
        and verified_at is not None
        and verified_at >= kickoff
    ):
        proof = _hash({
            "source": source,
            "terminal_status": terminal_status,
            "result_source": result_source,
        })
        return "Refunded", str(source.get("verified_at") or source.get("result_recorded_at")), proof
    return None


def _audit_row(
    recovered: dict[str, Any], matches: dict[str, dict[str, Any]],
) -> dict[str, Any]:
    fixture = str(recovered.get("match_id") or "")
    match = matches.get(fixture)
    base = {
        key: recovered.get(key)
        for key in (
            "match_id", "kickoff", "stage_at", "league", "home", "away",
            "side", "line", "odds",
        )
    }
    if not match:
        return {**base, "audit_status": "unresolved", "reason": "history_match_missing"}
    valid, reason = _same_identity(recovered, match)
    source = match.get("source") or {}
    identifiers = {
        "hkjc_match_id": source.get("hkjc_match_id"),
        "titan_match_id": source.get("titan_match_id") or match.get("fixture"),
        "native_fixture_id": source.get("native_fixture_id"),
        "pinnapi_event_id": source.get("pinnapi_event_id"),
    }
    if not valid:
        return {
            **base, **identifiers, "audit_status": "unresolved",
            "reason": reason,
        }
    grade = _grade(match)
    if grade is None:
        grade = _trusted_terminal(match)
    if grade is not None:
        result, settled_at, proof_hash = grade
        return {
            **base, **identifiers, "audit_status": "resolved",
            "reason": None, "result": result, "settled_at": settled_at,
            "result_proof_hash": proof_hash,
            "score": source.get("score"),
            "result_source": source.get("result_source"),
        }
    return {
        **base, **identifiers, "audit_status": "unresolved",
        "reason": source.get("result_missing_reason") or "no_verified_result_match",
        "result_status": source.get("result_status"),
        "result_source": source.get("result_source"),
        "result_attempted_at": source.get("result_attempted_at"),
    }


def run(
    ledger_path: Path,
    history_path: Path,
    *,
    detail_budget: int,
    titan_seconds: float,
    hkjc_seconds: float,
) -> dict[str, Any]:
    ledger = _read(ledger_path)
    history = _read(history_path)
    pending = _pending_rows(ledger)
    pending_ids = {str(row.get("match_id") or "") for row in pending}
    all_rows = _rows(history)
    selected_rows = [
        copy.deepcopy(row) for row in all_rows
        if str(row.get("match_id") or row.get("history_key") or "") in pending_ids
    ]
    history["rows"] = selected_rows
    _write_atomic(history_path, history)

    prediction_history._RESULT_DETAIL_REQUEST_BUDGET = max(0, detail_budget)
    prediction_history._TITAN_RESULT_PASS_SECONDS = max(1.0, titan_seconds)
    prediction_history._HKJC_RESULT_PASS_SECONDS = max(1.0, hkjc_seconds)
    os.environ.pop("LEARNING_DB_PATH", None)
    refreshed = prediction_history.grade_history(settings())

    matches = {
        str(item.get("fixture") or ""): item
        for item in _matching_rows(_rows(refreshed), _condition(ledger)[1]["definition"],
                                   settled_only=False)
    }
    rows = [_audit_row(row, matches) for row in pending]
    statuses = Counter(str(row.get("audit_status") or "unknown") for row in rows)
    results = Counter(
        str(row.get("result"))
        for row in rows if row.get("audit_status") == "resolved"
    )
    reasons = Counter(
        str(row.get("reason") or "unknown")
        for row in rows if row.get("audit_status") != "resolved"
    )
    return {
        "mode": "read_only_copy_audit",
        "condition_number": 2,
        "pending_before": len(pending),
        "history_rows_considered": len(selected_rows),
        "result_sync": refreshed.get("result_sync"),
        "resolved": statuses.get("resolved", 0),
        "unresolved": statuses.get("unresolved", 0),
        "resolved_results": dict(sorted(results.items())),
        "unresolved_reasons": dict(sorted(reasons.items())),
        "rows": rows,
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--ledger", type=Path, required=True)
    parser.add_argument("--history", type=Path, required=True)
    parser.add_argument("--detail-budget", type=int, default=200)
    parser.add_argument("--titan-seconds", type=float, default=300.0)
    parser.add_argument("--hkjc-seconds", type=float, default=300.0)
    args = parser.parse_args()
    report = run(
        args.ledger,
        args.history,
        detail_budget=args.detail_budget,
        titan_seconds=args.titan_seconds,
        hkjc_seconds=args.hkjc_seconds,
    )
    print(json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
