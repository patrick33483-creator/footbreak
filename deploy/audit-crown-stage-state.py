#!/usr/bin/env python3
"""Emit a bounded, redacted Crown stage-scheduling report without network I/O."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import sys
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

APP_DIR = Path(os.environ.get("FOOTBREAK_APP_DIR", "/opt/footbreak"))
if str(APP_DIR) not in sys.path:
    sys.path.insert(0, str(APP_DIR))

from crown.common import HKT, parse_time  # noqa: E402
from crown.ledger import PREDICTION_ERA, completed_stages, stage_for  # noqa: E402
from crown.matching import MATCHING_VERSION  # noqa: E402


ALLOWED_WINDOWS = {6, 12, 24}
ALLOWED_GRACE_MINUTES = {15, 30, 60}
ALLOWED_LIMITS = {25, 50, 100}
STAGE_FIELDS = ("stage", "status", "ts", "no_bet_reason")


def _load(path: Path, default: Any) -> tuple[Any, str | None]:
    try:
        with path.open(encoding="utf-8") as handle:
            value = json.load(handle)
    except FileNotFoundError:
        return default, "missing"
    except (OSError, json.JSONDecodeError) as exc:
        # Never include file contents or exception messages in an operational
        # report, because those can include private state from a damaged file.
        return default, type(exc).__name__
    return value, None


def _text(value: Any, limit: int = 240) -> str | None:
    value = str(value or "").strip()
    return value[:limit] if value else None


def _reason(value: Any) -> str | None:
    """Keep a short operational reason, never an accidental payload."""
    value = _text(value)
    if value is None:
        return None
    if (
        any(marker in value for marker in ("{", "}", "[", "]", "http://", "https://"))
        or re.search(r"(?i)(token|api[_-]?key|secret|password|authorization|bearer)\s*[:=]", value)
    ):
        return "[redacted_unstructured_reason]"
    return value


def _safe_timestamp(value: Any) -> str | None:
    parsed = parse_time(value)
    return parsed.astimezone(HKT).isoformat() if parsed is not None else None


def _first_timestamp(*values: Any) -> str | None:
    parsed = [parse_time(value) for value in values if parse_time(value) is not None]
    if not parsed:
        return None
    return min(parsed).astimezone(HKT).isoformat()


def _fixture_key(match_id: str, kickoff: datetime | None, home: Any, away: Any) -> str:
    material = "|".join((match_id, kickoff.isoformat() if kickoff else "", str(home or ""), str(away or "")))
    # Stable enough to correlate one report run to the next, while not exposing
    # provider fixture or team identifiers.
    return hashlib.sha256(material.encode("utf-8")).hexdigest()[:16]


def _stage_rows(watch: dict[str, Any]) -> list[dict[str, Any]]:
    rows = []
    for source in watch.get("stages") or []:
        if not isinstance(source, dict) or source.get("stage") not in {"首預", "T-30", "T-5"}:
            continue
        row = {key: _text(source.get(key)) for key in STAGE_FIELDS}
        row["ts"] = _safe_timestamp(source.get("ts"))
        row["no_bet_reason"] = _reason(source.get("no_bet_reason"))
        markets = [
            item for item in (source.get("market_predictions") or [])
            if isinstance(item, dict)
        ]
        valid_priced = 0
        for market in markets:
            try:
                odds = float(market.get("odds"))
                line = float(
                    market.get("line")
                    if market.get("line") is not None
                    else market.get("condition")
                )
            except (TypeError, ValueError):
                continue
            if (
                odds > 1
                and line == line
                and market.get("code") in {"HDC", "HIL", "CHL"}
                and market.get("side") in {"H", "A", "L"}
            ):
                valid_priced += 1
        rejection_reasons: dict[str, int] = {}
        for rejection in source.get("market_prediction_rejections") or []:
            if not isinstance(rejection, dict):
                continue
            reason = _reason(rejection.get("reason")) or "unspecified"
            rejection_reasons[reason] = rejection_reasons.get(reason, 0) + 1
        row["market_prediction_count"] = len(markets)
        row["valid_priced_market_count"] = valid_priced
        row["odds_status"] = _text(source.get("odds_status"))
        row["rejection_reason_counts"] = rejection_reasons
        rows.append(row)
    order = {"首預": 1, "T-30": 2, "T-5": 3}
    return sorted(rows, key=lambda row: order[row["stage"]])


def _merged_fixtures(
    predictions: list[dict[str, Any]], watch: dict[str, Any]
) -> dict[str, dict[str, Any]]:
    merged: dict[str, dict[str, Any]] = {}
    for card in predictions:
        if not isinstance(card, dict):
            continue
        match_id = _text(card.get("match_id"))
        if match_id:
            merged[match_id] = {"card": card, "watch": watch.get(match_id) or {}}
    for raw_id, item in watch.items():
        match_id = _text(raw_id)
        if match_id and isinstance(item, dict):
            merged.setdefault(match_id, {"card": {}, "watch": item})
    return merged


def _report_fixture(
    match_id: str,
    card: dict[str, Any],
    watch: dict[str, Any],
    history_keys: set[tuple[str, str]],
    now: datetime,
) -> dict[str, Any] | None:
    source = dict(watch)
    source.update({key: value for key, value in card.items() if value is not None})
    kickoff = parse_time(source.get("kickoff_hkt") or source.get("kickoff"))
    if kickoff is None:
        return None
    kickoff = kickoff.astimezone(HKT)
    stages = _stage_rows(watch)
    done = completed_stages(watch, MATCHING_VERSION, PREDICTION_ERA)
    first = next((row for row in stages if row["stage"] == "首預"), None)
    observed_at = _first_timestamp(
        watch.get("discovered_at"),
        card.get("discovered_at"),
        card.get("generated_at"),
        card.get("source_snapshot_at"),
        *(row.get("ts") for row in stages),
    )
    observed = parse_time(observed_at)
    if first is not None:
        should_have_run: bool | None = False
        first_reason = "first_look_recorded"
    elif kickoff <= now:
        should_have_run = False
        first_reason = "kickoff_passed_before_report"
    elif observed is None:
        should_have_run = None
        first_reason = "discovery_timestamp_unavailable"
    elif observed <= now:
        should_have_run = True
        first_reason = "fixture_known_pre_kickoff_first_look_missing"
    else:  # Defensive only: a malformed future timestamp must not become due.
        should_have_run = False
        first_reason = "discovery_timestamp_after_report"
    return {
        "fixture_key": _fixture_key(match_id, kickoff, source.get("home"), source.get("away")),
        "fixture": {
            "league": _text(source.get("league")),
            "home": _text(source.get("home")),
            "away": _text(source.get("away")),
            "kickoff_hkt": kickoff.isoformat(),
        },
        "discovery_timestamps": {
            "recorded_discovered_at": _safe_timestamp(watch.get("discovered_at") or card.get("discovered_at")),
            "card_generated_at": _safe_timestamp(card.get("generated_at")),
            "source_snapshot_at": _safe_timestamp(card.get("source_snapshot_at")),
            "first_stage_attempt_at": _first_timestamp(*(row.get("ts") for row in stages)),
        },
        "completed_stages": [stage for stage in ("首預", "T-30", "T-5") if stage in done],
        "stage_status": stages,
        "history_projection": {
            stage: (match_id, stage) in history_keys
            for stage in ("首預", "T-30", "T-5")
        },
        "latest_status": _text(card.get("status")),
        "latest_reason": _reason(card.get("no_bet_reason")),
        "first_look": {
            "recorded": first is not None,
            "completed": "首預" in done,
            "status": first.get("status") if first else None,
            "last_attempt_at": first.get("ts") if first else None,
            "should_have_run": should_have_run,
            "reason": first_reason,
        },
        "scheduler": {
            "minutes_to_kickoff": round((kickoff - now).total_seconds() / 60, 1),
            "next_due_stage": stage_for((kickoff - now).total_seconds() / 60, False, done),
        },
    }


def build_report(
    state_dir: Path,
    future_hours: int,
    current_grace_minutes: int,
    limit: int,
    now: datetime | None = None,
) -> dict[str, Any]:
    if future_hours not in ALLOWED_WINDOWS:
        raise ValueError("future_hours must be one of 6, 12, 24")
    if current_grace_minutes not in ALLOWED_GRACE_MINUTES:
        raise ValueError("current_grace_minutes must be one of 15, 30, 60")
    if limit not in ALLOWED_LIMITS:
        raise ValueError("limit must be one of 25, 50, 100")
    now = (now or datetime.now(HKT)).astimezone(HKT)
    raw_predictions, prediction_error = _load(state_dir / "predictions.json", [])
    raw_ledger, ledger_error = _load(state_dir / "ledger.json", {})
    raw_history, history_error = _load(
        state_dir / "prediction_history.json", {"rows": []}
    )
    predictions = raw_predictions if isinstance(raw_predictions, list) else []
    watches = (raw_ledger.get("watch") or {}) if isinstance(raw_ledger, dict) else {}
    watches = watches if isinstance(watches, dict) else {}
    history_rows = (
        raw_history.get("rows") or [] if isinstance(raw_history, dict) else []
    )
    history_keys = {
        (str(row.get("match_id") or ""), str(row.get("stage") or ""))
        for row in history_rows
        if isinstance(row, dict)
    }
    current_after = now - timedelta(minutes=current_grace_minutes)
    future_before = now + timedelta(hours=future_hours)
    fixtures = []
    for match_id, values in _merged_fixtures(predictions, watches).items():
        row = _report_fixture(
            match_id, values["card"], values["watch"], history_keys, now
        )
        if row is None:
            continue
        kickoff = parse_time(row["fixture"]["kickoff_hkt"])
        if kickoff is not None and current_after <= kickoff <= future_before:
            fixtures.append(row)
    fixtures.sort(key=lambda row: (row["fixture"]["kickoff_hkt"], row["fixture_key"]))
    return {
        "report": "crown_stage_status_v1",
        "read_only": True,
        "provider_requests": False,
        "generated_at_hkt": now.isoformat(),
        "scope": {
            "future_hours": future_hours,
            "current_grace_minutes": current_grace_minutes,
            "fixture_limit": limit,
            "raw_provider_ids_emitted": False,
            "provider_payloads_emitted": False,
        },
        "state": {
            "predictions": "available" if prediction_error is None else prediction_error,
            "ledger": "available" if ledger_error is None else ledger_error,
            "prediction_history": (
                "available" if history_error is None else history_error
            ),
            "fixtures_observed": len(fixtures),
            "fixtures_emitted": min(len(fixtures), limit),
        },
        "fixtures": fixtures[:limit],
    }


def _arguments() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--state-dir", type=Path, default=Path("/var/lib/footbreak/crown"))
    parser.add_argument("--future-hours", type=int, default=int(os.environ.get("CROWN_STAGE_FUTURE_HOURS", "12")))
    parser.add_argument("--current-grace-minutes", type=int, default=int(os.environ.get("CROWN_STAGE_CURRENT_GRACE_MINUTES", "30")))
    parser.add_argument("--limit", type=int, default=int(os.environ.get("CROWN_STAGE_FIXTURE_LIMIT", "50")))
    parser.add_argument("--now", default=None, help=argparse.SUPPRESS)
    return parser.parse_args()


def main() -> None:
    args = _arguments()
    now = parse_time(args.now) if args.now else None
    print(json.dumps(
        build_report(args.state_dir, args.future_hours, args.current_grace_minutes, args.limit, now),
        ensure_ascii=False,
        separators=(",", ":"),
    ))


if __name__ == "__main__":
    main()
