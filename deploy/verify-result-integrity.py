#!/usr/bin/env python3
"""Fail production settlement when known history-integrity defects remain."""

from __future__ import annotations

import json
import math
from collections import Counter
from pathlib import Path
from typing import Any


FOOTBREAK_DATA = Path("/var/www/footbreak/data.json")
CROWN_HISTORY = Path("/var/lib/footbreak/crown/prediction_history.json")


def load(path: Path) -> Any:
    return json.loads(path.read_text(encoding="utf-8"))


def rows(payload: Any) -> list[dict[str, Any]]:
    if isinstance(payload, list):
        return payload
    if isinstance(payload, dict):
        value = payload.get("rows") or []
        return value if isinstance(value, list) else []
    return []


def assert_no_nan(label: str, history_rows: list[dict[str, Any]]) -> None:
    invalid: list[tuple[Any, ...]] = []
    for row in history_rows:
        for prediction in row.get("market_predictions") or []:
            raw = prediction.get("line")
            if raw is None:
                raw = prediction.get("condition")
            try:
                line = float(raw)
            except (TypeError, ValueError):
                line = float("nan")
            if not math.isfinite(line):
                invalid.append(
                    (
                        row.get("match_id"),
                        row.get("home"),
                        row.get("away"),
                        prediction.get("code"),
                        raw,
                    )
                )
    assert not invalid, f"{label} still contains NaN/invalid lines: {invalid[:20]}"
    print(f"{label} NaN check OK rows={len(history_rows)}")


def verify_known_crown_incident(crown_rows: list[dict[str, Any]]) -> None:
    incident = []
    for row in crown_rows:
        titan_id = str(row.get("titan_match_id") or row.get("match_id") or "")
        home = str(row.get("home") or "")
        away = str(row.get("away") or "")
        if titan_id == "3031468" or (
            ("中央" in home or "中央" in away)
            and ("南市" in home or "南市" in away)
        ):
            incident.append(row)

    assert incident, "Crown incident 3031468 / 中央骏马 v 南市台钢 is missing"
    for row in incident:
        assert row.get("result_status") == "已核對", row
        assert row.get("score") == "2-2", row
        corner_markets = [
            prediction
            for prediction in row.get("market_predictions") or []
            if prediction.get("code") == "CHL"
        ]
        if corner_markets:
            assert (row.get("result_detail") or {}).get("corners_total") == 10, row
    print(
        "Crown incident 3031468 OK "
        f"records={len(incident)} score=2-2 corners=10"
    )


def assert_market_stats_consistent(
    label: str,
    history_rows: list[dict[str, Any]],
    stats: dict[str, Any],
) -> None:
    direct: dict[str, Counter[str]] = {}
    by_stage: dict[str, dict[str, Counter[str]]] = {}
    for row in history_rows:
        stage = str(row.get("stage") or "")
        for grade in row.get("market_grades") or []:
            if not isinstance(grade, dict) or grade.get("grade_status") != "GRADED":
                continue
            code = str(grade.get("code") or "")
            if not code:
                continue
            direct.setdefault(code, Counter())["graded"] += 1
            by_stage.setdefault(stage, {}).setdefault(code, Counter())["graded"] += 1
            if grade.get("settlement") != "Refunded":
                direct[code]["decided"] += 1
                by_stage[stage][code]["decided"] += 1
                if grade.get("hit") is True:
                    direct[code]["hits"] += 1
                    by_stage[stage][code]["hits"] += 1

    reported = stats.get("by_market") or {}
    reported_stage = stats.get("by_stage_market") or {}
    for code, counts in direct.items():
        for key in ("graded", "decided", "hits"):
            assert int((reported.get(code) or {}).get(key, -1)) == counts[key], (
                label, code, key, reported.get(code), counts
            )
            stage_sum = sum(
                int(((markets or {}).get(code) or {}).get(key, 0))
                for markets in reported_stage.values()
            )
            assert stage_sum == counts[key], (
                label, code, key, "stage_sum", stage_sum, counts[key]
            )
    print(f"{label} market statistics OK markets={sorted(direct)}")


def main() -> None:
    footbreak = load(FOOTBREAK_DATA)
    crown = load(CROWN_HISTORY)
    footbreak_rows = rows(footbreak.get("prediction_history") or {})
    crown_rows = rows(crown)
    assert_no_nan("Footbreak", footbreak_rows)
    assert_no_nan("Crown", crown_rows)
    assert_market_stats_consistent(
        "Footbreak",
        footbreak_rows,
        (footbreak.get("prediction_history") or {}).get("stats") or {},
    )
    assert_market_stats_consistent("Crown", crown_rows, crown.get("stats") or {})
    verify_known_crown_incident(crown_rows)


if __name__ == "__main__":
    main()
