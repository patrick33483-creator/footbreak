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
import os
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


def _condition_identity_audit(ledger: dict[str, Any], system: str) -> dict[str, Any]:
    """Expose bounded, provider-free identity/rollover evidence for diagnosis.

    The dashboard must never substitute a display rank for a durable Wilson
    identity.  This local audit lets an operator compare a persisted formal
    row (or low-odds observation) to the frozen condition it actually names,
    including its active version boundary and current 20-result counter.
    """
    namespace = ledger.get("wilson_validation")
    namespace = namespace if isinstance(namespace, dict) else {}
    conditions = namespace.get("conditions")
    conditions = conditions if isinstance(conditions, dict) else {}
    counters: list[dict[str, Any]] = []
    for signature, frozen in conditions.items():
        if not isinstance(frozen, dict):
            continue
        active = frozen.get("active_evidence")
        active = active if isinstance(active, dict) else {}
        counters.append({
            "condition_number": frozen.get("condition_number"),
            "condition_signature": str(signature),
            "definition": frozen.get("definition"),
            "active_evidence_version": active.get("version"),
            "activation_boundary_at": active.get("activation_boundary_at"),
            "pending_rollover_progress": frozen.get("pending_rollover_progress"),
        })
    counters.sort(key=lambda row: (
        int(row["condition_number"]) if str(row.get("condition_number") or "").isdigit() else 10**9,
        str(row["condition_signature"]),
    ))

    durable_rows = list(ledger.get("bets") or []) + list(
        namespace.get("observations") or []
    )
    current_date = datetime.now(timezone.utc).date().isoformat()
    today: list[dict[str, Any]] = []
    for row in durable_rows:
        if not isinstance(row, dict):
            continue
        kickoff = str(row.get("kickoff") or "")
        if not kickoff.startswith(current_date):
            continue
        signature = str(row.get("frozen_condition_signature") or "")
        frozen = conditions.get(signature)
        frozen = frozen if isinstance(frozen, dict) else {}
        active = frozen.get("active_evidence")
        active = active if isinstance(active, dict) else {}
        formal = (
            str(row.get("portfolio") or "") == f"{system}_wilson_test"
            and row.get("formal_bet") is not False
        )
        marker = row.get("rollover_provenance")
        marker = marker if isinstance(marker, dict) else None
        today.append({
            "row_id": row.get("bet_id") or row.get("observation_id"),
            "formal_bet": formal,
            "bet_status": row.get("bet_status") or row.get("status"),
            "result": row.get("result"),
            "settled_at": row.get("settled_at"),
            "match_id": row.get("match_id"),
            "home": row.get("home"),
            "away": row.get("away"),
            "kickoff": row.get("kickoff"),
            "market": row.get("market") or row.get("code"),
            "selected_role": row.get("selected_role"),
            "selected_line": row.get("selected_line", row.get("line")),
            "stage": row.get("stage"),
            "condition_number": row.get("condition_number"),
            "condition_signature": signature or None,
            "frozen_definition": row.get("frozen_condition_definition"),
            "evidence_version": row.get("evidence_version"),
            "active_evidence_version": active.get("version"),
            "activation_boundary_at": active.get("activation_boundary_at"),
            "pending_rollover_progress": frozen.get("pending_rollover_progress"),
            "native_pre_kickoff_t5": row.get("first_native_pre_kickoff_t5"),
            "rollover_provenance_present": marker is not None,
            "rollover_eligibility": (
                "eligible_after_binary_settlement"
                if formal and row.get("status") == "PENDING" and marker
                else "settled_row_requires_binary_and_unique_provenance_check"
                if formal and row.get("status") == "SETTLED" and marker
                else "excluded_low_odds_observation_not_formal_bet"
                if row.get("formal_bet") is False
                else "excluded_missing_formal_rollover_provenance"
            ),
        })
    today.sort(key=lambda row: (
        str(row.get("kickoff") or ""), str(row.get("row_id") or ""),
    ), reverse=True)
    return {
        "system": system,
        "frozen_condition_counters": counters,
        # Today-only, bounded records make incident analysis possible without
        # publishing an unbounded historical ledger or contacting a provider.
        "today_durable_condition_rows": today[:96],
    }


