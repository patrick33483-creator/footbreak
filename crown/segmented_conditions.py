"""Read-only Crown V2 S/A condition projection.

The rules in this module are frozen display/measurement rules.  They never
change a model prediction, create a bet, or modify the prediction history.
Historical figures are fixed research baselines; prospective figures count
only fixtures kicking off on or after the activation cutoff.
"""
from __future__ import annotations

from collections import Counter
from datetime import datetime
from typing import Any, Callable

from .common import HKT, parse_time


ACTIVATION_CUTOFF = "2026-08-31T00:00:00+08:00"
PUBLIC_TIERS = {"S", "A"}
_STAGE_ORDER = ("首預", "T-30", "T-5")
_SETTLEMENT_LABEL = {
    "Won": "全贏",
    "Half Won": "半贏",
    "Refunded": "走水",
    "Half Lost": "半輸",
    "Lost": "全輸",
}
_SETTLEMENT_PNL = {
    "Half Lost": -0.5,
    "Lost": -1.0,
    "Refunded": 0.0,
}


def _selected_line(prediction: dict[str, Any]) -> float | None:
    raw = prediction.get("line")
    if raw is None:
        raw = prediction.get("condition")
    try:
        line = float(raw)
    except (TypeError, ValueError):
        return None
    if str(prediction.get("code") or "").upper() == "HDC" and str(
        prediction.get("side") or ""
    ).upper() == "A":
        return -line
    return line


def _prediction(row: dict[str, Any], code: str) -> dict[str, Any] | None:
    matches = [
        item
        for item in (row.get("market_predictions") or [])
        if isinstance(item, dict) and str(item.get("code") or "").upper() == code
    ]
    return matches[0] if len(matches) == 1 else None


def _grade(row: dict[str, Any], prediction: dict[str, Any]) -> dict[str, Any] | None:
    code = str(prediction.get("code") or "").upper()
    side = str(prediction.get("side") or "").upper()
    line = _selected_line(prediction)
    matches = []
    for item in row.get("market_grades") or []:
        if not isinstance(item, dict) or item.get("grade_status") != "GRADED":
            continue
        if str(item.get("code") or "").upper() != code:
            continue
        if str(item.get("side") or "").upper() != side:
            continue
        item_line = _selected_line(item)
        if line is not None and item_line is not None and abs(line - item_line) > 1e-9:
            continue
        matches.append(item)
    return matches[0] if len(matches) == 1 else None


def _odds(prediction: dict[str, Any]) -> float | None:
    try:
        value = float(prediction.get("odds"))
    except (TypeError, ValueError):
        return None
    return value if value > 1 else None


def _same_line(values: list[float | None]) -> bool:
    return bool(values) and all(
        value is not None and abs(value - values[0]) < 1e-9 for value in values
    )


def _rule_s_t5_over(stages: dict[str, dict[str, Any]]) -> dict[str, Any] | None:
    row = stages.get("T-5")
    prediction = _prediction(row or {}, "HIL")
    if not prediction or str(prediction.get("side") or "").upper() != "H":
        return None
    odds = _odds(prediction)
    return {"decision_stage": "T-5", "prediction": prediction} if odds and odds > 1.85 else None


def _rule_a_open_t5_over(stages: dict[str, dict[str, Any]]) -> dict[str, Any] | None:
    opening = _prediction(stages.get("首預") or {}, "HIL")
    t5 = _prediction(stages.get("T-5") or {}, "HIL")
    if not opening or not t5:
        return None
    if any(str(item.get("side") or "").upper() != "H" for item in (opening, t5)):
        return None
    if any((_odds(item) or 0) <= 1.80 for item in (opening, t5)):
        return None
    return {"decision_stage": "T-5", "prediction": t5}


def _rule_a_open_away_minus_half(stages: dict[str, dict[str, Any]]) -> dict[str, Any] | None:
    prediction = _prediction(stages.get("首預") or {}, "HDC")
    if not prediction or str(prediction.get("side") or "").upper() != "A":
        return None
    line = _selected_line(prediction)
    if line is None or abs(line - (-0.5)) > 1e-9:
        return None
    return {"decision_stage": "首預", "prediction": prediction}


