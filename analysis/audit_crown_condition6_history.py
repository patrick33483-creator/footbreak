#!/usr/bin/env python3
"""Read-only independent-fixture audit for Crown Wilson condition #6."""
from __future__ import annotations

import argparse
import json
import math
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable

from analysis.granular_conditions import _descriptor, _paths, canonical_panels
from analysis.wilson_validation import _time


SYSTEM = "crown"
CONDITION_NUMBER = 6
SIGNATURE = "09ba238cb8400670519ce95a"
EXPECTED = {
    "market": "HDC",
    "path": "首預→T-30",
    "stage": "T-30",
    "odds_tier": "≥1.70",
    "direction": "A→A",
    "role": "主讓",
    "line_bucket": "0.25–0.5",
    "movement": "不變",
    "odds_trajectory": "",
}
EXPECTED_BASELINE = {"hits": 61, "decided": 97, "pushes": 0}
EXPECTED_DUPLICATE = {"hits": 19, "decided": 30, "pushes": 0}


def _read(path: Path) -> dict[str, Any]:
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise ValueError(f"{path} must contain one JSON object")
    return value


def _rows(document: dict[str, Any]) -> list[dict[str, Any]]:
    value = document.get("rows")
    if not isinstance(value, list):
        raise ValueError("prediction history rows are unavailable")
    return [row for row in value if isinstance(row, dict)]


def _stamp(row: dict[str, Any]) -> datetime | None:
    return _time(
        row.get("predicted_at")
        or row.get("ts")
        or row.get("source_snapshot_at")
        or row.get("created_at")
    )


def _kickoff(row: dict[str, Any]) -> datetime | None:
    return _time(row.get("kickoff") or row.get("kickoff_hkt"))


def _raw_stage_map(
    rows: Iterable[dict[str, Any]],
) -> dict[tuple[str, str], dict[str, Any]]:
    output: dict[tuple[str, str], dict[str, Any]] = {}
    for row in rows:
        fixture = str(row.get("match_id") or row.get("history_key") or "").strip()
        stage = str(row.get("stage") or "").strip()
        if not fixture or stage not in {"首預", "T-30"}:
            continue
        key = fixture, stage
        previous = output.get(key)
        if previous is None or (_stamp(row) or datetime.min.replace(
            tzinfo=timezone.utc
        )) >= (_stamp(previous) or datetime.min.replace(tzinfo=timezone.utc)):
            output[key] = row
    return output


def _condition(
    ledger: dict[str, Any],
) -> tuple[dict[str, Any], dict[str, Any]]:
    namespace = ledger.get("wilson_validation")
    if not isinstance(namespace, dict) or namespace.get("system") != SYSTEM:
        raise ValueError("Crown Wilson namespace is unavailable")
    found = [
        row for row in (namespace.get("conditions") or {}).values()
        if isinstance(row, dict)
        and int(row.get("condition_number") or 0) == CONDITION_NUMBER
    ]
    if len(found) != 1:
        raise ValueError(f"expected one Crown condition #6, found {len(found)}")
    frozen = found[0]
    if frozen.get("signature") != SIGNATURE:
        raise ValueError("condition #6 signature changed")
    definition = frozen.get("definition")
    if not isinstance(definition, dict) or any(
        definition.get(key) != value for key, value in EXPECTED.items()
    ):
        raise ValueError(f"condition #6 immutable axes changed: {definition}")
    versions = frozen.get("evidence_versions")
    if not isinstance(versions, list) or len(versions) < 2:
        raise ValueError("condition #6 evidence chain is unavailable")
    v1, v2 = versions[:2]
    if (
        not isinstance(v1, dict)
        or not isinstance(v2, dict)
        or (v1.get("cumulative_hits"), v1.get("cumulative_decided"))
        != (61, 97)
        or (v2.get("batch_hits"), v2.get("batch_decided")) != (19, 30)
        or (v2.get("cumulative_hits"), v2.get("cumulative_decided"))
        != (80, 127)
    ):
        raise ValueError("condition #6 stored 61/97 + 19/30 evidence changed")
    return namespace, frozen


