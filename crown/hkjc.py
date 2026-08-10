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


def _fetch_official_result_matches(dates: set[str]) -> list[dict[str, Any]]:
    """Return raw HKJC result-board matches, including non-finished states."""
    if not dates:
        return []
    endpoint = "https://info.cld.hkjc.com/graphql/base/"
    result: dict[str, dict[str, Any]] = {}
    failed_days: list[tuple[str, Exception]] = []
    for day in sorted(dates):
        last_error: Exception | None = None
        for attempt in range(2):
            try:
                day_rows: dict[str, dict[str, Any]] = {}
                for page in range(_RESULT_MAX_PAGES):
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
                    with urllib.request.urlopen(request, timeout=30) as response:
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
                    if not matches or start + len(matches) >= total:
                        break
                result.update(day_rows)
                last_error = None
                break
            except Exception as exc:
                last_error = exc
                if attempt == 0:
                    time.sleep(1.0)
        if last_error is not None:
            failed_days.append((day, last_error))
    # One bad date must not discard confirmed rows already fetched for other
    # dates. If every requested date failed, surface the source failure.
    if failed_days and len(failed_days) == len(dates):
        raise failed_days[-1][1]
    return list(result.values())


def fetch_official_match_statuses(
    match_ids: set[str], dates: set[str],
) -> dict[str, dict[str, Any]]:
    """Return exact-ID HKJC result-board statuses, including suspended/refunded."""
    if not match_ids or not dates:
        return {}
    out = {}
    for match in _fetch_official_result_matches(dates):
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


def fetch_official_result_events(dates: set[str]) -> list[dict[str, Any]]:
    """Return confirmed full-match HKJC result events with identity metadata."""
    result: dict[str, dict[str, Any]] = {}
    for match in _fetch_official_result_matches(dates):
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
        try:
            corners = int(row["ttlCornerResult"]) if row.get("ttlCornerResult") is not None else None
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
                # HKJC uses -1 as a missing-data sentinel, not a genuine count.
                "corners_total": corners if corners is not None and corners >= 0 else None,
                "source": "hkjc_official",
            }
        except (TypeError, ValueError, KeyError):
            continue
    return list(result.values())


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
