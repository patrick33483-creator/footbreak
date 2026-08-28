#!/usr/bin/env python3
"""Build a deterministic, non-writing recovery plan for Crown condition #10."""
from __future__ import annotations

import argparse
import hashlib
import json
from collections import Counter
from pathlib import Path
from typing import Any


SCHEMA = "crown-condition-10-recovery-dry-run-v1"
SIGNATURE = "f956f75e552c8de37b0f2656"
EXPECTED_DEFINITION = {
    "system": "crown",
    "market": "HIL",
    "path": "首預→T-30",
    "stage": "T-30",
    "odds_tier": "≥1.70",
    "direction": "A→A",
    "role": "大",
    "line_bucket": "2.75–3.0",
    "movement": "不變",
    "odds_trajectory": "",
}
BINARY = {"Won", "Lost", "Half Won", "Half Lost"}
HITS = {"Won", "Half Won"}
ALLOWED_GRADES = BINARY | {"Refunded", "PENDING"}


def _read(path: Path) -> dict[str, Any]:
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise ValueError(f"{path} must contain one JSON object")
    return value


def _canonical_hash(value: Any) -> str:
    return hashlib.sha256(json.dumps(
        value, ensure_ascii=False, sort_keys=True, separators=(",", ":"),
    ).encode("utf-8")).hexdigest()


def _validate_audit(audit: dict[str, Any]) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    if audit.get("read_only") is not True:
        raise ValueError("source audit is not read-only")
    condition = audit.get("condition")
    summary = audit.get("summary")
    rows = audit.get("missing_enrolments")
    if not isinstance(condition, dict) or not isinstance(summary, dict):
        raise ValueError("condition or summary is unavailable")
    if condition.get("condition_number") != 10 or condition.get("signature") != SIGNATURE:
        raise ValueError("Crown condition #10 identity changed")
    definition = condition.get("definition")
    if not isinstance(definition, dict) or any(
        definition.get(key) != value for key, value in EXPECTED_DEFINITION.items()
    ):
        raise ValueError("Crown condition #10 immutable axes changed")
    if (
        not isinstance(rows, list)
        or summary.get("post_activation_missing_enrolment") != 114
        or summary.get("post_activation_enrolled") != 0
        or len(rows) != 114
    ):
        raise ValueError("the audited 114-row recovery cohort changed")
    fixtures = [str(row.get("fixture") or "") for row in rows if isinstance(row, dict)]
    if len(fixtures) != 114 or len(set(fixtures)) != 114 or not all(fixtures):
        raise ValueError("recovery fixture identities are missing or duplicated")
    return condition, rows


def _validate_overrides(
    overrides: dict[str, Any], candidate_fixtures: set[str],
) -> dict[str, dict[str, Any]]:
    if overrides.get("schema") != "crown-condition-10-result-overrides-v1":
        raise ValueError("result override schema mismatch")
    output: dict[str, dict[str, Any]] = {}
    for row in overrides.get("results") or []:
        if not isinstance(row, dict):
            raise ValueError("result override row is malformed")
        fixture = str(row.get("fixture") or "")
        grade = str(row.get("grade") or "")
        sources = row.get("sources")
        if (
            not fixture or fixture not in candidate_fixtures or fixture in output
            or grade not in ALLOWED_GRADES or not isinstance(sources, list)
            or not sources or not all(str(url).startswith("https://") for url in sources)
        ):
            raise ValueError(f"invalid result override: {fixture or 'missing'}")
        if grade == "PENDING" and row.get("terminal_status") != "POSTPONED":
            raise ValueError(f"pending override lacks postponement proof: {fixture}")
        output[fixture] = row
    return output


def _identity(row: dict[str, Any]) -> str:
    return (
        f"{row['fixture']}|HIL|T-30|{SIGNATURE}|formal-observation"
    )


