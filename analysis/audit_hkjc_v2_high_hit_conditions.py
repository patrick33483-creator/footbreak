#!/usr/bin/env python3
"""Read-only, time-split search for robust high-hit HKJC V2 conditions."""

from __future__ import annotations

import collections
import json
import math
import sqlite3
from typing import Any, Callable

from analysis.footbreak_direction_path_conditions import DEFAULT_DB, extract_footbreak


ODDS_THRESHOLDS = (1.60, 1.65, 1.70, 1.75, 1.80, 1.85, 1.90)
MIN_ALL = 50
MIN_DISCOVERY = 30
MIN_HOLDOUT = 15


def wilson_low(hits: int, decided: int, z: float = 1.959963984540054) -> float | None:
    if decided <= 0:
        return None
    p = hits / decided
    den = 1 + z * z / decided
    centre = p + z * z / (2 * decided)
    margin = z * math.sqrt(p * (1 - p) / decided + z * z / (4 * decided * decided))
    return (centre - margin) / den


def summarize(rows: list[dict[str, Any]]) -> dict[str, Any]:
    decided_rows = [
        row for row in rows
        if row.get("settlement") in {"win", "half_win", "half_loss", "loss"}
    ]
    hits = sum(row["settlement"] in {"win", "half_win"} for row in decided_rows)
    returns = [float(row["unit_return"]) for row in rows if row.get("unit_return") is not None]
    odds = [float(row["T5"]["odds"]) for row in rows if row.get("T5", {}).get("odds")]
    decided = len(decided_rows)
    return {
        "bets": len(returns),
        "decided": decided,
        "hits": hits,
        "hit_rate": hits / decided if decided else None,
        "wilson_95_low": wilson_low(hits, decided),
        "roi": sum(returns) / len(returns) if returns else None,
        "unit_profit": sum(returns),
        "average_odds": sum(odds) / len(odds) if odds else None,
    }


def odds_move(row: dict[str, Any]) -> str:
    delta = float(row["T5"]["odds"]) - float(row["initial"]["odds"])
    if delta <= -0.05:
        return "shortened_0.05"
    if delta >= 0.05:
        return "drifted_0.05"
    return "stable_within_0.05"


def line_move(row: dict[str, Any]) -> str:
    market = row["market"]
    side = row["T5"]["side"]
    initial = float(row["initial"]["line"])
    t5 = float(row["T5"]["line"])
    if market == "HDC":
        advantage = (t5 - initial) if side == "H" else (initial - t5)
    else:
        advantage = (initial - t5) if side == "O" else (t5 - initial)
    if advantage >= 0.25:
        return "better_by_0.25"
    if advantage <= -0.25:
        return "worse_by_0.25"
    return "same_line"


def exact_line_label(value: float) -> str:
    return f"{value:+.2f}"


def candidate_specs(
    rows: list[dict[str, Any]],
) -> list[tuple[str, dict[str, Any], Callable[[dict[str, Any]], bool]]]:
    specs: list[tuple[str, dict[str, Any], Callable[[dict[str, Any]], bool]]] = []
    seen: set[str] = set()

    def add(label: str, definition: dict[str, Any], predicate: Callable[[dict[str, Any]], bool]) -> None:
        key = json.dumps(definition, ensure_ascii=False, sort_keys=True)
        if key not in seen:
            seen.add(key)
            specs.append((label, definition, predicate))

    markets = ("HDC", "HIL", "CHL")
    for market in markets:
        market_rows = [row for row in rows if row["market"] == market]
        sides = sorted({row["T5"]["side"] for row in market_rows})
        paths2 = sorted({"→".join(row["direction_path"].split("→")[-2:]) for row in market_rows})
        paths3 = sorted({row["direction_path"] for row in market_rows})
        lines = sorted({float(row["T5"]["line"]) for row in market_rows})

        bases: list[tuple[str, dict[str, Any], Callable[[dict[str, Any]], bool]]] = []
        for side in sides:
            bases.append((
                f"{market} T5={side}",
                {"market": market, "pattern": "T5", "value": side},
                lambda row, m=market, s=side: row["market"] == m and row["T5"]["side"] == s,
            ))
        for path in paths2:
            bases.append((
                f"{market} T30→T5={path}",
                {"market": market, "pattern": "T30_T5", "value": path},
                lambda row, m=market, p=path: (
                    row["market"] == m
                    and "→".join(row["direction_path"].split("→")[-2:]) == p
                ),
            ))
        for path in paths3:
            bases.append((
                f"{market} 初→T30→T5={path}",
                {"market": market, "pattern": "full_path", "value": path},
                lambda row, m=market, p=path: row["market"] == m and row["direction_path"] == p,
            ))

        for label, definition, predicate in bases:
            add(label, definition, predicate)
            for threshold in ODDS_THRESHOLDS:
                add(
                    f"{label}；T5賠率≥{threshold:.2f}",
                    {**definition, "T5_odds_gte": threshold},
                    lambda row, pred=predicate, t=threshold: pred(row) and float(row["T5"]["odds"]) >= t,
                )
            for move in ("shortened_0.05", "stable_within_0.05", "drifted_0.05"):
                add(
                    f"{label}；賠率變化={move}",
                    {**definition, "odds_move": move},
                    lambda row, pred=predicate, mv=move: pred(row) and odds_move(row) == mv,
                )
            for move in ("better_by_0.25", "same_line", "worse_by_0.25"):
                add(
                    f"{label}；線位變化={move}",
                    {**definition, "line_move": move},
                    lambda row, pred=predicate, mv=move: pred(row) and line_move(row) == mv,
                )

        for line in lines:
            add(
                f"{market} T5線={exact_line_label(line)}",
                {"market": market, "T5_line": line},
                lambda row, m=market, ln=line: row["market"] == m and abs(float(row["T5"]["line"]) - ln) < 1e-9,
            )
            for side in sides:
                add(
                    f"{market} T5={side}；線={exact_line_label(line)}",
                    {"market": market, "pattern": "T5", "value": side, "T5_line": line},
                    lambda row, m=market, s=side, ln=line: (
                        row["market"] == m
                        and row["T5"]["side"] == s
                        and abs(float(row["T5"]["line"]) - ln) < 1e-9
                    ),
                )
    return specs


