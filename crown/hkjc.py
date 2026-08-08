"""HKJC compatibility layer for canonical fixtures, CHL, and official results."""
from __future__ import annotations

import json
import sys
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
query matchResults($startDate: String, $endDate: String, $startIndex: Int, $endIndex: Int) {
  matches: matchResult(startDate: $startDate, endDate: $endDate, startIndex: $startIndex, endIndex: $endIndex) {
    id status matchDate kickOffTime
    homeTeam { id name_ch name_en } awayTeam { id name_ch name_en }
    results { homeResult awayResult ttlCornerResult payoutConfirmed stageId resultType sequence }
  }
}"""


def fetch_official_results(match_ids: set[str], dates: set[str]) -> dict[str, dict[str, Any]]:
    """Official fallback: exact HKJC match IDs only, confirmed full-match rows only."""
    if not match_ids or not dates:
        return {}
    endpoint = "https://info.cld.hkjc.com/graphql/base/"
    result: dict[str, dict[str, Any]] = {}
    for day in dates:
        body = json.dumps({"query": _RESULT_QUERY, "variables": {
            "startDate": day, "endDate": day, "startIndex": 0, "endIndex": 250,
        }}).encode()
        request = urllib.request.Request(endpoint, data=body, headers={
            "Content-Type": "application/json", "User-Agent": "Mozilla/5.0", "Referer": "https://bet.hkjc.com/",
        })
        with urllib.request.urlopen(request, timeout=30) as response:
            response_json = json.loads(response.read().decode("utf-8"))
        for match in ((response_json.get("data") or {}).get("matches") or []):
            match_id = str(match.get("id") or "")
            if match_id not in match_ids or str(match.get("status", "")).upper() not in {"FINISHED", "ENDED", "CLOSED"}:
                continue
            rows = [row for row in (match.get("results") or [])
                    if row.get("payoutConfirmed") is True and str(row.get("stageId")) == "5" and str(row.get("resultType")) == "1"]
            if not rows:
                continue
            row = max(rows, key=lambda item: int(item.get("sequence") or 0))
            try:
                result[match_id] = {"home_score": int(row["homeResult"]), "away_score": int(row["awayResult"]),
                                    "corners_total": int(row["ttlCornerResult"]) if row.get("ttlCornerResult") is not None else None,
                                    "source": "hkjc_official"}
            except (TypeError, ValueError, KeyError):
                continue
    return result
