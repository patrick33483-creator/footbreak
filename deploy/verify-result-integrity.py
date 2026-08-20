#!/usr/bin/env python3
"""Fail production settlement when known history-integrity defects remain."""

from __future__ import annotations

import json
import math
import copy
from collections import Counter
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any


FOOTBREAK_DATA = Path("/var/www/footbreak/data.json")
FOOTBREAK_PUBLIC_HISTORY = Path("/var/www/footbreak/history.json")
CROWN_HISTORY = Path("/var/lib/footbreak/crown/prediction_history.json")
CROWN_DATA = Path("/var/www/crown/data.json")
STAGES = ("首預", "T-30", "T-5")
MARKETS = ("HDC", "HIL", "CHL")
HKT = timezone(timedelta(hours=8))


def load(path: Path) -> Any:
    return json.loads(path.read_text(encoding="utf-8"))


def published_history(
    label: str,
    public: dict[str, Any],
    sidecar: dict[str, Any],
    expected_schema: str,
) -> dict[str, Any]:
    assert public.get("history_data_url") == "history.json", (
        f"{label} boot payload is missing the history sidecar marker"
    )
    assert sidecar.get("schema_version") == expected_schema, (
        f"{label} history sidecar schema mismatch"
    )
    expected_version = public.get("history_data_version")
    assert expected_version, f"{label} boot payload history version missing"
    assert expected_version == sidecar.get("history_data_version"), (
        f"{label} boot/history sidecar version mismatch"
    )
    history = sidecar.get("prediction_history")
    assert isinstance(history, dict), f"{label} history sidecar payload missing"
    assert isinstance(history.get("rows"), list), f"{label} history sidecar rows missing"
    return history


def sidecar_path(public_path: Path, payload: dict[str, Any], label: str) -> Path:
    """Resolve a dashboard history sidecar without escaping its web root."""
    data_url = str(payload.get("history_data_url") or "").strip()
    assert data_url and data_url.endswith(".json"), (
        f"{label} boot payload is missing the history sidecar marker"
    )
    path = (public_path.parent / data_url).resolve()
    assert path.parent == public_path.parent.resolve(), (
        f"{label} history sidecar must be a sibling file"
    )
    return path


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
    # Dashboard statistics are serialized to six decimal places. Accept only
    # the maximum half-unit rounding distance while keeping count checks exact.
    return math.isclose(float(actual), expected, rel_tol=1e-6, abs_tol=5e-7)


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
    scope = stats.get("scope") or {}
    model_version = str(scope.get("model_version") or "").strip()
    if model_version:
        history_rows = [
            row for row in history_rows
            if str(row.get("prediction_era") or row.get("model_version") or "")
            == model_version
        ]

    def audit_cell(cell: dict[str, Any], context: tuple[str, ...]) -> dict[str, Any]:
        all_odds = cell.get("all_odds")
        if not isinstance(all_odds, dict):
            return cell

        groups = cell.get("odds_groups")
        assert isinstance(groups, dict), (*context, "missing odds_groups")
        expected_groups = ("at_or_above_1_70", "below_1_70")
        for key in expected_groups:
            assert isinstance(groups.get(key), dict), (*context, "missing odds group", key)

        for count_key in ("graded", "decided", "hits", "pushes"):
            grouped_total = sum(
                int((groups.get(group_key) or {}).get(count_key, 0))
                for group_key in expected_groups
            )
            assert grouped_total == int(all_odds.get(count_key, 0)), (
                *context,
                count_key,
                "odds group total mismatch",
                grouped_total,
                all_odds,
            )
        excluded_missing = cell.get("excluded_missing_odds")
        assert isinstance(excluded_missing, int) and excluded_missing >= 0, (
            *context,
            "invalid excluded_missing_odds",
            excluded_missing,
        )

        for group_key in expected_groups:
            group = groups[group_key]
            decided = int(group.get("decided", 0))
            hits = int(group.get("hits", 0))
            expected_accuracy = hits / decided if decided else None
            assert same_accuracy(group.get("accuracy"), expected_accuracy), (
                *context,
                group_key,
                "accuracy",
                group.get("accuracy"),
                expected_accuracy,
            )
        return all_odds

    direct = {code: Counter() for code in MARKETS}
    direct_missing = Counter()
    by_stage = {
        stage: {code: Counter() for code in MARKETS}
        for stage in STAGES
    }
    by_stage_missing = {
        stage: Counter() for stage in STAGES
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
            try:
                odds = float(grade.get("odds"))
            except (TypeError, ValueError):
                odds = float("nan")
            if not math.isfinite(odds) or odds <= 1.0:
                direct_missing[code] += 1
                by_stage_missing[stage][code] += 1
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
        cell = audit_cell(reported.get(code) or {}, (label, code))
        assert int((reported.get(code) or {}).get("excluded_missing_odds", -1)) == direct_missing[code], (
            label, code, "excluded_missing_odds",
            (reported.get(code) or {}).get("excluded_missing_odds"),
            direct_missing[code],
        )
        for key in ("graded", "decided", "hits"):
            assert int(cell.get(key, -1)) == counts[key], (
                label, code, key, cell, counts
            )
        expected_accuracy = (
            counts["hits"] / counts["decided"]
            if counts["decided"] else None
        )
        actual_accuracy = cell.get("accuracy")
        assert same_accuracy(actual_accuracy, expected_accuracy), (
            label, code, "accuracy", actual_accuracy, expected_accuracy
        )

    for stage in STAGES:
        for code in MARKETS:
            counts = by_stage[stage][code]
            cell = audit_cell(
                (reported_stage.get(stage) or {}).get(code) or {},
                (label, stage, code),
            )
            raw_cell = (reported_stage.get(stage) or {}).get(code) or {}
            assert int(raw_cell.get("excluded_missing_odds", -1)) == by_stage_missing[stage][code], (
                label, stage, code, "excluded_missing_odds",
                raw_cell.get("excluded_missing_odds"),
                by_stage_missing[stage][code],
            )
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
    crown_public_history: dict[str, Any] | None = None,
) -> None:
    from analysis.odds_recovery import overlay_rows
    from crown.ledger import PREDICTION_ERA
    from crown.prediction_history import (
        calculate_stats,
        normalize_history,
        project_watch_rows,
    )

    # Keep the verifier callable against archived inline-history snapshots,
    # while requiring the sidecar contract for the live publication path.
    if crown_public_history is None:
        public_history = crown_public.get("prediction_history") or {}
    else:
        public_history = crown_public_history.get("prediction_history") or {}
        assert str(crown_public.get("history_data_url") or "").endswith(".json"), (
            "Crown boot payload is missing the history sidecar marker"
        )
        assert crown_public.get("history_data_version") == crown_public_history.get(
            "history_data_version"
        ), "Crown boot/history sidecar version mismatch"
    projected_history = copy.deepcopy(crown_history)
    normalize_history(projected_history)
    raw_rows = rows(projected_history)
    public_ledger = crown_public.get("ledger") or {}
    projected_rows = overlay_rows(
        project_watch_rows(raw_rows, public_ledger), "crown",
    )
    projected_stats = calculate_stats(
        projected_rows,
        comparable_era=PREDICTION_ERA,
    )
    public_rows = rows(public_history)
    assert len(public_rows) == len(projected_rows), (
        "Crown public/projected prediction row count mismatch",
        len(public_rows),
        len(projected_rows),
    )
    assert public_rows == projected_rows, (
        "Crown public prediction rows do not match the recovery overlay projection"
    )
    assert public_history.get("stats") == projected_stats, (
        "Crown public prediction stats do not match the recovery overlay projection"
    )
    projected_keys = [
        (str(row.get("match_id") or ""), str(row.get("stage") or ""))
        for row in projected_rows
    ]
    public_keys = [
        (str(row.get("match_id") or ""), str(row.get("stage") or ""))
        for row in public_rows
    ]
    assert public_keys == projected_keys, (
        "Crown public/projected prediction row order mismatch"
    )
    print(
        "Crown publication recovery-projection sync check OK "
        f"rows={len(projected_rows)}"
    )


