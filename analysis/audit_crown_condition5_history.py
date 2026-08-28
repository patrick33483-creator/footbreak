#!/usr/bin/env python3
"""Read-only reconstruction and admission audit for Crown Wilson condition #5."""
from __future__ import annotations

import argparse
import json
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable

from analysis.granular_conditions import (
    _descriptor, _movement, _paths, _relative_direction, _time, canonical_panels,
)


SYSTEM = "crown"
DEFAULT_CONDITION_NUMBER = 5
EXPECTED_DEFINITIONS = {
    5: {
        "market": "HIL",
        "path": "首預→T-30",
        "stage": "T-30",
        "odds_tier": "≥1.70",
        "direction": "A→B",
        "role": "大",
        "line_bucket": "2.75–3.0",
        "movement": "不變",
        "odds_trajectory": "≥1.70→≥1.70",
    },
    10: {
        "market": "HIL",
        "path": "首預→T-30",
        "stage": "T-30",
        "odds_tier": "≥1.70",
        "direction": "A→A",
        "role": "大",
        "line_bucket": "2.75–3.0",
        "movement": "不變",
        "odds_trajectory": "",
    },
}


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
        if previous is None or (_stamp(row) or datetime.min.replace(
            tzinfo=timezone.utc
        )) >= (_stamp(previous) or datetime.min.replace(tzinfo=timezone.utc)):
            output[key] = row
    return output


def _condition(
    ledger: dict[str, Any], condition_number: int = DEFAULT_CONDITION_NUMBER,
) -> tuple[dict[str, Any], dict[str, Any]]:
    namespace = ledger.get("wilson_validation")
    if not isinstance(namespace, dict) or namespace.get("system") != SYSTEM:
        raise ValueError("Crown Wilson namespace is unavailable")
    conditions = namespace.get("conditions")
    if not isinstance(conditions, dict):
        raise ValueError("Crown condition registry is unavailable")
    found = [
        row for row in conditions.values()
        if isinstance(row, dict)
        and int(row.get("condition_number") or 0) == condition_number
    ]
    if len(found) != 1:
        raise ValueError(
            f"expected one Crown condition #{condition_number}, found {len(found)}"
        )
    frozen = found[0]
    definition = frozen.get("definition")
    if not isinstance(definition, dict):
        raise ValueError(f"condition #{condition_number} definition is unavailable")
    expected = EXPECTED_DEFINITIONS.get(condition_number)
    if expected is None:
        raise ValueError(f"unsupported Crown condition #{condition_number}")
    if any(definition.get(key) != value for key, value in expected.items()):
        raise ValueError(
            f"condition #{condition_number} immutable axes changed: {definition}"
        )
    return namespace, frozen


def _matched_panels(
    rows: list[dict[str, Any]], target: tuple[str, ...], *, settled_only: bool,
) -> list[dict[str, Any]]:
    matches: list[dict[str, Any]] = []
    descriptor_level = (
        3 if any(axis.startswith("tier_path=") for axis in target) else 2
    )
    for panel in canonical_panels(rows, settled_only=settled_only):
        if panel.get("market") != "HIL":
            continue
        for path in _paths(panel, "T-30"):
            if tuple(item.get("stage") for item in path) != ("首預", "T-30"):
                continue
            key, _label, _specificity = _descriptor(
                SYSTEM, path, descriptor_level
            )
            if key != target:
                continue
            matches.append({"panel": panel, "path": path})
    return sorted(matches, key=lambda item: (
        item["panel"].get("kickoff") or datetime.min.replace(tzinfo=timezone.utc),
        str(item["panel"].get("fixture") or ""),
    ))


