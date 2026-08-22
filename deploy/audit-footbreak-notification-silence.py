#!/usr/bin/env python3
"""Bounded, provider-free Footbreak notification-silence audit.

This diagnostic reads only durable local files and systemd/journal metadata.
It never invokes a prediction, provider client, notification sender, repair,
or state writer.  The output deliberately contains aggregate counts and a
small representative fixture list rather than provider payloads or secrets.
"""
from __future__ import annotations

import argparse
import json
import subprocess
import sys
from collections import Counter
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any


ROOT = Path("/opt/footbreak")
SYSTEM = ROOT / "system"
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
if str(SYSTEM) not in sys.path:
    sys.path.insert(0, str(SYSTEM))

from crown.common import HKT, parse_time  # noqa: E402


LEDGER_PATH = SYSTEM / "sim_ledger.json"
NOTIFY_PATH = SYSTEM / "notify_state.json"
EVIDENCE_PATH = Path("/var/lib/footbreak/crown/footbreak-execution-evidence.json")
STAGES = ("首預", "T-30", "T-5")
MAX_REPRESENTATIVES = 24


def _read(path: Path, default: Any) -> Any:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError, json.JSONDecodeError):
        return default
    return value if isinstance(value, type(default)) else default


def _time(value: Any) -> datetime | None:
    try:
        parsed = parse_time(value)
    except (TypeError, ValueError, OverflowError):
        return None
    return parsed.astimezone(HKT) if parsed is not None else None


def _within(value: Any, start: datetime, end: datetime) -> bool:
    parsed = _time(value)
    return parsed is not None and start <= parsed < end


def _idset(value: Any) -> set[str]:
    return {str(item) for item in value or [] if str(item)}


def _row_at(row: dict[str, Any]) -> Any:
    return row.get("created_at") or row.get("decision_at") or row.get("ts")


def _native_stage_rows(ledger: dict[str, Any]) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    watches = ledger.get("watch") if isinstance(ledger.get("watch"), dict) else {}
    for match_id, watch in watches.items():
        if not isinstance(watch, dict):
            continue
        kickoff = watch.get("kickoff") or watch.get("kickoff_hkt")
        for stage in watch.get("stages") or []:
            if not isinstance(stage, dict) or stage.get("stage") not in STAGES:
                continue
            rows.append({
                "match_id": str(match_id),
                "fixture": f"{str(watch.get('home') or '')} vs {str(watch.get('away') or '')}".strip(),
                "league": str(watch.get("league") or ""),
                "kickoff": kickoff,
                "stage": stage.get("stage"),
                "at": stage.get("ts") or stage.get("source_snapshot_at"),
                "status": stage.get("status") or stage.get("verdict"),
                "reason": stage.get("no_bet_reason"),
            })
    return rows


def _native_rows(ledger: dict[str, Any]) -> tuple[list[dict[str, Any]], list[dict[str, Any]], list[dict[str, Any]]]:
    validation = ledger.get("wilson_validation") if isinstance(ledger.get("wilson_validation"), dict) else {}
    bets = [
        row for row in ledger.get("bets") or []
        if isinstance(row, dict)
        and row.get("portfolio") == "footbreak_wilson_test"
    ]
    observations = [
        row for row in validation.get("observations") or []
        if isinstance(row, dict)
        and row.get("portfolio") == "footbreak_wilson_observations"
        and row.get("bet_status") == "NO_BET_LOW_ODDS"
    ]
    audit = [row for row in validation.get("audit") or [] if isinstance(row, dict)]
    return bets, observations, audit


def _cross_rows(ledger: dict[str, Any]) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    namespace = ledger.get("footbreak_crown_execution_test")
    namespace = namespace if isinstance(namespace, dict) else {}
    decisions = [row for row in namespace.get("decisions") or [] if isinstance(row, dict)]
    outbox = [row for row in namespace.get("decision_outbox") or [] if isinstance(row, dict)]
    return decisions, outbox


def _historically_messageable(row: dict[str, Any]) -> bool:
    """Whether a native record was intentionally notification-classed.

    This is a structural policy classification for historical rows.  It does
    not call the live formatter, because the formatter correctly rejects a
    fixture after kickoff and would turn past delivery evidence into a false
    negative.
    """
    if row.get("portfolio") == "footbreak_wilson_test":
        return str(row.get("strategy") or "") == "wilson-test-strategy-v1"
    return (
        row.get("portfolio") == "footbreak_wilson_observations"
        and row.get("bet_status") == "NO_BET_LOW_ODDS"
    )


