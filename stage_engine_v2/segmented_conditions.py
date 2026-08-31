"""Read-only S/A segmented-condition projection for Crown Stage Engine V2.

This module evaluates the model's selected prediction direction at the
opening, T-30 and T-5 decision points.  It does not infer direction from
bookmaker quote movement and never changes predictions, bets or notifications.
"""
from __future__ import annotations

import math
import re
from collections import Counter
from datetime import datetime
from typing import Any, Callable

from .fixtures import HKT, _parse_kickoff


ACTIVATION_CUTOFF = "2026-08-31T00:00:00+08:00"
PUBLIC_TIERS = ("S", "A")
STAGES = ("首預", "T-30", "T-5")

_MARKET_ALIASES = {
    "HDC": "HDC", "AH": "HDC", "亞洲讓球": "HDC", "讓球": "HDC",
    "HIL": "HIL", "OU": "HIL", "入球大細": "HIL", "大小球": "HIL",
    "CHL": "CHL", "COU": "CHL", "角球大細": "CHL",
}
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


def _as_float(value: Any) -> float | None:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if math.isfinite(number) else None


def _market(row: dict[str, Any]) -> str:
    raw = str(
        row.get("lead_code")
        or row.get("code")
        or row.get("lead_market")
        or row.get("market")
        or ""
    ).strip()
    return _MARKET_ALIASES.get(raw.upper(), _MARKET_ALIASES.get(raw, raw.upper()))


def _line_from_label(label: str) -> float | None:
    split = re.search(
        r"(?<!\d)([+-]?\d+(?:\.\d+)?)\s*/\s*([+-]?\d+(?:\.\d+)?)",
        label,
    )
    if split:
        first = _as_float(split.group(1))
        second_text = split.group(2)
        if split.group(1).startswith("-") and not second_text.startswith(("+", "-")):
            second_text = f"-{second_text}"
        second = _as_float(second_text)
        if first is not None and second is not None:
            return (first + second) / 2
    matches = re.findall(r"(?<!\d)[+-]?\d+(?:\.\d+)?", label)
    return _as_float(matches[-1]) if matches else None


def _side(row: dict[str, Any], market: str, home: str = "", away: str = "") -> str:
    raw = str(row.get("lead_side") or row.get("side") or "").strip().upper()
    if raw in {"H", "A", "L"}:
        return raw
    label = str(row.get("lead_label") or row.get("label") or "").strip()
    lower = label.lower()
    if market in {"HIL", "CHL"}:
        if re.search(r"(^|\s)(o|over)(\s|$)", lower) or "大" in label:
            return "H"
        if re.search(r"(^|\s)(u|under)(\s|$)", lower) or "細" in label:
            return "L"
    if market == "HDC":
        if lower in {"h", "home", "主"} or "主隊" in label or re.search(r"(^|\s)主(\s|$)", label):
            return "H"
        if lower in {"a", "away", "客"} or "客隊" in label or re.search(r"(^|\s)客(\s|$)", label):
            return "A"
        if home and home.lower() in lower:
            return "H"
        if away and away.lower() in lower:
            return "A"
    return ""


def _raw_line(row: dict[str, Any]) -> float | None:
    value = row.get("lead_line")
    if value is None:
        value = row.get("line")
    if value is None:
        value = row.get("condition")
    if value is None:
        value = _line_from_label(str(row.get("lead_label") or row.get("label") or ""))
    return _as_float(value)


def _selected_line(row: dict[str, Any], market: str, side: str) -> float | None:
    line = _raw_line(row)
    if line is None:
        return None
    return -line if market == "HDC" and side == "A" else line


def _prediction(slot: dict[str, Any], stage: str, code: str) -> dict[str, Any] | None:
    row = (slot.get("stages") or {}).get(stage)
    if not isinstance(row, dict) or _market(row) != code:
        return None
    side = _side(row, code, str(slot.get("home") or ""), str(slot.get("away") or ""))
    if not side:
        return None
    odds = _as_float(row.get("lead_odds") if "lead_odds" in row else row.get("odds"))
    explicit_line = any(row.get(key) is not None for key in ("lead_line", "line", "condition"))
    raw_line = _raw_line(row)
    if code == "HDC" and side == "A" and not explicit_line and raw_line is not None:
        # Historical V2 labels display the selected team's line, while the
        # newly persisted lead_line keeps Crown's raw home-team line.
        selected_line = raw_line
        raw_line = -raw_line
    else:
        selected_line = _selected_line(row, code, side)
    return {
        "code": code,
        "side": side,
        "line": raw_line,
        "selected_line": selected_line,
        "odds": odds,
        "label": row.get("lead_label") or row.get("label"),
        "stage": stage,
        "predicted_at": row.get("predicted_at_utc") or row.get("predicted_at"),
    }


def _same_line(predictions: list[dict[str, Any]]) -> bool:
    values = [item.get("selected_line") for item in predictions]
    return bool(values) and values[0] is not None and all(
        value is not None and abs(float(value) - float(values[0])) < 1e-9
        for value in values
    )


