"""Fast, identity-locked result sync for Stage Engine V2.

This is deliberately separate from Crown's full settlement/history pass.  It
fetches only the completed-result pages needed by due V2 fixtures, persists a
small match-level cache atomically, and projects scores onto V2 stages without
changing predictions, bets, or the legacy Crown history.
"""
from __future__ import annotations

import contextlib
import fcntl
import json
import os
import tempfile
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from crown.common import SETTLE_AFTER_SECONDS
from crown.config import settings
from crown.lines import settle_handicap, settle_total
from crown.matching import (
    Event,
    canonical_league_key,
    canonical_team_key,
    match_event,
)
from crown.titan import TitanClient

from .fixtures import _parse_kickoff
from .segmented_conditions import STAGES, _prediction


DEFAULT_AUTOMATIC_RESULTS_PATH = Path(
    "/var/lib/footbreak/stage_engine_v2/automatic_results.json"
)
SCHEMA_VERSION = 1
DEFAULT_LOOKBACK_DAYS = 7


def _empty_payload() -> dict[str, Any]:
    return {"schema_version": SCHEMA_VERSION, "results": {}}


def _load(path: Path) -> dict[str, Any]:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return _empty_payload()
    if (
        not isinstance(payload, dict)
        or payload.get("schema_version") != SCHEMA_VERSION
        or not isinstance(payload.get("results"), dict)
    ):
        raise ValueError("automatic result cache schema invalid")
    return payload


def _save(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    lock_path = path.with_suffix(path.suffix + ".lock")
    fd = os.open(str(lock_path), os.O_CREAT | os.O_RDWR, 0o600)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX)
        latest = _load(path)
        latest_results = latest.setdefault("results", {})
        for match_id, row in (payload.get("results") or {}).items():
            existing = latest_results.get(match_id)
            if existing and (
                existing.get("home_score"),
                existing.get("away_score"),
            ) != (row.get("home_score"), row.get("away_score")):
                raise ValueError(f"automatic result score conflict: {match_id}")
            latest_results[match_id] = row
        latest["updated_at_utc"] = datetime.now(timezone.utc).isoformat()
        out_fd, tmp = tempfile.mkstemp(
            dir=str(path.parent), prefix=".automatic-results-", suffix=".tmp"
        )
        try:
            with os.fdopen(out_fd, "w", encoding="utf-8") as handle:
                json.dump(latest, handle, ensure_ascii=False, indent=2, sort_keys=True)
                handle.flush()
                os.fsync(handle.fileno())
            os.chmod(tmp, 0o600)
            os.replace(tmp, path)
        except Exception:
            with contextlib.suppress(OSError):
                os.unlink(tmp)
            raise
    finally:
        fcntl.flock(fd, fcntl.LOCK_UN)
        os.close(fd)


def _target(slot: dict[str, Any]) -> Event | None:
    kickoff = _parse_kickoff(slot.get("kickoff_utc") or slot.get("kickoff_hkt"))
    if kickoff is None:
        return None
    return Event(
        str(slot.get("id") or ""),
        str(slot.get("league") or ""),
        str(slot.get("home") or ""),
        str(slot.get("away") or ""),
        kickoff,
    )


def _candidate(row: dict[str, Any]) -> Event | None:
    kickoff = row.get("kickoff")
    if not isinstance(kickoff, datetime):
        return None
    return Event(
        str(row.get("id") or ""),
        str(row.get("league") or ""),
        str(row.get("home") or ""),
        str(row.get("away") or ""),
        kickoff.astimezone(timezone.utc),
        row,
    )


def _match_result(
    slot: dict[str, Any],
    candidates: list[Event],
    exact_by_id: dict[str, Event],
) -> tuple[dict[str, Any], bool] | None:
    target = _target(slot)
    if target is None:
        return None
    exact = exact_by_id.get(str(slot.get("id") or ""))
    matched = match_event(
        target,
        [exact] if exact is not None else candidates,
        team_key=canonical_team_key,
        league_key=canonical_league_key,
        allow_reversed=True,
        require_qualifiers=True,
    )
    if not matched.event or not isinstance(matched.event.extra, dict):
        return None
    row = matched.event.extra
    if row.get("home_score") is None or row.get("away_score") is None:
        return None
    return row, matched.reversed


