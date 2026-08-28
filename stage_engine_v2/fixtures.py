"""讀舊 crown data.json 抽出 fixture list。

Read-only。唔會呼叫任何 provider API，唔會 fetch 任何 HTTP。
舊 crown-sweep / crown-first-look-reconcile 已經負責抓賽事，v2 只做時機管理。
"""
from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import datetime, timezone, timedelta
from pathlib import Path
from typing import Any, Iterable

HKT = timezone(timedelta(hours=8))

DEFAULT_CROWN_DATA_PATH = Path("/var/www/crown/data.json")


@dataclass(frozen=True)
class Fixture:
    """v2 統一 fixture schema。所有時間用 UTC。"""

    id: str
    league: str
    home: str
    away: str
    kickoff_utc: datetime
    kickoff_hkt: datetime
    source: str  # "hkjc" / "pinnapi" / "titan" / "unknown"
    raw: dict[str, Any] = field(default_factory=dict, repr=False, compare=False)


def _parse_kickoff(value: Any) -> datetime | None:
    """接受 ISO string、'YYYY-MM-DD HH:MM'、或 datetime。返回 UTC。"""
    if value is None or value == "":
        return None
    if isinstance(value, datetime):
        dt = value
    elif isinstance(value, str):
        text = value.strip()
        # 常見格式：'2026-08-29 00:30' → HKT
        if len(text) == 16 and text[10] == " ":
            try:
                dt = datetime.strptime(text, "%Y-%m-%d %H:%M").replace(tzinfo=HKT)
            except ValueError:
                return None
        else:
            try:
                dt = datetime.fromisoformat(text.replace(" ", "T"))
            except ValueError:
                return None
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=HKT)
    else:
        return None
    return dt.astimezone(timezone.utc)


def _infer_source(match: dict[str, Any]) -> str:
    """從 match 記錄推斷源頭。純粹 tag 用途，唔會用嚟做決定。"""
    if match.get("hkjc_match_id") or match.get("hkjc_id"):
        return "hkjc"
    if match.get("pinnapi_event_id"):
        return "pinnapi"
    if match.get("titan_match_id"):
        return "titan"
    return "unknown"


def _extract_id(match: dict[str, Any]) -> str:
    """統一 fixture id：優先 native / hkjc / pinnapi / titan / match_id。"""
    for key in ("native_fixture_id", "hkjc_match_id", "pinnapi_event_id",
                "titan_match_id", "match_id", "id"):
        value = match.get(key)
        if value:
            return str(value)
    return ""


def parse_crown_matches(payload: dict[str, Any]) -> list[Fixture]:
    """從 crown data.json payload 抽出 Fixture list。"""
    matches = payload.get("matches") or []
    fixtures: list[Fixture] = []
    for match in matches:
        if not isinstance(match, dict):
            continue
        fx_id = _extract_id(match)
        if not fx_id:
            continue
        kickoff_utc = _parse_kickoff(
            match.get("kickoff_utc")
            or match.get("kickoff_hkt")
            or match.get("kickoff")
        )
        if kickoff_utc is None:
            continue
        fixtures.append(Fixture(
            id=fx_id,
            league=str(match.get("league") or ""),
            home=str(match.get("home") or ""),
            away=str(match.get("away") or ""),
            kickoff_utc=kickoff_utc,
            kickoff_hkt=kickoff_utc.astimezone(HKT),
            source=_infer_source(match),
            raw=match,
        ))
    return fixtures


def refresh_fixtures(
    *,
    crown_data_path: Path | str = DEFAULT_CROWN_DATA_PATH,
    window_hours: int = 48,
    now_utc: datetime | None = None,
) -> list[Fixture]:
    """讀 crown data.json，返回未來 window_hours 小時內嘅 fixture。"""
    path = Path(crown_data_path)
    if not path.exists():
        return []
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return []
    now = now_utc or datetime.now(timezone.utc)
    horizon = now + timedelta(hours=window_hours)
    fixtures = parse_crown_matches(payload)
    upcoming: list[Fixture] = []
    seen_ids: set[str] = set()
    for fx in fixtures:
        if fx.id in seen_ids:
            continue
        if fx.kickoff_utc <= now:
            continue
        if fx.kickoff_utc > horizon:
            continue
        seen_ids.add(fx.id)
        upcoming.append(fx)
    upcoming.sort(key=lambda f: f.kickoff_utc)
    return upcoming


__all__ = ["Fixture", "refresh_fixtures", "parse_crown_matches", "HKT"]