def main() -> None:
    db = sqlite3.connect(DEFAULT_DB, uri=True)
    db.row_factory = sqlite3.Row
    db.execute("PRAGMA query_only=ON")
    db.execute("BEGIN")
    try:
        observations, diagnostics = extract_footbreak(db)
    finally:
        db.close()

    settled = sorted(
        [row for row in observations if row.get("settlement") is not None],
        key=lambda row: (int(row["kickoff"]), str(row["fixture_id"]), row["market"]),
    )
    cutoff_index = max(1, math.floor(len(settled) * 0.70))
    cutoff = int(settled[cutoff_index - 1]["kickoff"]) if settled else 0
    discovery = [row for row in settled if int(row["kickoff"]) <= cutoff]
    holdout = [row for row in settled if int(row["kickoff"]) > cutoff]

    evaluated = []
    for label, definition, predicate in candidate_specs(discovery):
        all_rows = [row for row in settled if predicate(row)]
        discovery_rows = [row for row in discovery if predicate(row)]
        holdout_rows = [row for row in holdout if predicate(row)]
        metrics = {
            "all": summarize(all_rows),
            "discovery": summarize(discovery_rows),
            "holdout": summarize(holdout_rows),
        }
        if (
            metrics["all"]["decided"] < MIN_ALL
            or metrics["discovery"]["decided"] < MIN_DISCOVERY
            or metrics["holdout"]["decided"] < MIN_HOLDOUT
        ):
            continue
        robust = (
            metrics["all"]["hit_rate"] >= 0.60
            and metrics["discovery"]["hit_rate"] >= 0.58
            and metrics["holdout"]["hit_rate"] >= 0.58
            and metrics["all"]["roi"] > 0
            and metrics["discovery"]["roi"] > 0
            and metrics["holdout"]["roi"] > 0
        )
        evaluated.append({
            "label": label,
            "definition": definition,
            "robust_high_hit": robust,
            **metrics,
        })

    ranked = sorted(
        evaluated,
        key=lambda row: (
            row["robust_high_hit"],
            min(row["discovery"]["hit_rate"], row["holdout"]["hit_rate"]),
            row["all"]["decided"],
            row["all"]["roi"],
        ),
        reverse=True,
    )
    by_market = {
        market: summarize([row for row in settled if row["market"] == market])
        for market in ("HDC", "HIL", "CHL")
    }
    result = {
        "method": {
            "source": DEFAULT_DB,
            "read_only": True,
            "split": "global chronological 70/30",
            "cutoff_epoch": cutoff,
            "minimum_decided": {
                "all": MIN_ALL,
                "discovery": MIN_DISCOVERY,
                "holdout": MIN_HOLDOUT,
            },
            "robust_rule": (
                "all hit>=60%; discovery and holdout hit>=58%; "
                "ROI positive in all/discovery/holdout"
            ),
            "candidate_families": [
                "T5 direction",
                "T30→T5 path",
                "full three-stage path",
                "T5 odds threshold",
                "T5 exact line",
                "odds movement",
                "line movement",
            ],
        },
        "coverage": {
            "all_complete_three_stage_observations": len(observations),
            "settled_observations": len(settled),
            "discovery": len(discovery),
            "holdout": len(holdout),
            "diagnostics": diagnostics,
        },
        "market_baselines": by_market,
        "eligible_candidate_count": len(evaluated),
        "robust_high_hit_count": sum(row["robust_high_hit"] for row in evaluated),
        "robust_high_hit": [row for row in ranked if row["robust_high_hit"]],
        "top_25_eligible": ranked[:25],
    }
    print(json.dumps(result, ensure_ascii=False, indent=2, sort_keys=True))


if __name__ == "__main__":
    main()