def _rule_a_three_home_same_line(stages: dict[str, dict[str, Any]]) -> dict[str, Any] | None:
    predictions = [_prediction(stages.get(stage) or {}, "HDC") for stage in _STAGE_ORDER]
    if any(item is None for item in predictions):
        return None
    typed = [item for item in predictions if item is not None]
    if any(str(item.get("side") or "").upper() != "H" for item in typed):
        return None
    if not _same_line([_selected_line(item) for item in typed]):
        return None
    return {"decision_stage": "T-5", "prediction": typed[-1]}


RuleMatcher = Callable[[dict[str, dict[str, Any]]], dict[str, Any] | None]

CONDITIONS: tuple[dict[str, Any], ...] = (
    {
        "id": "S-HIL-T5-OVER-185",
        "tier": "S",
        "market": "HIL",
        "title": "T-5 預測大；賠率 > 1.85",
        "definition": "只睇 T-5 入球大細預測方向；預測為大，而且所選方向賠率高於 1.85。",
        "path_label": "T-5：大",
        "historical": {
            "sample": 94, "full_win": 52, "half_win": 6, "push": 7,
            "half_loss": 2, "full_loss": 27, "hit_rate": 0.6667, "roi": 0.2284,
        },
        "matcher": _rule_s_t5_over,
    },
    {
        "id": "A-HIL-OPEN-T5-OVER-180",
        "tier": "A",
        "market": "HIL",
        "title": "初盤預測大 → T-5 大；兩段賠率均 > 1.80",
        "definition": "初盤預測及 T-5 入球大細都預測大，而且兩段所選方向賠率都高於 1.80。",
        "path_label": "初盤預測：大 → T-5：大",
        "historical": {
            "sample": 47, "full_win": 29, "half_win": 2, "push": 1,
            "half_loss": 1, "full_loss": 14, "hit_rate": 0.6739, "roi": 0.2410,
        },
        "matcher": _rule_a_open_t5_over,
    },
    {
        "id": "A-HDC-OPEN-AWAY-MINUS-050",
        "tier": "A",
        "market": "HDC",
        "title": "初盤預測客；被預測客隊中位線 -0.5",
        "definition": "只睇初盤讓球預測；方向為客，而且以被預測客隊角度計算的中位線為 -0.5，無賠率門檻。",
        "path_label": "初盤預測：客",
        "historical": {
            "sample": 45, "full_win": 31, "half_win": 0, "push": 0,
            "half_loss": 0, "full_loss": 14, "hit_rate": 0.6889, "roi": 0.2696,
        },
        "matcher": _rule_a_open_away_minus_half,
    },
    {
        "id": "A-HDC-HHH-SAME-LINE",
        "tier": "B",
        "market": "HDC",
        "title": "初盤預測主 → T-30 主 → T-5 主；三段中位線相同",
        "definition": "三段讓球預測方向全部為主，而且以被預測一方角度計算的三段中位線完全相同，無賠率門檻。",
        "path_label": "初盤預測：主 → T-30：主 → T-5：主",
        "historical": {
            "sample": 186, "full_win": 92, "half_win": 14, "push": 10,
            "half_loss": 11, "full_loss": 59, "hit_rate": 0.6023, "roi": 0.1004,
        },
        "matcher": _rule_a_three_home_same_line,
    },
)


def _prospective_metrics(observations: list[dict[str, Any]]) -> dict[str, Any]:
    settlements = Counter(
        str(item.get("settlement"))
        for item in observations
        if item.get("settlement") in _SETTLEMENT_LABEL
    )
    decided = sum(settlements.values())
    hit_denominator = decided - settlements["Refunded"]
    hits = settlements["Won"] + settlements["Half Won"]
    profit = 0.0
    priced = 0
    for item in observations:
        settlement = item.get("settlement")
        odds = item.get("odds")
        if settlement not in _SETTLEMENT_LABEL or odds is None:
            continue
        priced += 1
        if settlement == "Won":
            profit += odds - 1
        elif settlement == "Half Won":
            profit += (odds - 1) / 2
        else:
            profit += _SETTLEMENT_PNL.get(settlement, 0.0)
    return {
        "qualified": len(observations),
        "settled": decided,
        "pending": len(observations) - decided,
        "full_win": settlements["Won"],
        "half_win": settlements["Half Won"],
        "push": settlements["Refunded"],
        "half_loss": settlements["Half Lost"],
        "full_loss": settlements["Lost"],
        "hit_rate": round(hits / hit_denominator, 6) if hit_denominator else None,
        "roi": round(profit / priced, 6) if priced else None,
        "roi_priced": priced,
    }


