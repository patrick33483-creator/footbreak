#!/usr/bin/env python3
"""Read-only aggregate review of the active Footbreak and Crown portfolios."""

from __future__ import annotations

import json
import math
from collections import Counter, defaultdict
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable


HKT = timezone(timedelta(hours=8))
WINDOW_START = datetime(2026, 8, 15, 0, 0, tzinfo=HKT)
SOURCES = {
    "足破": (
        Path("/opt/footbreak/system/sim_ledger.json"),
        "footbreak_condition_simulation",
    ),
    "皇冠": (
        Path("/var/lib/footbreak/crown/ledger.json"),
        "condition_simulation",
    ),
}
STRATEGY = "granular-condition-v1"
RESULTS = ("Won", "Half Won", "Refunded", "Half Lost", "Lost")
MARKET_NAMES = {"HDC": "讓球", "HIL": "入球大細", "CHL": "角球大細"}


def parse_time(value: Any) -> datetime | None:
    try:
        result = datetime.fromisoformat(str(value or "").replace("Z", "+00:00"))
    except (TypeError, ValueError):
        return None
    if result.tzinfo is None:
        result = result.replace(tzinfo=HKT)
    return result.astimezone(HKT)


def number(value: Any) -> float | None:
    try:
        result = float(value)
    except (TypeError, ValueError):
        return None
    return result if math.isfinite(result) else None


def wilson(hits: int, decided: int) -> list[float] | None:
    if decided <= 0:
        return None
    z = 1.959963984540054
    p = hits / decided
    denominator = 1 + z * z / decided
    centre = (p + z * z / (2 * decided)) / denominator
    margin = (
        z
        * math.sqrt((p * (1 - p) + z * z / (4 * decided)) / decided)
        / denominator
    )
    return [round(max(0.0, centre - margin), 4), round(min(1.0, centre + margin), 4)]


def market_name(bet: dict[str, Any]) -> str:
    code = str(bet.get("code") or bet.get("market") or "").upper()
    return MARKET_NAMES.get(code, str(bet.get("market_label") or code or "其他"))


def active_bets(path: Path, portfolio: str) -> list[dict[str, Any]]:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return []
    return [
        bet
        for bet in (payload.get("bets") or [])
        if isinstance(bet, dict)
        and bet.get("portfolio") == portfolio
        and bet.get("strategy") == STRATEGY
    ]


def segment(
    bets: list[dict[str, Any]],
    key_fn: Callable[[dict[str, Any]], str],
    *,
    minimum: int = 1,
) -> dict[str, Any]:
    grouped: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for bet in bets:
        grouped[key_fn(bet)].append(bet)
    output: dict[str, Any] = {}
    for key, rows in sorted(grouped.items()):
        if len(rows) < minimum:
            continue
        output[key] = metrics(rows)
    return output


