"""Identity-locked operator result overlays for Stage Engine V2.

The overlay is a durable, operator-supplied settlement input.  It never fetches
results, mutates the prediction ledger, or changes the automatic result path.
"""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from crown.lines import settle_handicap, settle_total

from .fixtures import _parse_kickoff
from .segmented_conditions import STAGES, _prediction


DEFAULT_OPERATOR_RESULTS_PATH = Path(
    "/var/lib/footbreak/stage_engine_v2/operator_results.json"
)
SCORE_SCOPE = "90_minutes_including_stoppage_time_excluding_extra_time"


def _required_text(row: dict[str, Any], key: str, match_id: str) -> str:
    value = str(row.get(key) or "").strip()
    if not value:
        raise ValueError(f"operator result {key} missing: {match_id}")
    return value


def _score(row: dict[str, Any], key: str, match_id: str) -> int:
    value = row.get(key)
    if isinstance(value, bool):
        raise ValueError(f"operator result {key} invalid: {match_id}")
    try:
        number = int(value)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"operator result {key} invalid: {match_id}") from exc
    if number < 0 or str(value).strip() != str(number):
        raise ValueError(f"operator result {key} invalid: {match_id}")
    return number


def _grade(
    prediction: dict[str, Any],
    home_score: int,
    away_score: int,
) -> dict[str, Any]:
    code = str(prediction["code"])
    side = str(prediction["side"])
    line = prediction.get("line")
    if line is None:
        return {
            "code": code,
            "side": side,
            "line": None,
            "grade_status": "NOT_APPLICABLE",
            "reason": "line_missing",
        }
    if code == "HDC":
        settlement = settle_handicap(float(line), side, home_score, away_score)
    elif code == "HIL":
        settlement = settle_total(float(line), side, home_score, away_score)
    else:
        return {
            "code": code,
            "side": side,
            "line": line,
            "grade_status": "NOT_APPLICABLE",
            "reason": "unsupported_market",
        }
    return {
        "code": code,
        "side": side,
        "line": line,
        "odds": prediction.get("odds"),
        "grade_status": "GRADED",
        "settlement": settlement,
    }


def load_operator_history_rows(
    path: Path | str,
    ledger: dict[str, Any],
) -> list[dict[str, Any]]:
    """Validate one overlay file and project its results as history rows."""
    overlay_path = Path(path)
    if not overlay_path.exists():
        return []
    try:
        payload = json.loads(overlay_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ValueError(f"operator result overlay unreadable: {overlay_path}") from exc
    if not isinstance(payload, dict) or payload.get("schema_version") != 1:
        raise ValueError("operator result overlay schema invalid")
    if payload.get("score_scope") != SCORE_SCOPE:
        raise ValueError("operator result score scope invalid")
    batch_id = _required_text(payload, "batch_id", "overlay")
    verified_at = _required_text(payload, "verified_at", "overlay")
    specs = payload.get("results")
    if not isinstance(specs, list) or not specs:
        raise ValueError("operator result rows missing")

    fixtures = ledger.get("fixtures")
    if not isinstance(fixtures, dict):
        raise ValueError("Stage Engine V2 ledger fixtures missing")
    seen: set[str] = set()
    output: list[dict[str, Any]] = []
    for spec in specs:
        if not isinstance(spec, dict):
            raise ValueError("operator result row must be an object")
        match_id = _required_text(spec, "match_id", "unknown")
        if match_id in seen:
            raise ValueError(f"operator result match id duplicated: {match_id}")
        seen.add(match_id)
        slot = fixtures.get(match_id)
        if not isinstance(slot, dict):
            raise ValueError(f"operator result fixture missing from V2 ledger: {match_id}")
        for key in ("league", "home", "away"):
            expected = _required_text(spec, key, match_id)
            if str(slot.get(key) or "").strip() != expected:
                raise ValueError(f"operator result {key} mismatch: {match_id}")
        kickoff = _parse_kickoff(spec.get("kickoff"))
        slot_kickoff = _parse_kickoff(slot.get("kickoff_utc") or slot.get("kickoff_hkt"))
        provider_start = _parse_kickoff(spec.get("provider_start"))
        if kickoff is None or slot_kickoff is None or kickoff != slot_kickoff:
            raise ValueError(f"operator result kickoff mismatch: {match_id}")
        if provider_start is None or provider_start != kickoff:
            raise ValueError(f"operator provider kickoff mismatch: {match_id}")
        orientation = spec.get("orientation")
        if orientation not in {"direct", "reversed"}:
            raise ValueError(f"operator result orientation invalid: {match_id}")
        provider_event_id = _required_text(spec, "provider_event_id", match_id)
        provider_home = _required_text(spec, "provider_home", match_id)
        provider_away = _required_text(spec, "provider_away", match_id)
        home_score = _score(spec, "home_score", match_id)
        away_score = _score(spec, "away_score", match_id)

        for stage in STAGES:
            stage_row = (slot.get("stages") or {}).get(stage)
            if not isinstance(stage_row, dict):
                continue
            grades = []
            for code in ("HDC", "HIL"):
                prediction = _prediction(slot, stage, code)
                if prediction is not None:
                    grades.append(_grade(prediction, home_score, away_score))
            output.append({
                "match_id": match_id,
                "stage": stage,
                "league": slot.get("league"),
                "home": slot.get("home"),
                "away": slot.get("away"),
                "kickoff": kickoff.isoformat(),
                "predicted_at": stage_row.get("predicted_at_utc"),
                "score": f"{home_score}-{away_score}",
                "result_status": "已核實",
                "verified_at": verified_at,
                "result_source": "opticodds_operator_verified_overlay",
                "result_detail": {
                    "home_score": home_score,
                    "away_score": away_score,
                    "operator_batch_id": batch_id,
                    "provider_event_id": provider_event_id,
                    "provider_home": provider_home,
                    "provider_away": provider_away,
                    "provider_start": spec["provider_start"],
                    "orientation": orientation,
                    "score_scope": SCORE_SCOPE,
                },
                "market_grades": grades,
            })
    return output


def merge_operator_history_rows(
    history_rows: list[dict[str, Any]],
    operator_rows: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    """Merge operator rows after rejecting conflicts with verified history."""
    verified = {}
    for row in history_rows:
        if row.get("result_status") not in {"已核對", "已核實"}:
            continue
        key = (str(row.get("match_id") or ""), str(row.get("stage") or ""))
        if all(key):
            verified[key] = str(row.get("score") or "")
    for row in operator_rows:
        key = (str(row.get("match_id") or ""), str(row.get("stage") or ""))
        existing_score = verified.get(key)
        if existing_score and existing_score != str(row.get("score") or ""):
            raise ValueError(
                f"operator result conflicts with verified history: {key[0]}:{key[1]}"
            )
    return [*history_rows, *operator_rows]


__all__ = [
    "DEFAULT_OPERATOR_RESULTS_PATH",
    "load_operator_history_rows",
    "merge_operator_history_rows",
]
