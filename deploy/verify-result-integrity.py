#!/usr/bin/env python3
"""Fail production settlement when known history-integrity defects remain."""

from __future__ import annotations

import json
import math
from collections import Counter
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any


FOOTBREAK_DATA = Path("/var/www/footbreak/data.json")
CROWN_HISTORY = Path("/var/lib/footbreak/crown/prediction_history.json")
CROWN_DATA = Path("/var/www/crown/data.json")
STAGES = ("首預", "T-30", "T-5")
MARKETS = ("HDC", "HIL", "CHL")
HKT = timezone(timedelta(hours=8))


def load(path: Path) -> Any:
    return json.loads(path.read_text(encoding="utf-8"))


def rows(payload: Any) -> list[dict[str, Any]]:
    if isinstance(payload, list):
        return payload
    if isinstance(payload, dict):
        value = payload.get("rows") or []
        return value if isinstance(value, list) else []
    return []


def same_accuracy(actual: Any, expected: float | None) -> bool:
    if actual is None or expected is None:
        return actual is None and expected is None
    return math.isclose(float(actual), expected, rel_tol=1e-6)


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
    direct = {code: Counter() for code in MARKETS}
    by_stage = {
        stage: {code: Counter() for code in MARKETS}
        for stage in STAGES
    }
    for row in history_rows:
        stage = str(row.get("stage") or "")
        for grade in row.get("market_grades") or []:
            if not isinstance(grade, dict) or grade.get("grade_status") != "GRADED":
                continue
            code = str(grade.get("code") or "")
            if not code:
                continue
            if code not in direct or stage not in by_stage:
                continue
            direct[code]["graded"] += 1
            by_stage[stage][code]["graded"] += 1
            if grade.get("hit") is not None:
                direct[code]["decided"] += 1
                by_stage[stage][code]["decided"] += 1
                if grade.get("hit") is True:
                    direct[code]["hits"] += 1
                    by_stage[stage][code]["hits"] += 1

    reported = stats.get("by_market") or {}
    reported_stage = stats.get("by_stage_market") or {}
    for code in MARKETS:
        counts = direct[code]
        for key in ("graded", "decided", "hits"):
            assert int((reported.get(code) or {}).get(key, -1)) == counts[key], (
                label, code, key, reported.get(code), counts
            )
        expected_accuracy = (
            counts["hits"] / counts["decided"]
            if counts["decided"] else None
        )
        actual_accuracy = (reported.get(code) or {}).get("accuracy")
        assert same_accuracy(actual_accuracy, expected_accuracy), (
            label, code, "accuracy", actual_accuracy, expected_accuracy
        )

    for stage in STAGES:
        for code in MARKETS:
            counts = by_stage[stage][code]
            cell = (reported_stage.get(stage) or {}).get(code) or {}
            for key in ("graded", "decided", "hits"):
                assert int(cell.get(key, -1)) == counts[key], (
                    label, stage, code, key, cell, counts
                )
            expected_accuracy = (
                counts["hits"] / counts["decided"]
                if counts["decided"] else None
            )
            actual_accuracy = cell.get("accuracy")
            assert same_accuracy(actual_accuracy, expected_accuracy), (
                label, stage, code, "accuracy", actual_accuracy, expected_accuracy
            )
            pushes = counts["graded"] - counts["decided"]
            print(
                f"{label} {stage} {code} "
                f"hits={counts['hits']}/{counts['decided']} "
                f"graded={counts['graded']} pushes={pushes}"
            )
    print(f"{label} market statistics exact-cell check OK")


def _sort_key(row: dict[str, Any]) -> tuple[float, str, int]:
    raw = str(row.get("kickoff_hkt") or row.get("kickoff") or "").strip()
    try:
        kickoff = datetime.fromisoformat(raw.replace("Z", "+00:00")).timestamp()
    except (TypeError, ValueError):
        kickoff = float("-inf")
    return (
        kickoff,
        str(row.get("predicted_at") or ""),
        STAGES.index(str(row.get("stage"))) if str(row.get("stage")) in STAGES else -1,
    )


