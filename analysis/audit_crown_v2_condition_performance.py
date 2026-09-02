#!/usr/bin/env python3
"""Independently audit Crown V2 public-condition performance from data.json."""

from __future__ import annotations

import argparse
import json
import math
from collections import Counter, defaultdict
from datetime import datetime
from pathlib import Path
from typing import Any


SETTLED = {"Won", "Half Won", "Refunded", "Half Lost", "Lost"}


def canonical_key(row: dict[str, Any]) -> tuple[str, str, str]:
    def normalise(value: Any) -> str:
        return " ".join(str(value or "").strip().casefold().split())

    return (
        normalise(row.get("home_team")),
        normalise(row.get("away_team")),
        normalise(row.get("kickoff_hkt") or row.get("kickoff")),
    )


def profit(row: dict[str, Any]) -> float | None:
    settlement = row.get("settlement")
    try:
        odds = float(row.get("odds"))
    except (TypeError, ValueError):
        return None
    if settlement == "Won":
        return odds - 1.0
    if settlement == "Half Won":
        return (odds - 1.0) / 2.0
    if settlement == "Refunded":
        return 0.0
    if settlement == "Half Lost":
        return -0.5
    if settlement == "Lost":
        return -1.0
    return None


def wilson_interval(successes: int, total: int, z: float = 1.959963984540054) -> list[float] | None:
    if total <= 0:
        return None
    p = successes / total
    denominator = 1.0 + z * z / total
    centre = (p + z * z / (2.0 * total)) / denominator
    margin = z * math.sqrt((p * (1.0 - p) + z * z / (4.0 * total)) / total) / denominator
    return [round(max(0.0, centre - margin), 6), round(min(1.0, centre + margin), 6)]


def lower_tail_probability(successes: int, total: int, expected_rate: float) -> float | None:
    if total <= 0 or not 0.0 <= expected_rate <= 1.0:
        return None
    probability = sum(
        math.comb(total, k)
        * expected_rate**k
        * (1.0 - expected_rate) ** (total - k)
        for k in range(successes + 1)
    )
    return round(min(1.0, probability), 8)


def metrics(rows: list[dict[str, Any]]) -> dict[str, Any]:
    settled_rows = [row for row in rows if row.get("settlement") in SETTLED]
    counts = Counter(str(row.get("settlement")) for row in settled_rows)
    denominator = len(settled_rows) - counts["Refunded"]
    hits = counts["Won"] + counts["Half Won"]
    priced_profits = [value for row in settled_rows if (value := profit(row)) is not None]
    total_profit = sum(priced_profits)
    return {
        "settled": len(settled_rows),
        "pending": len(rows) - len(settled_rows),
        "settlement_counts": {key: counts[key] for key in ("Won", "Half Won", "Refunded", "Half Lost", "Lost")},
        "hit_denominator": denominator,
        "hits": hits,
        "hit_rate": round(hits / denominator, 6) if denominator else None,
        "wilson_95": wilson_interval(hits, denominator),
        "priced_bets": len(priced_profits),
        "profit_units": round(total_profit, 6),
        "roi": round(total_profit / len(priced_profits), 6) if priced_profits else None,
    }


def parse_time(row: dict[str, Any]) -> datetime:
    raw = str(row.get("kickoff_hkt") or row.get("kickoff") or "")
    try:
        return datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except ValueError:
        return datetime.min


def audit_condition(condition: dict[str, Any]) -> dict[str, Any]:
    rows = list(condition.get("observations") or [])
    grouped: dict[tuple[str, str, str], list[dict[str, Any]]] = defaultdict(list)
    for row in rows:
        grouped[canonical_key(row)].append(row)
    duplicate_groups = {
        " | ".join(key): [
            {
                "match_id": row.get("match_id"),
                "settlement": row.get("settlement"),
                "odds": row.get("odds"),
            }
            for row in group
        ]
        for key, group in grouped.items()
        if len(group) > 1
    }
    deduped = [max(group, key=parse_time) for group in grouped.values()]
    deduped.sort(key=parse_time)

    current = metrics(deduped)
    historical = condition.get("historical") or {}
    historical_rate = historical.get("hit_rate")
    try:
        historical_rate_float = float(historical_rate)
    except (TypeError, ValueError):
        historical_rate_float = None
    if historical_rate_float is not None and historical_rate_float > 1:
        historical_rate_float /= 100.0

    hit_rate = current.get("hit_rate")
    current["historical_hit_rate"] = historical_rate_float
    current["hit_rate_drift_pp"] = (
        round((hit_rate - historical_rate_float) * 100.0, 2)
        if hit_rate is not None and historical_rate_float is not None
        else None
    )
    current["historical_lower_tail_probability"] = (
        lower_tail_probability(current["hits"], current["hit_denominator"], historical_rate_float)
        if historical_rate_float is not None
        else None
    )

    settled_chronological = [row for row in deduped if row.get("settlement") in SETTLED]
    recent = {}
    for window in (5, 10, 20):
        sample = settled_chronological[-window:]
        recent[str(window)] = metrics(sample) if sample else None

    prospective = condition.get("prospective") or {}
    expected_rows = prospective.get("qualified")
    row_count_matches_summary = expected_rows == len(rows) if isinstance(expected_rows, int) else None

    return {
        "condition_id": condition.get("id"),
        "name": condition.get("name"),
        "tier": condition.get("tier"),
        "evidence_status": condition.get("evidence_status"),
        "rule": condition.get("rule"),
        "observation_rows": len(rows),
        "summary_qualified": expected_rows,
        "row_count_matches_summary": row_count_matches_summary,
        "duplicate_rows": sum(len(group) - 1 for group in grouped.values() if len(group) > 1),
        "duplicate_groups": duplicate_groups,
        "current_deduped": current,
        "recent_settled_windows": recent,
        "historical": historical,
    }


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("data_json", type=Path)
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()

    payload = json.loads(args.data_json.read_text(encoding="utf-8"))
    conditions = payload.get("public_conditions") or []
    report = {
        "generated_at": datetime.now().astimezone().isoformat(),
        "source_updated_at": payload.get("updated_at"),
        "condition_count": len(conditions),
        "conditions": [audit_condition(condition) for condition in conditions],
    }
    rendered = json.dumps(report, ensure_ascii=False, indent=2)
    if args.output:
        args.output.write_text(rendered + "\n", encoding="utf-8")
    else:
        print(rendered)


if __name__ == "__main__":
    main()
