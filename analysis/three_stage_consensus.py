"""Shared one-fixture statistics for predictions stable across all three stages."""
from __future__ import annotations

import math
import json
from typing import Any


STAGES = ("首預", "T-30", "T-5")
MARKETS = ("HDC", "HIL", "CHL")
MARKET_LABELS = {"HDC": "讓球", "HIL": "入球大細", "CHL": "角球大細"}
RANKING_MIN_DECIDED = 30
LOW_ODDS_THRESHOLD = 1.70

TRANSITION_CONDITIONS = (
    (
        "same_direction_line_moved",
        "同向改盤",
        "三段同一選擇，但數字盤口曾改動",
    ),
    (
        "first_missing_then_stable",
        "首預缺向後定",
        "首預冇有效方向；T-30 有方向，並維持到 T-5",
    ),
    (
        "flip_then_stable",
        "T-30 反向後定",
        "首預有方向；T-30 反向，並維持到 T-5",
    ),
)


def _line(grade: dict[str, Any]) -> float | None:
    raw = grade.get("line")
    if raw is None:
        raw = grade.get("condition")
    try:
        value = float(raw)
    except (TypeError, ValueError):
        return None
    return value if math.isfinite(value) else None


def _valid_odds(value: Any) -> float | None:
    try:
        odds = float(value)
    except (TypeError, ValueError):
        return None
    return odds if math.isfinite(odds) and odds > 1.0 else None


def _canonical_team(value: Any) -> str | None:
    text = "".join(str(value or "").split()).casefold()
    return text or None


def _direction_identity(grade: dict[str, Any], code: str) -> str | None:
    """Return a selection identity suitable for cross-stage comparisons.

    HDC is intentionally identified by the actual selected team.  A home/away
    token alone is not sufficient because source feeds can reverse team order
    between snapshots.  HIL and CHL have the two stable semantic choices,
    over and under.
    """
    side = str(grade.get("side") or "")
    if _line(grade) is None:
        return None
    if code == "HDC":
        if side not in {"H", "A"}:
            return None
        team = None
        for key in ("selected_team", "selection_team", "team"):
            team = _canonical_team(grade.get(key))
            if team:
                break
        if not team:
            team = _canonical_team(
                grade.get("_fixture_home") if side == "H" else grade.get("_fixture_away")
            )
        return f"team:{team}" if team else None
    if code in {"HIL", "CHL"} and side in {"H", "L"}:
        return "over" if side == "H" else "under"
    return None


def _selection_line(grade: dict[str, Any], code: str) -> float | None:
    """Return the numeric line from the selected side's perspective."""
    line = _line(grade)
    if line is None:
        return None
    if code == "HDC" and str(grade.get("side") or "") == "A":
        return -line
    return line


def _deterministic_grade(
    rows: list[tuple[dict[str, Any], dict[str, Any]]],
) -> dict[str, Any]:
    """Choose one duplicate stage row without relying on source input order."""
    def rank(item: tuple[dict[str, Any], dict[str, Any]]) -> tuple[str, str]:
        row, grade = item
        payload = {
            "row": row,
            "grade": grade,
        }
        return (
            str(row.get("predicted_at") or row.get("ts") or ""),
            json.dumps(payload, ensure_ascii=False, sort_keys=True, default=str),
        )

    row, grade = max(rows, key=rank)
    selected = dict(grade)
    selected["_fixture_home"] = row.get("home")
    selected["_fixture_away"] = row.get("away")
    return selected


def _group_market_stages(rows: list[dict[str, Any]]) -> dict[
    tuple[str, str], dict[str, dict[str, Any]]
]:
    """Build one deterministic grade for each fixture, market and stage."""
    candidates: dict[
        tuple[str, str, str], list[tuple[dict[str, Any], dict[str, Any]]]
    ] = {}
    for row in rows:
        match_id = str(row.get("match_id") or "")
        stage = str(row.get("stage") or "")
        if not match_id or stage not in STAGES:
            continue
        for grade in row.get("market_grades") or []:
            if not isinstance(grade, dict):
                continue
            code = str(grade.get("code") or "")
            if code in MARKETS:
                candidates.setdefault((match_id, code, stage), []).append((row, grade))

    grouped: dict[tuple[str, str], dict[str, dict[str, Any]]] = {}
    for (match_id, code, stage), duplicates in candidates.items():
        grouped.setdefault((match_id, code), {})[stage] = _deterministic_grade(duplicates)
    return grouped


def _metrics(
    samples: list[dict[str, dict[str, Any]]],
    stage: str,
) -> dict[str, Any]:
    grades = [
        sample[stage]
        for sample in samples
        if sample[stage].get("grade_status") == "GRADED"
        and sample[stage].get("hit") is not None
    ]
    hits = sum(grade.get("hit") is True for grade in grades)
    return {
        "fixtures": len(samples),
        "decided": len(grades),
        "hits": hits,
        "accuracy": round(hits / len(grades), 6) if grades else None,
    }