def assert_unique_and_sorted(label: str, history_rows: list[dict[str, Any]]) -> None:
    keys = [
        (str(row.get("match_id") or ""), str(row.get("stage") or ""))
        for row in history_rows
    ]
    missing = [key for key in keys if not all(key)]
    duplicates = [key for key, count in Counter(keys).items() if count > 1]
    assert not missing, f"{label} history rows missing match/stage keys: {missing[:20]}"
    assert not duplicates, f"{label} duplicate match/stage rows: {duplicates[:20]}"

    actual = [_sort_key(row) for row in history_rows]
    assert actual == sorted(actual, reverse=True), (
        f"{label} prediction history is not newest-kickoff-first"
    )
    print(f"{label} uniqueness/order check OK rows={len(history_rows)}")


def report_result_gaps(label: str, history_rows: list[dict[str, Any]]) -> None:
    cutoff = datetime.now(HKT) - timedelta(hours=4)
    overdue: list[tuple[str, str, str]] = []
    overdue_corners: list[tuple[str, str, str]] = []
    for row in history_rows:
        raw = str(row.get("kickoff_hkt") or row.get("kickoff") or "").strip()
        try:
            kickoff = datetime.fromisoformat(raw.replace("Z", "+00:00"))
            if kickoff.tzinfo is None:
                kickoff = kickoff.replace(tzinfo=HKT)
            kickoff = kickoff.astimezone(HKT)
        except (TypeError, ValueError):
            continue
        if kickoff > cutoff or row.get("result_status") in {"已核對", "不計"}:
            continue
        item = (
            str(row.get("match_id") or ""),
            str(row.get("home") or ""),
            str(row.get("away") or ""),
        )
        overdue.append(item)
        has_corner_prediction = any(
            isinstance(prediction, dict) and prediction.get("code") == "CHL"
            for prediction in (row.get("market_predictions") or [])
        )
        has_graded_corner = any(
            isinstance(grade, dict)
            and grade.get("code") == "CHL"
            and grade.get("grade_status") == "GRADED"
            for grade in (row.get("market_grades") or [])
        )
        if has_corner_prediction and not has_graded_corner:
            overdue_corners.append(item)
    print(
        f"{label} result-gap report overdue={len(overdue)} "
        f"overdue_corners={len(overdue_corners)}"
    )
    if overdue:
        print(f"{label} overdue sample={overdue[:20]}")
    if overdue_corners:
        print(f"{label} overdue corner sample={overdue_corners[:20]}")


def assert_crown_publication_matches(
    crown_history: dict[str, Any],
    crown_public: dict[str, Any],
) -> None:
    public_history = crown_public.get("prediction_history") or {}
    raw_rows = rows(crown_history)
    public_rows = rows(public_history)
    assert len(public_rows) == len(raw_rows), (
        "Crown public/raw prediction row count mismatch",
        len(public_rows),
        len(raw_rows),
    )
    assert public_history.get("stats") == crown_history.get("stats"), (
        "Crown public/raw prediction stats mismatch"
    )
    raw_keys = [
        (str(row.get("match_id") or ""), str(row.get("stage") or ""))
        for row in raw_rows
    ]
    public_keys = [
        (str(row.get("match_id") or ""), str(row.get("stage") or ""))
        for row in public_rows
    ]
    assert public_keys == raw_keys, "Crown public/raw prediction row order mismatch"
    print(f"Crown publication sync check OK rows={len(raw_rows)}")


def main() -> None:
    footbreak = load(FOOTBREAK_DATA)
    crown = load(CROWN_HISTORY)
    crown_public = load(CROWN_DATA)
    footbreak_rows = rows(footbreak.get("prediction_history") or {})
    crown_rows = rows(crown)
    assert_unique_and_sorted("Footbreak", footbreak_rows)
    assert_unique_and_sorted("Crown", crown_rows)
    assert_no_nan("Footbreak", footbreak_rows)
    assert_no_nan("Crown", crown_rows)
    assert_market_stats_consistent(
        "Footbreak",
        footbreak_rows,
        (footbreak.get("prediction_history") or {}).get("stats") or {},
    )
    assert_market_stats_consistent("Crown", crown_rows, crown.get("stats") or {})
    assert_crown_publication_matches(crown, crown_public)
    report_result_gaps("Footbreak", footbreak_rows)
    report_result_gaps("Crown", crown_rows)
    verify_known_crown_incident(crown_rows)


if __name__ == "__main__":
    main()
