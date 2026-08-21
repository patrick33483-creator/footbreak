#!/usr/bin/env python3
"""Safe local audit and one-shot Telegram transport verification.

This utility never runs a prediction, settlement, scan, or provider client.  It
only reads the two already-persisted notification outboxes.  A system test is
explicitly opt-in and records its Telegram response acknowledgement in the
existing private notification-state file of each system.
"""
from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


ROOT = Path(__file__).resolve().parents[1]
SYSTEM = ROOT / "system"
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
if str(SYSTEM) not in sys.path:
    sys.path.insert(0, str(SYSTEM))

import notify as footbreak_notify  # noqa: E402
from crown.config import settings  # noqa: E402
from crown.notify import (  # noqa: E402
    _observation_groups,
    _hkjc_execution_message,
    _send as crown_send,
    _wilson_message,
    _wilson_observation_message,
)
from crown.state import load_ledger as load_crown_ledger  # noqa: E402
from crown.state import notification_lock, paths, read_json, write_json_atomic  # noqa: E402


def _ids(value: Any) -> set[str]:
    return {str(item) for item in value or [] if str(item)}


def _footbreak_audit() -> dict[str, Any]:
    ledger = footbreak_notify.load_ledger()
    state = footbreak_notify.load_state()
    acknowledged = _ids(state.get("wilson_match_alerts"))
    condition_rows = list(ledger.get("bets") or []) + list(
        (ledger.get("wilson_validation") or {}).get("observations") or []
    )
    condition_pending: list[str] = []
    for row in condition_rows:
        if not isinstance(row, dict):
            continue
        ident = str(row.get("bet_id") or row.get("observation_id") or "")
        message = (
            footbreak_notify._condition_bet_message(row)
            if row.get("bet_id") else footbreak_notify._condition_observation_message(row)
        )
        if ident and ident not in acknowledged and message is not None:
            condition_pending.append(ident)
    crown_acknowledged = _ids(state.get("crown_execution_test_alerts"))
    execution_rows = (
        (ledger.get(footbreak_notify.CROWN_EXECUTION_PORTFOLIO) or {}).get("bets") or []
    )
    execution_pending = [
        str(row.get("bet_id"))
        for row in execution_rows
        if isinstance(row, dict)
        and str(row.get("bet_id") or "")
        and str(row.get("bet_id")) not in crown_acknowledged
        and footbreak_notify._crown_execution_message(row) is not None
    ]
    return {
        "telegram_enabled": bool(footbreak_notify.BOT_TOKEN and footbreak_notify.CHAT_ID),
        "bot_token_configured": bool(footbreak_notify.BOT_TOKEN),
        "chat_id_configured": bool(footbreak_notify.CHAT_ID),
        "eligible_outbox": {
            "wilson_condition": len(condition_pending),
            "crown_execution": len(execution_pending),
            "total": len(condition_pending) + len(execution_pending),
        },
        "acknowledged": {
            "wilson_condition": len(acknowledged),
            "crown_execution": len(crown_acknowledged),
        },
        "state_last_sent": state.get("last_sent"),
        "transport_tests": len((state.get("transport_tests") or {})),
    }


