#!/usr/bin/env python3
"""Fail production settlement when known history-integrity defects remain."""

from __future__ import annotations

import json
import math
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
            assert (row.get("actual") or {}).get("corners_total") == 10, row
    print(
        "Crown incident 3031468 OK "
        f"records={len(incident)} score=2-2 corners=10"
    )


def main() -> None:
    footbreak = load(FOOTBREAK_DATA)
    crown = load(CROWN_HISTORY)
    footbreak_rows = rows(footbreak.get("prediction_history") or {})
    crown_rows = rows(crown)
    assert_no_nan("Footbreak", footbreak_rows)
    assert_no_nan("Crown", crown_rows)
    verify_known_crown_incident(crown_rows)


if __name__ == "__main__":
    main()