def _decision_for_native(row: dict[str, Any], decisions: list[dict[str, Any]]) -> dict[str, Any] | None:
    signature = str(row.get("frozen_condition_signature") or "")
    fixture = str(row.get("match_id") or "")
    market = str(row.get("market") or row.get("code") or "").upper()
    for decision in decisions:
        if (
            decision.get("system") == "footbreak"
            and str(decision.get("fixture") or "") == fixture
            and str(decision.get("market") or "").upper() == market
            and str(decision.get("condition_signature") or "") == signature
        ):
            return decision
    return None


def _window_summary(
    *, ledger: dict[str, Any], state: dict[str, Any], start: datetime, end: datetime,
) -> dict[str, Any]:
    stages = _native_stage_rows(ledger)
    bets, observations, audit = _native_rows(ledger)
    decisions, outbox = _cross_rows(ledger)
    wilson_ack = _idset(state.get("wilson_match_alerts"))
    bilateral_ack = _idset(state.get("bilateral_decision_alerts"))
    cross_ack = _idset(state.get("crown_execution_test_alerts"))
    stage_counts = Counter(
        str(row.get("stage"))
        for row in stages if _within(row.get("at"), start, end)
    )
    structural = [
        row for row in audit
        if _within(row.get("ts"), start, end)
        and row.get("status") in {"CREATED", "MATCHED_NO_BET"}
    ]
    window_bets = [row for row in bets if _within(_row_at(row), start, end)]
    window_observations = [row for row in observations if _within(_row_at(row), start, end)]
    window_native = window_bets + window_observations
    window_decisions = [row for row in decisions if _within(_row_at(row), start, end)]
    decision_ids = {str(row.get("decision_id") or "") for row in window_decisions}
    window_outbox = [
        row for row in outbox
        if str(row.get("decision_id") or "") in decision_ids
    ]
    native_direct_ack = sum(
        str(row.get("bet_id") or row.get("observation_id") or "") in wilson_ack
        for row in window_native if _historically_messageable(row)
    )
    native_via_bilateral = 0
    native_missing_delivery_path = 0
    for row in window_bets:
        decision = _decision_for_native(row, decisions)
        if decision is None:
            continue
        did = str(decision.get("decision_id") or "")
        if did in bilateral_ack:
            native_via_bilateral += 1
    for row in window_native:
        identity = str(row.get("bet_id") or row.get("observation_id") or "")
        decision = _decision_for_native(row, decisions) if row.get("bet_id") else None
        if not identity or not _historically_messageable(row):
            native_missing_delivery_path += 1
        elif identity not in wilson_ack and (decision is None or str(decision.get("decision_id") or "") not in bilateral_ack):
            native_missing_delivery_path += 1
    decision_by_id = {str(row.get("decision_id") or ""): row for row in decisions}
    pending_bilateral = [
        row for row in outbox
        if row.get("notification_required")
        and str(row.get("decision_id") or "") not in bilateral_ack
    ]
    pending_cross = [
        row for row in (ledger.get("footbreak_crown_execution_test") or {}).get("bets") or []
        if isinstance(row, dict)
        and str(row.get("bet_id") or "")
        and str(row.get("bet_id") or "") not in cross_ack
        and row.get("status") == "PENDING"
    ]
    return {
        "window_hkt": {"start": start.isoformat(), "end": end.isoformat()},
        "native_stages": {stage: stage_counts.get(stage, 0) for stage in STAGES},
        "native_t5": stage_counts.get("T-5", 0),
        "formal_structural_matches": len(structural),
        "formal_bets": len(window_bets),
        "low_odds_observations": len(window_observations),
        "x20_lifecycle": {
            "settled_formal_rows": sum(str(row.get("status") or "") == "SETTLED" for row in window_bets),
            "settled_low_odds_observations": sum(str(row.get("status") or "") == "SETTLED" for row in window_observations),
        },
        "native_wilson_outbox": {
            "notification_classed_rows": sum(_historically_messageable(row) for row in window_native),
            "direct_acknowledged": native_direct_ack,
            "acknowledged_via_bilateral": native_via_bilateral,
            "without_ack_or_bilateral_ack": native_missing_delivery_path,
        },
        "footbreak_to_crown_execution_outbox": {
            "decisions": len(window_decisions),
            "decisions_counterpart_available": sum(
                str(row.get("counterpart_status") or "") == "AVAILABLE"
                for row in window_decisions
            ),
            "outbox_rows": len(window_outbox),
            "outbox_acknowledged": sum(
                str(row.get("decision_id") or "") in bilateral_ack
                for row in window_outbox
            ),
            "currently_unacknowledged_all_windows": len(pending_bilateral),
            "execution_bets_currently_unacknowledged": len(pending_cross),
        },
        "delivery_integrity": {
            "native_formal_with_bilateral_decision": sum(
                _decision_for_native(row, decisions) is not None for row in window_bets
            ),
            "native_formal_without_bilateral_decision": sum(
                _decision_for_native(row, decisions) is None for row in window_bets
            ),
            "bilateral_decision_catalogued": len(decision_by_id),
        },
    }


