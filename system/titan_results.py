"""Strict Titan007 corner-result fallback for Footbreak.

The HKJC official goal score remains authoritative.  Titan may fill only the
missing corner fields after a unique team/league/kickoff match and an exact
score cross-check.  No fuzzy result is ever written when identity is unclear.
"""
from __future__ import annotations

import json
import os
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

from crown.config import settings
from crown.matching import (
    Event,
    Match,
    canonical_league_key,
    canonical_team_key,
    match_event,
    qualifiers,
)
from crown.titan import TitanClient

HKT = timezone(timedelta(hours=8))
EXACT_TEAM_FALLBACK_TOLERANCE_SECONDS = 15 * 60


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


def load_crown_titan_match_map(path: Path | None = None) -> dict[str, str]:
    """Return unique HKJC -> Titan IDs already verified by Crown ingestion."""
    source = path or (settings().state_dir / "prediction_history.json")
    try:
        payload = json.loads(source.read_text(encoding="utf-8"))
    except (OSError, ValueError, TypeError):
        return {}
    if not isinstance(payload, dict):
        return {}
    candidates: dict[str, set[str]] = {}
    for row in payload.get("rows") or []:
        if not isinstance(row, dict):
            continue
        hkjc_id = str(row.get("hkjc_match_id") or "").strip()
        titan_id = str(row.get("titan_match_id") or "").strip()
        if hkjc_id and titan_id:
            candidates.setdefault(hkjc_id, set()).add(titan_id)
    return {
        hkjc_id: next(iter(titan_ids))
        for hkjc_id, titan_ids in candidates.items()
        if len(titan_ids) == 1
    }


def _exact_team_fallback(target: Event, candidates: list[Event]) -> Match:
    """Accept one exact reviewed team identity despite a small clock offset.

    This is result-only matching.  The caller still requires an exact official
    score cross-check before any corner statistic can be merged.
    """
    matches: list[tuple[Event, bool]] = []
    for candidate in candidates:
        if abs((candidate.kickoff - target.kickoff).total_seconds()) > EXACT_TEAM_FALLBACK_TOLERANCE_SECONDS:
            continue
        if qualifiers(target) != qualifiers(candidate):
            continue
        direct = (
            canonical_team_key(target.home) == canonical_team_key(candidate.home)
            and canonical_team_key(target.away) == canonical_team_key(candidate.away)
        )
        reversed_order = (
            canonical_team_key(target.home) == canonical_team_key(candidate.away)
            and canonical_team_key(target.away) == canonical_team_key(candidate.home)
        )
        if direct:
            matches.append((candidate, False))
        if reversed_order:
            matches.append((candidate, True))
    unique = {(event.id, reversed_order): event for event, reversed_order in matches}
    event_ids = {event_id for event_id, _ in unique}
    if len(event_ids) != 1 or len(unique) != 1:
        reason = "no_exact_team_candidate" if not unique else "ambiguous_exact_team_candidate"
        return Match(None, False, 0.0, reason)
    (event_id, reversed_order), event = next(iter(unique.items()))
    assert event.id == event_id
    return Match(event, reversed_order, 1.0, None)


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
    titan_id = str(record.get("titan_match_id") or "").strip()
    exact_id_candidate = next(
        (candidate for candidate in candidates if titan_id and candidate.id == titan_id),
        None,
    )
    exact_cross_source_id = exact_id_candidate is not None
    if exact_id_candidate:
        matched = Match(exact_id_candidate, False, 1.0, None)
    else:
        matched = match_event(
            target,
            candidates,
            team_key=canonical_team_key,
            league_key=canonical_league_key,
            allow_reversed=True,
            require_qualifiers=True,
        )
        if not matched.event:
            matched = _exact_team_fallback(target, candidates)
            if not matched.event:
                return result

    row = matched.event.extra or {}
    titan_home, titan_away = row.get("home_score"), row.get("away_score")
    if titan_home is None or titan_away is None:
        return result
    official_score = (result.get("goals_home"), result.get("goals_away"))
    if exact_cross_source_id and None not in official_score:
        direct_score = (titan_home, titan_away) == official_score
        reversed_score = (titan_away, titan_home) == official_score
        if not direct_score and not reversed_score:
            return result
        if reversed_score and not direct_score:
            matched = Match(matched.event, True, matched.score, matched.reason)
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
    suffix = (
        "titan007_match_detail_exact_cross_source_id_score"
        if exact_cross_source_id
        else "titan007_match_detail_strict_identity_score"
    )
    merged["source"] = f"{result.get('source') or 'official'}+{suffix}"
    merged["titan_id"] = matched.event.id
    return merged