def _condition10_funnel(
    rows: list[dict[str, Any]], raw: dict[tuple[str, str], dict[str, Any]],
) -> dict[str, Any]:
    panels = [
        panel for panel in canonical_panels(rows, settled_only=False)
        if panel.get("market") == "HIL"
    ]
    first = [panel for panel in panels if "首預" in panel["stages"]]
    t30 = [panel for panel in panels if "T-30" in panel["stages"]]
    paired = [
        panel for panel in panels
        if {"首預", "T-30"}.issubset(panel["stages"])
    ]
    criteria = {
        "terminal_tier_gte_1_70": lambda path: path[-1]["odds_tier"] == "≥1.70",
        "direction_a_to_a": lambda path: _relative_direction(
            "HIL", (item["side"] for item in path)
        ) == "A→A",
        "terminal_role_over": lambda path: path[-1]["role"] == "大",
        "terminal_bucket_2_75_to_3_0": lambda path: (
            path[-1]["line_bucket"] == "2.75–3.0"
        ),
        "line_movement_unchanged": lambda path: _movement(
            item["selected_line"] for item in path
        ) == "不變",
    }
    sequential = {"paired_first_to_t30": len(paired)}
    survivors: list[tuple[dict[str, Any], tuple[dict[str, Any], ...]]] = [
        (panel, (panel["stages"]["首預"], panel["stages"]["T-30"]))
        for panel in paired
    ]
    independent_failures: dict[str, int] = {}
    scored: list[dict[str, Any]] = []
    for panel, path in survivors:
        checks = {name: test(path) for name, test in criteria.items()}
        fixture = str(panel["fixture"])
        source = raw.get((fixture, "T-30"), {}) or raw.get((fixture, "首預"), {})
        scored.append({
            "fixture": fixture,
            "league": source.get("league"),
            "home": source.get("home"),
            "away": source.get("away"),
            "kickoff": (
                panel["kickoff"].isoformat()
                if isinstance(panel.get("kickoff"), datetime) else None
            ),
            "matched_axes": sum(checks.values()),
            "checks": checks,
            "first": {
                key: path[0].get(key)
                for key in ("side", "role", "selected_line", "odds", "odds_tier")
            },
            "t30": {
                key: path[1].get(key)
                for key in ("side", "role", "selected_line", "odds", "odds_tier")
            },
        })
    for name, test in criteria.items():
        independent_failures[name] = sum(
            not test((panel["stages"]["首預"], panel["stages"]["T-30"]))
            for panel in paired
        )
        survivors = [
            item for item in survivors if test(item[1])
        ]
        sequential[f"after_{name}"] = len(survivors)
    scored.sort(key=lambda item: (
        -item["matched_axes"],
        item.get("kickoff") or "",
        item["fixture"],
    ))
    return {
        "hil_unique_fixtures": len(panels),
        "with_first_stage": len(first),
        "with_t30_stage": len(t30),
        "sequential_counts": sequential,
        "independent_failure_counts_among_paired": independent_failures,
        "nearest_matches": scored[:20],
    }


def _identity_rows(
    ledger: dict[str, Any], namespace: dict[str, Any], signature: str,
) -> list[dict[str, Any]]:
    rows = list(ledger.get("bets") or []) + list(namespace.get("observations") or [])
    return [
        row for row in rows
        if isinstance(row, dict)
        and str(row.get("frozen_condition_signature") or "") == signature
        and str(row.get("stage") or "") == "T-30"
        and str(row.get("code") or row.get("market") or "") == "HIL"
    ]


def _admission_map(
    ledger: dict[str, Any],
) -> dict[tuple[str, str], dict[str, Any]]:
    output: dict[tuple[str, str], dict[str, Any]] = {}
    watches = ledger.get("watch") or {}
    if not isinstance(watches, dict):
        return output
    for watch_key, watch in watches.items():
        if not isinstance(watch, dict):
            continue
        fixture = str(
            watch.get("match_id") or watch.get("fixture_id") or watch_key or ""
        ).strip()
        if not fixture:
            continue
        for row in watch.get("stages") or []:
            if not isinstance(row, dict):
                continue
            stage = str(row.get("stage") or "").strip()
            if stage not in {"首預", "T-30"}:
                continue
            output[(fixture, stage)] = {
                "status": str(row.get("formal_admission_status") or "UNMARKED"),
                "reason": row.get("formal_admission_reason"),
                "pending": row.get("formal_admission_pending") is True,
                "snapshot_id": row.get("formal_admission_snapshot_id"),
                "completed_at": row.get("formal_admission_completed_at"),
            }
    return output


def _admission(
    admissions: dict[tuple[str, str], dict[str, Any]], fixture: str, stage: str,
) -> dict[str, Any]:
    return admissions.get((fixture, stage), {
        "status": "NOT_IN_LEDGER_WATCH",
        "reason": None,
        "pending": False,
        "snapshot_id": None,
        "completed_at": None,
    })


def _detail(
    item: dict[str, Any], raw: dict[tuple[str, str], dict[str, Any]],
    settled: dict[str, dict[str, Any]], enrolled: dict[str, list[dict[str, Any]]],
    audits: dict[str, list[dict[str, Any]]],
    admissions: dict[tuple[str, str], dict[str, Any]],
) -> dict[str, Any]:
    panel, path = item["panel"], item["path"]
    fixture = str(panel["fixture"])
    first_raw = raw.get((fixture, "首預"), {})
    t30_raw = raw.get((fixture, "T-30"), {})
    settled_item = settled.get(fixture)
    terminal = settled_item["path"][-1] if settled_item else {}
    return {
        "fixture": fixture,
        "league": t30_raw.get("league") or first_raw.get("league"),
        "home": t30_raw.get("home") or first_raw.get("home"),
        "away": t30_raw.get("away") or first_raw.get("away"),
        "kickoff": (
            panel["kickoff"].isoformat()
            if isinstance(panel.get("kickoff"), datetime) else None
        ),
        "first_stage_at": (
            _stamp(first_raw).isoformat() if _stamp(first_raw) else None
        ),
        "t30_stage_at": _stamp(t30_raw).isoformat() if _stamp(t30_raw) else None,
        "first": {
            key: path[0].get(key)
            for key in ("role", "selected_line", "odds", "odds_tier")
        },
        "t30": {
            key: path[1].get(key)
            for key in ("role", "selected_line", "odds", "odds_tier")
        },
        "grade": (
            "Won" if terminal.get("hit") is True
            else "Lost" if terminal.get("hit") is False
            else "Refunded" if settled_item else "PENDING"
        ),
        "enrolled": fixture in enrolled,
        "enrolment_ids": [
            row.get("observation_id") or row.get("bet_id")
            for row in enrolled.get(fixture, [])
        ],
        "formal_admission": {
            "first": _admission(admissions, fixture, "首預"),
            "t30": _admission(admissions, fixture, "T-30"),
        },
        "audit_reasons": [
            row.get("reason") for row in audits.get(fixture, [])
            if row.get("reason")
        ],
    }