def metrics(bets: list[dict[str, Any]]) -> dict[str, Any]:
    settled = [bet for bet in bets if bet.get("status") == "SETTLED"]
    pending = [bet for bet in bets if bet.get("status") == "PENDING"]
    decided = [bet for bet in settled if bet.get("result") != "Refunded"]
    hits = sum(bet.get("result") in {"Won", "Half Won"} for bet in decided)
    stake = sum(number(bet.get("stake")) or 0.0 for bet in settled)
    pnl = sum(number(bet.get("pnl")) or 0.0 for bet in settled)
    odds = [value for bet in settled if (value := number(bet.get("odds"))) and value > 1]
    condition_accuracy = [
        value
        for bet in settled
        if (value := number(bet.get("condition_accuracy"))) is not None
    ]
    condition_samples = [
        int(value)
        for bet in settled
        if (value := number(bet.get("condition_decided"))) is not None
    ]
    ordered = sorted(
        settled,
        key=lambda bet: parse_time(bet.get("settled_at") or bet.get("created_at"))
        or datetime.min.replace(tzinfo=HKT),
    )
    run = max_run = 0
    equity = peak = 50_000.0
    max_drawdown = 0.0
    for bet in ordered:
        value = number(bet.get("pnl")) or 0.0
        equity += value
        peak = max(peak, equity)
        max_drawdown = max(max_drawdown, peak - equity)
        if value < 0:
            run += 1
            max_run = max(max_run, run)
        else:
            run = 0
    return {
        "bets": len(bets),
        "settled": len(settled),
        "pending": len(pending),
        "voided": sum(bet.get("status") == "VOIDED" for bet in bets),
        "turnover": round(stake, 2),
        "pnl": round(pnl, 2),
        "roi": round(pnl / stake, 4) if stake else None,
        "decided": len(decided),
        "positive_results": hits,
        "positive_result_rate": round(hits / len(decided), 4) if decided else None,
        "positive_result_wilson95": wilson(hits, len(decided)),
        "result_counts": {name: sum(bet.get("result") == name for bet in settled) for name in RESULTS},
        "average_odds": round(sum(odds) / len(odds), 4) if odds else None,
        "average_implied_probability": (
            round(sum(1 / value for value in odds) / len(odds), 4) if odds else None
        ),
        "average_condition_accuracy": (
            round(sum(condition_accuracy) / len(condition_accuracy), 4)
            if condition_accuracy
            else None
        ),
        "condition_minus_implied": (
            round(
                sum(condition_accuracy) / len(condition_accuracy)
                - sum(1 / value for value in odds) / len(odds),
                4,
            )
            if condition_accuracy and len(condition_accuracy) == len(odds)
            else None
        ),
        "condition_sample_median": (
            sorted(condition_samples)[len(condition_samples) // 2]
            if condition_samples
            else None
        ),
        "max_consecutive_negative_returns": max_run,
        "max_drawdown": round(max_drawdown, 2),
    }


def review(name: str, bets: list[dict[str, Any]]) -> dict[str, Any]:
    recent = [
        bet
        for bet in bets
        if (created := parse_time(bet.get("created_at"))) is not None
        and created >= WINDOW_START
    ]
    fixtures: Counter[str] = Counter()
    for bet in recent:
        fixture = str(
            bet.get("match_id")
            or bet.get("fixture_id")
            or f"{bet.get('kickoff')}|{bet.get('home')}|{bet.get('away')}"
        )
        fixtures[fixture] += 1
    conditions = segment(
        [bet for bet in recent if bet.get("status") == "SETTLED"],
        lambda bet: f"{market_name(bet)}｜{str(bet.get('condition_label') or '未標示')}",
        minimum=2,
    )
    ranked_conditions = sorted(
        (
            {"condition": label, **row}
            for label, row in conditions.items()
        ),
        key=lambda row: (
            -(row.get("roi") if row.get("roi") is not None else -999),
            -row.get("settled", 0),
            row["condition"],
        ),
    )
    return {
        "system": name,
        "window_start_hkt": WINDOW_START.isoformat(),
        "all_active_portfolio": metrics(bets),
        "since_window_start": metrics(recent),
        "by_market": segment(recent, market_name),
        "by_odds_tier": segment(
            recent,
            lambda bet: "賠率≥1.70"
            if (number(bet.get("odds")) or 0) >= 1.70
            else "賠率<1.70",
        ),
        "by_condition_sample": segment(
            recent,
            lambda bet: (
                "樣本10-19"
                if (number(bet.get("condition_decided")) or 0) < 20
                else "樣本20-29"
                if (number(bet.get("condition_decided")) or 0) < 30
                else "樣本≥30"
            ),
        ),
        "by_selection_role": segment(
            recent,
            lambda bet: str(bet.get("selected_role") or bet.get("label") or "未標示"),
        ),
        "multi_market_exposure": {
            "fixtures": len(fixtures),
            "fixtures_with_multiple_bets": sum(value > 1 for value in fixtures.values()),
            "maximum_bets_on_one_fixture": max(fixtures.values(), default=0),
        },
        "condition_groups_with_at_least_two_settled": ranked_conditions,
    }


def main() -> None:
    systems = {
        name: review(name, active_bets(path, portfolio))
        for name, (path, portfolio) in SOURCES.items()
    }
    print(
        json.dumps(
            {
                "read_only": True,
                "generated_at_hkt": datetime.now(HKT).isoformat(timespec="seconds"),
                "systems": systems,
            },
            ensure_ascii=False,
            sort_keys=True,
        )
    )


if __name__ == "__main__":
    main()