def _bridge_summary(ledger: dict[str, Any], now: datetime) -> dict[str, Any]:
    watch = ledger.get("watch") if isinstance(ledger.get("watch"), dict) else {}
    counts: dict[str, Counter[str]] = {key: Counter() for key in ("first_look", "t30", "t5")}
    upcoming = 0
    for row in watch.values():
        if not isinstance(row, dict):
            continue
        kickoff = _time(row.get("kickoff") or row.get("kickoff_hkt"))
        if kickoff is None or kickoff <= now:
            continue
        upcoming += 1
        bridge = ((row.get("counterpart_bridges") or {}).get("crown") or {})
        for key in counts:
            value = bridge.get(key)
            if not isinstance(value, dict):
                counts[key]["missing"] += 1
            elif str(value.get("status") or "") == "RESOLVED":
                counts[key]["resolved"] += 1
            else:
                counts[key][str(value.get("reason") or "unavailable")] += 1
    return {
        "upcoming_cards": upcoming,
        "first_look": dict(sorted(counts["first_look"].items())),
        "t30": dict(sorted(counts["t30"].items())),
        "t5": dict(sorted(counts["t5"].items())),
    }


def _sidecar_summary(now: datetime) -> dict[str, Any]:
    data = _read(EVIDENCE_PATH, [])
    cards = data if isinstance(data, list) else []
    boards = Counter()
    quote_status = Counter()
    fresh_t5 = 0
    for card in cards:
        if not isinstance(card, dict):
            continue
        kickoff = _time(card.get("kickoff") or card.get("kickoff_hkt"))
        for stage, board in (card.get("native_stage_quote_boards") or {}).items():
            if not isinstance(board, dict):
                continue
            boards[str(stage)] += 1
            for quote in board.get("quotes") or []:
                if isinstance(quote, dict):
                    quote_status[f"{stage}:{str(quote.get('status') or 'AVAILABLE')}"] += 1
            if stage == "T-5" and kickoff is not None and kickoff > now:
                fresh_t5 += 1
    return {
        "readable": isinstance(data, list),
        "cards": len(cards),
        "boards_by_stage": dict(sorted(boards.items())),
        "quote_rows_by_stage_status": dict(sorted(quote_status.items())),
        "pre_kickoff_t5_boards": fresh_t5,
    }


def _systemctl(units: list[str]) -> dict[str, dict[str, str]]:
    fields = (
        "Id", "ActiveState", "SubState", "Result", "ExecMainStatus",
        "LastTriggerUSec", "NextElapseUSecRealtime",
    )
    command = ["systemctl", "show", *units, "--no-pager"]
    for field in fields:
        command.extend(["-p", field])
    try:
        result = subprocess.run(command, text=True, capture_output=True, timeout=15, check=False)
    except (OSError, subprocess.SubprocessError):
        return {"error": {"state": "unavailable"}}
    current: dict[str, str] = {}
    output: dict[str, dict[str, str]] = {}
    for raw in result.stdout.splitlines():
        if "=" not in raw:
            continue
        key, value = raw.split("=", 1)
        if key == "Id":
            if current.get("Id"):
                output[current["Id"]] = current
            current = {"Id": value}
        elif current:
            current[key] = value
    if current.get("Id"):
        output[current["Id"]] = current
    return output or {"error": {"state": f"systemctl_rc_{result.returncode}"}}


def _journal_failure_counts(start: datetime) -> dict[str, int]:
    since = start.isoformat()
    command = [
        "journalctl", "-u", "footbreak-tick.service",
        "-u", "footbreak-server-health-monitor.service",
        "--since", since, "--no-pager", "-o", "cat",
    ]
    try:
        result = subprocess.run(command, text=True, capture_output=True, timeout=20, check=False)
    except (OSError, subprocess.SubprocessError):
        return {"unavailable": 1}
    lines = result.stdout.lower().splitlines()
    return {
        "notification_failure_lines": sum(
            ("通知" in line or "notification" in line or "telegram" in line)
            and ("失敗" in line or "fail" in line or "error" in line or "timeout" in line)
            for line in lines
        ),
        "tick_deadline_lines": sum("deadline" in line or "timeout" in line for line in lines),
        "lock_rejection_lines": sum("lock" in line and ("reject" in line or "busy" in line) for line in lines),
    }