def _matches(
    rows: list[dict[str, Any]], definition: dict[str, Any], *, settled_only: bool,
) -> list[dict[str, Any]]:
    target = tuple(definition.get("miner_key") or [])
    if not target:
        raise ValueError("condition #6 miner key is unavailable")
    output: dict[str, dict[str, Any]] = {}
    for panel in canonical_panels(rows, settled_only=settled_only):
        if panel.get("market") != "HDC":
            continue
        for path in _paths(panel, "T-30"):
            if tuple(item.get("stage") for item in path) != ("首預", "T-30"):
                continue
            key, _label, _specificity = _descriptor(SYSTEM, path, 2)
            if key == target:
                fixture = str(panel.get("fixture") or "").strip()
                if fixture:
                    output[fixture] = {"panel": panel, "path": path}
    return sorted(output.values(), key=lambda item: (
        item["panel"].get("kickoff") or datetime.min.replace(tzinfo=timezone.utc),
        str(item["panel"].get("fixture") or ""),
    ))


def _grade(item: dict[str, Any] | None) -> str:
    if item is None:
        return "PENDING"
    terminal = item["path"][-1]
    if terminal.get("hit") is True:
        return "Won"
    if terminal.get("hit") is False:
        return "Lost"
    return "Refunded"


def audit(ledger: dict[str, Any], history: dict[str, Any]) -> dict[str, Any]:
    namespace, frozen = _condition(ledger)
    definition = frozen["definition"]
    rows = _rows(history)
    raw = _raw_stage_map(rows)
    all_matches = _matches(rows, definition, settled_only=False)
    settled_matches = {
        str(item["panel"]["fixture"]): item
        for item in _matches(rows, definition, settled_only=True)
    }

    identity_rows = [
        row for collection in (
            ledger.get("bets") or [], namespace.get("observations") or [],
        )
        for row in collection
        if isinstance(row, dict)
        and str(row.get("frozen_condition_signature") or "") == SIGNATURE
        and str(row.get("stage") or "") == "T-30"
        and str(row.get("code") or row.get("market") or "").upper() == "HDC"
    ]
    enrolled: dict[str, list[dict[str, Any]]] = {}
    for row in identity_rows:
        enrolled.setdefault(str(row.get("match_id") or ""), []).append(row)

    details: list[dict[str, Any]] = []
    for item in all_matches:
        panel, path = item["panel"], item["path"]
        fixture = str(panel["fixture"])
        first_raw = raw.get((fixture, "首預"), {})
        t30_raw = raw.get((fixture, "T-30"), {})
        first, t30 = path
        details.append({
            "fixture": fixture,
            "league": t30_raw.get("league") or first_raw.get("league"),
            "home": t30_raw.get("home") or first_raw.get("home"),
            "away": t30_raw.get("away") or first_raw.get("away"),
            "kickoff_hkt": (
                panel["kickoff"].isoformat()
                if isinstance(panel.get("kickoff"), datetime) else None
            ),
            "first_stage_at": (
                _stamp(first_raw).isoformat() if _stamp(first_raw) else None
            ),
            "t30_stage_at": (
                _stamp(t30_raw).isoformat() if _stamp(t30_raw) else None
            ),
            "first": {
                key: first.get(key) for key in (
                    "side", "role", "selected_line", "odds", "odds_tier",
                )
            },
            "t30": {
                key: t30.get(key) for key in (
                    "side", "role", "selected_line", "odds", "odds_tier",
                )
            },
            "grade": _grade(settled_matches.get(fixture)),
            "enrolment_count": len(enrolled.get(fixture, [])),
            "enrolment_ids": [
                row.get("observation_id") or row.get("bet_id")
                for row in enrolled.get(fixture, [])
            ],
        })

    historical = frozen.get("historical_evidence") or {}
    artifact = historical.get("artifact") or {}
    discovery_boundary = _time(artifact.get("as_of") or frozen.get("frozen_at"))
    if discovery_boundary is None:
        raise ValueError("condition #6 discovery boundary is unavailable")
    eligible_baseline_rows = [
        row for row in rows
        if _kickoff(row) is not None
        and _kickoff(row) <= discovery_boundary
        and _time(row.get("verified_at")) is not None
        and _time(row.get("verified_at")) <= discovery_boundary
    ]
    reconstructed_baseline = _matches(
        eligible_baseline_rows, definition, settled_only=True,
    )
    baseline_grades = Counter(_grade(item) for item in reconstructed_baseline)
    baseline_metrics = {
        "hits": baseline_grades["Won"],
        "decided": baseline_grades["Won"] + baseline_grades["Lost"],
        "pushes": baseline_grades["Refunded"],
    }
    holdout_size = max(1, math.ceil(len(reconstructed_baseline) * 0.30))
    reconstructed_holdout = reconstructed_baseline[-holdout_size:]
    holdout_grades = Counter(_grade(item) for item in reconstructed_holdout)
    holdout_metrics = {
        "hits": holdout_grades["Won"],
        "decided": holdout_grades["Won"] + holdout_grades["Lost"],
        "pushes": holdout_grades["Refunded"],
    }

    activation = _time(
        (frozen.get("active_evidence") or {}).get("activation_boundary_at")
    )
    if activation is None:
        raise ValueError("condition #6 activation boundary is unavailable")
    post_activation = [
        row for row in details
        if _time(row.get("t30_stage_at")) is not None
        and _time(row["t30_stage_at"]) > activation
    ]
    missing = [row for row in post_activation if row["enrolment_count"] == 0]
    duplicates = [row for row in details if row["enrolment_count"] > 1]
    grades = Counter(row["grade"] for row in details)

    return {
        "report": "crown_condition6_independent_history_audit",
        "read_only": True,
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "condition": {
            "condition_number": CONDITION_NUMBER,
            "signature": SIGNATURE,
            "definition": definition,
            "stored_historical_evidence": historical,
            "active_evidence": frozen.get("active_evidence"),
        },
        "legacy_duplicate_proof": {
            "reconstructed_baseline": baseline_metrics,
            "expected_baseline": EXPECTED_BASELINE,
            "baseline_matches": baseline_metrics == EXPECTED_BASELINE,
            "reconstructed_last_30_percent": holdout_metrics,
            "expected_v2_batch": EXPECTED_DUPLICATE,
            "v2_is_duplicate_holdout": holdout_metrics == EXPECTED_DUPLICATE,
            "independent_starting_cohort": EXPECTED_BASELINE,
        },
        "summary": {
            "history_rows": len(rows),
            "exact_unique_fixtures": len(details),
            "settled_unique_fixtures": sum(
                row["grade"] != "PENDING" for row in details
            ),
            "wins": grades["Won"],
            "losses": grades["Lost"],
            "pushes": grades["Refunded"],
            "pending_results": grades["PENDING"],
            "post_activation_exact_fixtures": len(post_activation),
            "post_activation_enrolled": sum(
                row["enrolment_count"] == 1 for row in post_activation
            ),
            "post_activation_missing_enrolment": len(missing),
            "duplicate_enrolment_fixtures": len(duplicates),
            "ledger_condition_rows": len(identity_rows),
        },
        "fixtures": details,
        "missing_enrolment": missing,
        "duplicate_enrolments": duplicates,
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--ledger", type=Path, required=True)
    parser.add_argument("--history", type=Path, required=True)
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()
    result = audit(_read(args.ledger), _read(args.history))
    text = json.dumps(result, ensure_ascii=False, indent=2, sort_keys=True) + "\n"
    if args.output:
        args.output.write_text(text, encoding="utf-8")
    print(text, end="")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
