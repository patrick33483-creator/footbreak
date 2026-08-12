#!/usr/bin/env python3
"""Isolated prospective condition reports for Footbreak and Crown.

These reports read immutable learning snapshots only.  They never modify a
prediction, probability, pick, ledger, stake, notification, or promotion path.
The one private state file freezes the start boundary once; all prior rows are
excluded permanently from prospective evidence.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import math
import os
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable

from analysis.learning_store import LearningStore

STATE_KIND = "shadow_condition_reports"
SCHEMA_VERSION = 1
MIN_REVIEW_FIXTURES = 100
STAGES = ("首預", "T-30", "T-5")
CONDITIONS = {
    "footbreak_hil_t5_under": {
        "system": "footbreak", "market": "HIL", "stage": "T-5", "side": "L",
        "label": "Footbreak only · T-5 HIL 細(L)",
    },
    "crown_hdc_three_stage_exact": {
        "system": "crown", "market": "HDC", "stage": "T-5",
        "label": "Crown only · HDC 首預/T-30/T-5 同方向同盤口",
    },
}


def _canonical(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _hash(value: Any) -> str:
    return hashlib.sha256(_canonical(value).encode("utf-8")).hexdigest()


def _now(value: datetime | None = None) -> datetime:
    return (value or datetime.now(timezone.utc)).astimezone(timezone.utc)


def _stamp(value: datetime) -> str:
    return value.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


def _number(value: Any) -> float | None:
    try:
        answer = float(value)
    except (TypeError, ValueError):
        return None
    return answer if math.isfinite(answer) else None


def _line(prediction: dict[str, Any]) -> float | None:
    return _number(prediction.get("line", prediction.get("condition")))


def _prediction(row: dict[str, Any], market: str) -> dict[str, Any] | None:
    for value in (row.get("payload") or {}).get("market_predictions") or []:
        if isinstance(value, dict) and str(value.get("code")) == market:
            return value
    return None


def _grade(row: dict[str, Any], market: str, prediction: dict[str, Any]) -> dict[str, Any] | None:
    side, line = str(prediction.get("side") or ""), _line(prediction)
    if not side or line is None:
        return None
    for grade in row.get("grades") or []:
        if str(grade.get("market")) != market:
            continue
        key = str(grade.get("target_key") or "")
        before, sep, target_side = key.rpartition("|")
        target_line = _number(before) if sep else None
        if target_side == side and target_line is not None and abs(target_line - line) < 1e-9:
            return grade
    return None


def _settlement(grade: dict[str, Any] | None) -> tuple[float | None, str]:
    if not grade or grade.get("state") != "GRADED":
        return None, "outcome_unavailable"
    target = _number((grade.get("metrics") or {}).get("target"))
    if target not in {0.0, 0.25, 0.5, 0.75, 1.0}:
        return None, "outcome_unavailable"
    return target, "settled"


def _entry_odds(prediction: dict[str, Any]) -> float | None:
    odds = _number(prediction.get("odds"))
    return odds if odds is not None and odds > 1.0 else None


def _closing_odds(prediction: dict[str, Any]) -> float | None:
    """Only a persisted quote on this exact selected prediction can count."""
    odds = _number(prediction.get("closing_odds"))
    return odds if odds is not None and odds > 1.0 else None


def _state_payload(cutoff: datetime) -> dict[str, Any]:
    frozen = {
        "kind": STATE_KIND, "schema_version": SCHEMA_VERSION,
        "freeze_cutoff": _stamp(cutoff), "boundary": "kickoff_strictly_after_cutoff",
        "conditions": CONDITIONS, "auto_apply": False, "human_review_only": True,
    }
    return {**frozen, "integrity_hash": _hash(frozen)}


def load_state(path: Path) -> dict[str, Any]:
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise ValueError("shadow condition state is not an object")
    integrity = value.get("integrity_hash")
    unsigned = {key: item for key, item in value.items() if key != "integrity_hash"}
    if integrity != _hash(unsigned) or value.get("kind") != STATE_KIND:
        raise ValueError("shadow condition state integrity check failed")
    return value


def freeze_once(path: Path, now: datetime | None = None) -> dict[str, Any]:
    if path.exists():
        return load_state(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    os.chmod(path.parent, 0o700)
    value = _state_payload(_now(now))
    data = (_canonical(value) + "\n").encode("utf-8")
    try:
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    except FileExistsError:
        return load_state(path)
    with os.fdopen(fd, "wb") as handle:
        handle.write(data)
        handle.flush()
        os.fsync(handle.fileno())
    return value


def _prospective(rows: Iterable[dict[str, Any]], system: str, cutoff: datetime) -> list[dict[str, Any]]:
    return [row for row in rows if row["kickoff"] > cutoff and row["predicted_at"] < row["kickoff"] and row.get("stage") in STAGES]


def _condition_a(rows: list[dict[str, Any]]) -> list[tuple[str, dict[str, Any], dict[str, Any]]]:
    chosen: dict[str, tuple[dict[str, Any], dict[str, Any]]] = {}
    for row in rows:
        if row.get("stage") != "T-5":
            continue
        prediction = _prediction(row, "HIL")
        if prediction and str(prediction.get("side") or "") == "L":
            chosen.setdefault(str(row["match_id"]), (row, prediction))
    return [(fixture, row, prediction) for fixture, (row, prediction) in sorted(chosen.items())]


def _condition_b(rows: list[dict[str, Any]]) -> list[tuple[str, dict[str, Any], dict[str, Any]]]:
    grouped: dict[str, dict[str, tuple[dict[str, Any], dict[str, Any]]]] = {}
    for row in rows:
        prediction = _prediction(row, "HDC")
        if prediction:
            grouped.setdefault(str(row["match_id"]), {})[str(row["stage"])] = (row, prediction)
    output = []
    for fixture, stage_map in sorted(grouped.items()):
        if any(stage not in stage_map for stage in STAGES):
            continue
        predictions = [stage_map[stage][1] for stage in STAGES]
        sides = {str(value.get("side") or "") for value in predictions}
        lines = [_line(value) for value in predictions]
        if len(sides) != 1 or "" in sides or any(value is None for value in lines):
            continue
        if max(lines) - min(lines) > 1e-9:  # type: ignore[arg-type]
            continue
        row, prediction = stage_map["T-5"]
        output.append((fixture, row, prediction))
    return output


def _metric_report(items: list[tuple[str, dict[str, Any], dict[str, Any]]]) -> dict[str, Any]:
    counts = {
        "qualified_fixtures": len(items), "settled": 0, "decided": 0, "hits": 0,
        "half_won": 0, "half_lost": 0, "pushes_refunds": 0,
        "outcome_unavailable": 0, "missing_selected_direction_odds": 0,
        "missing_same_direction_closing_quote": 0,
    }
    brier_values: list[float] = []
    returns: list[float] = []
    clv_values: list[float] = []
    roi_blocked = clv_blocked = False
    for _, row, prediction in items:
        target, state = _settlement(_grade(row, str(prediction.get("code")), prediction))
        if target is None:
            counts["outcome_unavailable"] += 1
            continue
        counts["settled"] += 1
        probability = _number(prediction.get("probability"))
        if probability is not None and 0.0 <= probability <= 1.0:
            brier_values.append((probability - target) ** 2)
        # A refund has a zero return, but is still a genuinely selected,
        # settled unit.  A missing selected-side quote must make ROI/CLV
        # unavailable rather than presenting a partially observed result.
        odds = _entry_odds(prediction)
        if odds is None:
            counts["missing_selected_direction_odds"] += 1
            roi_blocked = True
        else:
            returns.append((2 * (target - 0.5) * (odds - 1)) if target >= 0.5 else 2 * (target - 0.5))
        closing = _closing_odds(prediction)
        if odds is None or closing is None:
            counts["missing_same_direction_closing_quote"] += 1
            clv_blocked = True
        else:
            clv_values.append(math.log(odds / closing))
        if target == 0.5:
            counts["pushes_refunds"] += 1
            continue
        counts["decided"] += 1
        if target >= 0.75:
            counts["hits"] += 1
        if target == 0.75:
            counts["half_won"] += 1
        elif target == 0.25:
            counts["half_lost"] += 1
    accuracy = counts["hits"] / counts["decided"] if counts["decided"] else None
    roi_reason = ("no_settled_outcomes" if not counts["settled"] else
                  "selected_direction_pre_kickoff_odds_unavailable" if roi_blocked else None)
    clv_reason = ("no_settled_outcomes" if not counts["settled"] else
                  "same_market_direction_line_closing_quote_unavailable" if clv_blocked else None)
    return {
        "counts": counts,
        "hit_rate": round(accuracy, 6) if accuracy is not None else None,
        "roi": None if roi_reason else round(sum(returns) / len(returns), 6),
        "roi_reason": roi_reason,
        "clv": None if clv_reason else round(sum(clv_values) / len(clv_values), 6),
        "clv_reason": clv_reason,
        "brier": round(sum(brier_values) / len(brier_values), 6) if brier_values else None,
        "brier_reason": None if brier_values else "stored_probability_or_settlement_target_unavailable",
    }


def evaluate(rows_by_system: dict[str, list[dict[str, Any]]], state: dict[str, Any]) -> dict[str, Any]:
    cutoff = datetime.fromisoformat(str(state["freeze_cutoff"]).replace("Z", "+00:00"))
    conditions: dict[str, Any] = {}
    for condition_id, config in CONDITIONS.items():
        prospective = _prospective(rows_by_system.get(config["system"], []), config["system"], cutoff)
        selected = _condition_a(prospective) if condition_id == "footbreak_hil_t5_under" else _condition_b(prospective)
        metrics = _metric_report(selected)
        decided = metrics["counts"]["decided"]
        conditions[condition_id] = {
            "condition": config["label"], "system": config["system"], "status": "human_review_ready" if decided >= MIN_REVIEW_FIXTURES else "collecting_insufficient",
            "progress": {
                "qualified_unique_fixtures": metrics["counts"]["qualified_fixtures"],
                "decided_unique_fixtures": decided,
                "required_unique_decided_fixtures": MIN_REVIEW_FIXTURES,
                "remaining": max(0, MIN_REVIEW_FIXTURES - decided),
            },
            "freeze_cutoff": state["freeze_cutoff"], "metrics": metrics,
            "qualification": ("one immutable T-5 HIL row with side L (Under)" if condition_id == "footbreak_hil_t5_under"
                              else "immutable HDC at 首預, T-30, T-5 with identical side and numeric line; grade T-5"),
            "auto_apply": False, "human_review_only": True,
        }
    return {
        "report": "shadow_conditions", "schema_version": SCHEMA_VERSION, "generated_at": _stamp(_now()),
        "policy": {
            "report_only": True, "auto_apply": False, "human_review_only": True,
            "strict_isolation": "No path to live probabilities, picks, official or confidence-only shadow ledgers, staking, alerts, or promotion.",
            "definitions": {
                "primary_unit": "one unique qualified fixture per condition; human-review progress uses unique decided fixtures only",
                "accuracy": "Won and Half Won count as hits; Lost and Half Lost as misses; Refunded/push and unavailable outcomes are excluded from the denominator and counted.",
                "roi": "Flat one-unit settled return; Won=odds-1, Half Won=(odds-1)/2, Refund=0, Half Lost=-0.5, Lost=-1. Null unless every decided selected direction has its own stored pre-kickoff odds.",
                "clv": "mean(log(selected pre-kickoff odds / persisted closing odds)), only with an exact same-market, same-direction, same-line stored closing quote for every decided fixture.",
                "brier": "mean((stored selected-direction pre-kickoff probability - immutable settlement target)^2), targets Won=1, Half Won=.75, Refund=.5, Half Lost=.25, Lost=0; refunds excluded from accuracy but retained in counts.",
            },
        },
        "freeze": {"cutoff": state["freeze_cutoff"], "boundary": state["boundary"], "state_integrity_hash": state["integrity_hash"]},
        "conditions": conditions,
    }


def _write_public(path: Path, value: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temp = tempfile.mkstemp(prefix=".shadow-conditions.", dir=path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(_canonical(value) + "\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temp, path)
        os.chmod(path, 0o644)
    finally:
        if os.path.exists(temp):
            os.unlink(temp)


def run(database: Path, state_path: Path, footbreak_public: Path, crown_public: Path, now: datetime | None = None) -> dict[str, Any]:
    state = freeze_once(state_path, now)
    with LearningStore(database) as store:
        footbreak, foot_diag = store.shadow_condition_rows("footbreak")
        crown, crown_diag = store.shadow_condition_rows("crown")
    report = evaluate({"footbreak": footbreak, "crown": crown}, state)
    report["source_diagnostics"] = {"footbreak": foot_diag, "crown": crown_diag}
    # Each public artifact carries only its own condition.  This is deliberate
    # dashboard separation, not a filtered view of another system's report.
    for system, destination, condition_id in (
        ("footbreak", footbreak_public, "footbreak_hil_t5_under"),
        ("crown", crown_public, "crown_hdc_three_stage_exact"),
    ):
        _write_public(destination, {
            **{key: value for key, value in report.items() if key not in {"conditions", "source_diagnostics"}},
            "system": system, "condition_id": condition_id,
            "condition": report["conditions"][condition_id],
            "source_diagnostics": report["source_diagnostics"][system],
        })
    return report


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--learning-db", type=Path, required=True)
    parser.add_argument("--state", type=Path, default=Path("/var/lib/footbreak/shadow-conditions/state.json"))
    parser.add_argument("--public-footbreak", type=Path, default=Path("/var/www/footbreak/shadow-condition-report.json"))
    parser.add_argument("--public-crown", type=Path, default=Path("/var/www/crown/shadow-condition-report.json"))
    args = parser.parse_args()
    report = run(args.learning_db, args.state, args.public_footbreak, args.public_crown)
    print(f"shadow conditions generated: {report['freeze']['cutoff']}")


if __name__ == "__main__":
    main()
