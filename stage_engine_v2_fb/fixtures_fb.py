"""Load Footbreak fixtures directly from the durable simulation ledger."""
from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from stage_engine_v2.fixtures import Fixture, HKT

DEFAULT_SOURCE_LEDGER = Path("/opt/footbreak/system/sim_ledger.json")


def _parse(value: Any) -> datetime | None:
    try:
        parsed = datetime.fromisoformat(str(value or "").replace("Z", "+00:00"))
    except (TypeError, ValueError):
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=HKT)
    return parsed.astimezone(timezone.utc)


def load_ledger_fixtures(
    path: Path | str = DEFAULT_SOURCE_LEDGER,
    *,
    now_utc: datetime,
    window_hours: int = 48,
    history_hours: int = 48,
) -> list[Fixture]:
    """Return recent and upcoming fixtures with their persisted stage rows."""
    try:
        payload = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return []
    watches = payload.get("watch")
    if not isinstance(watches, dict):
        return []

    earliest = now_utc - timedelta(hours=history_hours)
    latest = now_utc + timedelta(hours=window_hours)
    fixtures: list[Fixture] = []
    seen: set[str] = set()
    for raw_id, watch in watches.items():
        if not isinstance(watch, dict):
            continue
        manifest = watch.get("native_stage_manifest")
        identity = manifest.get("identity") if isinstance(manifest, dict) else {}
        match_id = str(
            watch.get("match_id")
            or (identity.get("hkjc_match_id") if isinstance(identity, dict) else "")
            or raw_id
        )
        if not match_id or match_id in seen:
            continue
        kickoff = _parse(
            watch.get("kickoff_hkt")
            or watch.get("kickoff")
            or watch.get("kickoff_utc")
            or (manifest.get("kickoff_at_hkt") if isinstance(manifest, dict) else None)
        )
        if kickoff is None or kickoff < earliest or kickoff > latest:
            continue
        seen.add(match_id)
        fixtures.append(Fixture(
            id=match_id,
            league=str(watch.get("league") or ""),
            home=str(watch.get("home") or ""),
            away=str(watch.get("away") or ""),
            kickoff_utc=kickoff,
            kickoff_hkt=kickoff.astimezone(HKT),
            source="hkjc",
            raw=watch,
        ))
    fixtures.sort(key=lambda fixture: fixture.kickoff_utc)
    return fixtures


__all__ = ["DEFAULT_SOURCE_LEDGER", "load_ledger_fixtures"]