def run_integrity_check(name: str, check: Any, *args: Any) -> None:
    """Run one verifier section while exposing only a stable failure category."""
    try:
        check(*args)
    except AssertionError:
        raise AssertionError(f"integrity_check={name}") from None


def main() -> None:
    footbreak = load(FOOTBREAK_DATA)
    footbreak_public_history = load(FOOTBREAK_PUBLIC_HISTORY)
    crown = load(CROWN_HISTORY)
    crown_public = load(CROWN_DATA)
    crown_public_history = load(sidecar_path(CROWN_DATA, crown_public, "Crown"))
    footbreak_history = published_history(
        "Footbreak",
        footbreak,
        footbreak_public_history,
        "footbreak-history-v1",
    )
    footbreak_rows = rows(footbreak_history)
    crown_rows = rows(crown)
    run_integrity_check(
        "footbreak_history_shape",
        assert_unique_and_sorted,
        "Footbreak",
        footbreak_rows,
    )
    run_integrity_check(
        "crown_history_shape",
        assert_unique_and_sorted,
        "Crown",
        crown_rows,
    )
    run_integrity_check("footbreak_finite_values", assert_no_nan, "Footbreak", footbreak_rows)
    run_integrity_check("crown_finite_values", assert_no_nan, "Crown", crown_rows)
    run_integrity_check(
        "footbreak_market_stats",
        assert_market_stats_consistent,
        "Footbreak",
        footbreak_rows,
        footbreak_history.get("stats") or {},
    )
    run_integrity_check(
        "crown_market_stats",
        assert_market_stats_consistent,
        "Crown",
        crown_rows,
        crown.get("stats") or {},
    )
    run_integrity_check(
        "crown_publication_projection",
        assert_crown_publication_matches,
        crown,
        crown_public,
        crown_public_history,
    )
    report_result_gaps("Footbreak", footbreak_rows)
    report_result_gaps("Crown", crown_rows)
    verify_known_crown_incident(crown_rows)


if __name__ == "__main__":
    main()
