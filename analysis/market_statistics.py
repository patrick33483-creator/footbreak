"""Shared, auditable market-prediction statistics.

The public market hit rate is deliberately scoped to selected decimal odds
``>= 1.70``.  Lower prices and missing/non-finite prices are retained in
separate audit cohorts and never leak into the public denominator.
"""
from __future__ import annotations

import math
from typing import Any, Iterable


MARKETS = ("HDC", "HIL", "CHL")
ODDS_THRESHOLD = 1.70


def odds_bucket(value: Any) -> str:
    """Return the exclusive selected-odds cohort for a value.

    Non-numeric, non-finite and non-positive/non-bettable decimal prices are
    ``missing``.  In particular, neither ``None`` nor ``NaN`` may be treated
    as a low-odds sample.
    """
    try:
        odds = float(value)
    except (TypeError, ValueError):
        return "missing"
    if not math.isfinite(odds) or odds <= 1.0:
        return "missing"
    return "at_or_above_1_70" if odds >= ODDS_THRESHOLD else "below_1_70"


def _empty_metrics() -> dict[str, Any]:
    return {
        "graded": 0,
        "decided": 0,
        "hits": 0,
        "pushes": 0,
        "accuracy": None,
        "brier": None,
        "log_loss": None,
    }


def _aggregate(grades: Iterable[dict[str, Any]]) -> dict[str, Any]:
    grades = list(grades)
    result = _empty_metrics()
    result["graded"] = len(grades)
    decided = [grade for grade in grades if grade.get("hit") is not None]
    result["decided"] = len(decided)
    result["pushes"] = len(grades) - len(decided)
    result["hits"] = sum(grade.get("hit") is True for grade in decided)
    result["accuracy"] = (
        round(result["hits"] / result["decided"], 6) if result["decided"] else None
    )
    for key in ("brier", "log_loss"):
        values = []
        for grade in grades:
            try:
                value = float(grade.get(key))
            except (TypeError, ValueError):
                continue
            if math.isfinite(value):
                values.append(value)
        result[key] = round(sum(values) / len(values), 6) if values else None
    return result


def market_metrics(rows: Iterable[dict[str, Any]], code: str | None = None) -> dict[str, Any]:
    """Aggregate scored market grades with explicit selected-odds cohorts.

    Top-level metrics are the primary ``>=1.70`` scope.  ``all_odds`` is a
    complete audit total; its three ``odds_groups`` are mutually exclusive and
    sum exactly to it.  A graded push stays in its odds group but is excluded
    from every decided denominator.  Pending/ungraded rows are absent from all
    market statistics rather than being counted as losses.
    """
    grades = [
        grade
        for row in rows
        for grade in (row.get("market_grades") or [])
        if isinstance(grade, dict)
        and grade.get("grade_status") == "GRADED"
        and (code is None or grade.get("code") == code)
    ]
    cohorts = {
        bucket: [grade for grade in grades if odds_bucket(grade.get("odds")) == bucket]
        for bucket in ("at_or_above_1_70", "below_1_70", "missing")
    }
    high = _aggregate(cohorts["at_or_above_1_70"])
    groups = {bucket: _aggregate(items) for bucket, items in cohorts.items()}
    all_odds = _aggregate(grades)
    return {
        **high,
        "odds_scope": "selected_odds_at_or_above_1_70",
        "odds_threshold": ODDS_THRESHOLD,
        "all_odds": all_odds,
        "odds_groups": groups,
    }
