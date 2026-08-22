#!/usr/bin/env python3
"""Emit a bounded, redacted Crown stage-scheduling report without network I/O."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

APP_DIR = Path(os.environ.get("FOOTBREAK_APP_DIR", "/opt/footbreak"))
if str(APP_DIR) not in sys.path:
    sys.path.insert(0, str(APP_DIR))

from crown.common import HKT, parse_time  # noqa: E402
from crown.ledger import PREDICTION_ERA, completed_stages, stage_for, stages_due  # noqa: E402
from crown.matching import MATCHING_VERSION  # noqa: E402


ALLOWED_WINDOWS = {6, 12, 24}
ALLOWED_GRACE_MINUTES = {15, 30, 60}
ALLOWED_LIMITS = {25, 50, 100}
STAGE_FIELDS = ("stage", "status", "ts", "no_bet_reason", "odds_status", "odds_reason")
SNAPSHOT_PREDICTION_FIELDS = (
    "status", "verdict", "pick", "lead_view", "forecast", "prediction_source",
)


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
        row["odds_reason"] = _reason(source.get("odds_reason"))
        rows.append(row)
    order = {"首預": 1, "T-30": 2, "T-5": 3}
    return sorted(rows, key=lambda row: order[row["stage"]])


def _number(value: Any) -> float | int | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        return value
    try:
        return float(str(value))
    except (TypeError, ValueError):
        return None


def _binding_key(value: Any) -> str | None:
    """Correlate native fixture identity without emitting raw provider IDs."""
    text = _text(value)
    if text is None:
        return None
    return hashlib.sha256(text.encode("utf-8")).hexdigest()[:16]


def _snapshot_rows(
    watch: dict[str, Any],
    match_id: str,
) -> dict[str, dict[str, Any]]:
    """Emit compact immutable native stage evidence; never raw board payloads."""
    snapshots: dict[str, dict[str, Any]] = {}
    watch_native = _binding_key(
        watch.get("native_fixture_id") or watch.get("titan_match_id") or match_id
    )
    for source in watch.get("stages") or []:
        if not isinstance(source, dict):
            continue
        stage = _text(source.get("stage"))
        if stage not in {"首預", "T-30", "T-5"}:
            continue
        candidates = []
        for row in source.get("market_predictions") or []:
            if not isinstance(row, dict):
                continue
            candidates.append({
                "prediction": _text(
                    row.get("prediction") or row.get("pick") or row.get("verdict")
                ),
                "market": _text(row.get("code") or row.get("market")),
                "side": _text(row.get("side") or row.get("selection")),
                "line": _number(row.get("line")),
                "odds": _number(row.get("odds")),
                "source": _text(row.get("quote_source") or row.get("source")),
                "observed_at": _safe_timestamp(
                    row.get("observed_at") or row.get("source_at")
                ),
            })
        raw_snapshot_native = (
            source.get("native_fixture_id")
            or source.get("titan_match_id")
            or source.get("match_id")
        )
        snapshot_native = _binding_key(raw_snapshot_native)
        snapshots[stage] = {
            "status": _text(source.get("status")),
            "ts": _safe_timestamp(source.get("ts")),
            "prediction": {
                key: _text(source.get(key))
                for key in SNAPSHOT_PREDICTION_FIELDS
                if _text(source.get(key)) is not None
            },
            "odds_status": _text(source.get("odds_status")),
            "odds_reason": _reason(source.get("odds_reason")),
            "native_fixture_key": snapshot_native,
            "same_watch_native_fixture": (
                snapshot_native is not None and snapshot_native == watch_native
            ),
            "selected_markets": candidates[:24],
        }
    return snapshots


def _stage_attempts(watch: dict[str, Any]) -> dict[str, dict[str, Any]]:
    source = watch.get("stage_attempts")
    if not isinstance(source, dict):
        return {}
    output: dict[str, dict[str, Any]] = {}
    for stage in ("首預", "T-30", "T-5"):
        attempt = source.get(stage)
        if not isinstance(attempt, dict):
            continue
        output[stage] = {
            "state": _text(attempt.get("state")),
            "retry_count": attempt.get("retry_count") if isinstance(attempt.get("retry_count"), int) else None,
            "started_at": _safe_timestamp(attempt.get("started_at")),
            "updated_at": _safe_timestamp(attempt.get("updated_at")),
            "reason": _reason(attempt.get("reason")),
        }
    return output


def _stage_jobs(watch: dict[str, Any], now: datetime) -> dict[str, dict[str, Any]]:
    source = watch.get("stage_jobs")
    if not isinstance(source, dict):
        return {}
    kickoff = parse_time(watch.get("kickoff_utc") or watch.get("kickoff_hkt") or watch.get("kickoff"))
    output: dict[str, dict[str, Any]] = {}
    for stage in ("T-30", "T-5"):
        job = source.get(stage)
        if not isinstance(job, dict):
            continue
        due_at = parse_time(job.get("due_at_utc"))
        output[stage] = {
            "state": _text(job.get("state")),
            "due_at_utc": due_at.astimezone(timezone.utc).isoformat() if due_at else None,
            "due_at_hkt": due_at.astimezone(HKT).isoformat() if due_at else None,
            "due_now_pre_kickoff": bool(
                due_at is not None
                and due_at <= now
                and kickoff is not None
                and now < kickoff
            ),
            "retry_count": job.get("retry_count") if isinstance(job.get("retry_count"), int) else None,
            "reason": _reason(job.get("reason")),
        }
    return output


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
    minutes_to_kickoff = (kickoff - now).total_seconds() / 60
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
        "native_fixture_key": _binding_key(
            watch.get("native_fixture_id") or watch.get("titan_match_id") or match_id
        ),
        "immutable_snapshots": _snapshot_rows(watch, match_id),
        "stage_attempts": _stage_attempts(watch),
        "stage_jobs": _stage_jobs(watch, now),
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
            "minutes_to_kickoff": round(minutes_to_kickoff, 1),
            "next_due_stage": stage_for(minutes_to_kickoff, False, done),
            "next_due_stages": stages_due(minutes_to_kickoff, False, done),
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
    predictions = raw_predictions if isinstance(raw_predictions, list) else []
    watches = (raw_ledger.get("watch") or {}) if isinstance(raw_ledger, dict) else {}
    watches = watches if isinstance(watches, dict) else {}
    current_after = now - timedelta(minutes=current_grace_minutes)
    future_before = now + timedelta(hours=future_hours)
    fixtures = []
    for match_id, values in _merged_fixtures(predictions, watches).items():
        row = _report_fixture(match_id, values["card"], values["watch"], now)
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