def _batches(rows: list[dict[str, Any]]) -> tuple[list[dict[str, Any]], dict[str, int]]:
    eligible = [row for row in rows if row["grade"] in BINARY]
    sealed: list[dict[str, Any]] = []
    for offset in range(0, len(eligible) // 20 * 20, 20):
        batch = eligible[offset:offset + 20]
        sealed.append({
            "batch_number": len(sealed) + 1,
            "decided": 20,
            "hits": sum(row["grade"] in HITS for row in batch),
            "first_fixture": batch[0]["fixture"],
            "last_fixture": batch[-1]["fixture"],
            "ordered_identities_sha256": _canonical_hash(
                [row["identity"] for row in batch]
            ),
        })
    tail = eligible[len(sealed) * 20:]
    return sealed, {
        "eligible_decided": len(tail),
        "eligible_hits": sum(row["grade"] in HITS for row in tail),
    }


def plan(audit: dict[str, Any], overrides: dict[str, Any]) -> dict[str, Any]:
    condition, source_rows = _validate_audit(audit)
    override_map = _validate_overrides(
        overrides, {str(row["fixture"]) for row in source_rows},
    )
    rows: list[dict[str, Any]] = []
    for source in source_rows:
        fixture = str(source["fixture"])
        grade = str(source.get("grade") or "PENDING")
        override = override_map.get(fixture)
        if override is not None:
            if grade != "PENDING":
                raise ValueError(f"override would replace a settled grade: {fixture}")
            grade = str(override["grade"])
        if grade not in ALLOWED_GRADES:
            raise ValueError(f"unsupported grade for {fixture}: {grade}")
        rows.append({
            "fixture": fixture,
            "identity": _identity(source),
            "kickoff": source.get("kickoff"),
            "stage_at": source.get("t30_stage_at"),
            "league": source.get("league"),
            "home": source.get("home"),
            "away": source.get("away"),
            "line": (source.get("t30") or {}).get("selected_line"),
            "odds": (source.get("t30") or {}).get("odds"),
            "grade": grade,
            "result_override": override,
        })
    rows.sort(key=lambda row: (row.get("stage_at") or "", row["fixture"]))
    identities = [row["identity"] for row in rows]
    if len(set(identities)) != len(identities):
        raise ValueError("formal observation identities are duplicated")

    simulated_store: set[str] = set()
    first_added = [identity for identity in identities if identity not in simulated_store]
    simulated_store.update(first_added)
    second_added = [identity for identity in identities if identity not in simulated_store]
    second_skipped = [identity for identity in identities if identity in simulated_store]
    sealed, tail = _batches(rows)
    grades = Counter(row["grade"] for row in rows)
    active = condition.get("active_evidence") or {}
    sealed_hits = sum(batch["hits"] for batch in sealed)
    sealed_decided = sum(batch["decided"] for batch in sealed)
    return {
        "schema": SCHEMA,
        "mode": "dry-run-only",
        "production_touched": False,
        "writes": 0,
        "deletes": 0,
        "condition_signature": SIGNATURE,
        "source_audit_sha256": _canonical_hash(audit),
        "result_overrides_sha256": _canonical_hash(overrides),
        "candidate_count": len(rows),
        "grade_counts": dict(sorted(grades.items())),
        "first_pass": {
            "added": len(first_added),
            "skipped": len(rows) - len(first_added),
        },
        "idempotent_second_pass": {
            "added": len(second_added),
            "skipped": len(second_skipped),
        },
        "rollover": {
            "starting_active_version": active.get("version"),
            "starting_cumulative_hits": active.get("cumulative_hits"),
            "starting_cumulative_decided": active.get("cumulative_decided"),
            "sealed_batches": sealed,
            "sealed_batch_count": len(sealed),
            "projected_active_version": (
                int(active.get("version") or 0) + len(sealed)
            ),
            "projected_cumulative_hits_after_sealed_batches": (
                int(active.get("cumulative_hits") or 0) + sealed_hits
            ),
            "projected_cumulative_decided_after_sealed_batches": (
                int(active.get("cumulative_decided") or 0) + sealed_decided
            ),
            "pending_rollover_progress": tail,
            "refunded_excluded_from_binary_counter": grades["Refunded"],
            "pending_excluded_from_binary_counter": grades["PENDING"],
        },
        "ordered_candidate_identities_sha256": _canonical_hash(identities),
        "candidates": rows,
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--audit", type=Path, required=True)
    parser.add_argument("--result-overrides", type=Path, required=True)
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()
    result = plan(_read(args.audit), _read(args.result_overrides))
    encoded = json.dumps(result, ensure_ascii=False, indent=2, sort_keys=True) + "\n"
    if args.output:
        args.output.write_text(encoded, encoding="utf-8")
    print(encoded, end="")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