def _samples_by_odds(
    samples: list[dict[str, dict[str, Any]]],
    band: str,
    stage: str = "T-5",
) -> list[dict[str, dict[str, Any]]]:
    selected = []
    for sample in samples:
        try:
            odds = float(sample[stage].get("odds"))
        except (KeyError, TypeError, ValueError):
            continue
        if not math.isfinite(odds) or odds <= 1.0:
            continue
        if band == "low" and odds < LOW_ODDS_THRESHOLD:
            selected.append(sample)
        elif band == "eligible" and odds >= LOW_ODDS_THRESHOLD:
            selected.append(sample)
    return selected


def _priced_samples(
    samples: list[dict[str, dict[str, Any]]],
    stage: str = "T-5",
) -> list[dict[str, dict[str, Any]]]:
    """Return only samples with a valid selected price at the grading stage."""
    return (
        _samples_by_odds(samples, "low", stage)
        + _samples_by_odds(samples, "eligible", stage)
    )


def _odds_bias(
    samples: list[dict[str, dict[str, Any]]],
    stage: str = "T-5",
) -> dict[str, Any]:
    grades = [
        sample[stage]
        for sample in samples
        if sample[stage].get("grade_status") == "GRADED"
        and sample[stage].get("hit") is not None
    ]
    priced = []
    for grade in grades:
        try:
            odds = float(grade.get("odds"))
        except (TypeError, ValueError):
            continue
        if math.isfinite(odds) and odds > 1.0:
            priced.append((grade, odds))
    low = [item for item in priced if item[1] < LOW_ODDS_THRESHOLD]
    eligible = [item for item in priced if item[1] >= LOW_ODDS_THRESHOLD]

    def cohort(items: list[tuple[dict[str, Any], float]]) -> dict[str, Any]:
        hits = sum(grade.get("hit") is True for grade, _ in items)
        return {
            "decided": len(items),
            "hits": hits,
            "accuracy": round(hits / len(items), 6) if items else None,
            "average_odds": (
                round(sum(odds for _, odds in items) / len(items), 3)
                if items else None
            ),
        }

    low_metrics = cohort(low)
    eligible_metrics = cohort(eligible)
    return {
        "threshold": LOW_ODDS_THRESHOLD,
        "decided": len(grades),
        "priced_decided": len(priced),
        "missing_odds": len(grades) - len(priced),
        "average_odds": (
            round(sum(odds for _, odds in priced) / len(priced), 3)
            if priced else None
        ),
        "low_odds": {
            **low_metrics,
            "share": round(len(low) / len(priced), 6) if priced else None,
        },
        "at_or_above_threshold": eligible_metrics,
    }


def _breakdown(
    samples: list[dict[str, dict[str, Any]]],
    code: str,
) -> list[dict[str, Any]]:
    """Split exact three-stage samples by the selected T-5 market role."""
    if code == "HDC":
        definitions = (
            ("home_giving", "主讓"),
            ("home_receiving", "主受讓"),
            ("scratch_home", "平手盤（主）"),
            ("scratch_away", "平手盤（客）"),
            ("away_giving", "客讓"),
            ("away_receiving", "客受讓"),
        )

        def category(sample: dict[str, dict[str, Any]]) -> str | None:
            grade = sample["T-5"]
            side = str(grade.get("side") or "")
            line = _line(grade)
            if line is None or side not in {"H", "A"}:
                return None
            if abs(line) < 1e-9:
                return "scratch_home" if side == "H" else "scratch_away"
            if side == "H":
                return "home_giving" if line < 0 else "home_receiving"
            return "away_giving" if line > 0 else "away_receiving"
    else:
        definitions = (("over", "大"), ("under", "細"))

        def category(sample: dict[str, dict[str, Any]]) -> str | None:
            side = str(sample["T-5"].get("side") or "")
            return {"H": "over", "L": "under"}.get(side)

    output = []
    for key, label in definitions:
        subset = [sample for sample in samples if category(sample) == key]
        eligible = _samples_by_odds(subset, "eligible")
        output.append({
            "key": key,
            "label": label,
            **_metrics(eligible, "T-5"),
            "all_fixtures": len(subset),
            "odds_bias": _odds_bias(subset),
        })
    return output


