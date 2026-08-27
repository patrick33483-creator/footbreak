#!/usr/bin/env python3
"""Read-only, fixture-level audit of Crown Wilson condition #1."""
from __future__ import annotations

import argparse
import json
import math
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from analysis.granular_conditions import _descriptor, _paths, canonical_panels
from analysis.wilson_validation import _signature_rows_for_rollover, _time


SYSTEM = "crown"
CONDITION_NUMBER = 1
STAGES = ("首預", "T-30", "T-5")
BINARY_HITS = {"Won", "Half Won"}
BINARY_LOSSES = {"Lost", "Half Lost"}


def _read(path: Path) -> dict[str, Any]:
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise ValueError(f"{path} must contain a JSON object")
    return value


def _rows(document: dict[str, Any]) -> list[dict[str, Any]]:
    value = document.get("rows")
    if not isinstance(value, list):
        raise ValueError("prediction history rows are unavailable")
    return [row for row in value if isinstance(row, dict)]


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


def _condition(ledger: dict[str, Any]) -> tuple[dict[str, Any], dict[str, Any]]:
    namespace = ledger.get("wilson_validation")
    if not isinstance(namespace, dict) or namespace.get("system") != SYSTEM:
        raise ValueError("Crown Wilson namespace is unavailable")
    conditions = namespace.get("conditions")
    if not isinstance(conditions, dict):
        raise ValueError("Crown frozen registry is unavailable")
    matches = [
        item for item in conditions.values()
        if isinstance(item, dict)
        and int(item.get("condition_number") or 0) == CONDITION_NUMBER
    ]
    if len(matches) != 1:
        raise ValueError(f"expected one condition #1, found {len(matches)}")
    frozen = matches[0]
    definition = frozen.get("definition")
    if not isinstance(definition, dict):
        raise ValueError("condition #1 definition is unavailable")
    expected = {
        "market": "HDC", "path": "首預→T-30→T-5", "stage": "T-5",
        "odds_tier": "≥1.70", "direction": "A→A→A", "role": "主讓",
        "line_bucket": "0.25–0.5", "movement": "不變",
        "odds_trajectory": "≥1.70→≥1.70→≥1.70",
    }
    changed = {
        key: {"expected": value, "actual": definition.get(key)}
        for key, value in expected.items() if definition.get(key) != value
    }
    if changed:
        raise ValueError(f"condition #1 immutable axes changed: {changed}")
    return namespace, frozen


def _raw_stage_map(
    rows: list[dict[str, Any]],
) -> dict[tuple[str, str], dict[str, Any]]:
    output: dict[tuple[str, str], dict[str, Any]] = {}
    for row in rows:
        fixture = str(row.get("match_id") or row.get("history_key") or "").strip()
        stage = str(row.get("stage") or "").strip()
        if not fixture or stage not in STAGES:
            continue
        key = fixture, stage
        previous = output.get(key)
        if previous is None or str(_stamp(row) or "") >= str(_stamp(previous) or ""):
            output[key] = row
    return output


def _matching_rows(
    rows: list[dict[str, Any]], frozen: dict[str, Any],
) -> list[dict[str, Any]]:
    wanted = tuple(frozen["definition"]["miner_key"])
    raw = _raw_stage_map(rows)
    output: dict[str, dict[str, Any]] = {}
    for panel in canonical_panels(rows, settled_only=False):
        fixture = str(panel.get("fixture") or "")
        for path in _paths(panel, "T-5"):
            if len(path) != 3 or path[-1].get("stage") != "T-5":
                continue
            key, _label, _specificity = _descriptor(SYSTEM, path, 3)
            if key != wanted:
                continue
            sources = {stage: raw.get((fixture, stage)) for stage in STAGES}
            if not fixture or any(not isinstance(sources[stage], dict) for stage in STAGES):
                continue
            output[fixture] = {
                "fixture": fixture,
                "terminal": path[-1],
                "path": path,
                "sources": sources,
                "stage_at": _stamp(sources["T-5"]),
                "kickoff": _kickoff(sources["T-5"]),
            }
    return sorted(output.values(), key=lambda item: (
        _time(item.get("kickoff")) or datetime.min.replace(tzinfo=timezone.utc),
        item["fixture"],
    ))


