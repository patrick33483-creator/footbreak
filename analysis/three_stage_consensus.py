"""Shared one-fixture statistics for predictions stable across all three stages."""
from __future__ import annotations

import math
from typing import Any


STAGES = ("首預", "T-30", "T-5")
MARKETS = ("HDC", "HIL", "CHL")
MARKET_LABELS = {"HDC": "讓球", "HIL": "入球大細", "CHL": "角球大細"}
RANKING_MIN_DECIDED = 30
LOW_ODDS_THRESHOLD = 1.70


def _line(grade: dict[str, Any]) -> float | None:
    raw = grade.get("line")
    if raw is None:
        raw = grade.get("condition")
    try:
        value = float(raw)
    except (TypeError, ValueError):
        return None
    return value if math.isfinite(value) else None


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


def _samples_by_odds(
    samples: list[dict[str, dict[str, Any]]],
    band: str,
    stage: str = "T-5",
) -> list[dict[str, dict[str, Any]]]:
    selected = []
    for sample in samples:
        try:
            odds = float(sample[stage].get("odds"))
        except (KeyError, TypeError, ValueError):
            continue
        if not math.isfinite(odds) or odds <= 1.0:
            continue
        if band == "low" and odds < LOW_ODDS_THRESHOLD:
            selected.append(sample)
        elif band == "eligible" and odds >= LOW_ODDS_THRESHOLD:
            selected.append(sample)
    return selected


def _priced_samples(
    samples: list[dict[str, dict[str, Any]]],
    stage: str = "T-5",
) -> list[dict[str, dict[str, Any]]]:
    """Return only samples with a valid selected price at the grading stage."""
    return (
        _samples_by_odds(samples, "low", stage)
        + _samples_by_odds(samples, "eligible", stage)
    )


def _odds_bias(
    samples: list[dict[str, dict[str, Any]]],
    stage: str = "T-5",
) -> dict[str, Any]:
    grades = [
        sample[stage]
        for sample in samples
        if sample[stage].get("grade_status") == "GRADED"
        and sample[stage].get("hit") is not None
    ]
    priced = []
    for grade in grades:
        try:
            odds = float(grade.get("odds"))
        except (TypeError, ValueError):
            continue
        if math.isfinite(odds) and odds > 1.0:
            priced.append((grade, odds))
    low = [item for item in priced if item[1] < LOW_ODDS_THRESHOLD]
    eligible = [item for item in priced if item[1] >= LOW_ODDS_THRESHOLD]

    def cohort(items: list[tuple[dict[str, Any], float]]) -> dict[str, Any]:
        hits = sum(grade.get("hit") is True for grade, _ in items)
        return {
            "decided": len(items),
            "hits": hits,
            "accuracy": round(hits / len(items), 6) if items else None,
            "average_odds": (
                round(sum(odds for _, odds in items) / len(items), 3)
                if items else None
            ),
        }

    low_metrics = cohort(low)
    eligible_metrics = cohort(eligible)
    return {
        "threshold": LOW_ODDS_THRESHOLD,
        "decided": len(grades),
        "priced_decided": len(priced),
        "missing_odds": len(grades) - len(priced),
        "average_odds": (
            round(sum(odds for _, odds in priced) / len(priced), 3)
            if priced else None
        ),
        "low_odds": {
            **low_metrics,
            "share": round(len(low) / len(priced), 6) if priced else None,
        },
        "at_or_above_threshold": eligible_metrics,
    }