def sync_results(
    ledger: dict[str, Any],
    *,
    path: Path | str = DEFAULT_AUTOMATIC_RESULTS_PATH,
    now_utc: datetime | None = None,
    lookback_days: int = DEFAULT_LOOKBACK_DAYS,
    max_seconds: float = 30.0,
    client: TitanClient | None = None,
) -> dict[str, Any]:
    """Fetch and persist scores for recent due V2 fixtures only."""
    now = now_utc or datetime.now(timezone.utc)
    cache_path = Path(path)
    payload = _load(cache_path)
    known = payload.get("results") or {}
    fixtures = ledger.get("fixtures") or {}
    due: list[dict[str, Any]] = []
    oldest = now - timedelta(days=max(1, lookback_days))
    for slot in fixtures.values():
        if not isinstance(slot, dict) or str(slot.get("id") or "") in known:
            continue
        kickoff = _parse_kickoff(slot.get("kickoff_utc") or slot.get("kickoff_hkt"))
        if (
            kickoff is None
            or kickoff < oldest
            or (now - kickoff).total_seconds() < SETTLE_AFTER_SECONDS
        ):
            continue
        if not any(
            isinstance((slot.get("stages") or {}).get(stage), dict)
            for stage in STAGES
        ):
            continue
        due.append(slot)
    if not due:
        return {
            "ok": True,
            "due": 0,
            "fetched": 0,
            "settled_now": 0,
            "cached_total": len(known),
        }

    dates = {
        _parse_kickoff(slot.get("kickoff_utc") or slot.get("kickoff_hkt"))
        .astimezone(timezone(timedelta(hours=8)))
        .strftime("%Y-%m-%d")
        for slot in due
    }
    titan = client or TitanClient(settings())
    rows = titan.results(dates, max_seconds=max_seconds)
    # Building and normalising provider Events is relatively expensive.  The
    # first backfill can contain hundreds of due fixtures, so doing this inside
    # the per-fixture loop turns the pass into O(fixtures × provider rows).
    # Compile the candidate pool once and keep an exact native-id index.
    candidates = [event for row in rows if (event := _candidate(row)) is not None]
    exact_by_id = {event.id: event for event in candidates if event.id}
    additions: dict[str, dict[str, Any]] = {}
    verified_at = datetime.now(timezone.utc).isoformat()
    for slot in due:
        result = _match_result(slot, candidates, exact_by_id)
        if result is None:
            continue
        row, reversed_order = result
        home_score = int(row["away_score"] if reversed_order else row["home_score"])
        away_score = int(row["home_score"] if reversed_order else row["away_score"])
        match_id = str(slot["id"])
        additions[match_id] = {
            "match_id": match_id,
            "league": str(slot.get("league") or ""),
            "home": str(slot.get("home") or ""),
            "away": str(slot.get("away") or ""),
            "kickoff_utc": str(slot.get("kickoff_utc") or ""),
            "home_score": home_score,
            "away_score": away_score,
            "provider_event_id": str(row.get("id") or ""),
            "orientation": "reversed" if reversed_order else "direct",
            "source": "titan007_completed_results_identity_locked",
            "verified_at_utc": verified_at,
        }
    if additions:
        _save(cache_path, {"results": additions})
    return {
        "ok": True,
        "due": len(due),
        "fetched": len(rows),
        "settled_now": len(additions),
        "cached_total": len(known) + len(additions),
    }


def _grade(
    prediction: dict[str, Any],
    home_score: int,
    away_score: int,
) -> dict[str, Any]:
    code = str(prediction["code"])
    line = prediction.get("line")
    side = str(prediction["side"])
    if line is None or code not in {"HDC", "HIL"}:
        return {
            "code": code,
            "side": side,
            "line": line,
            "grade_status": "NOT_APPLICABLE",
            "reason": "line_missing" if line is None else "unsupported_market",
        }
    settlement = (
        settle_handicap(float(line), side, home_score, away_score)
        if code == "HDC"
        else settle_total(float(line), side, home_score, away_score)
    )
    return {
        "code": code,
        "side": side,
        "line": line,
        "odds": prediction.get("odds"),
        "grade_status": "GRADED",
        "settlement": settlement,
    }


def load_automatic_history_rows(
    path: Path | str,
    ledger: dict[str, Any],
) -> list[dict[str, Any]]:
    """Validate the cache against the current ledger and project stage grades."""
    results = (_load(Path(path)).get("results") or {})
    fixtures = ledger.get("fixtures") or {}
    output: list[dict[str, Any]] = []
    for match_id, result in results.items():
        slot = fixtures.get(match_id)
        if not isinstance(slot, dict) or not isinstance(result, dict):
            continue
        for key in ("league", "home", "away"):
            if str(result.get(key) or "") != str(slot.get(key) or ""):
                raise ValueError(f"automatic result {key} mismatch: {match_id}")
        kickoff = _parse_kickoff(result.get("kickoff_utc"))
        slot_kickoff = _parse_kickoff(slot.get("kickoff_utc") or slot.get("kickoff_hkt"))
        if kickoff is None or slot_kickoff is None or kickoff != slot_kickoff:
            raise ValueError(f"automatic result kickoff mismatch: {match_id}")
        home_score = int(result["home_score"])
        away_score = int(result["away_score"])
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
                "result_status": "已核對",
                "verified_at": result.get("verified_at_utc"),
                "result_source": result.get("source"),
                "result_detail": {
                    "home_score": home_score,
                    "away_score": away_score,
                    "provider_event_id": result.get("provider_event_id"),
                    "orientation": result.get("orientation"),
                },
                "market_grades": grades,
            })
    return output


def merge_automatic_history_rows(
    history_rows: list[dict[str, Any]],
    automatic_rows: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    """Prefer existing verified Crown rows and reject score disagreements."""
    verified: dict[tuple[str, str], str] = {}
    for row in history_rows:
        if row.get("result_status") not in {"已核對", "已核實"}:
            continue
        key = (str(row.get("match_id") or ""), str(row.get("stage") or ""))
        if all(key):
            verified[key] = str(row.get("score") or "")
    accepted = []
    for row in automatic_rows:
        key = (str(row.get("match_id") or ""), str(row.get("stage") or ""))
        existing = verified.get(key)
        if existing:
            if existing != str(row.get("score") or ""):
                raise ValueError(f"automatic result conflicts with Crown history: {key[0]}:{key[1]}")
            continue
        accepted.append(row)
    return [*history_rows, *accepted]


__all__ = [
    "DEFAULT_AUTOMATIC_RESULTS_PATH",
    "load_automatic_history_rows",
    "merge_automatic_history_rows",
    "sync_results",
]