def _normal_result(grade: dict[str, Any]) -> str | None:
    raw = str(grade.get("settlement") or "").strip().lower()
    mapped = {
        "won": "Won", "half won": "Half Won", "lost": "Lost",
        "half lost": "Half Lost", "refunded": "Refunded",
        "push": "Refunded", "void": "Refunded",
    }.get(raw)
    if mapped is not None:
        return mapped
    hit = grade.get("hit")
    if hit is True:
        return "Won"
    if hit is False:
        return "Lost"
    if hit is None and str(grade.get("grade_status") or "") == "GRADED":
        return "Refunded"
    return None


def _grade(match: dict[str, Any]) -> str | None:
    terminal = match["terminal"]
    candidates: list[str] = []
    for source in match["sources"].values():
        for grade in source.get("market_grades") or []:
            if not isinstance(grade, dict):
                continue
            if str(grade.get("code") or "").upper() != "HDC":
                continue
            if str(grade.get("side") or grade.get("selection") or "").upper() != str(
                terminal.get("side") or ""
            ).upper():
                continue
            if not _same_number(
                grade.get("line", grade.get("condition")),
                terminal.get("selected_line"),
            ):
                continue
            result = _normal_result(grade)
            if result is not None:
                candidates.append(result)
    unique = sorted(set(candidates))
    return unique[0] if len(unique) == 1 else None


def _public_row(
    match: dict[str, Any], result: str, evidence_rows: list[dict[str, Any]],
    *, boundary: datetime,
) -> dict[str, Any]:
    source = match["sources"]["T-5"]
    kickoff = _time(match.get("kickoff"))
    stage_at = _time(match.get("stage_at"))
    now = datetime.now(timezone.utc)
    formal_results = sorted({
        str(row.get("result") or "")
        for row in evidence_rows
        if str(row.get("result") or "") in BINARY_HITS | BINARY_LOSSES | {"Refunded"}
    })
    effective_result = (
        formal_results[0]
        if result == "PENDING" and len(formal_results) == 1
        else result
    )
    return {
        "match_id": match["fixture"],
        "kickoff": match.get("kickoff"),
        "league": source.get("league"),
        "home": source.get("home"),
        "away": source.get("away"),
        "stage_at": match.get("stage_at"),
        "period": (
            "pre_boundary" if stage_at is not None and stage_at <= boundary
            else "post_boundary"
        ),
        "history_result": result,
        "formal_results": formal_results,
        "effective_result": effective_result,
        "result_state": (
            "future" if kickoff is not None and kickoff > now
            else "unknown_after_kickoff" if effective_result == "PENDING"
            else "settled"
        ),
        "formal_evidence_rows": len(evidence_rows),
        "formal_statuses": sorted({
            str(row.get("status") or "") for row in evidence_rows
        }),
        "stages": {
            stage: {
                "side": item.get("side"),
                "line": item.get("selected_line"),
                "odds": item.get("odds"),
            }
            for stage, item in zip(STAGES, match["path"])
        },
    }


