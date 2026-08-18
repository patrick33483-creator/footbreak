"""HKJC compatibility layer for canonical fixtures, CHL, and official results."""
from __future__ import annotations

import json
import sys
import time
import urllib.request
from datetime import timedelta
from pathlib import Path
from typing import Any

from .common import HKT, parse_time
from .matching import Event

_SYSTEM = Path(__file__).resolve().parents[1] / "system"
if str(_SYSTEM) not in sys.path:
    sys.path.insert(0, str(_SYSTEM))


def fetch_matches() -> list[dict[str, Any]]:
    """Reuse Footbreak's public HKJC reader without sharing its state or secrets."""
    import hkjc_feed  # Imported lazily so parser/unit tests remain offline.
    today = hkjc_feed.now_hkt().strftime("%Y-%m-%d")
    tomorrow = (hkjc_feed.now_hkt() + timedelta(days=2)).strftime("%Y-%m-%d")
    return hkjc_feed.fetch_matches(today, tomorrow, odds_types=["HDC", "HIL", "CHL"])


def event_from_match(match: dict[str, Any]) -> Event | None:
    kickoff = parse_time(match.get("kickOffTime") or match.get("matchDate"))
    home, away, tournament = match.get("homeTeam") or {}, match.get("awayTeam") or {}, match.get("tournament") or {}
    match_id = str(match.get("id") or match.get("frontEndId") or "")
    if not (kickoff and match_id and home.get("name_ch") and away.get("name_ch")):
        return None
    return Event(match_id, str(tournament.get("name_ch") or tournament.get("name_en") or ""),
                 str(home["name_ch"]), str(away["name_ch"]), kickoff,
                 {"home_team_id": home.get("id"), "away_team_id": away.get("id"),
                  "home_en": home.get("name_en"), "away_en": away.get("name_en"),
                  "league_en": tournament.get("name_en"), "raw": match})


def flatten_odds(match: dict[str, Any]) -> dict[str, list[dict[str, Any]]]:
    import hkjc_feed
    return hkjc_feed.flatten_odds(match)


_RESULT_QUERY = """
query matchResults($startDate: String, $endDate: String, $startIndex: Int,$endIndex: Int,$teamId: String) {
    timeOffset {
    fb
    }
    matchNumByDate(startDate: $startDate, endDate: $endDate, teamId: $teamId) {
    total
    }
    matches: matchResult(startDate: $startDate, endDate: $endDate, startIndex: $startIndex,endIndex: $endIndex, teamId: $teamId) {
    id
    status
    frontEndId
    matchDayOfWeek
    matchNumber
    matchDate
    kickOffTime
    sequence
    homeTeam {
        id
        name_en
        name_ch
    }
    awayTeam {
        id
        name_en
        name_ch
    }
    tournament {
        code
        name_en
        name_ch
    }
    results {
        homeResult
        awayResult
        ttlCornerResult
        resultConfirmType
        payoutConfirmed
        stageId
        resultType
        sequence
    }
    poolInfo {
        payoutRefundPools
        refundPools
        ntsInfo
        entInfo
        definedPools
        ngsInfo {
        str
        name_en
        name_ch
        instNo
        }
        agsInfo {
        str
        name_en
        name_ch
        }
    }
    }
}
"""

_ENDED_STATUSES = {"MATCHENDED", "INPLAYMATCHENDED", "FINISHED", "ENDED", "CLOSED"}
_RESULT_PAGE_SIZE = 20
_RESULT_MAX_PAGES = 20