def _representatives(ledger: dict[str, Any], state: dict[str, Any]) -> list[dict[str, Any]]:
    bets, observations, _audit = _native_rows(ledger)
    decisions, _outbox = _cross_rows(ledger)
    wilson_ack = _idset(state.get("wilson_match_alerts"))
    bilateral_ack = _idset(state.get("bilateral_decision_alerts"))
    source = bets + observations
    source.sort(key=lambda row: str(_row_at(row) or ""), reverse=True)
    rows: list[dict[str, Any]] = []
    for row in source[:MAX_REPRESENTATIVES]:
        identity = str(row.get("bet_id") or row.get("observation_id") or "")
        decision = _decision_for_native(row, decisions) if row.get("bet_id") else None
        rows.append({
            "fixture": f"{str(row.get('home') or '')} vs {str(row.get('away') or '')}".strip(),
            "kickoff": row.get("kickoff"),
            "created_at": _row_at(row),
            "market": row.get("market") or row.get("code"),
            "class": "formal_bet" if row.get("bet_id") else "low_odds_observation",
            "condition_number": row.get("condition_number"),
            "native_acknowledged": identity in wilson_ack,
            "bilateral_decision": decision.get("decision") if decision else None,
            "bilateral_acknowledged": (
                str(decision.get("decision_id") or "") in bilateral_ack if decision else False
            ),
            "counterpart_status": decision.get("counterpart_status") if decision else None,
        })
    return rows


def build_report(now: datetime | None = None) -> dict[str, Any]:
    now = (now or datetime.now(HKT)).astimezone(HKT)
    day_start = now.replace(hour=0, minute=0, second=0, microsecond=0)
    producer_boundary = now.replace(hour=7, minute=10, second=26, microsecond=0)
    # The actual deployed boundary remains fixed for this incident day; for a
    # later read the report still compares the first production day faithfully.
    producer_boundary = datetime(2026, 8, 22, 7, 10, 26, tzinfo=HKT)
    ledger = _read(LEDGER_PATH, {})
    state = _read(NOTIFY_PATH, {})
    windows = {
        "previous_24_hours": _window_summary(
            ledger=ledger, state=state, start=now - timedelta(hours=24), end=now,
        ),
        "daily_before_91e51b9_deployment": _window_summary(
            ledger=ledger, state=state, start=day_start, end=min(producer_boundary, now),
        ),
        "daily_after_91e51b9_deployment": _window_summary(
            ledger=ledger, state=state, start=max(producer_boundary, day_start), end=now,
        ),
    }
    validation = ledger.get("wilson_validation") if isinstance(ledger.get("wilson_validation"), dict) else {}
    conditions = validation.get("conditions") if isinstance(validation.get("conditions"), dict) else {}
    x20 = []
    for signature, condition in conditions.items():
        if not isinstance(condition, dict):
            continue
        progress = condition.get("pending_rollover_progress")
        x20.append({
            "condition_number": condition.get("condition_number"),
            "signature": str(signature),
            "active_evidence_version": (condition.get("active_evidence") or {}).get("version"),
            "progress": (progress or {}).get("display") if isinstance(progress, dict) else None,
        })
    return {
        "schema_version": 1,
        "mode": "read_only_provider_free",
        "provider_calls": 0,
        "writes": 0,
        "telegram_send_attempts": 0,
        "generated_at_hkt": now.isoformat(),
        "code_version_boundary": {
            "commit": "91e51b99d045232a8110619ee3f13194c86073bc",
            "deployment_hkt": producer_boundary.isoformat(),
        },
        "transport": {
            "telegram_configured": bool(
                str(__import__("os").environ.get("TELEGRAM_BOT_TOKEN") or "")
                and str(__import__("os").environ.get("TELEGRAM_CHAT_ID") or "")
            ),
            "state_last_sent": state.get("last_sent"),
            "wilson_ack_count": len(_idset(state.get("wilson_match_alerts"))),
            "bilateral_ack_count": len(_idset(state.get("bilateral_decision_alerts"))),
            "cross_execution_ack_count": len(_idset(state.get("crown_execution_test_alerts"))),
            "transport_test_count": len(state.get("transport_tests") or {}),
        },
        "windows": windows,
        "x20_current_conditions": x20[:64],
        "current_bridge_status": _bridge_summary(ledger, now),
        "current_crown_sidecar": _sidecar_summary(now),
        "current_timer_and_service_state": _systemctl([
            "footbreak-tick.timer", "footbreak-tick.service",
            "footbreak-server-health-monitor.timer", "footbreak-server-health-monitor.service",
            "crown-tick.timer", "crown-tick.service",
        ]),
        "journal_since_daily_start": _journal_failure_counts(day_start),
        "representative_native_notifications": _representatives(ledger, state),
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--now", help=argparse.SUPPRESS)
    args = parser.parse_args()
    now = _time(args.now) if args.now else None
    print(json.dumps(build_report(now), ensure_ascii=False, sort_keys=True, separators=(",", ":")))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