def _crown_audit() -> dict[str, Any]:
    config = settings()
    ledger = load_crown_ledger(config)
    state = read_json(paths(config)["notify"], {})
    wilson_acknowledged = _ids(state.get("wilson_match_alerts"))
    wilson_rows = list(ledger.get("bets") or []) + list(
        (ledger.get("wilson_validation") or {}).get("observations") or []
    )
    wilson_pending: list[str] = []
    for row in wilson_rows:
        if not isinstance(row, dict):
            continue
        ident = str(row.get("bet_id") or row.get("observation_id") or "")
        message = _wilson_message(row) if row.get("bet_id") else _wilson_observation_message(row)
        if ident and ident not in wilson_acknowledged and message is not None:
            wilson_pending.append(ident)
    execution_acknowledged = _ids(state.get("hkjc_execution_test_alerts"))
    execution_rows = ((ledger.get("crown_hkjc_execution_test") or {}).get("bets") or [])
    execution_pending = [
        str(row.get("bet_id"))
        for row in execution_rows
        if isinstance(row, dict)
        and str(row.get("bet_id") or "")
        and str(row.get("bet_id")) not in execution_acknowledged
        and _hkjc_execution_message(row) is not None
    ]
    wilson_namespace = ledger.get("wilson_validation") or {}
    raw_audit = (
        wilson_namespace.get("audit")
        if isinstance(wilson_namespace, dict) else []
    )
    latest_decisions: list[dict[str, Any]] = []
    for row in reversed(raw_audit if isinstance(raw_audit, list) else []):
        if not isinstance(row, dict):
            continue
        admission = (
            row.get("wilson_admission")
            if isinstance(row.get("wilson_admission"), dict) else {}
        )
        latest_decisions.append({
            "at": row.get("ts"),
            "match_id": row.get("match_id"),
            "market": row.get("market"),
            "status": row.get("status"),
            "reason": row.get("reason"),
            "condition_number": row.get("condition_number"),
            "observation_persisted": bool(row.get("observation_id")),
            "actual_odds": admission.get("actual_decimal_odds_raw"),
            "minimum_odds": admission.get("minimum_acceptable_odds_raw"),
            "wilson_passes": admission.get("passes"),
        })
        if len(latest_decisions) >= 12:
            break
    observations = [
        row for row in (wilson_namespace.get("observations") or [])
        if isinstance(row, dict)
    ]
    observation_acknowledged = _ids(state.get("wilson_match_alerts"))
    recent_observations: list[dict[str, Any]] = []
    for row in reversed(observations[-128:]):
        admission = row.get("wilson_admission") if isinstance(row.get("wilson_admission"), dict) else {}
        ident = str(row.get("observation_id") or "")
        recent_observations.append({
            "observation_id": ident,
            "match_id": row.get("match_id"),
            "market": row.get("market") or row.get("code"),
            "stage": row.get("stage"),
            "created_at": row.get("created_at"),
            "kickoff": row.get("kickoff"),
            "condition_number": row.get("condition_number"),
            "actual_odds": admission.get("actual_decimal_odds_raw"),
            "minimum_odds": admission.get("minimum_acceptable_odds_raw"),
            "acknowledged": bool(ident and ident in observation_acknowledged),
        })
    unacknowledged_groups = _observation_groups(observations, observation_acknowledged)
    return {
        "telegram_enabled": config.telegram_enabled,
        "bot_token_configured": bool(config.telegram_bot_token),
        "chat_id_configured": bool(config.telegram_chat_id),
        "runner_enabled": config.enabled,
        "eligible_outbox": {
            "wilson": len(wilson_pending),
            "hkjc_execution": len(execution_pending),
            "total": len(wilson_pending) + len(execution_pending),
        },
        "acknowledged": {
            "wilson": len(wilson_acknowledged),
            "hkjc_execution": len(execution_acknowledged),
        },
        "state_updated_at": state.get("updated_at"),
        "transport_tests": len((state.get("transport_tests") or {})),
        # The newest T-5 records distinguish an actual no-condition outcome
        # from a matched condition whose price is below its raw Wilson floor.
        # No team/odds-board payload is read and no provider is contacted.
        "latest_wilson_decisions": latest_decisions,
        # Persisted low-odds rows remain visible after kickoff for forensic
        # diagnosis, while their normal Telegram eligibility still fails closed.
        "recent_low_odds_observations": recent_observations,
        "unacknowledged_low_odds_group_sizes": [
            len(group) for group in unacknowledged_groups
        ],
    }


def _classify(audit: dict[str, Any]) -> str:
    if not audit["telegram_enabled"]:
        return "telegram_transport_or_config_disabled"
    if audit["eligible_outbox"]["total"] == 0:
        return "no_qualifying_unacknowledged_signal"
    return "eligible_outbox_pending_transport_retry"


def audit() -> dict[str, Any]:
    footbreak = _footbreak_audit()
    crown = _crown_audit()
    footbreak["classification"] = _classify(footbreak)
    crown["classification"] = _classify(crown)
    return {
        "generated_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "safe_read_only": True,
        "footbreak": footbreak,
        "crown": crown,
    }


def _record_footbreak_test(test_id: str) -> dict[str, Any]:
    state = footbreak_notify.load_state()
    tests = state.setdefault("transport_tests", {})
    previous = tests.get(test_id)
    if isinstance(previous, dict):
        return {"status": "already_acknowledged", **previous}
    response = footbreak_notify.send(
        f"【SYSTEM TEST】Footbreak Telegram delivery verification · {test_id}",
        return_response=True,
    )
    record = {
        "sent_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "telegram_response_ok": bool(response.get("ok")),
        "transport": response.get("transport"),
        "message_id": response.get("message_id"),
    }
    tests[test_id] = record
    footbreak_notify.save_state(state)
    confirmed = footbreak_notify.load_state().get("transport_tests", {}).get(test_id)
    if confirmed != record:
        raise RuntimeError("Footbreak test acknowledgement was not durable")
    return {"status": "sent_and_acknowledged", **record}


def _record_crown_test(test_id: str) -> dict[str, Any]:
    config = settings()
    with notification_lock(config) as acquired:
        if not acquired:
            raise RuntimeError("Crown notification lock is busy; system test was not sent")
        state = read_json(paths(config)["notify"], {})
        tests = state.setdefault("transport_tests", {})
        previous = tests.get(test_id)
        if isinstance(previous, dict):
            return {"status": "already_acknowledged", **previous}
        response = crown_send(
            config,
            f"【SYSTEM TEST】Crown Telegram delivery verification · {test_id}",
            return_response=True,
        )
        record = {
            "sent_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
            "telegram_response_ok": bool(response.get("ok")),
            "transport": response.get("transport"),
            "message_id": response.get("message_id"),
        }
        tests[test_id] = record
        write_json_atomic(paths(config)["notify"], state)
        confirmed = read_json(paths(config)["notify"], {}).get("transport_tests", {}).get(test_id)
        if confirmed != record:
            raise RuntimeError("Crown test acknowledgement was not durable")
        return {"status": "sent_and_acknowledged", **record}


def system_test(test_id: str) -> dict[str, Any]:
    return {
        "test_id": test_id,
        "footbreak": _record_footbreak_test(test_id),
        "crown": _record_crown_test(test_id),
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--system-test-id")
    args = parser.parse_args()
    output = audit()
    if args.system_test_id:
        output["system_test"] = system_test(args.system_test_id)
        output["safe_read_only"] = False
    print(json.dumps(output, ensure_ascii=False, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