def _fetch_official_result_matches(
    dates: set[str],
    *,
    target_ids: set[str] | None = None,
    max_seconds: float | None = None,
    request_timeout: float = 30.0,
    attempts: int = 2,
) -> list[dict[str, Any]]:
    """Return raw HKJC result-board matches, including non-finished states."""
    if not dates:
        return []
    wanted = {str(value) for value in (target_ids or set()) if value}
    deadline = time.monotonic() + max_seconds if max_seconds is not None else None
    endpoint = "https://info.cld.hkjc.com/graphql/base/"
    result: dict[str, dict[str, Any]] = {}
    failed_days: list[tuple[str, Exception]] = []
    for day in sorted(dates):
        if deadline is not None and time.monotonic() >= deadline:
            failed_days.append((day, TimeoutError("HKJC result pass budget exhausted")))
            break
        last_error: Exception | None = None
        for attempt in range(max(1, attempts)):
            day_rows: dict[str, dict[str, Any]] = {}
            try:
                for page in range(_RESULT_MAX_PAGES):
                    remaining = (
                        deadline - time.monotonic()
                        if deadline is not None else request_timeout
                    )
                    if remaining <= 0:
                        raise TimeoutError("HKJC result pass budget exhausted")
                    start = page * _RESULT_PAGE_SIZE
                    body = json.dumps({"query": _RESULT_QUERY, "variables": {
                        "startDate": day,
                        "endDate": day,
                        "startIndex": start,
                        "endIndex": start + _RESULT_PAGE_SIZE,
                        "teamId": None,
                    }}).encode()
                    request = urllib.request.Request(endpoint, data=body, headers={
                        "Content-Type": "application/json",
                        "Accept": "application/json",
                        "Origin": "https://bet.hkjc.com",
                        "User-Agent": "Mozilla/5.0",
                        "Referer": "https://bet.hkjc.com/",
                    })
                    with urllib.request.urlopen(
                        request,
                        timeout=max(0.1, min(request_timeout, remaining)),
                    ) as response:
                        raw = response.read()
                    if raw[:2] == b"\x1f\x8b":
                        import gzip
                        raw = gzip.decompress(raw)
                    response_json = json.loads(raw.decode("utf-8"))
                    if response_json.get("errors"):
                        raise RuntimeError(str(response_json["errors"])[:400])
                    data = response_json.get("data") or {}
                    matches = data.get("matches") or []
                    for match in matches:
                        match_id = str(match.get("id") or "")
                        if match_id:
                            day_rows[match_id] = match
                    total = int(((data.get("matchNumByDate") or {}).get("total")) or 0)
                    if (
                        (wanted and wanted.issubset(set(result) | set(day_rows)))
                        or not matches
                        or start + len(matches) >= total
                    ):
                        break
                result.update(day_rows)
                last_error = None
                break
            except Exception as exc:
                last_error = exc
                # Confirmed rows already received before the cutoff remain
                # usable. Missing target IDs stay unresolved and retry later.
                result.update(day_rows)
                if attempt + 1 < max(1, attempts):
                    if deadline is not None and time.monotonic() + 1.0 >= deadline:
                        break
                    time.sleep(1.0)
        if last_error is not None:
            failed_days.append((day, last_error))
        if wanted and wanted.issubset(result):
            break
    # One bad date must not discard confirmed rows already fetched for other
    # dates. If every requested date failed, surface the source failure.
    if not result and failed_days and len(failed_days) == len(dates):
        raise failed_days[-1][1]
    return list(result.values())


def _official_match_statuses(
    match_ids: set[str], matches: list[dict[str, Any]],
) -> dict[str, dict[str, Any]]:
    out = {}
    for match in matches:
        match_id = str(match.get("id") or "")
        if match_id not in match_ids:
            continue
        pool_info = match.get("poolInfo") or {}
        out[match_id] = {
            "status": str(match.get("status") or "").upper(),
            "refund_pools": list(pool_info.get("refundPools") or []),
            "payout_refund_pools": list(pool_info.get("payoutRefundPools") or []),
            "source": "hkjc_official",
        }
    return out


