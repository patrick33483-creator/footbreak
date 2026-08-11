"""Persistent all-prediction history and outcome scoring for Crown.

This is deliberately separate from the simulated-bet ledger.  Every formal
stage is retained, whether or not it produced a bet.  Evaluation is
observation-only: no model threshold or weight is silently changed.
"""
from __future__ import annotations

import math
import os
from collections import Counter
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

from .common import (
    HKT,
    SETTLE_AFTER_SECONDS,
    is_non_result_terminal_status,
    iso_hkt,
    parse_time,
    read_json,
    write_json_atomic,
)
from .config import Settings
from .hkjc import fetch_official_match_statuses, fetch_official_result_events
from .ledger import PREDICTION_ERA, PREDICTION_SCHEMA_VERSION, STAGES
from .lines import settle_handicap, settle_total
from .matching import Event, canonical_league_key, canonical_team_key, match_event
from .titan import TitanClient


def _path(config: Settings):
    return config.state_dir / "prediction_history.json"


_SCOREABLE_MARKETS = {"HDC", "HIL", "CHL"}
_CORNER_RESULT_RETRY_DAYS = 7
_HKJC_RESULT_GRACE_SECONDS = 6 * 60 * 60


def _valid_market_prediction(prediction: Any) -> bool:
    if not isinstance(prediction, dict):
        return False
    if str(prediction.get("code") or "") not in _SCOREABLE_MARKETS:
        return False
    if prediction.get("side") not in {"H", "A", "L"}:
        return False
    raw_line = prediction.get("line")
    if raw_line is None:
        raw_line = prediction.get("condition")
    try:
        line = float(raw_line)
    except (TypeError, ValueError):
        return False
    return math.isfinite(line)


def _has_scoreable_market_prediction(row: dict[str, Any]) -> bool:
    """WDL-only / empty snapshots are not learning samples."""
    return any(
        _valid_market_prediction(prediction)
        for prediction in row.get("market_predictions") or []
    )


def normalize_history(history: dict[str, Any]) -> dict[str, Any]:
    """Purge non-market rows and keep later kickoffs at the top."""
    rows = []
    for row in history.get("rows") or []:
        if not isinstance(row, dict):
            continue
        row["market_predictions"] = [
            prediction for prediction in (row.get("market_predictions") or [])
            if _valid_market_prediction(prediction)
        ]
        if _has_scoreable_market_prediction(row):
            rows.append(row)
    for row in rows:
        if row.get("result_status") == "已核實":
            row["result_status"] = "已核對"

    def sort_key(row: dict[str, Any]) -> tuple[float, str, int]:
        kickoff = parse_time(row.get("kickoff"))
        return (
            kickoff.timestamp() if kickoff else float("-inf"),
            str(row.get("predicted_at") or ""),
            STAGES.get(str(row.get("stage") or ""), 0),
        )

    rows.sort(key=sort_key, reverse=True)
    history["rows"] = rows
    history["stats"] = calculate_stats(rows)
    return history


def load_history(config: Settings) -> dict[str, Any]:
    value = read_json(_path(config), {"rows": [], "stats": {}})
    if not isinstance(value, dict):
        value = {"rows": [], "stats": {}}
    # 2026-08-10 起重新建立乾淨市場學習樣本；舊 WDL-only 紀錄不混入。
    if value.get("prediction_era") != PREDICTION_ERA:
        value = {
            "prediction_era": PREDICTION_ERA,
            "schema_version": PREDICTION_SCHEMA_VERSION,
            "started_at": iso_hkt(),
            "rows": [],
            "stats": {},
        }
    value["rows"] = value.get("rows") if isinstance(value.get("rows"), list) else []
    value["stats"] = value.get("stats") if isinstance(value.get("stats"), dict) else {}
    return normalize_history(value)


