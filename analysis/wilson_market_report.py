"""Aggregate native T-5 market grades into a privacy-safe Wilson report."""
from __future__ import annotations

import argparse
import json
import math
from collections import defaultdict
from pathlib import Path
from typing import Any, Iterable

Z_95 = 1.959963984540054
MARKETS = ("HDC", "HIL", "CHL")
EXCLUSION_FLAGS = (
    "backfill",
    "post_hoc",
    "recovered",
    "recovery_mode",
    "settlement_excluded",
    "exclude_from_settlement",
)


def wilson_lower(hits: int, decided: int) -> float | None:
    if decided <= 0 or hits < 0 or hits > decided:
        return None
    p = hits / decided
    z2 = Z_95 * Z_95
    return (
        p
        + z2 / (2 * decided)
        - Z_95
        * math.sqrt(
            p * (1 - p) / decided + z2 / (4 * decided * decided)
        )
    ) / (1 + z2 / decided)


def _excluded(row: dict[str, Any]) -> bool:
    for key in EXCLUSION_FLAGS:
        value = row.get(key)
        if value not in (None, False, "", 0, "native"):
            return True
    return str(row.get("prediction_origin") or "native").lower() not in {
        "",
        "native",
        "live",
    }


def _history_rows(payload: dict[str, Any], system: str) -> Iterable[dict[str, Any]]:
    if system == "footbreak":
        for match in payload.get("matches") or []:
            if not isinstance(match, dict) or _excluded(match):
                continue
            identity = str(
                match.get("match_id")
                or match.get("fixture_id")
                or match.get("id")
                or ""
            )
            for stage in match.get("stages") or []:
                if isinstance(stage, dict):
                    yield {**stage, "_fixture_identity": identity}
        return
    for row in payload.get("rows") or []:
        if isinstance(row, dict):
            identity = str(
                row.get("match_id")
                or row.get("fixture_id")
                or row.get("id")
                or ""
            )
            yield {**row, "_fixture_identity": identity}


def aggregate(payload: dict[str, Any], system: str) -> dict[str, Any]:
    observations: dict[tuple[str, str], list[bool | None]] = defaultdict(list)
    diagnostics = defaultdict(int)
    for row in _history_rows(payload, system):
        if _excluded(row) or str(row.get("stage") or "") != "T-5":
            diagnostics["non_native_or_non_t5_rows"] += 1
            continue
        identity = str(row.get("_fixture_identity") or "")
        if not identity:
            diagnostics["missing_fixture_identity"] += 1
            continue
        grades = row.get("market_grades") or []
        if not isinstance(grades, list):
            diagnostics["malformed_market_grades"] += 1
            continue
        for grade in grades:
            if not isinstance(grade, dict) or _excluded(grade):
                diagnostics["excluded_grade"] += 1
                continue
            market = str(grade.get("code") or "").upper()
            if market not in MARKETS:
                diagnostics["unsupported_market"] += 1
                continue
            if grade.get("grade_status") != "GRADED":
                diagnostics["not_graded"] += 1
                continue
            settlement = str(grade.get("settlement") or "")
            hit = grade.get("hit")
            if settlement == "Refunded" or hit is None:
                observations[(identity, market)].append(None)
            elif isinstance(hit, bool):
                observations[(identity, market)].append(hit)
            else:
                diagnostics["invalid_hit"] += 1

    by_market: dict[str, dict[str, Any]] = {}
    conflicts = defaultdict(int)
    deduped: dict[str, list[bool | None]] = defaultdict(list)
    for (_, market), values in observations.items():
        decided_values = {value for value in values if value is not None}
        if len(decided_values) > 1:
            conflicts[market] += 1
            continue
        deduped[market].append(
            next(iter(decided_values)) if decided_values else None
        )

    for market in MARKETS:
        values = deduped.get(market, [])
        hits = sum(value is True for value in values)
        losses = sum(value is False for value in values)
        refunded = sum(value is None for value in values)
        decided = hits + losses
        lower = wilson_lower(hits, decided)
        by_market[market] = {
            "unique_fixture_markets": len(values),
            "hits": hits,
            "losses": losses,
            "refunded": refunded,
            "decided": decided,
            "hit_rate_pct": round(hits / decided * 100, 2) if decided else None,
            "wilson_95_lower_pct": round(lower * 100, 2)
            if lower is not None
            else None,
            "conflicting_fixture_markets_excluded": conflicts.get(market, 0),
        }
    return {
        "schema_version": "wilson-market-report-v1",
        "system": system,
        "scope": "native_prospective_t5_graded_unique_fixture_market",
        "markets": by_market,
        "diagnostics": dict(sorted(diagnostics.items())),
    }


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--footbreak", type=Path, required=True)
    parser.add_argument("--crown", type=Path, required=True)
    args = parser.parse_args()
    reports = []
    for system, path in (("footbreak", args.footbreak), ("crown", args.crown)):
        payload = json.loads(path.read_text(encoding="utf-8"))
        reports.append(aggregate(payload, system))
    print(json.dumps({"reports": reports}, ensure_ascii=False, sort_keys=True))


if __name__ == "__main__":
    main()