def _rule_s_t5_over(slot: dict[str, Any]) -> dict[str, Any] | None:
    prediction = _prediction(slot, "T-5", "HIL")
    if not prediction or prediction["side"] != "H" or (prediction["odds"] or 0) <= 1.85:
        return None
    return prediction


def _rule_a_open_t5_over(slot: dict[str, Any]) -> dict[str, Any] | None:
    opening = _prediction(slot, "首預", "HIL")
    t5 = _prediction(slot, "T-5", "HIL")
    if not opening or not t5:
        return None
    if opening["side"] != "H" or t5["side"] != "H":
        return None
    if (opening["odds"] or 0) <= 1.80 or (t5["odds"] or 0) <= 1.80:
        return None
    return t5


def _rule_a_open_away_minus_half(slot: dict[str, Any]) -> dict[str, Any] | None:
    prediction = _prediction(slot, "首預", "HDC")
    if not prediction or prediction["side"] != "A":
        return None
    line = prediction.get("selected_line")
    if line is None or abs(float(line) - (-0.5)) > 1e-9:
        return None
    return prediction


def _rule_a_three_home_same_line(slot: dict[str, Any]) -> dict[str, Any] | None:
    predictions = [_prediction(slot, stage, "HDC") for stage in STAGES]
    if any(item is None for item in predictions):
        return None
    typed = [item for item in predictions if item is not None]
    if any(item["side"] != "H" for item in typed) or not _same_line(typed):
        return None
    return typed[-1]


RuleMatcher = Callable[[dict[str, Any]], dict[str, Any] | None]

