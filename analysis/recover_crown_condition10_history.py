#!/usr/bin/env python3
"""Run the formal Crown condition #10 recovery engine in audit or apply mode."""
from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path
from typing import Any

from analysis import audit_crown_condition5_history as audit
from analysis import recover_crown_condition5_history as engine
from analysis.dry_run_crown_condition10_recovery import (
    ALLOWED_GRADES,
    _validate_overrides,
)


SIGNATURE = "f956f75e552c8de37b0f2656"


def configure() -> None:
    engine.SIGNATURE = SIGNATURE
    engine.MIGRATION = "crown-condition10-t30-missed-admission-v1"
    engine.MIGRATION_FIELD = "condition10_history_recovery_v1"
    engine.RECOVERY_REASON = "condition10_missed_admission_recovery"
    engine.RECOVERY_ACTION = "條件 #10 漏入組修復：套用既有正常賽果"
    engine._condition = lambda ledger: audit._condition(ledger, 10)


def _canonical_hash(value: Any) -> str:
    return hashlib.sha256(json.dumps(
        value, ensure_ascii=False, sort_keys=True, separators=(",", ":"),
    ).encode("utf-8")).hexdigest()


def _install_audit_result_overrides(
    document: dict[str, Any], candidate_fixtures: set[str],
) -> tuple[dict[str, dict[str, Any]], Any]:
    """Temporarily grade unresolved rows from a pinned, source-linked document."""
    overrides = _validate_overrides(document, candidate_fixtures)
    verified_at = str(document.get("verified_at") or "")
    if engine._time(verified_at) is None:
        raise ValueError("result override verified_at is invalid")
    original_grade = engine._grade

    def grade(match: dict[str, Any]) -> Any:
        fixture = str(match.get("fixture") or "")
        existing = original_grade(match)
        override = overrides.get(fixture)
        if existing is not None:
            if override is not None:
                raise ValueError(f"override would replace a settled grade: {fixture}")
            return existing
        if override is None or override.get("grade") == "PENDING":
            return None
        kickoff = engine._time(match.get("kickoff"))
        settled = engine._time(verified_at)
        if kickoff is None or settled is None or settled < kickoff:
            raise ValueError(f"result override predates kickoff: {fixture}")
        return str(override["grade"]), verified_at, _canonical_hash(override)

    engine._grade = grade
    return overrides, original_grade


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--ledger", type=Path, required=True)
    parser.add_argument("--history", type=Path, required=True)
    parser.add_argument("--output", type=Path)
    parser.add_argument("--result-overrides", type=Path)
    parser.add_argument("--audit-ledger-output", type=Path)
    parser.add_argument("--apply", action="store_true")
    args = parser.parse_args()
    if args.apply and args.result_overrides:
        parser.error("--result-overrides is audit-only; production apply is disabled")
    if args.apply and args.audit_ledger_output:
        parser.error("--audit-ledger-output cannot be used with --apply")
    if args.audit_ledger_output and args.audit_ledger_output.resolve() == args.ledger.resolve():
        parser.error("--audit-ledger-output must not replace the input ledger")
    configure()
    ledger = engine._read(args.ledger)
    history = engine._read(args.history)
    overrides: dict[str, dict[str, Any]] = {}
    original_grade = None
    override_document = None
    if args.result_overrides:
        override_document = engine._read(args.result_overrides)
        _, frozen = audit._condition(ledger, 10)
        boundary = str(
            (frozen.get("active_evidence") or {}).get("activation_boundary_at")
            or ""
        )
        candidates = engine._candidate_matches(
            engine._rows(history), frozen["definition"], boundary,
        )
        candidate_fixtures = {
            str(row.get("fixture") or "") for row in candidates
        }
        overrides, original_grade = _install_audit_result_overrides(
            override_document, candidate_fixtures,
        )
    try:
        result = engine.recover(ledger, history, apply=args.apply)
    finally:
        if original_grade is not None:
            engine._grade = original_grade
    if override_document is not None:
        recovered = {
            str(row.get("match_id") or ""): str(row.get("result") or "")
            for row in result.get("fixtures") or []
        }
        if set(overrides) - set(recovered):
            raise ValueError("one or more result overrides were not replayed")
        expected = {
            fixture: str(row.get("grade") or "")
            for fixture, row in overrides.items()
        }
        if any(recovered.get(fixture) != grade for fixture, grade in expected.items()):
            raise ValueError("result override replay mismatch")
        result["result_overrides"] = {
            "mode": "audit-only",
            "document_sha256": _canonical_hash(override_document),
            "verified_at": override_document.get("verified_at"),
            "count": len(overrides),
            "grades": {
                grade: sum(
                    1 for row in overrides.values() if row.get("grade") == grade
                )
                for grade in sorted(ALLOWED_GRADES)
                if any(row.get("grade") == grade for row in overrides.values())
            },
            "fixtures": [
                {
                    "fixture": fixture,
                    "grade": row.get("grade"),
                    "terminal_status": row.get("terminal_status"),
                    "sources": row.get("sources"),
                }
                for fixture, row in sorted(overrides.items())
            ],
        }
    if args.apply:
        engine._write_atomic(args.ledger, ledger)
    elif args.audit_ledger_output:
        engine._write_atomic(args.audit_ledger_output, ledger)
    encoded = json.dumps(result, ensure_ascii=False, indent=2, sort_keys=True) + "\n"
    if args.output:
        args.output.write_text(encoded, encoding="utf-8")
    print(encoded, end="")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