def _history_row(watch: dict[str, Any], stage: dict[str, Any]) -> dict[str, Any]:
    match_id = str(stage.get("match_id") or watch.get("match_id") or "")
    stage_name = str(stage.get("stage") or "")
    pick = stage.get("pick") if isinstance(stage.get("pick"), dict) else None
    return {
        "_origin": "crown_ledger_v1",
        "prediction_era": PREDICTION_ERA,
        "schema_version": PREDICTION_SCHEMA_VERSION,
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
        "learning_snapshot_id": stage.get("learning_snapshot_id"),
        "learning_attempt": stage.get("learning_attempt"),
        "learning_pre_kickoff": stage.get("learning_pre_kickoff"),
        "learning_payload_sha256": stage.get("learning_payload_sha256"),
        "market_predictions": stage.get("market_predictions") or [],
        "market_grades": [],
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
            if not _has_scoreable_market_prediction(row):
                continue
            old = generated.get(row["history_key"])
            if old is None:
                rows.append(row)
                generated[row["history_key"]] = row
            else:
                result_fields = {
                    key: old.get(key)
                    for key in (
                        "actual", "score", "correct", "result_status", "verified_at",
                        "result_source", "market_grades", "result_detail",
                        "result_attempted_at", "result_missing_reason",
                    )
                }
                old.update(row)
                old.update({key: value for key, value in result_fields.items() if value is not None})
    normalize_history(history)
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


def _titan_event(row: dict[str, Any]) -> Event | None:
    kickoff = parse_time(row.get("kickoff"))
    if (
        not kickoff
        or not row.get("id")
        or not row.get("home")
        or not row.get("away")
        or row.get("home_score") is None
        or row.get("away_score") is None
    ):
        return None
    return Event(
        str(row["id"]),
        str(row.get("league") or ""),
        str(row["home"]),
        str(row["away"]),
        kickoff,
        row,
    )


def _match_titan_result(
    row: dict[str, Any],
    titan_by_id: dict[str, dict[str, Any]],
) -> tuple[dict[str, Any] | None, bool, bool]:
    """Return one strictly verified Titan result and its orientation.

    The stored fixture ID remains the preferred path.  Titan can replace an
    event ID after a schedule correction, so a unique identity fallback is
    allowed only when kickoff, both teams, league and qualifiers pass the
    existing strict matcher.
    """
    target = _target(row)
    if target is None:
        return None, False, False

    titan_id = str(row.get("titan_match_id") or row.get("match_id") or "")
    exact = titan_by_id.get(titan_id)
    exact_event = _titan_event(exact) if exact else None
    if exact_event:
        matched = match_event(
            target,
            [exact_event],
            team_key=canonical_team_key,
            league_key=canonical_league_key,
            allow_reversed=True,
            require_qualifiers=True,
        )
        if matched.event:
            return exact, matched.reversed, True

    candidates = [
        event
        for candidate in titan_by_id.values()
        if (event := _titan_event(candidate))
    ]
    matched = match_event(
        target,
        candidates,
        team_key=canonical_team_key,
        league_key=canonical_league_key,
        allow_reversed=True,
        require_qualifiers=True,
    )
    if not matched.event or not isinstance(matched.event.extra, dict):
        return None, False, False
    return matched.event.extra, matched.reversed, False


def _oriented_titan_result(result: dict[str, Any], reversed_order: bool) -> dict[str, Any]:
    if not reversed_order:
        return result
    oriented = dict(result)
    oriented["home_score"], oriented["away_score"] = (
        result.get("away_score"),
        result.get("home_score"),
    )
    oriented["corners_home"], oriented["corners_away"] = (
        result.get("corners_away"),
        result.get("corners_home"),
    )
    return oriented


def _terminal_titan_status(
    row: dict[str, Any],
    titan_by_id: dict[str, dict[str, Any]],
) -> str | None:
    """Accept a no-contest state only from the stored Titan ID plus identity."""
    target = _target(row)
    titan_id = str(row.get("titan_match_id") or row.get("match_id") or "")
    candidate = titan_by_id.get(titan_id)
    kickoff = parse_time((candidate or {}).get("kickoff"))
    if (
        target is None
        or not candidate
        or not kickoff
        or not is_non_result_terminal_status(candidate.get("status"))
    ):
        return None
    event = Event(
        titan_id,
        str(candidate.get("league") or ""),
        str(candidate.get("home") or ""),
        str(candidate.get("away") or ""),
        kickoff,
    )
    matched = match_event(
        target,
        [event],
        team_key=canonical_team_key,
        league_key=canonical_league_key,
        allow_reversed=True,
        require_qualifiers=True,
    )
    return str(candidate.get("status") or "") if matched.event else None


def _exclude_no_contest(row: dict[str, Any], status: str, source: str) -> None:
    row.update({
        "actual": None,
        "score": None,
        "correct": None,
        "result_status": "不計",
        "verified_at": iso_hkt(),
        "result_source": source,
        "result_detail": {"terminal_status": status},
        "market_grades": [
            {
                **prediction,
                "grade_status": "NOT_APPLICABLE",
                "reason": "fixture_not_played",
            }
            for prediction in (row.get("market_predictions") or [])
        ],
        "result_missing_reason": None,
    })


def _result(row: dict[str, Any], titan_by_id: dict[str, dict[str, Any]],
            hkjc_by_id: dict[str, dict[str, Any]], hkjc_events: list[tuple[Event, dict[str, Any]]]
            ) -> tuple[dict[str, Any] | None, str | None]:
    target = _target(row)
    if target is None:
        return None, None
    official = hkjc_by_id.get(str(row.get("hkjc_match_id") or ""))
    if official:
        return official, "hkjc_official_exact_id"
    matched = match_event(
        target, [event for event, _ in hkjc_events],
        allow_reversed=False, require_qualifiers=True,
    )
    if matched.event:
        return next(data for event, data in hkjc_events if event.id == matched.event.id), "hkjc_official_strict_identity"
    # HKJC remains authoritative during the normal result-publication window.
    # It occasionally leaves an otherwise completed fixture absent for hours;
    # after the grace period, allow the same strict Titan identity check used
    # for Crown-only rows rather than leaving both score and corners pending
    # forever.
    if row.get("hkjc_match_id"):
        age_seconds = (datetime.now(HKT) - target.kickoff).total_seconds()
        if age_seconds < _HKJC_RESULT_GRACE_SECONDS:
            return None, None
    titan, reversed_order, exact_id = _match_titan_result(row, titan_by_id)
    if titan:
        source = (
            "titan_verified_identity"
            if exact_id
            else "titan_verified_unique_identity_fallback"
        )
        if row.get("hkjc_match_id"):
            source = f"{source}_after_hkjc_grace"
        return (
            _oriented_titan_result(titan, reversed_order),
            source,
        )
    return None, None


def _merge_titan_corner_detail(
    row: dict[str, Any],
    score: dict[str, Any],
    source: str | None,
    titan_by_id: dict[str, dict[str, Any]],
    client: TitanClient,
) -> tuple[dict[str, Any], str | None, str]:
    """Fill corners only after exact Titan ID, identity and score checks."""
    if score.get("corners_total") is not None:
        return score, source, "already_present"
    titan, reversed_order, exact_id = _match_titan_result(row, titan_by_id)
    if not titan:
        return score, source, "titan_row_missing"
    titan_id = str(titan["id"])
    titan_home, titan_away = titan.get("home_score"), titan.get("away_score")
    if reversed_order:
        titan_home, titan_away = titan_away, titan_home
    try:
        verified_score = (int(score["home_score"]), int(score["away_score"]))
        titan_score = (int(titan_home), int(titan_away))
    except (KeyError, TypeError, ValueError):
        return score, source, "titan_score_invalid"
    if titan_score != verified_score:
        return score, source, "titan_score_mismatch"
    try:
        detail = client.result_detail(titan_id)
    except Exception as exc:
        return score, source, f"titan_detail_{type(exc).__name__}"
    if not detail or detail.get("corners_total") is None:
        return score, source, "titan_detail_corners_missing"
    corners_home = detail.get("corners_home")
    corners_away = detail.get("corners_away")
    if reversed_order:
        corners_home, corners_away = corners_away, corners_home
    merged = {
        **score,
        "corners_home": corners_home,
        "corners_away": corners_away,
        "corners_total": detail["corners_total"],
    }
    return (
        merged,
        (
            f"{source or 'verified_result'}+titan007_detail_"
            f"{'exact_id' if exact_id else 'unique_identity_fallback'}_identity_score"
        ),
        "filled",
    )


def _hkjc_event(row: dict[str, Any]) -> Event | None:
    kickoff = parse_time(row.get("kickoff"))
    if not kickoff or not row.get("home") or not row.get("away"):
        return None
    return Event(str(row["id"]), str(row.get("league") or ""), str(row["home"]), str(row["away"]), kickoff)


def _footbreak_results_by_hkjc_id() -> dict[str, dict[str, Any]]:
    """Reuse only Footbreak results joined by the exact HKJC match ID.

    Footbreak's settlement source includes corner statistics while HKJC's
    official result board commonly publishes only the final score.  The join
    is deliberately exact-ID only: no team-name or kickoff guess is permitted.
    """
    ledger_path = Path(os.getenv(
        "FOOTBREAK_LEDGER_PATH",
        "/opt/footbreak/system/sim_ledger.json",
    ))
    cache_dir = Path(os.getenv(
        "FOOTBREAK_RESULT_CACHE_DIR",
        "/opt/footbreak/system/cache/results",
    ))
    ledger = read_json(ledger_path, {})
    watch = ledger.get("watch") if isinstance(ledger, dict) else {}
    if not isinstance(watch, dict):
        return {}
    results: dict[str, dict[str, Any]] = {}
    for hkjc_id, item in watch.items():
        if not isinstance(item, dict):
            continue
        fixture_id = str(item.get("fixture_id") or "")
        if not fixture_id:
            continue
        cached = read_json(cache_dir / f"{fixture_id}.json", None)
        if not isinstance(cached, dict):
            continue
        try:
            home = int(cached["goals_home"])
            away = int(cached["goals_away"])
            corners = cached.get("corners_total")
            corners = int(corners) if corners is not None else None
        except (KeyError, TypeError, ValueError):
            continue
        results[str(hkjc_id)] = {
            "home_score": home,
            "away_score": away,
            "corners_total": corners,
            "source": "footbreak_result_cache_exact_hkjc_id",
        }
    return results


_SETTLEMENT_TARGET = {
    "Won": 1.0, "Half Won": 0.75, "Refunded": 0.5,
    "Half Lost": 0.25, "Lost": 0.0,
}


def _grade_market(prediction: dict[str, Any], result: dict[str, Any]) -> dict[str, Any]:
    code = str(prediction.get("code") or "")
    try:
        raw_line = prediction.get("line")
        if raw_line is None:
            raw_line = prediction.get("condition")
        line = float(raw_line)
        side = str(prediction["side"])
        probability = float(prediction["probability"])
        if not math.isfinite(line) or not math.isfinite(probability):
            raise ValueError("non-finite prediction")
        home, away = int(result["home_score"]), int(result["away_score"])
        if code == "HDC":
            status = settle_handicap(line, side, home, away)
        elif code == "HIL":
            status = settle_total(line, side, home, away)
        elif code == "CHL":
            corners = result.get("corners_total")
            if corners is None:
                return {**prediction, "grade_status": "NOT_APPLICABLE", "reason": "corners_result_missing"}
            status = settle_total(line, side, int(corners), 0)
        else:
            return {**prediction, "grade_status": "NOT_APPLICABLE", "reason": "unsupported_market"}
    except (KeyError, TypeError, ValueError):
        return {**prediction, "grade_status": "NOT_APPLICABLE", "reason": "invalid_prediction_or_result"}
    target = _SETTLEMENT_TARGET[status]
    p = min(.999999, max(.000001, probability))
    return {
        **prediction,
        "grade_status": "GRADED",
        "settlement": status,
        "target": target,
        "hit": None if status == "Refunded" else status in {"Won", "Half Won"},
        "brier": round((p - target) ** 2, 6),
        "log_loss": round(-(target * math.log(p) + (1 - target) * math.log(1 - p)), 6),
    }


def _persist_learning_result(row: dict[str, Any], score: dict[str, Any], source: str) -> None:
    path = os.environ.get("LEARNING_DB_PATH")
    snapshot_id = row.get("learning_snapshot_id")
    if not path or not snapshot_id:
        return
    # Loading the learning stack pulls in the numerical modelling
    # dependencies.  Keep it off the dashboard API startup path and import it
    # only when a newly verified result is actually persisted.
    from analysis.learning_store import LearningStore

    with LearningStore(path) as store:
        result = store.record_result(
            "crown",
            str(row.get("match_id")),
            home_score=score.get("home_score"),
            away_score=score.get("away_score"),
            terminal_status="finished",
            source=source,
            provenance={
                "hkjc_match_id": row.get("hkjc_match_id"),
                "titan_match_id": row.get("titan_match_id"),
                "pinnapi_event_id": row.get("pinnapi_event_id"),
                "corners_total": score.get("corners_total"),
            },
        )
        if row.get("forecast"):
            store.record_grade(
                int(snapshot_id),
                "WDL",
                str(row.get("forecast")),
                "GRADED",
                {
                    "hit": row.get("correct"),
                    "actual": row.get("actual"),
                    "score": row.get("score"),
                },
                result_id=result["result_id"],
            )
        for grade in row.get("market_grades") or []:
            store.record_grade(
                int(snapshot_id),
                str(grade.get("code") or "UNKNOWN"),
                f"{grade.get('condition')}|{grade.get('side')}",
                str(grade.get("grade_status") or "NOT_APPLICABLE"),
                grade,
                result_id=result["result_id"],
            )


def grade_history(config: Settings) -> dict[str, Any]:
    history = load_history(config)
    rows = history["rows"]
    now = datetime.now(HKT)
    def pending_corner_result(row: dict[str, Any], kickoff: datetime) -> bool:
        if not any(
            str(prediction.get("code") or "") == "CHL"
            for prediction in (row.get("market_predictions") or [])
            if isinstance(prediction, dict)
        ):
            return False
        grades = {
            str(grade.get("code") or ""): grade
            for grade in (row.get("market_grades") or [])
            if isinstance(grade, dict)
        }
        corner_grade = grades.get("CHL")
        if corner_grade and corner_grade.get("grade_status") == "GRADED":
            return False
        age = (now - kickoff).total_seconds()
        return age <= _CORNER_RESULT_RETRY_DAYS * 86400

    due = []
    for row in rows:
        kickoff = parse_time(row.get("kickoff"))
        if kickoff is None or (now - kickoff).total_seconds() < SETTLE_AFTER_SECONDS:
            continue
        unresolved_result = row.get("result_status") not in {"已核對", "不計"}
        if unresolved_result or pending_corner_result(row, kickoff):
            due.append(row)
    dates = {
        parse_time(row.get("kickoff")).strftime("%Y-%m-%d")
        for row in due if parse_time(row.get("kickoff"))
    }
    # HKJC's result board can file an after-midnight HKT fixture under the
    # preceding betting-business date.
    for raw in list(dates):
        dates.add((datetime.strptime(raw, "%Y-%m-%d") - timedelta(days=1)).strftime("%Y-%m-%d"))
    titan_error = None
    official_error = None
    titan_client = TitanClient(config)
    try:
        titan_rows = titan_client.results(dates) if due else []
    except Exception as exc:
        titan_error = type(exc).__name__
        titan_rows = []
    try:
        official_rows = fetch_official_result_events(dates) if due else []
    except Exception as exc:
        official_error = type(exc).__name__
        official_rows = []
    hkjc_ids = {
        str(row.get("hkjc_match_id") or "")
        for row in due if row.get("hkjc_match_id")
    }
    try:
        official_statuses = fetch_official_match_statuses(hkjc_ids, dates) if due else {}
    except Exception:
        official_statuses = {}
    titan_by_id = {str(row.get("id")): row for row in titan_rows}
    hkjc_by_id = {str(row.get("id")): row for row in official_rows}
    hkjc_events = [(event, row) for row in official_rows if (event := _hkjc_event(row))]
    footbreak_by_hkjc_id = _footbreak_results_by_hkjc_id()
    graded_now = 0
    excluded_now = 0
    corner_detail_reasons: Counter[str] = Counter()
    for row in due:
        hkjc_state = official_statuses.get(str(row.get("hkjc_match_id") or "")) or {}
        if is_non_result_terminal_status(
            hkjc_state.get("status"),
            refund_pools=hkjc_state.get("refund_pools"),
            payout_refund_pools=hkjc_state.get("payout_refund_pools"),
        ):
            _exclude_no_contest(
                row,
                str(hkjc_state.get("status") or "REFUNDED"),
                "hkjc_official_exact_id_terminal_status",
            )
            excluded_now += 1
            continue
        titan_status = _terminal_titan_status(row, titan_by_id)
        if titan_status:
            _exclude_no_contest(
                row, titan_status, "titan_exact_id_terminal_status"
            )
            excluded_now += 1
            continue
        score, source = _result(row, titan_by_id, hkjc_by_id, hkjc_events)
        hkjc_id = str(row.get("hkjc_match_id") or "")
        cached = footbreak_by_hkjc_id.get(hkjc_id)
        if score and score.get("corners_total") is None and cached and cached.get("corners_total") is not None:
            score = {**score, "corners_total": cached["corners_total"]}
            source = f"{source}+footbreak_corner_exact_hkjc_id"
        elif not score and cached:
            score = cached
            source = str(cached.get("source"))
        if not score:
            row["result_attempted_at"] = iso_hkt()
            if titan_error and official_error:
                row["result_missing_reason"] = (
                    f"result_sources_unavailable:titan={titan_error};hkjc={official_error}"
                )
            else:
                row["result_missing_reason"] = "no_verified_result_match"
            continue
        has_corner_prediction = any(
            str(prediction.get("code") or "") == "CHL"
            for prediction in (row.get("market_predictions") or [])
            if isinstance(prediction, dict)
        )
        if has_corner_prediction:
            score, source, corner_reason = _merge_titan_corner_detail(
                row, score, source, titan_by_id, titan_client
            )
            corner_detail_reasons[corner_reason] += 1
        try:
            home, away = int(score["home_score"]), int(score["away_score"])
        except (KeyError, TypeError, ValueError):
            continue
        actual = "主勝" if home > away else ("和局" if home == away else "客勝")
        row.update({
            "actual": actual,
            "score": f"{home}-{away}",
            "correct": (row.get("forecast") == actual) if row.get("forecast") else None,
            "result_status": "已核對",
            "verified_at": iso_hkt(),
            "result_source": source,
            "result_detail": {
                "home_score": home,
                "away_score": away,
                "corners_total": score.get("corners_total"),
            },
            "market_grades": [
                _grade_market(prediction, score)
                for prediction in (row.get("market_predictions") or [])
            ],
            "result_missing_reason": None,
        })
        _persist_learning_result(row, score, str(source))
        graded_now += 1
    history["result_sync"] = {
        "attempted_at": iso_hkt(),
        "due": len(due),
        "titan_rows": len(titan_rows),
        "hkjc_rows": len(official_rows),
        "footbreak_cached_rows": len(footbreak_by_hkjc_id),
        "titan_error": titan_error,
        "hkjc_error": official_error,
        "graded_now": graded_now,
        "excluded_now": excluded_now,
        "corner_detail": dict(sorted(corner_detail_reasons.items())),
        "unresolved": sum(
            row.get("result_status") not in {"已核對", "不計"}
            or any(
                grade.get("reason") == "corners_result_missing"
                for grade in (row.get("market_grades") or [])
                if isinstance(grade, dict)
            )
            for row in due
        ),
    }
    normalize_history(history)
    write_json_atomic(_path(config), history)
    return history


def _metrics(rows: list[dict[str, Any]]) -> dict[str, Any]:
    graded = [row for row in rows if row.get("actual") and row.get("forecast")]
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


def _market_metrics(rows: list[dict[str, Any]], code: str | None = None) -> dict[str, Any]:
    grades = [
        grade
        for row in rows
        for grade in (row.get("market_grades") or [])
        if grade.get("grade_status") == "GRADED"
        and (code is None or grade.get("code") == code)
    ]
    decided = [grade for grade in grades if grade.get("hit") is not None]
    return {
        "graded": len(grades),
        "decided": len(decided),
        "hits": sum(grade.get("hit") is True for grade in decided),
        "accuracy": (
            round(sum(grade.get("hit") is True for grade in decided) / len(decided), 6)
            if decided else None
        ),
        "brier": (
            round(sum(float(grade["brier"]) for grade in grades) / len(grades), 6)
            if grades else None
        ),
        "log_loss": (
            round(sum(float(grade["log_loss"]) for grade in grades) / len(grades), 6)
            if grades else None
        ),
    }


def calculate_stats(rows: list[dict[str, Any]]) -> dict[str, Any]:
    overall = _metrics(rows)
    stage_rows = {
        stage: [row for row in rows if row.get("stage") == stage]
        for stage in STAGES
    }
    by_stage = {stage: _metrics(stage_rows[stage]) for stage in STAGES}
    by_stage_market = {
        stage: {
            code: _market_metrics(stage_rows[stage], code)
            for code in ("HDC", "HIL", "CHL")
        }
        for stage in STAGES
    }
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
        "pending": sum(row.get("result_status") == "待賽果" for row in rows),
        **overall,
        "by_stage": by_stage,
        "by_stage_market": by_stage_market,
        "by_market": {
            code: _market_metrics(rows, code)
            for code in ("HDC", "HIL", "CHL")
        },
        "market_overall": _market_metrics(rows),
        "result_coverage": round(
            sum(row.get("result_status") == "已核對" for row in rows) / len(rows), 6
        ) if rows else None,
        "latest": latest,
        "learning_status": "collecting_market_level_shadow_samples",
        "minimum_sample_per_bucket": 30,
    }


def update_history(config: Settings, ledger: dict[str, Any] | None = None) -> dict[str, Any]:
    if ledger is not None:
        archive_watch(config, ledger)
    return grade_history(config)