def _breakdown(
    samples: list[dict[str, dict[str, Any]]],
    code: str,
) -> list[dict[str, Any]]:
    """Split exact three-stage samples by the selected T-5 market role."""
    if code == "HDC":
        definitions = (
            ("home_giving", "主讓"),
            ("home_receiving", "主受讓"),
            ("scratch_home", "平手盤（主）"),
            ("scratch_away", "平手盤（客）"),
            ("away_giving", "客讓"),
            ("away_receiving", "客受讓"),
        )

        def category(sample: dict[str, dict[str, Any]]) -> str | None:
            grade = sample["T-5"]
            side = str(grade.get("side") or "")
            line = _line(grade)
            if line is None or side not in {"H", "A"}:
                return None
            if abs(line) < 1e-9:
                return "scratch_home" if side == "H" else "scratch_away"
            if side == "H":
                return "home_giving" if line < 0 else "home_receiving"
            return "away_giving" if line > 0 else "away_receiving"
    else:
        definitions = (("over", "大"), ("under", "細"))

        def category(sample: dict[str, dict[str, Any]]) -> str | None:
            side = str(sample["T-5"].get("side") or "")
            return {"H": "over", "L": "under"}.get(side)

    output = []
    for key, label in definitions:
        subset = [sample for sample in samples if category(sample) == key]
        eligible = _samples_by_odds(subset, "eligible")
        output.append({
            "key": key,
            "label": label,
            **_metrics(eligible, "T-5"),
            "all_fixtures": len(subset),
            "odds_bias": _odds_bias(subset),
        })
    return output


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
                "fixtures": len(_priced_samples(same_direction)),
                "excluded_missing_odds": (
                    len(same_direction) - len(_priced_samples(same_direction))
                ),
                "line_changed_fixtures": (
                    len(_priced_samples(same_direction))
                    - len(_priced_samples(same_direction_and_line))
                ),
                "primary": _metrics(
                    _samples_by_odds(same_direction, "eligible"),
                    "T-5",
                ),
                "odds_segments": _odds_bias(same_direction),
                "stage_diagnostics": {
                    stage: _metrics(_priced_samples(same_direction), stage)
                    for stage in STAGES
                },
            },
            "same_direction_and_line": {
                "fixtures": len(_priced_samples(same_direction_and_line)),
                "excluded_missing_odds": (
                    len(same_direction_and_line)
                    - len(_priced_samples(same_direction_and_line))
                ),
                "primary": _metrics(
                    _samples_by_odds(same_direction_and_line, "eligible"),
                    "T-5",
                ),
                "odds_segments": _odds_bias(same_direction_and_line),
                "breakdown": _breakdown(same_direction_and_line, code),
                "stage_diagnostics": {
                    stage: _metrics(_priced_samples(same_direction_and_line), stage)
                    for stage in STAGES
                },
            },
        }
    ranking_candidates = []
    for code in MARKETS:
        for item in markets[code]["same_direction_and_line"]["breakdown"]:
            if item["accuracy"] is None:
                continue
            ranking_candidates.append({
                "market": code,
                "market_label": MARKET_LABELS[code],
                "condition_key": item["key"],
                "condition_label": item["label"],
                "fixtures": item["fixtures"],
                "all_fixtures": item["all_fixtures"],
                "decided": item["decided"],
                "hits": item["hits"],
                "accuracy": item["accuracy"],
                "sample_qualified": item["decided"] >= RANKING_MIN_DECIDED,
                "odds_bias": item["odds_bias"],
            })
    ranking_candidates.sort(key=lambda item: (
        -int(item["sample_qualified"]),
        -item["accuracy"],
        -item["decided"],
        MARKETS.index(item["market"]),
        item["condition_key"],
    ))

    return {
        "definition": "same selection side at 首預, T-30 and T-5",
        "primary_unit": "one unique fixture per market, graded at T-5 line",
        "primary_odds_scope": "T-5 selected odds >= 1.70",
        "low_odds_scope": "T-5 selected odds < 1.70, reported separately",
        "missing_odds_excluded": True,
        "pushes_excluded": True,
        "ranking": {
            "scope": "same direction and exact line at all three stages",
            "odds_scope": "T-5 selected odds >= 1.70 only",
            "minimum_decided": RANKING_MIN_DECIDED,
            "priority": "qualified sample first, then accuracy, then decided sample",
            "top": ranking_candidates[:3],
            "candidate_count": len(ranking_candidates),
        },
        "markets": markets,
    }
