#!/usr/bin/env python3
"""Emit a bounded, redacted Crown stage-scheduling report without network I/O."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import sys
from collections import Counter
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


def _date_bucket(value: Any) -> str:
    parsed = parse_time(value)
    return parsed.astimezone(HKT).date().isoformat() if parsed is not None else "unparseable"


def _counter_rows(counter: Counter[str]) -> dict[str, int]:
    return {key: int(counter[key]) for key in sorted(counter)}


def _notification_silence_audit(
    ledger: dict[str, Any],
    state: dict[str, Any],
) -> dict[str, Any]:
    """Audit persisted Crown admissions and Telegram state, without network I/O.

    This function intentionally recomputes matcher outcomes from the immutable
    stored T-5 stages and frozen registry only.  It reads no results, invokes
    no provider, and does not save the in-memory ledger used for projection.
    """
    from analysis.granular_conditions import MARKETS
    from analysis.wilson_portfolio import _native_t5, _selected
    from analysis.wilson_validation import (
        DECISION_STAGE,
        formal_registry_candidates,
        match_formal_registry,
        matching_admissions,
    )
    from crown.notify import _observation_group_key

    validation = (
        ledger.get("wilson_validation")
        if isinstance(ledger.get("wilson_validation"), dict) else {}
    )
    conditions = (
        validation.get("conditions")
        if isinstance(validation.get("conditions"), dict) else {}
    )
    registry = formal_registry_candidates(ledger, "crown")
    activation = sorted({
        str((item.get("active_evidence") or {}).get("activation_boundary_at") or "")
        for item in conditions.values()
        if isinstance(item, dict)
    })
    activation = [value for value in activation if value]
    observations = [
        row for row in validation.get("observations") or []
        if isinstance(row, dict)
    ]
    bets = [
        row for row in ledger.get("bets") or []
        if isinstance(row, dict)
        and str(row.get("portfolio") or "") == "crown_wilson_test"
    ]
    acknowledged = {
        str(item) for item in state.get("wilson_match_alerts") or [] if str(item)
    }
    watches = ledger.get("watch") if isinstance(ledger.get("watch"), dict) else {}

    by_day = Counter()
    by_window = Counter()
    market_outcome = Counter()
    structural_by_market = Counter()
    formal_admissions_by_market = Counter()
    examples: list[dict[str, Any]] = []
    native_count = 0

    # The active v2 boundary is a stored immutable value.  For a malformed
    # registry with no unique boundary we still report all rows fail-closed,
    # rather than inferring a replacement boundary.
    boundary = parse_time(activation[0]) if len(activation) == 1 else None
    for raw_fixture, watch in sorted(watches.items(), key=lambda item: str(item[0])):
        if not isinstance(watch, dict):
            continue
        stages = [
            stage for stage in watch.get("stages") or []
            if isinstance(stage, dict) and stage.get("stage") == DECISION_STAGE
        ]
        for stage in stages:
            if not _native_t5(watch, stage, parse_time):
                continue
            stage_at = parse_time(stage.get("ts") or stage.get("source_snapshot_at"))
            if stage_at is None:
                continue
            native_count += 1
            by_day[_date_bucket(stage_at)] += 1
            if boundary is None:
                by_window["boundary_unavailable"] += 1
            elif stage_at < boundary:
                by_window["before_active_v2_boundary"] += 1
            else:
                by_window["at_or_after_active_v2_boundary"] += 1
            fixture = str(watch.get("match_id") or raw_fixture)
            stage_rows = [{
                "match_id": fixture,
                "stage": DECISION_STAGE,
                "kickoff": watch.get("kickoff") or watch.get("kickoff_hkt"),
                "predicted_at": str(stage.get("ts") or stage.get("source_snapshot_at") or ""),
                "market_predictions": stage.get("market_predictions") or [],
            }]
            matched = match_formal_registry(
                stage_rows, registry, system="crown", decision_stage=DECISION_STAGE,
            ).get(fixture, [])
            for market in MARKETS:
                selected, selection_reason = _selected(
                    stage, market, parse_time,
                    fixture_kickoff=watch.get("kickoff") or watch.get("kickoff_hkt"),
                )
                if selected is None:
                    market_outcome[f"{market}:{selection_reason or 'selection_invalid'}"] += 1
                    continue
                structural = [
                    item for item in matched
                    if str(item.get("market") or "") == market
                ]
                if structural:
                    structural_by_market[market] += 1
                admissions, admission_reason = matching_admissions(
                    "crown", market, selected, matched,
                    stage_at=str(stage.get("ts") or stage.get("source_snapshot_at") or ""),
                )
                if admissions:
                    formal_admissions_by_market[market] += 1
                    outcome = "formal_admission"
                else:
                    outcome = admission_reason or "no_admission"
                market_outcome[f"{market}:{outcome}"] += 1
                if len(examples) < 18:
                    examples.append({
                        "fixture": fixture,
                        "fixture_name": (
                            f"{str(watch.get('home') or '')} vs "
                            f"{str(watch.get('away') or '')}"
                        ).strip(),
                        "kickoff_hkt": _safe_timestamp(
                            watch.get("kickoff") or watch.get("kickoff_hkt")
                        ),
                        "native_t5_at_hkt": _safe_timestamp(
                            stage.get("ts") or stage.get("source_snapshot_at")
                        ),
                        "market": market,
                        "structural_frozen_match": bool(structural),
                        "matcher_outcome": outcome,
                    })

    durable_rows = bets + observations
    formal_rows = [
        row for row in durable_rows
        if row.get("formal_bet") is not False
        and str(row.get("portfolio") or "") == "crown_wilson_test"
    ]
    low_odds = [
        row for row in observations
        if row.get("formal_bet") is False
    ]
    eligible = []
    for row in durable_rows:
        row_id = str(row.get("bet_id") or row.get("observation_id") or "")
        if row_id and row_id not in acknowledged and (
            row.get("bet_id") or _observation_group_key(row) is not None
        ):
            eligible.append(row_id)

    notification_keys = {
        key: len(value) if isinstance(value, list) else None
        for key, value in state.items()
        if key in {
            "signals", "corner_t5", "bets", "wilson_bets",
            "wilson_match_alerts", "bilateral_decision_alerts",
        }
    }
    legacy_signals = state.get("signals") if isinstance(state.get("signals"), list) else []
    legacy_formats = Counter()
    for item in legacy_signals:
        text = str(item)
        if "|three-stage-v1|" in text:
            legacy_formats["three_stage_signal"] += 1
        elif "|T-5|" in text:
            legacy_formats["other_t5_signal"] += 1
        else:
            legacy_formats["other"] += 1

    return {
        "read_only": True,
        "provider_requests": False,
        "results_read_or_used": False,
        "frozen_registry": {
            "loaded_entries": len(registry),
            "coverage_by_market": _counter_rows(Counter(
                str(item.get("market") or "") for item in registry
            )),
            "active_evidence_boundaries": activation,
        },
        "native_t5": {
            "count": native_count,
            "by_hkt_day": _counter_rows(by_day),
            "by_registry_version_window": _counter_rows(by_window),
        },
        "recomputed_matcher": {
            "structural_frozen_matches_by_market": _counter_rows(structural_by_market),
            "formal_admissions_by_market": _counter_rows(formal_admissions_by_market),
            "outcomes_by_market_and_reason": _counter_rows(market_outcome),
            "representative_rows": examples,
        },
        "durable_formal_lifecycle": {
            "formal_rows": len(formal_rows),
            "low_odds_observations": len(low_odds),
            "eligible_unacknowledged_outbox_rows": len(eligible),
            "acknowledged_wilson_ids": len(acknowledged),
        },
        "historical_notification_state": {
            "state_updated_at": state.get("updated_at"),
            "key_counts": notification_keys,
            "legacy_signal_policy_formats": _counter_rows(legacy_formats),
            "legacy_signal_samples": [
                str(item)[:180] for item in legacy_signals[:4]
            ],
        },
    }


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
    raw_notify, notify_error = _load(state_dir / "notify_state.json", {})
    predictions = raw_predictions if isinstance(raw_predictions, list) else []
    watches = (raw_ledger.get("watch") or {}) if isinstance(raw_ledger, dict) else {}
    watches = watches if isinstance(watches, dict) else {}
    ledger = raw_ledger if isinstance(raw_ledger, dict) else {}
    notify_state = raw_notify if isinstance(raw_notify, dict) else {}
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
        # Keep the incident evidence before the bounded fixture list so GitHub
        # log viewers retain it even if a long list is display-truncated.
        "notification_silence_audit": _notification_silence_audit(
            ledger, notify_state,
        ),
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
            "notify_state": "available" if notify_error is None else notify_error,
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