def _official_result_events(matches: list[dict[str, Any]]) -> list[dict[str, Any]]:
    result: dict[str, dict[str, Any]] = {}
    for match in matches:
        match_id = str(match.get("id") or "")
        if not match_id or str(match.get("status", "")).upper() not in _ENDED_STATUSES:
            continue
        rows = [
            row for row in (match.get("results") or [])
            if row.get("payoutConfirmed") is True
            and str(row.get("stageId")) == "5"
            and str(row.get("resultType")) == "1"
        ]
        if not rows:
            continue
        row = max(rows, key=lambda item: int(item.get("sequence") or 0))
        # HKJC's detailed-results page stores corners as a separate market
        # result stream (resultType=2).  ttlCornerResult on the score stream
        # is normally -1, so reading only resultType=1 silently loses every
        # otherwise published corner result.
        corner_rows = [
            item for item in (match.get("results") or [])
            if item.get("payoutConfirmed") is True
            and str(item.get("stageId")) == "5"
            and str(item.get("resultType")) == "2"
        ]
        corner_row = (
            max(corner_rows, key=lambda item: int(item.get("sequence") or 0))
            if corner_rows else None
        )
        try:
            corners = None
            if corner_row is not None:
                corner_home = int(corner_row["homeResult"])
                corner_away = int(corner_row["awayResult"])
                if corner_home >= 0 and corner_away >= 0:
                    corners = corner_home + corner_away
            if corners is None and row.get("ttlCornerResult") is not None:
                legacy_corners = int(row["ttlCornerResult"])
                corners = legacy_corners if legacy_corners >= 0 else None
            home_team = match.get("homeTeam") or {}
            away_team = match.get("awayTeam") or {}
            tournament = match.get("tournament") or {}
            result[match_id] = {
                "id": match_id,
                "front_end_id": str(match.get("frontEndId") or "") or None,
                "league": str(tournament.get("name_ch") or tournament.get("name_en") or ""),
                "home": str(home_team.get("name_ch") or home_team.get("name_en") or ""),
                "away": str(away_team.get("name_ch") or away_team.get("name_en") or ""),
                "kickoff": match.get("kickOffTime") or match.get("matchDate"),
                "home_score": int(row["homeResult"]),
                "away_score": int(row["awayResult"]),
                "corners_home": corner_home if corner_row is not None else None,
                "corners_away": corner_away if corner_row is not None else None,
                "corners_total": corners,
                "source": "hkjc_official",
            }
        except (TypeError, ValueError, KeyError):
            continue
    return list(result.values())


def fetch_official_match_statuses(
    match_ids: set[str], dates: set[str],
) -> dict[str, dict[str, Any]]:
    """Return exact-ID HKJC result-board statuses, including suspended/refunded."""
    if not match_ids or not dates:
        return {}
    return _official_match_statuses(
        match_ids,
        _fetch_official_result_matches(dates),
    )


def fetch_official_result_events(dates: set[str]) -> list[dict[str, Any]]:
    """Return confirmed full-match HKJC result events with identity metadata."""
    return _official_result_events(_fetch_official_result_matches(dates))


def fetch_official_settlement_bundle(
    match_ids: set[str],
    dates: set[str],
    *,
    max_seconds: float = 60.0,
) -> tuple[dict[str, dict[str, Any]], dict[str, dict[str, Any]]]:
    """Fetch one bounded exact-ID snapshot for both results and statuses."""
    if not match_ids or not dates:
        return {}, {}
    matches = _fetch_official_result_matches(
        dates,
        target_ids=match_ids,
        max_seconds=max_seconds,
        request_timeout=8.0,
        attempts=1,
    )
    results = {
        row["id"]: row
        for row in _official_result_events(matches)
        if row["id"] in match_ids
    }
    return results, _official_match_statuses(match_ids, matches)


def fetch_official_results(match_ids: set[str], dates: set[str]) -> dict[str, dict[str, Any]]:
    """Official fallback: exact HKJC match IDs only, confirmed full-match rows only."""
    if not match_ids or not dates:
        return {}
    return {
        row["id"]: {
            "home_score": row["home_score"],
            "away_score": row["away_score"],
            "corners_total": row.get("corners_total"),
            "source": row["source"],
        }
        for row in fetch_official_result_events(dates)
        if row["id"] in match_ids
    }