def _transition_metrics(samples: list[dict[str, dict[str, Any]]]) -> dict[str, Any]:
    """Metrics for valid-priced, T-5-settled transition samples.

    ``fixtures`` retains graded pushes for auditability, while ``decided`` is
    deliberately limited to won/lost outcomes.  Callers pre-filter invalid
    prices, so no missing or invalid odds can enter this public aggregate.
    """
    t5 = [sample["T-5"] for sample in samples]
    decided = [grade for grade in t5 if grade.get("hit") is not None]
    hits = sum(grade.get("hit") is True for grade in decided)
    return {
        "fixtures": len(t5),
        "settled": len(t5),
        "pushes": len(t5) - len(decided),
        "decided": len(decided),
        "hits": hits,
        "accuracy": round(hits / len(decided), 6) if decided else None,
    }


def _transition_tiers(
    samples: list[dict[str, dict[str, Any]]],
) -> dict[str, dict[str, Any]]:
    return {
        "at_or_above_1_70": _transition_metrics([
            sample for sample in samples
            if (_valid_odds(sample["T-5"].get("odds")) or 0) >= LOW_ODDS_THRESHOLD
        ]),
        "below_1_70": _transition_metrics([
            sample for sample in samples
            if (
                (odds := _valid_odds(sample["T-5"].get("odds"))) is not None
                and odds < LOW_ODDS_THRESHOLD
            )
        ]),
    }


def _transition_category(
    sample: dict[str, dict[str, Any]],
    code: str,
) -> str | None:
    grade = sample["T-5"]
    side = str(grade.get("side") or "")
    if code == "HDC":
        line = _line(grade)
        if line is None or side not in {"H", "A"}:
            return None
        if abs(line) < 1e-9:
            return "scratch_home" if side == "H" else "scratch_away"
        if side == "H":
            return "home_giving" if line < 0 else "home_receiving"
        return "away_giving" if line > 0 else "away_receiving"
    return {"H": "over", "L": "under"}.get(side)


def _transition_breakdown(
    samples: list[dict[str, dict[str, Any]]],
    code: str,
) -> list[dict[str, Any]]:
    definitions = (
        (
            ("home_giving", "主讓"),
            ("home_receiving", "主受讓"),
            ("scratch_home", "平手盤（主）"),
            ("scratch_away", "平手盤（客）"),
            ("away_giving", "客讓"),
            ("away_receiving", "客受讓"),
        )
        if code == "HDC"
        else (("over", "大" if code == "HIL" else "角球大"),
              ("under", "細" if code == "HIL" else "角球細"))
    )
    return [
        {
            "key": key,
            "label": label,
            "tiers": _transition_tiers([
                sample for sample in samples
                if _transition_category(sample, code) == key
            ]),
        }
        for key, label in definitions
    ]


def _eligible_transition_sample(stage_grades: dict[str, dict[str, Any]]) -> bool:
    """A public transition requires a valid T-5 price and settled result."""
    t5 = stage_grades.get("T-5")
    return bool(
        t5
        and t5.get("grade_status") == "GRADED"
        and _valid_odds(t5.get("odds")) is not None
    )


def _transition_matches(
    stage_grades: dict[str, dict[str, Any]],
    code: str,
) -> set[str]:
    """Return the transition condition keys met by one fixture/market."""
    first = stage_grades.get("首預")
    t30 = stage_grades.get("T-30")
    t5 = stage_grades.get("T-5")
    first_direction = _direction_identity(first, code) if first else None
    t30_direction = _direction_identity(t30, code) if t30 else None
    t5_direction = _direction_identity(t5, code) if t5 else None
    matched: set[str] = set()

    if first_direction and t30_direction and t5_direction:
        if first_direction == t30_direction == t5_direction:
            lines = [
                _selection_line(first, code),
                _selection_line(t30, code),
                _selection_line(t5, code),
            ]
            if len(set(lines)) > 1:
                matched.add("same_direction_line_moved")
        if t30_direction != first_direction and t5_direction == t30_direction:
            matched.add("flip_then_stable")
    if not first_direction and t30_direction and t5_direction == t30_direction:
        matched.add("first_missing_then_stable")
    return matched


def calculate_three_stage_transitions(rows: list[dict[str, Any]]) -> dict[str, Any]:
    """Aggregate the three auditable three-stage transition conditions.

    This report is intentionally separate from exact-consensus reporting.
    One deterministic fixture+market grade is retained per stage; public
    counts then accept only valid selected T-5 decimal odds and a settled T-5
    market result.  A push is retained in its odds tier but never in a hit-rate
    denominator.
    """
    grouped = _group_market_stages(rows)
    by_condition: dict[str, dict[str, Any]] = {}
    for condition_key, label, definition in TRANSITION_CONDITIONS:
        market_data: dict[str, Any] = {}
        for code in MARKETS:
            samples = [
                stages
                for (_, grouped_code), stages in grouped.items()
                if grouped_code == code
                and _eligible_transition_sample(stages)
                and condition_key in _transition_matches(stages, code)
            ]
            market_data[code] = {
                "aggregate": {"tiers": _transition_tiers(samples)},
                "breakdown": _transition_breakdown(samples, code),
            }
        by_condition[condition_key] = {
            "label": label,
            "definition": definition,
            "markets": market_data,
        }
    return {
        "primary_unit": "one deterministic fixture per market, settled and graded at T-5",
        "t5_selected_odds": "valid decimal selected odds only; >= 1.70 and < 1.70 are separate",
        "missing_or_invalid_odds_excluded": True,
        "pushes_excluded_from_decided": True,
        "conditions": by_condition,
    }


