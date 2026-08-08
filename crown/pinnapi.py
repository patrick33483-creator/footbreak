"""PinnAPI Edge client and strict full-match parser used by Crown."""
from __future__ import annotations

import json
import time
import urllib.parse
import urllib.request
from datetime import datetime, timezone
from typing import Any

from .config import Settings
from .lines import is_quarter


def _record(value: Any) -> dict[str, Any] | None:
    return value if isinstance(value, dict) else None


def _num(value: Any) -> float | None:
    try:
        result = float(value)
        return result if result == result else None
    except (TypeError, ValueError):
        return None


def _timestamp(value: Any) -> float | None:
    number = _num(value)
    if number is not None:
        value = number * 1000 if number < 10_000_000_000 else number
        return value / 1000 if 1577836800000 <= value <= 4102444800000 else None
    try:
        return datetime.fromisoformat(str(value).replace("Z", "+00:00")).astimezone(timezone.utc).timestamp()
    except (TypeError, ValueError):
        return None


def _collect(value: Any, out: list[dict[str, Any]], depth: int = 0) -> None:
    if depth > 5:
        return
    if isinstance(value, list):
        for item in value:
            _collect(item, out, depth + 1)
    elif isinstance(value, dict):
        if any(key in value for key in ("event_id", "eventId")):
            out.append(value)
        for key in ("fixtures", "events", "data", "leagues", "league"):
            if key in value:
                _collect(value[key], out, depth + 1)


def _inplay(row: dict[str, Any]) -> bool:
    state = str(row.get("status") or row.get("event_status") or row.get("state") or "").lower()
    return bool(row.get("inplay") or row.get("is_live") or row.get("live") or row.get("live_status") == 1 or
                any(term in state for term in ("live", "inplay", "in-play", "started", "running")))


def parse_fixtures(payload: Any) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    _collect(payload, rows)
    parsed: list[dict[str, Any]] = []
    for row in rows:
        event_id = str(row.get("event_id") or row.get("eventId") or row.get("id") or "")
        kickoff = _timestamp(row.get("starts", row.get("start_ts")))
        home = str(row.get("home") or row.get("home_team") or row.get("homeTeam") or "")
        away = str(row.get("away") or row.get("away_team") or row.get("awayTeam") or "")
        league = str(row.get("league_name") or row.get("league") or row.get("leagueName") or "")
        if not (event_id and kickoff and home and away and league) or _inplay(row):
            continue
        periods = _record(row.get("periods")) or {}
        parsed.append({
            "id": event_id, "league": league, "home": home, "away": away, "kickoff": kickoff,
            "parent_id": str(row.get("parent_id") or row.get("parentId") or "") or None,
            "has_full_match": isinstance(periods.get("num_0"), dict),
        })
    # The parent/full-match fixture wins; duplicate exact event identities are removed.
    by_key: dict[tuple[str, str, str, int], dict[str, Any]] = {}
    for item in parsed:
        key = (item["league"].lower(), item["home"].lower(), item["away"].lower(), round(item["kickoff"] / 60))
        old = by_key.get(key)
        if old is None or (item["has_full_match"], item["parent_id"] is None) > (old["has_full_match"], old["parent_id"] is None):
            by_key[key] = item
    return [{key: value for key, value in item.items() if key != "has_full_match"} for item in by_key.values()]


def _values(value: Any) -> list[dict[str, Any]]:
    if isinstance(value, list):
        return [row for row in value if isinstance(row, dict)]
    return [row for row in (value or {}).values() if isinstance(row, dict)] if isinstance(value, dict) else []


def _decimal(value: Any) -> float | None:
    number = _num(value)
    return number if number is not None and number > 1 else None


