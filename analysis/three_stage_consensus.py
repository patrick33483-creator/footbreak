"""Shared one-fixture statistics for predictions stable across all three stages."""
from __future__ import annotations

from typing import Any


STAGES = ("首預", "T-30", "T-5")
MARKETS = ("HDC", "HIL", "CHL")


def _line(grade: dict[str, Any]) -> float | None:
    raw = grade.get("line")
    if raw is None:
        raw = grade.get("condition")
    try:
        return float(raw)
    except (TypeError, ValueError):
        return None


def _metrics(
    samples: list[dict[str, dict[str, Any]]],
    stage: str,
) -> dict[str, Any]:
    grades = [
        sample[stage]
        for sample in samples
        if sample[stage].get("grade_status") == "GRADED"
        and sample[stage].get("hit") is not None
    ]
    hits = sum(grade.get("hit") is True for grade in grades)
    return {
        "fixtures": len(samples),
        "decided": len(grades),
        "hits": hits,
        "accuracy": round(hits / len(grades), 6) if grades else None,
    }


def calculate_three_stage_consensus(
    rows: list[dict[str, Any]],
) -> dict[str, Any]:
    """Return direction-stable and exact-line-stable results by market.

    A fixture qualifies only when the same market has a prediction at 首預,
    T-30 and T-5 and all three selected sides are identical.  The primary
    metric grades one fixture once at its T-5 line.  Pushes and unavailable
    grades are retained in the fixture count but excluded from the accuracy
    denominator.
    """
    grouped: dict[tuple[str, str], dict[str, dict[str, Any]]] = {}
    for row in rows:
        match_id = str(row.get("match_id") or "")
        stage = str(row.get("stage") or "")
        if not match_id or stage not in STAGES:
            continue
        for grade in row.get("market_grades") or []:
            if not isinstance(grade, dict):
                continue
            code = str(grade.get("code") or "")
            if code not in MARKETS:
                continue
            grouped.setdefault((match_id, code), {})[stage] = grade

    markets: dict[str, Any] = {}
    for code in MARKETS:
        same_direction: list[dict[str, dict[str, Any]]] = []
        same_direction_and_line: list[dict[str, dict[str, Any]]] = []
        for (_, grouped_code), stage_grades in grouped.items():
            if grouped_code != code or any(stage not in stage_grades for stage in STAGES):
                continue
            sides = {str(stage_grades[stage].get("side") or "") for stage in STAGES}
            if len(sides) != 1 or "" in sides:
                continue
            same_direction.append(stage_grades)
            lines = [_line(stage_grades[stage]) for stage in STAGES]
            if all(line is not None for line in lines) and len(set(lines)) == 1:
                same_direction_and_line.append(stage_grades)

        markets[code] = {
            "same_direction": {
                "fixtures": len(same_direction),
                "line_changed_fixtures": (
                    len(same_direction) - len(same_direction_and_line)
                ),
                "primary": _metrics(same_direction, "T-5"),
                "stage_diagnostics": {
                    stage: _metrics(same_direction, stage) for stage in STAGES
                },
            },
            "same_direction_and_line": {
                "fixtures": len(same_direction_and_line),
                "primary": _metrics(same_direction_and_line, "T-5"),
                "stage_diagnostics": {
                    stage: _metrics(same_direction_and_line, stage)
                    for stage in STAGES
                },
            },
        }
    return {
        "definition": "same selection side at 首預, T-30 and T-5",
        "primary_unit": "one unique fixture per market, graded at T-5 line",
        "pushes_excluded": True,
        "markets": markets,
    }