def calculate_three_stage_consensus(
    rows: list[dict[str, Any]],
) -> dict[str, Any]:
    """Return direction-stable and exact-line-stable results by market.

    A fixture qualifies only when the same market has a prediction at 首預,
    T-30 and T-5 and all three selected sides are identical.  The primary
    metric grades one fixture once at its T-5 line.  Pushes and unavailable
    grades are retained in the fixture count but excluded from the accuracy
    denominator.
    """
    grouped = _group_market_stages(rows)

    markets: dict[str, Any] = {}
    for code in MARKETS:
        same_direction: list[dict[str, dict[str, Any]]] = []
        same_direction_and_line: list[dict[str, dict[str, Any]]] = []
        for (_, grouped_code), stage_grades in grouped.items():
            if grouped_code != code or any(stage not in stage_grades for stage in STAGES):
                continue
            sides = {str(stage_grades[stage].get("side") or "") for stage in STAGES}
            if len(sides) != 1 or "" in sides:
                continue
            same_direction.append(stage_grades)
            lines = [_line(stage_grades[stage]) for stage in STAGES]
            if all(line is not None for line in lines) and len(set(lines)) == 1:
                same_direction_and_line.append(stage_grades)

        markets[code] = {
            "same_direction": {
                "fixtures": len(_priced_samples(same_direction)),
                "excluded_missing_odds": (
                    len(same_direction) - len(_priced_samples(same_direction))
                ),
                "line_changed_fixtures": (
                    len(_priced_samples(same_direction))
                    - len(_priced_samples(same_direction_and_line))
                ),
                "primary": _metrics(
                    _samples_by_odds(same_direction, "eligible"),
                    "T-5",
                ),
                "odds_segments": _odds_bias(same_direction),
                "stage_diagnostics": {
                    stage: _metrics(_priced_samples(same_direction), stage)
                    for stage in STAGES
                },
            },
            "same_direction_and_line": {
                "fixtures": len(_priced_samples(same_direction_and_line)),
                "excluded_missing_odds": (
                    len(same_direction_and_line)
                    - len(_priced_samples(same_direction_and_line))
                ),
                "primary": _metrics(
                    _samples_by_odds(same_direction_and_line, "eligible"),
                    "T-5",
                ),
                "odds_segments": _odds_bias(same_direction_and_line),
                "breakdown": _breakdown(same_direction_and_line, code),
                "stage_diagnostics": {
                    stage: _metrics(_priced_samples(same_direction_and_line), stage)
                    for stage in STAGES
                },
            },
        }
    ranking_candidates = []
    for code in MARKETS:
        for item in markets[code]["same_direction_and_line"]["breakdown"]:
            if item["accuracy"] is None:
                continue
            ranking_candidates.append({
                "market": code,
                "market_label": MARKET_LABELS[code],
                "condition_key": item["key"],
                "condition_label": item["label"],
                "fixtures": item["fixtures"],
                "all_fixtures": item["all_fixtures"],
                "decided": item["decided"],
                "hits": item["hits"],
                "accuracy": item["accuracy"],
                "sample_qualified": item["decided"] >= RANKING_MIN_DECIDED,
                "odds_bias": item["odds_bias"],
            })
    ranking_candidates.sort(key=lambda item: (
        -int(item["sample_qualified"]),
        -item["accuracy"],
        -item["decided"],
        MARKETS.index(item["market"]),
        item["condition_key"],
    ))

    return {
        "definition": "same selection side at 首預, T-30 and T-5",
        "primary_unit": "one unique fixture per market, graded at T-5 line",
        "primary_odds_scope": "T-5 selected odds >= 1.70",
        "low_odds_scope": "T-5 selected odds < 1.70, reported separately",
        "missing_odds_excluded": True,
        "pushes_excluded": True,
        "ranking": {
            "scope": "same direction and exact line at all three stages",
            "odds_scope": "T-5 selected odds >= 1.70 only",
            "minimum_decided": RANKING_MIN_DECIDED,
            "priority": "qualified sample first, then accuracy, then decided sample",
            "top": ranking_candidates[:3],
            "candidate_count": len(ranking_candidates),
        },
        "markets": markets,
    }
