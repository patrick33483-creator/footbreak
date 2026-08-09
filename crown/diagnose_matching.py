"""Read-only production mapping diagnostics for Crown fixtures."""
from __future__ import annotations

import argparse
import json
from datetime import datetime
from pathlib import Path

from .common import HKT
from .config import settings
from .engine import _event_from_pinnapi, _event_from_titan
from .hkjc import event_from_match, fetch_matches
from .matching import (
    Event,
    bridge_titan_to_pinnapi,
    canonical_team_key,
    qualifiers,
    similarity,
)
from .pinnapi import PinnapiClient, parse_fixtures
from .titan import TitanClient


def _candidate(target: Event, candidate: Event) -> dict:
    direct_home = similarity(canonical_team_key(target.home), canonical_team_key(candidate.home))
    direct_away = similarity(canonical_team_key(target.away), canonical_team_key(candidate.away))
    reverse_home = similarity(canonical_team_key(target.home), canonical_team_key(candidate.away))
    reverse_away = similarity(canonical_team_key(target.away), canonical_team_key(candidate.home))
    direct = (direct_home + direct_away) / 2
    reverse = (reverse_home + reverse_away) / 2
    return {
        "id": candidate.id,
        "league": candidate.league,
        "home": candidate.home,
        "away": candidate.away,
        "kickoff": candidate.kickoff.isoformat(),
        "delta_min": round((candidate.kickoff - target.kickoff).total_seconds() / 60, 1),
        "team_score": round(max(direct, reverse), 3),
        "reversed": reverse > direct,
        "qualifiers": sorted(qualifiers(candidate)),
        "home_en": (candidate.extra or {}).get("home_en"),
        "away_en": (candidate.extra or {}).get("away_en"),
        "league_en": (candidate.extra or {}).get("league_en"),
    }


def _nearest(target: Event, candidates: list[Event], limit: int = 5) -> list[dict]:
    window = [
        candidate for candidate in candidates
        if abs((candidate.kickoff - target.kickoff).total_seconds()) <= 6 * 60 * 60
    ]
    rows = [_candidate(target, candidate) for candidate in window]
    rows.sort(key=lambda row: (-row["team_score"], abs(row["delta_min"])))
    return rows[:limit]


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--limit", type=int, default=60)
    parser.add_argument("--pinnapi-json", type=Path)
    args = parser.parse_args()
    config = settings()
    titan_rows = TitanClient(config).fixtures()
    if args.pinnapi_json:
        pinnapi_rows = parse_fixtures(json.loads(args.pinnapi_json.read_text(encoding="utf-8")))
    else:
        pinnapi_rows = PinnapiClient(config).fixtures()
    hkjc_rows = fetch_matches()
    hkjc_events = [event_from_match(row) for row in hkjc_rows]
    hkjc_events = [event for event in hkjc_events if event]
    pinnapi_events = [_event_from_pinnapi(row) for row in pinnapi_rows]
    now = datetime.now(HKT)
    output = []
    for row in titan_rows:
        event = _event_from_titan(row)
        if event.kickoff <= now:
            continue
        bridge = bridge_titan_to_pinnapi(event, hkjc_events, pinnapi_events)
        output.append({
            "titan": {
                "id": event.id,
                "league": event.league,
                "home": event.home,
                "away": event.away,
                "kickoff": event.kickoff.isoformat(),
                "qualifiers": sorted(qualifiers(event)),
            },
            "hkjc_nearest": _nearest(event, hkjc_events),
            "pinnapi_nearest": _nearest(event, pinnapi_events),
            "bridge": {
                "path": bridge.path,
                "reason": bridge.reason,
                "reversed": bridge.reversed,
                "hkjc_id": bridge.hkjc.event.id if bridge.hkjc.event else None,
                "hkjc_score": round(bridge.hkjc.score, 3),
                "pinnapi_id": bridge.event.id if bridge.event else None,
                "pinnapi_score": round(bridge.pinnapi.score, 3),
            },
        })
        if len(output) >= args.limit:
            break
    hkjc_coverage = []
    for event in hkjc_events:
        if event.kickoff <= now:
            continue
        hkjc_coverage.append({
            "hkjc": {
                "id": event.id,
                "league": event.league,
                "home": event.home,
                "away": event.away,
                "kickoff": event.kickoff.isoformat(),
                "home_en": (event.extra or {}).get("home_en"),
                "away_en": (event.extra or {}).get("away_en"),
                "league_en": (event.extra or {}).get("league_en"),
            },
            "titan_nearest": _nearest(event, [_event_from_titan(row) for row in titan_rows]),
            "pinnapi_nearest": _nearest(
                Event(
                    event.id,
                    str((event.extra or {}).get("league_en") or event.league),
                    str((event.extra or {}).get("home_en") or event.home),
                    str((event.extra or {}).get("away_en") or event.away),
                    event.kickoff,
                    event.extra,
                ),
                pinnapi_events,
            ),
        })
    print(json.dumps({
        "counts": {
            "titan": len(titan_rows),
            "hkjc": len(hkjc_events),
            "pinnapi": len(pinnapi_events),
            "diagnosed": len(output),
        },
        "fixtures": output,
        "hkjc_coverage": hkjc_coverage,
    }, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