CONDITIONS: tuple[dict[str, Any], ...] = (
    {
        "id": "S-HIL-T5-OVER-185",
        "tier": "S",
        "market": "HIL",
        "title": "T-5 預測大；賠率 > 1.85",
        "definition": "只睇 T-5 入球大細預測方向；預測為大，而且所選方向賠率嚴格高於 1.85。",
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
        "definition": "初盤預測及 T-5 都預測入球大，而且兩段所選方向賠率都嚴格高於 1.80。",
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
        "tier": "A",
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


def _identity_keys(row: dict[str, Any]) -> set[str]:
    keys = set()
    for name in (
        "match_id", "id", "native_fixture_id", "hkjc_match_id",
        "titan_match_id", "pinnapi_event_id",
    ):
        value = str(row.get(name) or "").strip()
        if value:
            keys.add(f"id:{value}")
    home = str(row.get("home") or "").strip().casefold()
    away = str(row.get("away") or "").strip().casefold()
    kickoff = _parse_kickoff(row.get("kickoff") or row.get("kickoff_hkt") or row.get("kickoff_utc"))
    if home and away and kickoff:
        keys.add(f"match:{home}|{away}|{kickoff.isoformat()}")
    return keys


def _history_index(rows: list[dict[str, Any]]) -> dict[str, list[dict[str, Any]]]:
    index: dict[str, list[dict[str, Any]]] = {}
    for row in rows:
        if not isinstance(row, dict):
            continue
        for key in _identity_keys(row):
            index.setdefault(key, []).append(row)
    return index


def _matching_history(
    slot: dict[str, Any],
    stage: str,
    index: dict[str, list[dict[str, Any]]],
) -> dict[str, Any] | None:
    candidates: list[dict[str, Any]] = []
    seen: set[int] = set()
    for key in _identity_keys(slot):
        for row in index.get(key, []):
            if id(row) in seen or str(row.get("stage") or "") != stage:
                continue
            seen.add(id(row))
            candidates.append(row)
    verified = [row for row in candidates if row.get("result_status") in {"已核對", "已核實"}]
    pool = verified or candidates
    return max(pool, key=lambda row: str(row.get("predicted_at") or ""), default=None)


def _matching_grade(history: dict[str, Any] | None, prediction: dict[str, Any]) -> dict[str, Any] | None:
    if not history:
        return None
    matches = []
    for grade in history.get("market_grades") or []:
        if not isinstance(grade, dict) or grade.get("grade_status") != "GRADED":
            continue
        if _market(grade) != prediction["code"]:
            continue
        side = _side(grade, prediction["code"])
        if side != prediction["side"]:
            continue
        grade_line = _raw_line(grade)
        pred_line = prediction.get("line")
        if grade_line is not None and pred_line is not None and abs(grade_line - pred_line) > 1e-9:
            continue
        matches.append(grade)
    return matches[0] if len(matches) == 1 else None


def _direction_label(code: str, side: str) -> str:
    if code == "HDC":
        return "主" if side == "H" else "客"
    return "大" if side == "H" else "細"


def _prospective_metrics(observations: list[dict[str, Any]]) -> dict[str, Any]:
    settlements = Counter(
        str(item.get("settlement"))
        for item in observations
        if item.get("settlement") in _SETTLEMENT_LABEL
    )
    settled = sum(settlements.values())
    denominator = settled - settlements["Refunded"]
    hits = settlements["Won"] + settlements["Half Won"]
    profit = 0.0
    priced = 0
    for item in observations:
        settlement = item.get("settlement")
        odds = _as_float(item.get("odds"))
        if settlement not in _SETTLEMENT_LABEL or odds is None:
            continue
        priced += 1
        if settlement == "Won":
            profit += odds - 1
        elif settlement == "Half Won":
            profit += (odds - 1) / 2
        else:
            profit += _SETTLEMENT_PNL.get(str(settlement), 0.0)
    return {
        "qualified": len(observations),
        "settled": settled,
        "pending": len(observations) - settled,
        "full_win": settlements["Won"],
        "half_win": settlements["Half Won"],
        "push": settlements["Refunded"],
        "half_loss": settlements["Half Lost"],
        "full_loss": settlements["Lost"],
        "hit_rate": round(hits / denominator, 6) if denominator else None,
        "roi": round(profit / priced, 6) if priced else None,
        "roi_priced": priced,
    }


def build_segmented_conditions(
    ledger: dict[str, Any],
    history_rows: list[dict[str, Any]] | None = None,
    *,
    activation_cutoff: str = ACTIVATION_CUTOFF,
    generated_at: str | None = None,
) -> dict[str, Any]:
    cutoff = _parse_kickoff(activation_cutoff)
    if cutoff is None:
        raise ValueError("invalid segmented condition activation cutoff")
    history_index = _history_index(history_rows or [])
    slots = []
    for slot in (ledger.get("fixtures") or {}).values():
        if not isinstance(slot, dict):
            continue
        kickoff = _parse_kickoff(slot.get("kickoff_utc") or slot.get("kickoff_hkt"))
        if kickoff is not None and kickoff >= cutoff:
            slots.append(slot)
    public = []
    matching_observations = []
    for condition in CONDITIONS:
        observations = []
        matcher: RuleMatcher = condition["matcher"]
        for slot in slots:
            prediction = matcher(slot)
            if not prediction:
                continue
            decision_stage = str(prediction["stage"])
            decision_at = _parse_kickoff(prediction.get("predicted_at"))
            # Prospective performance starts when the condition could actually
            # have been acted on.  Kickoff alone is insufficient: fixtures at
            # the boundary can contain a decision produced before activation.
            if decision_at is None or decision_at < cutoff:
                continue
            history = _matching_history(slot, decision_stage, history_index)
            grade = _matching_grade(history, prediction)
            directions, lines, odds_by_stage = {}, {}, {}
            for stage in STAGES:
                item = _prediction(slot, stage, str(condition["market"]))
                if not item:
                    continue
                directions[stage] = _direction_label(item["code"], item["side"])
                lines[stage] = item.get("selected_line")
                odds_by_stage[stage] = item.get("odds")
            settlement = grade.get("settlement") if grade else None
            kickoff = _parse_kickoff(slot.get("kickoff_utc") or slot.get("kickoff_hkt"))
            observation = {
                "condition_id": condition["id"],
                "tier": condition["tier"],
                "market": condition["market"],
                "match_id": slot.get("id"),
                "league": slot.get("league"),
                "home": slot.get("home"),
                "away": slot.get("away"),
                "kickoff": kickoff.astimezone(HKT).isoformat() if kickoff else slot.get("kickoff_hkt"),
                "decision_stage": decision_stage,
                "decision_at": decision_at.astimezone(HKT).isoformat(),
                "selected_side": prediction["side"],
                "selected_line": prediction.get("selected_line"),
                "odds": prediction.get("odds"),
                "directions": directions,
                "lines": lines,
                "odds_by_stage": odds_by_stage,
                "settlement": settlement,
                "settlement_label": _SETTLEMENT_LABEL.get(str(settlement), "待賽果"),
                "score": history.get("score") if history else None,
                "result_status": history.get("result_status") if history else "待賽果",
            }
            observations.append(observation)
            matching_observations.append(observation)
        observations.sort(key=lambda item: str(item.get("kickoff") or ""), reverse=True)
        output = {key: value for key, value in condition.items() if key != "matcher"}
        output["prospective"] = _prospective_metrics(observations)
        output["observations"] = observations[:100]
        public.append(output)
    matching_observations.sort(key=lambda item: str(item.get("kickoff") or ""), reverse=True)
    return {
        "schema_version": "crown-v2-segmented-conditions-v1",
        "generated_at": generated_at or datetime.now(HKT).isoformat(),
        "activation_cutoff": cutoff.astimezone(HKT).isoformat(),
        "mode": "read_only_prospective_measurement",
        "direction_basis": "model_prediction_not_quote_direction",
        "public_tiers": list(PUBLIC_TIERS),
        "public_conditions": public,
        "matching_observations": matching_observations[:200],
        "background_accumulation": {
            "enabled": True,
            "description": "其餘候選條件繼續在後台累積，未達 S／A 前不在此頁顯示。",
        },
    }


__all__ = ["ACTIVATION_CUTOFF", "CONDITIONS", "build_segmented_conditions"]