def audit(
    ledger: dict[str, Any],
    history: dict[str, Any],
    condition_number: int = DEFAULT_CONDITION_NUMBER,
) -> dict[str, Any]:
    namespace, frozen = _condition(ledger, condition_number)
    definition = frozen["definition"]
    target = tuple(definition.get("miner_key") or [])
    if not target:
        raise ValueError(f"condition #{condition_number} miner key is unavailable")
    rows = _rows(history)
    raw = _raw_stage_map(rows)
    all_matches = _matched_panels(rows, target, settled_only=False)
    settled_matches = _matched_panels(rows, target, settled_only=True)
    settled = {
        str(item["panel"]["fixture"]): item for item in settled_matches
    }
    identity_rows = _identity_rows(
        ledger, namespace, str(frozen.get("signature") or "")
    )
    enrolled: dict[str, list[dict[str, Any]]] = {}
    for row in identity_rows:
        enrolled.setdefault(str(row.get("match_id") or ""), []).append(row)
    audits: dict[str, list[dict[str, Any]]] = {}
    for row in namespace.get("audit") or []:
        if isinstance(row, dict):
            audits.setdefault(str(row.get("match_id") or ""), []).append(row)
    admissions = _admission_map(ledger)

    details = [
        _detail(item, raw, settled, enrolled, audits, admissions)
        for item in all_matches
    ]
    boundary = _time(
        (frozen.get("active_evidence") or {}).get("activation_boundary_at")
    )
    if boundary is None:
        raise ValueError(
            f"condition #{condition_number} activation boundary is unavailable"
        )
    post_boundary = [
        row for row in details
        if _time(row.get("t30_stage_at")) is not None
        and _time(row.get("t30_stage_at")) > boundary
    ]
    missing = [row for row in post_boundary if not row["enrolled"]]
    settled_metrics = Counter(row["grade"] for row in details)
    missing_reasons = Counter(
        reason for row in missing for reason in row.get("audit_reasons") or []
    )
    missing_t30_statuses = Counter(
        row["formal_admission"]["t30"]["status"] for row in missing
    )
    missing_t30_reasons = Counter(
        row["formal_admission"]["t30"].get("reason") or "NO_REASON_RECORDED"
        for row in missing
    )
    historical = frozen.get("historical_evidence") or {}
    active = frozen.get("active_evidence") or {}
    summary = {
        "history_rows": len(rows),
        "exact_unique_matches": len(details),
        "exact_settled_matches": len(settled_matches),
        "wins": settled_metrics["Won"],
        "losses": settled_metrics["Lost"],
        "pushes": settled_metrics["Refunded"],
        "pending_results": settled_metrics["PENDING"],
        "post_activation_exact_matches": len(post_boundary),
        "post_activation_enrolled": sum(row["enrolled"] for row in post_boundary),
        "post_activation_missing_enrolment": len(missing),
        "ledger_condition_rows": len(identity_rows),
    }
    if condition_number == 5:
        summary["ledger_condition5_rows"] = len(identity_rows)
    condition10_funnel = (
        _condition10_funnel(rows, raw) if condition_number == 10 else None
    )
    return {
        "report": f"crown_condition{condition_number}_history_audit",
        "read_only": True,
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "condition": {
            "condition_number": frozen.get("condition_number"),
            "signature": frozen.get("signature"),
            "definition": definition,
            "activation_boundary_at": active.get("activation_boundary_at"),
            "stored_historical_evidence": historical,
            "active_evidence": active,
            "pending_rollover_progress": frozen.get("pending_rollover_progress"),
        },
        "summary": summary,
        "condition10_funnel": condition10_funnel,
        "post_activation_matches": post_boundary,
        "missing_enrolments": missing,
        "missing_audit_reason_counts": dict(missing_reasons),
        "missing_t30_admission_status_counts": dict(missing_t30_statuses),
        "missing_t30_admission_reason_counts": dict(missing_t30_reasons),
        "all_exact_matches": details,
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--ledger", type=Path, required=True)
    parser.add_argument("--history", type=Path, required=True)
    parser.add_argument(
        "--condition-number",
        type=int,
        choices=sorted(EXPECTED_DEFINITIONS),
        default=DEFAULT_CONDITION_NUMBER,
    )
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()
    result = audit(
        _read(args.ledger),
        _read(args.history),
        condition_number=args.condition_number,
    )
    encoded = json.dumps(result, ensure_ascii=False, indent=2, sort_keys=True) + "\n"
    if args.output:
        args.output.write_text(encoded, encoding="utf-8")
    print(encoded, end="")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