def parse_lines(payload: Any, requested_event_id: str = "", observed_at: float | None = None) -> dict[str, Any]:
    root = _record(payload) or {}
    periods = _record(root.get("periods"))
    period = _record(periods.get("num_0")) if periods is not None else None
    if period is None and periods is None and any(key in root for key in ("moneyline", "money_line", "spreads", "totals")):
        period = root
    event_id = str(root.get("event_id") or root.get("eventId") or requested_event_id)
    observed_at = observed_at or time.time()
    source_at = _timestamp((period or {}).get("updated_at") or (period or {}).get("updatedAt") or
                           root.get("source_timestamp") or root.get("last") or root.get("updated_at"))
    inferred = source_at is None
    source_at = observed_at if source_at is None else source_at
    prices: list[dict[str, Any]] = []
    if not period:
        return {"event_id": event_id, "prices": prices, "source_at": source_at, "timestamp_inferred": inferred,
                "market_status": str(root.get("status") or "") or None}
    moneyline = _record(period.get("moneyline") or period.get("money_line") or period.get("1x2"))
    if moneyline:
        for selection, name in (("H", "home"), ("D", "draw"), ("A", "away")):
            odds = _decimal(moneyline.get(name, moneyline.get(f"{name}_odds")))
            if odds:
                prices.append({"market": "1X2", "line": None, "selection": selection, "odds": odds, "source_at": source_at})
    for index, spread in enumerate(_values(period.get("spreads") or period.get("handicaps"))):
        line = _num(spread.get("hdp", spread.get("handicap", spread.get("line"))))
        if line is None or not is_quarter(line):
            continue
        for selection, name in (("H", "home"), ("A", "away")):
            odds = _decimal(spread.get(name, spread.get(f"{name}_odds")))
            if odds:
                prices.append({"market": "HDC", "line": line, "selection": selection, "odds": odds, "source_at": source_at,
                               "main": bool(spread.get("is_main", spread.get("main", index == 0)))})
    for index, total in enumerate(_values(period.get("totals") or period.get("total"))):
        line = _num(total.get("points", total.get("total", total.get("line"))))
        if line is None or line < 0 or not is_quarter(line):
            continue
        for selection, name in (("H", "over"), ("L", "under")):
            odds = _decimal(total.get(name, total.get(f"{name}_odds")))
            if odds:
                prices.append({"market": "HIL", "line": line, "selection": selection, "odds": odds, "source_at": source_at,
                               "main": bool(total.get("is_main", total.get("main", index == 0)))})
    return {"event_id": event_id, "prices": prices, "source_at": source_at, "timestamp_inferred": inferred,
            "market_status": str(period.get("status") or root.get("status") or "") or None}


def parse_live_scores(payload: Any, observed_at: float | None = None) -> dict[str, dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    _collect(payload, rows)
    out: dict[str, dict[str, Any]] = {}
    for row in rows:
        state = _record(row.get("state")) or {}
        home, away = _record(state.get("home")) or {}, _record(state.get("away")) or {}
        match = _record(state.get("match")) or {}
        event_id = str(row.get("event_id") or row.get("eventId") or row.get("id") or "")
        minutes = _num(match.get("minutes", state.get("minutes", row.get("minutes"))))
        status = str(match.get("state") or match.get("status") or state.get("status") or row.get("status") or "")
        live = bool(state) or minutes is not None or bool(row.get("inplay") or row.get("live") or row.get("is_live")) or "live" in status.lower()
        hs, aws = _num(home.get("score", state.get("home_score"))), _num(away.get("score", state.get("away_score")))
        if event_id and live and hs is not None and aws is not None and hs >= 0 and aws >= 0 and hs.is_integer() and aws.is_integer():
            out[event_id] = {"home_score": int(hs), "away_score": int(aws), "minutes": int(minutes) if minutes is not None else None,
                             "state": status or None, "observed_at": observed_at or time.time()}
    return out


class PinnapiClient:
    def __init__(self, config: Settings):
        self.config = config

    def _get(self, path: str) -> Any:
        if not self.config.pinnapi_configured:
            raise RuntimeError("PinnAPI credentials are not configured")
        request = urllib.request.Request(
            f"{self.config.pinnapi_base_url}{path}",
            headers={
                "Accept": "application/json",
                "User-Agent": "node",
                "x-api-key": str(self.config.pinnapi_key),
            },
        )
        with urllib.request.urlopen(request, timeout=25) as response:
            return json.loads(response.read().decode("utf-8"))

    def fixtures(self) -> list[dict[str, Any]]:
        return parse_fixtures(self._get("/kit/v1/prematch/fixtures?sport_id=1"))

    def lines(self, event_id: str) -> dict[str, Any]:
        return parse_lines(self._get("/kit/v1/prematch/lines?event_id=" + urllib.parse.quote(event_id, safe="")), event_id)

    def live_scores(self) -> dict[str, dict[str, Any]]:
        return parse_live_scores(self._get("/kit/v1/markets?sport_id=1&event_type=live"))
