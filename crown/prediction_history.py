"""Persistent all-prediction history and outcome scoring for Crown.

This is deliberately separate from the simulated-bet ledger.  Every formal
stage is retained, whether or not it produced a bet.  Evaluation is
observation-only: no model threshold or weight is silently changed.
"""
from __future__ import annotations

import math
from datetime import datetime
from typing import Any

from .common import HKT, SETTLE_AFTER_SECONDS, iso_hkt, parse_time, read_json, write_json_atomic
from .config import Settings
from .hkjc import fetch_official_result_events
from .ledger import STAGES
from .matching import Event, match_event
from .titan import TitanClient


def _path(config: Settings):
    return config.state_dir / "prediction_history.json"


def load_history(config: Settings) -> dict[str, Any]:
    value = read_json(_path(config), {"rows": [], "stats": {}})
    if not isinstance(value, dict):
        value = {"rows": [], "stats": {}}
    value["rows"] = value.get("rows") if isinstance(value.get("rows"), list) else []
    value["stats"] = value.get("stats") if isinstance(value.get("stats"), dict) else {}
    return value


def _history_row(watch: dict[str, Any], stage: dict[str, Any]) -> dict[str, Any]:
    match_id = str(stage.get("match_id") or watch.get("match_id") or "")
    stage_name = str(stage.get("stage") or "")
    pick = stage.get("pick") if isinstance(stage.get("pick"), dict) else None
    return {
        "_origin": "crown_ledger_v1",
        "history_key": f"{match_id}|{stage_name}",
        "match_id": match_id,
        "hkjc_match_id": stage.get("hkjc_match_id") or watch.get("hkjc_match_id"),
        "titan_match_id": stage.get("titan_match_id") or watch.get("titan_match_id") or match_id,
        "pinnapi_event_id": stage.get("pinnapi_event_id") or watch.get("pinnapi_event_id"),
        "league": stage.get("league") or watch.get("league"),
        "home": stage.get("home") or watch.get("home"),
        "away": stage.get("away") or watch.get("away"),
        "kickoff": stage.get("kickoff_hkt") or watch.get("kickoff_hkt") or watch.get("kickoff"),
        "stage": stage_name,
        "predicted_at": stage.get("ts") or stage.get("source_snapshot_at") or iso_hkt(),
        "outcome": stage.get("outcome"),
        "forecast": stage.get("forecast"),
        "probability": stage.get("probability"),
        "likely_score": stage.get("likely_score"),
        "prediction_source": stage.get("prediction_source"),
        "conviction": stage.get("conviction"),
        "simulated_bet": bool(pick),
        "bet_label": pick.get("label") if pick else None,
        "no_bet_reason": stage.get("no_bet_reason"),
        "actual": None,
        "score": None,
        "correct": None,
        "result_status": "待賽果",
        "verified_at": None,
    }


def archive_watch(config: Settings, ledger: dict[str, Any]) -> dict[str, Any]:
    history = load_history(config)
    rows = history["rows"]
    generated = {
        str(row.get("history_key")): row
        for row in rows
        if row.get("_origin") == "crown_ledger_v1" and row.get("history_key")
    }
    for watch in (ledger.get("watch") or {}).values():
        if not isinstance(watch, dict):
            continue
        for stage in watch.get("stages") or []:
            if stage.get("stage") not in STAGES:
                continue
            row = _history_row(watch, stage)
            old = generated.get(row["history_key"])
            if old is None:
                rows.append(row)
                generated[row["history_key"]] = row
            else:
                result_fields = {
                    key: old.get(key)
                    for key in ("actual", "score", "correct", "result_status", "verified_at", "result_source")
                }
                old.update(row)
                old.update({key: value for key, value in result_fields.items() if value is not None})
    history["stats"] = calculate_stats(rows)
    write_json_atomic(_path(config), history)
    return history


def _target(row: dict[str, Any]) -> Event | None:
    kickoff = parse_time(row.get("kickoff"))
    if not kickoff or not row.get("home") or not row.get("away"):
        return None
    return Event(
        str(row.get("match_id") or ""),
        str(row.get("league") or ""),
        str(row["home"]),
        str(row["away"]),
        kickoff,
    )


def _result(row: dict[str, Any], titan_by_id: dict[str, dict[str, Any]],
            hkjc_by_id: dict[str, dict[str, Any]], hkjc_events: list[tuple[Event, dict[str, Any]]]
            ) -> tuple[dict[str, Any] | None, str | None]:
    target = _target(row)
    if target is None:
        return None, None
    official = hkjc_by_id.get(str(row.get("hkjc_match_id") or ""))
    if official:
        return official, "hkjc_official_exact_id"
    titan = titan_by_id.get(str(row.get("titan_match_id") or row.get("match_id") or ""))
    if titan and titan.get("home_score") is not None:
        candidate = Event(
            str(titan["id"]), str(titan.get("league") or ""), str(titan.get("home") or ""),
            str(titan.get("away") or ""), titan["kickoff"],
        )
        if match_event(target, [candidate], allow_reversed=False, require_qualifiers=True).event:
            return titan, "titan_verified_identity"
    matched = match_event(
        target, [event for event, _ in hkjc_events],
        allow_reversed=False, require_qualifiers=True,
    )
    if matched.event:
        return next(data for event, data in hkjc_events if event.id == matched.event.id), "hkjc_official_strict_identity"
    return None, None


