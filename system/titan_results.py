"""Strict Titan007 corner-result fallback for Footbreak.

The HKJC official goal score remains authoritative.  Titan may fill only the
missing corner fields after a unique team/league/kickoff match and an exact
score cross-check.  No fuzzy result is ever written when identity is unclear.
"""
from __future__ import annotations

import os
import sys
from datetime import datetime, timedelta, timezone
from typing import Any

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

from crown.config import settings
from crown.matching import (
    Event,
    canonical_league_key,
    canonical_team_key,
    match_event,
)
from crown.titan import TitanClient

HKT = timezone(timedelta(hours=8))


def _kickoff(record: dict[str, Any]) -> datetime | None:
    raw = record.get("kickoff") or record.get("kickoff_hkt")
    if not raw:
        return None
    try:
        parsed = datetime.fromisoformat(str(raw).replace("Z", "+00:00"))
    except ValueError:
        try:
            parsed = datetime.strptime(str(raw), "%Y-%m-%d %H:%M")
        except ValueError:
            return None
    return parsed.replace(tzinfo=HKT) if parsed.tzinfo is None else parsed.astimezone(HKT)


def fetch_titan_result_rows(client: TitanClient | None = None) -> tuple[TitanClient, list[dict[str, Any]]]:
    client = client or TitanClient(settings())
    return client, client.results()


def merge_titan_corners(
    result: dict[str, Any] | None,
    record: dict[str, Any],
    *,
    client: TitanClient,
    rows: list[dict[str, Any]],
) -> dict[str, Any] | None:
    """Fill missing corners only after strict identity and score validation."""
    if not result or result.get("corners_total") is not None:
        return result
    kickoff = _kickoff(record)
    home, away = record.get("home"), record.get("away")
    league = record.get("league") or ""
    if not kickoff or not home or not away:
        return result

    target = Event(str(record.get("match_id") or ""), str(league), str(home), str(away), kickoff)
    candidates = [
        Event(
            str(row.get("id") or ""),
            str(row.get("league") or ""),
            str(row.get("home") or ""),
            str(row.get("away") or ""),
            row["kickoff"],
            row,
        )
        for row in rows
        if row.get("id") and isinstance(row.get("kickoff"), datetime)
    ]
    matched = match_event(
        target,
        candidates,
        team_key=canonical_team_key,
        league_key=canonical_league_key,
        allow_reversed=True,
        require_qualifiers=True,
    )
    if not matched.event:
        return result

    row = matched.event.extra or {}
    titan_home, titan_away = row.get("home_score"), row.get("away_score")
    if titan_home is None or titan_away is None:
        return result
    aligned_home, aligned_away = (
        (titan_away, titan_home) if matched.reversed else (titan_home, titan_away)
    )
    if (
        result.get("goals_home") is not None
        and result.get("goals_away") is not None
        and (aligned_home, aligned_away)
        != (result.get("goals_home"), result.get("goals_away"))
    ):
        return result

    detail = client.result_detail(matched.event.id)
    if not detail or detail.get("corners_total") is None:
        return result
    corners_home, corners_away = detail["corners_home"], detail["corners_away"]
    if matched.reversed:
        corners_home, corners_away = corners_away, corners_home
    merged = dict(result)
    merged["corners_home"] = corners_home
    merged["corners_away"] = corners_away
    merged["corners_total"] = detail["corners_total"]
    merged["source"] = (
        f"{result.get('source') or 'official'}+"
        "titan007_match_detail_strict_identity_score"
    )
    merged["titan_id"] = matched.event.id
    return merged
