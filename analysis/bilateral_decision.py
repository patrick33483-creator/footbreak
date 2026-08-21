"""Immutable, local-only bilateral execution decision records.

This is deliberately a data contract, not a collector.  Native stage workers
may call it after they have independently persisted their source-book signal
and the bounded local counterpart attempt.  It never reads a provider, never
changes frozen Wilson evidence, and never represents a real wager.
"""
from __future__ import annotations

import hashlib
import json
from copy import deepcopy
from typing import Any

SCHEMA_VERSION = 1
DECISION_LIMIT = 4000


def _canonical(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), default=str)


def decision_id(
    *, system: str, fixture: str, market: str, side: str, line: float,
    condition_signature: str, evidence_version: Any,
) -> str:
    """Stable idempotency key: one formal condition per exact T-5 selection."""
    identity = {
        "system": system, "fixture": fixture, "market": market,
        "side": side, "line": f"{float(line):.8f}",
        "condition_signature": condition_signature,
        "evidence_version": str(evidence_version or ""),
    }
    return "bilateral-" + hashlib.sha256(_canonical(identity).encode("utf-8")).hexdigest()


def ensure_namespace(ns: dict[str, Any]) -> dict[str, Any]:
    ns.setdefault("bilateral_schema_version", SCHEMA_VERSION)
    ns.setdefault("counterpart_attempts", [])
    ns.setdefault("decisions", [])
    ns.setdefault("decision_outbox", [])
    if not all(isinstance(ns.get(key), list) for key in (
        "counterpart_attempts", "decisions", "decision_outbox",
    )):
        raise ValueError("bilateral collections must be arrays")
    return ns


def append_counterpart_attempt(ns: dict[str, Any], row: dict[str, Any]) -> dict[str, Any]:
    """Persist every exact-market local acquisition result before fan-in."""
    ensure_namespace(ns)
    fingerprint = _canonical({
        key: row.get(key) for key in (
            "system", "match_id", "market", "side", "line", "stage_at",
            "counterpart_status", "counterpart_reason", "counterpart_quote",
        )
    })
    for existing in ns["counterpart_attempts"]:
        if isinstance(existing, dict) and existing.get("fingerprint") == fingerprint:
            return existing
    committed = {"fingerprint": fingerprint, **deepcopy(row)}
    ns["counterpart_attempts"].append(committed)
    ns["counterpart_attempts"] = ns["counterpart_attempts"][-DECISION_LIMIT:]
    return committed


def persist_decision(ns: dict[str, Any], record: dict[str, Any]) -> tuple[dict[str, Any], bool]:
    """Append a frozen decision and its durable outbox row exactly once."""
    ensure_namespace(ns)
    required = ("decision_id", "system", "fixture", "market", "side", "line",
                "condition_signature", "decision", "created_at")
    if any(record.get(key) in (None, "") for key in required):
        raise ValueError("incomplete bilateral decision")
    key = str(record["decision_id"])
    for existing in ns["decisions"]:
        if isinstance(existing, dict) and str(existing.get("decision_id")) == key:
            return existing, False
    committed = deepcopy(record)
    committed["schema_version"] = SCHEMA_VERSION
    committed["provenance_hash"] = hashlib.sha256(_canonical({
        key: value for key, value in committed.items() if key != "provenance_hash"
    }).encode("utf-8")).hexdigest()
    ns["decisions"].append(committed)
    ns["decisions"] = ns["decisions"][-DECISION_LIMIT:]
    ns["decision_outbox"].append({
        "outbox_id": "outbox-" + key,
        "decision_id": key,
        "created_at": committed["created_at"],
        "notification_required": True,
        "delivery": "PENDING",
    })
    ns["decision_outbox"] = ns["decision_outbox"][-DECISION_LIMIT:]
    return committed, True


def decision_for_bet(ns: dict[str, Any], bet: dict[str, Any], system: str) -> dict[str, Any] | None:
    """Find only an already durable decision; formatters must not recalculate."""
    signature = str(bet.get("frozen_condition_signature") or "")
    fixture = str(bet.get("match_id") or "")
    market = str(bet.get("market") or bet.get("code") or "").upper()
    for row in ns.get("decisions") or []:
        if not isinstance(row, dict):
            continue
        if (row.get("system") == system and str(row.get("fixture")) == fixture
                and str(row.get("market")).upper() == market
                and str(row.get("condition_signature")) == signature):
            return row
    return None


def public_decision(row: dict[str, Any]) -> dict[str, Any]:
    """Privacy-safe dashboard projection with no raw provider response."""
    visible = {
        "decision_id", "system", "market", "side", "line", "condition_number",
        "condition_signature", "evidence_version", "signal_book", "signal_quote",
        "counterpart_book", "counterpart_status", "counterpart_quote",
        "counterpart_reason", "minimum_odds", "chosen_execution_book",
        "chosen_execution_odds", "decision", "created_at", "stage_at",
        "freshness_seconds", "provenance_hash",
    }
    return {key: deepcopy(value) for key, value in row.items() if key in visible}