def build_segmented_conditions(
    rows: list[dict[str, Any]],
    *,
    activation_cutoff: str = ACTIVATION_CUTOFF,
    generated_at: str | None = None,
) -> dict[str, Any]:
    cutoff = parse_time(activation_cutoff)
    if cutoff is None:
        raise ValueError("invalid segmented condition activation cutoff")
    grouped: dict[str, dict[str, dict[str, Any]]] = {}
    for row in rows:
        if not isinstance(row, dict):
            continue
        if row.get("post_hoc_backfill") or row.get("exclude_from_primary_statistics"):
            continue
        match_id = str(row.get("match_id") or "").strip()
        stage = str(row.get("stage") or "")
        kickoff = parse_time(row.get("kickoff") or row.get("kickoff_hkt"))
        if not match_id or stage not in _STAGE_ORDER or kickoff is None or kickoff < cutoff:
            continue
        stages = grouped.setdefault(match_id, {})
        prior = stages.get(stage)
        if prior is None or str(row.get("predicted_at") or "") > str(prior.get("predicted_at") or ""):
            stages[stage] = row

    public = []
    all_observations = []
    for condition in CONDITIONS:
        observations = []
        matcher: RuleMatcher = condition["matcher"]
        for match_id, stages in grouped.items():
            matched = matcher(stages)
            if not matched:
                continue
            decision_stage = str(matched["decision_stage"])
            decision_row = stages[decision_stage]
            prediction = matched["prediction"]
            grade = _grade(decision_row, prediction)
            kickoff = parse_time(decision_row.get("kickoff") or decision_row.get("kickoff_hkt"))
            directions = {}
            lines = {}
            odds_by_stage = {}
            for stage in _STAGE_ORDER:
                item = _prediction(stages.get(stage) or {}, str(condition["market"]))
                if not item:
                    continue
                side = str(item.get("side") or "").upper()
                directions[stage] = (
                    "主" if condition["market"] == "HDC" and side == "H"
                    else "客" if condition["market"] == "HDC" and side == "A"
                    else "大" if side == "H" else "細" if side == "L" else side
                )
                lines[stage] = _selected_line(item)
                odds_by_stage[stage] = _odds(item)
            settlement = grade.get("settlement") if grade else None
            observation = {
                "condition_id": condition["id"],
                "tier": condition["tier"],
                "market": condition["market"],
                "match_id": match_id,
                "league": decision_row.get("league"),
                "home": decision_row.get("home"),
                "away": decision_row.get("away"),
                "kickoff": kickoff.isoformat() if kickoff else decision_row.get("kickoff"),
                "decision_stage": decision_stage,
                "selected_side": str(prediction.get("side") or "").upper(),
                "selected_line": _selected_line(prediction),
                "odds": _odds(prediction),
                "directions": directions,
                "lines": lines,
                "odds_by_stage": odds_by_stage,
                "settlement": settlement,
                "settlement_label": _SETTLEMENT_LABEL.get(str(settlement), "待賽果"),
                "score": decision_row.get("score"),
                "result_status": decision_row.get("result_status") or "待賽果",
            }
            observations.append(observation)
            all_observations.append(observation)
        observations.sort(key=lambda item: str(item.get("kickoff") or ""), reverse=True)
        output = {key: value for key, value in condition.items() if key != "matcher"}
        output["prospective"] = _prospective_metrics(observations)
        output["observations"] = observations[:100]
        if output["tier"] in PUBLIC_TIERS:
            public.append(output)

    all_observations.sort(key=lambda item: str(item.get("kickoff") or ""), reverse=True)
    return {
        "schema_version": "crown-segmented-conditions-v1",
        "generated_at": generated_at or datetime.now(HKT).isoformat(),
        "activation_cutoff": cutoff.isoformat(),
        "mode": "read_only_prospective_measurement",
        "public_tiers": ["S", "A"],
        "public_conditions": public,
        "matching_observations": all_observations[:200],
        "background_accumulation": {
            "enabled": True,
            "description": "其餘候選條件繼續由既有後台細分礦掘器累積，未達 S／A 前不在此頁顯示。",
        },
    }
