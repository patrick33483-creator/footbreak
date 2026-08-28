"""Check that durable timed-stage snapshots are present in a dashboard JSON."""
from __future__ import annotations

import argparse
import json
import sys
from collections import Counter
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any


TIMED_STAGES = frozenset({"T-30", "T-5"})
HKT = timezone(timedelta(hours=8))
FOOTBREAK_DONE_AFTER = timedelta(minutes=130)
ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))


def _rows(value: Any, *, name: str) -> list[dict[str, Any]]:
    if not isinstance(value, list):
        raise ValueError(f"{name} is not a list")
    if not all(isinstance(row, dict) for row in value):
        raise ValueError(f"{name} contains an invalid row")
    return value


def _time(value: Any) -> datetime | None:
    try:
        parsed = datetime.fromisoformat(str(value or "").replace("Z", "+00:00"))
    except (TypeError, ValueError):
        return None
    return parsed.replace(tzinfo=HKT) if parsed.tzinfo is None else parsed


def _should_be_public(
    watch: dict[str, Any], *, system: str, now: datetime,
) -> bool:
    kickoff = _time(
        watch.get("kickoff_at_utc")
        or watch.get("kickoff_utc")
        or watch.get("kickoff_hkt")
        or watch.get("kickoff")
    )
    if kickoff is None:
        # Without a usable identity the builders cannot safely recover a card.
        return False
    if system == "crown":
        from crown.period import in_current_period
        return in_current_period(kickoff, now)
    return kickoff.astimezone(now.tzinfo) + FOOTBREAK_DONE_AFTER > now


def _footbreak_recoverable_without_existing_card(
    watch: dict[str, Any],
) -> bool:
    """Whether Footbreak can reconstruct this fixture from ledger alone."""
    if any(
        not str(watch.get(field) or "").strip()
        for field in ("home", "away", "league")
    ):
        return False
    return True


def _committed_timed_stages(watch: dict[str, Any]) -> set[str] | None:
    """Return timed snapshots durably present in the fixture ledger.

    Both systems atomically save a stage row as the durable capture. Crown also
    records COMMITTED stage_jobs, while Footbreak records native attempt events;
    the row is the common ledger authority consumed by both dashboard builders.
    DATA_MISSING rows are audit outcomes rather than captured snapshots.
    """
    committed_rows: list[str] = []
    for row in _rows(watch.get("stages", []), name="watch stages"):
        stage = str(row.get("stage") or "")
        if stage in TIMED_STAGES and str(row.get("status") or "") != "DATA_MISSING":
            committed_rows.append(stage)
    # An authoritative duplicate is ledger corruption, not a state that a
    # dashboard rebuild can safely normalize or hide.
    if any(count > 1 for count in Counter(committed_rows).values()):
        return None
    return set(committed_rows)


def projection_is_current(
    ledger: dict[str, Any], dashboard: dict[str, Any], *,
    system: str = "footbreak", now: datetime | None = None,
) -> bool:
    watches = ledger.get("watch")
    if not isinstance(watches, dict):
        raise ValueError("ledger watch state missing")

    public_rows = _rows(dashboard.get("matches"), name="dashboard matches")
    public_by_id: dict[str, dict[str, Any]] = {}
    for card in public_rows:
        match_id = str(card.get("match_id") or "").strip()
        if not match_id:
            continue
        if match_id in public_by_id:
            return False
        public_by_id[match_id] = card

    checked_at = now or datetime.now(timezone.utc)
    if checked_at.tzinfo is None:
        raise ValueError("checked_at timezone missing")
    for raw_id, watch in watches.items():
        if not isinstance(watch, dict):
            raise ValueError("ledger watch row invalid")
        match_id = str(watch.get("match_id") or raw_id).strip()
        if system == "crown":
            from crown.dashboard_data import (
                _dashboard_watch_card,
                _native_watch_stage_contract,
            )
            reference_card = public_by_id.get(match_id)
            if reference_card is None:
                reference_card = _dashboard_watch_card(watch, now=checked_at)
            # Use the builder's exact identity, kickoff, timestamp, status, and
            # duplicate validation rather than approximating Crown completion.
            snapshots, ambiguous = (
                _native_watch_stage_contract(reference_card, watch)
                if isinstance(reference_card, dict) else ({}, False)
            )
            if ambiguous:
                return False
            required: set[str] | None = set(snapshots).intersection(TIMED_STAGES)
        else:
            required = _committed_timed_stages(watch)
        if required is None:
            return False
        if not required:
            continue
        card = public_by_id.get(match_id)
        if card is None:
            # Historical fixtures intentionally hidden by the matching
            # dashboard contract cannot make a successful rebuild look stale.
            if (
                _should_be_public(watch, system=system, now=checked_at)
                and (
                    reference_card is not None
                    if system == "crown"
                    else _footbreak_recoverable_without_existing_card(watch)
                )
            ):
                return False
            continue
        public_stages = [
            str(row.get("stage") or "")
            for row in _rows(card.get("stages", []), name="dashboard stages")
        ]
        # A repair must not perpetuate duplicate fixture stages.
        if any(public_stages.count(stage) != 1 for stage in required):
            return False
    return True


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--ledger", type=Path, required=True)
    parser.add_argument("--dashboard", type=Path, required=True)
    parser.add_argument("--system", choices=("footbreak", "crown"), required=True)
    args = parser.parse_args()
    try:
        ledger = json.loads(args.ledger.read_text(encoding="utf-8"))
        dashboard = json.loads(args.dashboard.read_text(encoding="utf-8"))
        if not isinstance(ledger, dict) or not isinstance(dashboard, dict):
            raise ValueError("JSON root must be an object")
        return 0 if projection_is_current(
            ledger, dashboard, system=args.system,
        ) else 1
    except (OSError, TypeError, ValueError, json.JSONDecodeError):
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