def _hkjc_event(row: dict[str, Any]) -> Event | None:
    kickoff = parse_time(row.get("kickoff"))
    if not kickoff or not row.get("home") or not row.get("away"):
        return None
    return Event(str(row["id"]), str(row.get("league") or ""), str(row["home"]), str(row["away"]), kickoff)


def grade_history(config: Settings) -> dict[str, Any]:
    history = load_history(config)
    rows = history["rows"]
    now = datetime.now(HKT)
    due = [
        row for row in rows
        if not row.get("actual")
        and (kickoff := parse_time(row.get("kickoff"))) is not None
        and (now - kickoff).total_seconds() >= SETTLE_AFTER_SECONDS
    ]
    dates = {
        parse_time(row.get("kickoff")).strftime("%Y-%m-%d")
        for row in due if parse_time(row.get("kickoff"))
    }
    try:
        titan_rows = TitanClient(config).results() if due else []
    except Exception:
        titan_rows = []
    try:
        official_rows = fetch_official_result_events(dates) if due else []
    except Exception:
        official_rows = []
    titan_by_id = {str(row.get("id")): row for row in titan_rows}
    hkjc_by_id = {str(row.get("id")): row for row in official_rows}
    hkjc_events = [(event, row) for row in official_rows if (event := _hkjc_event(row))]
    for row in due:
        score, source = _result(row, titan_by_id, hkjc_by_id, hkjc_events)
        if not score:
            continue
        try:
            home, away = int(score["home_score"]), int(score["away_score"])
        except (KeyError, TypeError, ValueError):
            continue
        actual = "主勝" if home > away else ("和局" if home == away else "客勝")
        row.update({
            "actual": actual,
            "score": f"{home}-{away}",
            "correct": bool(row.get("forecast")) and row.get("forecast") == actual,
            "result_status": "已核實",
            "verified_at": iso_hkt(),
            "result_source": source,
        })
    history["stats"] = calculate_stats(rows)
    write_json_atomic(_path(config), history)
    return history


def _metrics(rows: list[dict[str, Any]]) -> dict[str, Any]:
    graded = [row for row in rows if row.get("actual")]
    hits = sum(row.get("correct") is True for row in graded)
    briers: list[float] = []
    losses: list[float] = []
    actual_key = {"主勝": "home", "和局": "draw", "客勝": "away"}
    for row in graded:
        probabilities = row.get("outcome")
        key = actual_key.get(row.get("actual"))
        if not isinstance(probabilities, dict) or not key:
            continue
        try:
            p = {name: float(probabilities[name]) for name in ("home", "draw", "away")}
        except (KeyError, TypeError, ValueError):
            continue
        total = sum(p.values())
        if total <= 0:
            continue
        p = {name: value / total for name, value in p.items()}
        briers.append(sum((p[name] - (1.0 if name == key else 0.0)) ** 2 for name in p))
        losses.append(-math.log(max(p[key], 1e-9)))
    return {
        "graded": len(graded),
        "hits": hits,
        "accuracy": round(hits / len(graded), 6) if graded else None,
        "brier": round(sum(briers) / len(briers), 6) if briers else None,
        "log_loss": round(sum(losses) / len(losses), 6) if losses else None,
        "probability_scored": len(briers),
    }


def calculate_stats(rows: list[dict[str, Any]]) -> dict[str, Any]:
    overall = _metrics(rows)
    by_stage = {stage: _metrics([row for row in rows if row.get("stage") == stage]) for stage in STAGES}
    latest_by_match: dict[str, dict[str, Any]] = {}
    for row in rows:
        match_id = str(row.get("match_id") or "")
        if not match_id:
            continue
        old = latest_by_match.get(match_id)
        rank = (STAGES.get(str(row.get("stage")), 0), str(row.get("predicted_at") or ""))
        old_rank = (STAGES.get(str((old or {}).get("stage")), 0), str((old or {}).get("predicted_at") or ""))
        if old is None or rank > old_rank:
            latest_by_match[match_id] = row
    latest = _metrics(list(latest_by_match.values()))
    return {
        "matches": len({str(row.get("match_id")) for row in rows if row.get("match_id")}),
        "predictions": len(rows),
        "pending": len(rows) - overall["graded"],
        **overall,
        "by_stage": by_stage,
        "latest": latest,
        "learning_status": "observation_only",
        "minimum_sample_per_bucket": 30,
    }


def update_history(config: Settings, ledger: dict[str, Any] | None = None) -> dict[str, Any]:
    if ledger is not None:
        archive_watch(config, ledger)
    return grade_history(config)
