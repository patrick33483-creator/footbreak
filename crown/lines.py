"""Canonical Asian-line parsing and settlement.  No silent rounding."""
from __future__ import annotations

from typing import Literal

QuarterStatus = Literal["Won", "Half Won", "Refunded", "Half Lost", "Lost"]


def is_quarter(value: float | int | None) -> bool:
    return value is not None and abs(float(value) * 4 - round(float(value) * 4)) < 1e-9


def _parts(raw: str, positive_only: bool = False) -> list[float] | None:
    try:
        values = [float(part.strip().lstrip("+")) for part in str(raw).split("/") if part.strip()]
    except ValueError:
        return None
    if not values or len(values) > 2 or (positive_only and any(v < 0 for v in values)):
        return None
    if len(values) == 2 and abs(abs(values[0] - values[1]) - 0.5) > 1e-9:
        return None
    average = sum(values) / len(values)
    return values if is_quarter(average) else None


def parse_hkjc_handicap(raw: str) -> float | None:
    values = _parts(raw)
    return None if values is None else sum(values) / len(values)


def parse_hkjc_total(raw: str) -> float | None:
    values = _parts(raw, positive_only=True)
    return None if values is None else sum(values) / len(values)


def parse_titan_handicap(raw: float | str) -> float | None:
    """Titan's positive goals means the home team gives; internal sign is opposite."""
    try:
        value = -float(raw)
    except (TypeError, ValueError):
        return None
    return 0.0 if value == 0 and is_quarter(value) else value if is_quarter(value) else None


def parse_titan_total(raw: float | str) -> float | None:
    try:
        value = abs(float(raw))
    except (TypeError, ValueError):
        return None
    return value if value >= 0 and is_quarter(value) else None


def split_line(value: float) -> tuple[float, ...]:
    if not is_quarter(value):
        raise ValueError("line must be a quarter-goal increment")
    return (value,) if abs(value * 2 - round(value * 2)) < 1e-9 else (value - 0.25, value + 0.25)


def _outcome(delta: float) -> int:
    return 1 if delta > 1e-9 else -1 if delta < -1e-9 else 0


def _combine(values: list[int]) -> QuarterStatus:
    if len(values) == 1:
        return {1: "Won", 0: "Refunded", -1: "Lost"}[values[0]]
    total = sum(values)
    if total == 2:
        return "Won"
    if total == 1:
        return "Half Won"
    if total == 0:
        return "Refunded"
    if total == -1:
        return "Half Lost"
    return "Lost"


def settle_handicap(home_handicap: float, selection: str, home_score: int, away_score: int) -> QuarterStatus:
    if selection not in {"H", "A"}:
        raise ValueError("handicap selection must be H or A")
    return _combine([
        _outcome((home_score + half - away_score) if selection == "H" else (away_score - home_score - half))
        for half in split_line(home_handicap)
    ])


def settle_total(total: float, selection: str, home_score: int, away_score: int) -> QuarterStatus:
    if selection not in {"H", "L"}:
        raise ValueError("total selection must be H (over) or L (under)")
    goals = home_score + away_score
    return _combine([_outcome(goals - half if selection == "H" else half - goals) for half in split_line(total)])


def pnl(status: QuarterStatus, stake: float, decimal_odds: float) -> float:
    profit = decimal_odds - 1
    multiplier = {"Won": profit, "Half Won": profit / 2, "Refunded": 0, "Half Lost": -0.5, "Lost": -1}[status]
    return round(stake * multiplier, 2)