def audit(ledger: dict[str, Any], history: dict[str, Any]) -> dict[str, Any]:
    namespace, frozen = _condition(ledger)
    history_rows = _rows(history)
    matches = _matching_rows(history_rows, frozen)
    signature = str(frozen.get("signature") or "")
    evidence_rows = _signature_rows_for_rollover(ledger, signature)
    evidence_by_fixture: dict[str, list[dict[str, Any]]] = {}
    for row in evidence_rows:
        fixture = str(
            row.get("match_id") or row.get("history_key")
            or row.get("fixture_id") or ""
        ).strip()
        if fixture:
            evidence_by_fixture.setdefault(fixture, []).append(row)

    artifact = (frozen.get("historical_evidence") or {}).get("artifact") or {}
    boundary_text = artifact.get("as_of") or frozen.get("frozen_at")
    boundary = _time(boundary_text)
    if boundary is None:
        raise ValueError("condition #1 discovery boundary is invalid")

    detailed = []
    for match in matches:
        result = _grade(match) or "PENDING"
        detailed.append(_public_row(
            match, result, evidence_by_fixture.get(match["fixture"], []),
            boundary=boundary,
        ))

    pre = [row for row in detailed if row["period"] == "pre_boundary"]
    post = [row for row in detailed if row["period"] == "post_boundary"]
    pre_decided = [
        row for row in pre
        if row["effective_result"] in BINARY_HITS | BINARY_LOSSES
    ]
    holdout_size = max(1, math.ceil(len(pre_decided) * 0.30)) if pre_decided else 0
    reconstructed_holdout = pre_decided[-holdout_size:] if holdout_size else []
    missing_observation = [
        row for row in post if row["formal_evidence_rows"] == 0
    ]
    unknown = [
        row for row in detailed if row["result_state"] == "unknown_after_kickoff"
    ]
    future = [row for row in detailed if row["result_state"] == "future"]
    duplicate_observation = [
        row for row in detailed if row["formal_evidence_rows"] > 1
    ]
    candidate_ids = {row["match_id"] for row in detailed}
    false_positive_rows = [
        {
            "match_id": fixture,
            "rows": len(rows),
            "statuses": sorted({str(row.get("status") or "") for row in rows}),
        }
        for fixture, rows in evidence_by_fixture.items()
        if fixture not in candidate_ids
    ]
    result_counts = Counter(row["effective_result"] for row in detailed)
    history_result_counts = Counter(row["history_result"] for row in detailed)
    pre_hits = sum(row["effective_result"] in BINARY_HITS for row in pre_decided)
    holdout_hits = sum(
        row["effective_result"] in BINARY_HITS for row in reconstructed_holdout
    )
    settlement_disagreements = [
        row for row in detailed
        if (
            row["history_result"] == "PENDING" and row["formal_results"]
        ) or (
            row["history_result"] in BINARY_HITS | BINARY_LOSSES
            and row["formal_evidence_rows"] > 0
            and (
                len(row["formal_results"]) != 1
                or row["formal_results"][0] != row["history_result"]
            )
        )
    ]
    versions = frozen.get("evidence_versions") or []
    stored = [{
        "version": version.get("version"),
        "batch_hits": version.get("batch_hits"),
        "batch_decided": version.get("batch_decided"),
        "cumulative_hits": version.get("cumulative_hits"),
        "cumulative_decided": version.get("cumulative_decided"),
        "initial_migration_full_cohort": version.get(
            "initial_migration_full_cohort"
        ),
        "legacy_prospective_cohort": version.get("legacy_prospective_cohort"),
    } for version in versions if isinstance(version, dict)]
    return {
        "mode": "read_only",
        "condition_number": CONDITION_NUMBER,
        "condition_signature": signature,
        "definition": frozen["definition"],
        "boundary": str(boundary_text),
        "stored_evidence_versions": stored,
        "stored_pending_rollover_progress": frozen.get(
            "pending_rollover_progress"
        ),
        "exact_history": {
            "fixtures": len(detailed),
            "results": dict(sorted(result_counts.items())),
            "raw_history_results": dict(sorted(history_result_counts.items())),
            "settled_decided": sum(result_counts[value] for value in BINARY_HITS | BINARY_LOSSES),
            "hits": sum(result_counts[value] for value in BINARY_HITS),
            "pushes": result_counts["Refunded"],
            "unknown_after_kickoff": len(unknown),
            "future": len(future),
        },
        "baseline_reconstruction": {
            "pre_boundary_decided": len(pre_decided),
            "pre_boundary_hits": pre_hits,
            "expected_historical_evidence": frozen.get("historical_evidence"),
            "reconstructed_30pct_holdout": {
                "decided": len(reconstructed_holdout),
                "hits": holdout_hits,
                "fixture_ids": [row["match_id"] for row in reconstructed_holdout],
            },
        },
        "post_boundary": {
            "matching_fixtures": len(post),
            "with_formal_observation": sum(
                row["formal_evidence_rows"] > 0 for row in post
            ),
            "missing_formal_observation": len(missing_observation),
        },
        "integrity": {
            "matching_fixtures_with_duplicate_formal_rows": len(
                duplicate_observation
            ),
            "formal_fixture_ids_not_matching_history_rule": len(
                false_positive_rows
            ),
            "history_vs_formal_settlement_disagreements": len(
                settlement_disagreements
            ),
        },
        "missing_formal_observation_rows": missing_observation,
        "unknown_result_rows": unknown,
        "future_rows": future,
        "duplicate_formal_observation_rows": duplicate_observation,
        "formal_rows_not_matching_history_rule": false_positive_rows,
        "history_vs_formal_settlement_disagreement_rows": settlement_disagreements,
        "all_matching_rows": detailed,
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--ledger", type=Path, required=True)
    parser.add_argument("--history", type=Path, required=True)
    args = parser.parse_args()
    print(json.dumps(
        audit(_read(args.ledger), _read(args.history)),
        ensure_ascii=False, indent=2, sort_keys=True,
    ))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