def _dashboard_authority_parity(
    payload_path: Path,
    ledger: dict[str, Any],
) -> dict[str, Any]:
    """Audit the public card contract against durable native Wilson state.

    This remains strictly local and provider-free.  A research-card match is
    permitted only when explicitly labelled as such; only a persisted formal
    bet or low-odds observation may carry a formal condition number and be
    notification eligible.
    """
    payload = read_json(payload_path, {})
    if not isinstance(payload, dict):
        return {"available": False, "reason": "dashboard_missing_or_malformed"}
    matches = payload.get("matches")
    if not isinstance(matches, list):
        return {"available": False, "reason": "dashboard_matches_missing"}
    durable = list(ledger.get("bets") or []) + list(
        (ledger.get("wilson_validation") or {}).get("observations") or []
    )
    durable_keys = {
        (
            str(row.get("match_id") or ""),
            str(row.get("condition_number") or ""),
            str(row.get("market") or row.get("code") or ""),
            str(row.get("selected_role") or ""),
            str(row.get("selected_line", row.get("line"))),
        )
        for row in durable
        if isinstance(row, dict)
        and (
            str(row.get("portfolio") or "").endswith("_wilson_test")
            or row.get("formal_bet") is False
        )
    }
    research = authoritative = 0
    violations: list[str] = []
    for card in matches:
        if not isinstance(card, dict):
            continue
        match_id = str(card.get("match_id") or "")
        for row in card.get("condition_matches") or []:
            if not isinstance(row, dict):
                continue
            research += 1
            if (
                row.get("match_class") != "research_only"
                or row.get("authoritative") is not False
                or row.get("notification_eligible") is not False
                or row.get("condition_number") is not None
            ):
                violations.append("research_card_not_explicitly_non_authoritative")
        for row in card.get("wilson_matches") or []:
            if not isinstance(row, dict):
                continue
            authoritative += 1
            key = (
                match_id,
                str(row.get("condition_number") or ""),
                str(row.get("market") or row.get("code") or ""),
                str(row.get("selected_role") or ""),
                str(row.get("selected_line", row.get("line"))),
            )
            if (
                row.get("match_class") != "authoritative_admission"
                or row.get("authoritative") is not True
                or row.get("notification_eligible") is not True
                or not str(row.get("condition_number") or "")
                or key not in durable_keys
            ):
                violations.append("authoritative_card_missing_durable_native_admission")
    return {
        "available": True,
        "dashboard_generated_at": payload.get("generated_at"),
        "research_card_matches": research,
        "authoritative_card_matches": authoritative,
        "violations": sorted(set(violations)),
        "healthy": not violations,
    }


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
    legacy_enabled = os.environ.get(
        "FOOTBREAK_TELEGRAM_ENABLED", "0"
    ).strip().lower() in {"1", "true", "yes", "on"}
    return {
        "telegram_enabled": bool(
            legacy_enabled and footbreak_notify.BOT_TOKEN and footbreak_notify.CHAT_ID
        ),
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
        "condition_identity_audit": _condition_identity_audit(ledger, "footbreak"),
        "dashboard_authority_parity": _dashboard_authority_parity(
            Path(footbreak_notify.DASHBOARD_DATA), ledger,
        ),
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
    watch = ledger.get("watch") if isinstance(ledger.get("watch"), dict) else {}
    raw_audit_rows = raw_audit if isinstance(raw_audit, list) else []
    recent_native_t5: list[dict[str, Any]] = []
    for fixture in watch.values():
        if not isinstance(fixture, dict):
            continue
        stages = fixture.get("stages") if isinstance(fixture.get("stages"), list) else []
        t5 = next((
            row for row in stages
            if isinstance(row, dict) and row.get("stage") == "T-5"
        ), None)
        if not isinstance(t5, dict):
            continue
        match_id = str(fixture.get("match_id") or "")
        decisions = [
            {
                "market": row.get("market"),
                "status": row.get("status"),
                "reason": row.get("reason"),
                "condition_number": row.get("condition_number"),
                "observation_id": row.get("observation_id"),
            }
            for row in raw_audit_rows
            if isinstance(row, dict)
            and str(row.get("match_id") or "") == match_id
            and str(row.get("ts") or "") == str(t5.get("ts") or t5.get("source_snapshot_at") or "")
        ]
        recent_native_t5.append({
            "match_id": match_id,
            "home": fixture.get("home"),
            "away": fixture.get("away"),
            "kickoff": fixture.get("kickoff") or fixture.get("kickoff_hkt"),
            "stage_at": t5.get("ts") or t5.get("source_snapshot_at"),
            "markets": [
                {
                    "code": row.get("code"),
                    "side": row.get("side"),
                    "line": row.get("line"),
                    "odds": row.get("odds"),
                }
                for row in (t5.get("market_predictions") or [])
                if isinstance(row, dict)
            ],
            "wilson_decisions": decisions,
        })
    recent_native_t5.sort(key=lambda row: str(row.get("stage_at") or ""), reverse=True)
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
        "condition_identity_audit": _condition_identity_audit(ledger, "crown"),
        # A bounded local view links every recent native T-5 card to its
        # durable Wilson outcome. It permits post-kickoff diagnosis without
        # invoking a provider or reviving a normal alert.
        "recent_native_t5_wilson_outcomes": recent_native_t5[:48],
        "dashboard_authority_parity": _dashboard_authority_parity(
            config.web_root / "data.json", ledger,
        ),
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
