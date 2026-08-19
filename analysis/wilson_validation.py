"""Immutable Wilson-test simulation portfolio primitives.

This module is deliberately separate from :mod:`independent_validation`.
That namespace is the retired v1 strategy and remains readable/settleable,
whereas this namespace owns only the Wilson 測試攻略 prospective experiment.
"""
from __future__ import annotations

import copy
import hashlib
import json
import math
from datetime import datetime, timezone
from typing import Any, Iterable

SCHEMA_VERSION = 1
NAMESPACE = "wilson_validation"
STRATEGY = "wilson-test-strategy-v1"
DISPLAY_NAME = "Wilson 測試攻略"
STARTING_BANKROLL = 50_000.0
FIXED_STAKE = 500.0
FIXTURE_STAKE_CAP = 1_500.0
FIXTURE_MARKET_CAP = 3
DECISION_STAGE = "T-5"
MIN_DECIDED = 50
EDGE_BUFFER = 0.03
Z_95 = 1.959963984540054
LEGACY_STRATEGY = "independent-validation-v1"


def _number(value: Any) -> float | None:
    try:
        answer = float(value)
    except (TypeError, ValueError):
        return None
    return answer if math.isfinite(answer) else None


def _now() -> str:
    return datetime.now(timezone.utc).astimezone().isoformat(timespec="seconds")


def portfolio_name(system: str) -> str:
    return f"{system}_wilson_test"


def wilson95(hits: int, decided: int) -> tuple[float, float] | None:
    """Return full precision Wilson 95% interval; callers round only for display."""
    if decided <= 0 or hits < 0 or hits > decided:
        return None
    p = hits / decided
    denom = 1.0 + Z_95 * Z_95 / decided
    center = (p + Z_95 * Z_95 / (2.0 * decided)) / denom
    margin = Z_95 * math.sqrt(
        (p * (1.0 - p) + Z_95 * Z_95 / (4.0 * decided)) / decided
    ) / denom
    return max(0.0, center - margin), min(1.0, center + margin)


def admission_arithmetic(hits: int, decided: int, odds: Any) -> dict[str, Any] | None:
    """Calculate the exact Wilson admission inequality without rounded inputs."""
    decimal = _number(odds)
    interval = wilson95(hits, decided)
    if decimal is None or decimal <= 1.0 or interval is None:
        return None
    lower, upper = interval
    break_even = 1.0 / decimal
    required = break_even + EDGE_BUFFER
    minimum = 1.0 / (lower - EDGE_BUFFER) if lower > EDGE_BUFFER else None
    return {
        "hits": hits,
        "decided": decided,
        "hit_rate_raw": hits / decided,
        "wilson95_lower_raw": lower,
        "wilson95_upper_raw": upper,
        "actual_decimal_odds_raw": decimal,
        "break_even_rate_raw": break_even,
        "required_rate_raw": required,
        "minimum_acceptable_odds_raw": minimum,
        # A tolerance is intentionally not used: equality is a valid pass.
        "passes": lower >= required,
        "display": {
            "hit_rate_pct": round(100.0 * hits / decided, 1),
            "wilson95_lower_pct": round(100.0 * lower, 1),
            "wilson95_upper_pct": round(100.0 * upper, 1),
            "break_even_pct": round(100.0 * break_even, 1),
            "required_pct": round(100.0 * required, 1),
            "minimum_acceptable_odds": round(minimum, 2) if minimum is not None else None,
            "actual_decimal_odds": round(decimal, 2),
        },
    }


def _legacy_rows(ledger: dict[str, Any], system: str) -> list[dict[str, Any]]:
    return [
        row for row in ledger.get("bets") or []
        if isinstance(row, dict)
        and row.get("portfolio") == f"{system}_independent_validation"
        and row.get("strategy") == LEGACY_STRATEGY
    ]


def _legacy_archive(ledger: dict[str, Any], system: str, cutover_at: str) -> dict[str, Any]:
    """A read-only v1 snapshot made exactly once at the Wilson cutover."""
    rows = _legacy_rows(ledger, system)
    legacy_ns = ledger.get("independent_validation")
    legacy_stats = copy.deepcopy(legacy_ns.get("stats") if isinstance(legacy_ns, dict) else {})
    bankroll = _number(legacy_stats.get("starting_bankroll")) if isinstance(legacy_stats, dict) else None
    return {
        "label": "已封存／退役 previous strategy（唯讀）",
        "read_only": True,
        "strategy": LEGACY_STRATEGY,
        "cutover_at": cutover_at,
        "new_entries_disabled": True,
        "entry_notifications_disabled": True,
        "pending_settlement_retained": True,
        "legacy_bets": copy.deepcopy(rows),
        "legacy_bet_count": len(rows),
        "legacy_pending_count": sum(row.get("status") == "PENDING" for row in rows),
        "legacy_bankroll": bankroll,
        "legacy_stats": legacy_stats,
    }


def ensure_namespace(ledger: dict[str, Any], system: str, *, now: str | None = None) -> dict[str, Any]:
    """Install an idempotent, non-destructive Wilson cutover namespace."""
    ledger.setdefault("bets", [])
    ns = ledger.get(NAMESPACE)
    if ns is None:
        ns = {}
        ledger[NAMESPACE] = ns
    if not isinstance(ns, dict):
        raise ValueError("Wilson namespace must be an object")
    if ns.get("schema_version") not in (None, SCHEMA_VERSION):
        raise ValueError("unsupported Wilson namespace schema")
    if ns.get("system") not in (None, system):
        raise ValueError("Wilson namespace system mismatch")
    activation = now or _now()
    ns.setdefault("schema_version", SCHEMA_VERSION)
    ns.setdefault("system", system)
    ns.setdefault("display_name", DISPLAY_NAME)
    ns.setdefault("activation_at", activation)
    ns.setdefault("cutover_at", ns["activation_at"])
    ns.setdefault("starting_bankroll", STARTING_BANKROLL)
    ns.setdefault("fixed_stake", FIXED_STAKE)
    ns.setdefault("fixture_stake_cap", FIXTURE_STAKE_CAP)
    ns.setdefault("fixture_market_cap", FIXTURE_MARKET_CAP)
    ns.setdefault("minimum_decided", MIN_DECIDED)
    ns.setdefault("edge_buffer", EDGE_BUFFER)
    ns.setdefault("conditions", {})
    ns.setdefault("audit", [])
    ns.setdefault("notifications", {"sent": []})
    # This one-time snapshot labels old v1 clearly without modifying a row,
    # stat, or pending settlement obligation.
    ns.setdefault("retired_v1", _legacy_archive(ledger, system, ns["cutover_at"]))
    if not isinstance(ns["conditions"], dict):
        raise ValueError("Wilson conditions must be an object")
    if not isinstance(ns["audit"], list):
        raise ValueError("Wilson audit must be an array")
    return ns


def condition_definition(system: str, candidate: dict[str, Any]) -> dict[str, Any]:
    """Canonical immutable definition, preserving all supplied matcher axes."""
    key = candidate.get("key")
    return {
        "system": system,
        "version": str(candidate.get("version") or candidate.get("condition_version") or "granular-condition-v1"),
        "market": str(candidate.get("market") or ""),
        "stage": str(candidate.get("decision_stage") or candidate.get("stage") or DECISION_STAGE),
        "path": str(candidate.get("observed_path") or candidate.get("path") or ""),
        "direction": str(candidate.get("direction") or candidate.get("selected_side") or candidate.get("side") or ""),
        "role": str(candidate.get("role") or candidate.get("selected_role") or ""),
        "line_bucket": str(candidate.get("line_bucket") or candidate.get("bucket") or ""),
        "odds_tier": str(candidate.get("odds_tier") or ""),
        "movement": str(candidate.get("movement") or ""),
        "odds_trajectory": str(candidate.get("odds_trajectory") or candidate.get("tier_path") or ""),
        "miner_key": [str(v) for v in key] if isinstance(key, list) else [str(key or "")],
    }


def condition_signature(system: str, candidate: dict[str, Any]) -> tuple[str, dict[str, Any]]:
    definition = condition_definition(system, candidate)
    raw = json.dumps(definition, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(raw.encode()).hexdigest()[:24], definition


def _artifact(candidate: dict[str, Any], definition: dict[str, Any], stage_at: str) -> dict[str, Any]:
    source = candidate.get("source_artifact") if isinstance(candidate.get("source_artifact"), dict) else {}
    raw = json.dumps(
        {"definition": definition, "total": candidate.get("total"), "source": source},
        ensure_ascii=False, sort_keys=True, separators=(",", ":"),
    ).encode()
    return {
        "hash": str(source.get("hash") or source.get("sha256") or hashlib.sha256(raw).hexdigest()),
        "version": str(source.get("version") or candidate.get("source_artifact_version") or "granular-ranking-v1"),
        "as_of": str(source.get("as_of") or candidate.get("source_artifact_as_of") or stage_at),
    }


def _historical(candidate: dict[str, Any], definition: dict[str, Any], stage_at: str) -> dict[str, Any] | None:
    total = candidate.get("total") if isinstance(candidate.get("total"), dict) else {}
    # A discovery producer may carry source fixture-market ids.  They are
    # evidence, not a second sample: duplicate ids are deduped and conflicting
    # outcomes fail closed rather than inflating the hit rate.
    raw_rows = candidate.get("fixture_markets")
    if isinstance(raw_rows, list):
        unique: dict[str, bool] = {}
        for row in raw_rows:
            if not isinstance(row, dict):
                return None
            fid = str(row.get("fixture_market_id") or row.get("id") or "")
            won = row.get("won")
            if not fid or not isinstance(won, bool) or (fid in unique and unique[fid] != won):
                return None
            unique[fid] = won
        if unique:
            total = {**total, "hits": sum(unique.values()), "decided": len(unique)}
    try:
        hits, decided = int(total.get("hits")), int(total.get("decided"))
    except (TypeError, ValueError):
        return None
    if decided < MIN_DECIDED or hits < 0 or hits > decided:
        return None
    return {
        "hits": hits, "decided": decided,
        "pushes": int(total.get("pushes") or 0),
        "artifact": _artifact(candidate, definition, stage_at),
        "label": candidate.get("label") or "凍結歷史條件",
    }


def _selection_signature(market: str, item: dict[str, Any]) -> tuple[str, float] | None:
    side = str(item.get("selected_side") or item.get("side") or "").upper()
    line = _number(item.get("selected_line", item.get("line", item.get("condition"))))
    if side not in ({"H", "A"} if market == "HDC" else {"H", "L"}) or line is None:
        return None
    if market == "HDC" and side == "A" and "selected_line" not in item:
        line = -line
    return side, round(line, 8)


def active_bets(ledger: dict[str, Any], system: str) -> list[dict[str, Any]]:
    return [
        row for row in ledger.get("bets") or []
        if isinstance(row, dict) and row.get("portfolio") == portfolio_name(system)
        and row.get("strategy") == STRATEGY
    ]


def all_settleable_bets(ledger: dict[str, Any], system: str) -> list[dict[str, Any]]:
    """Old pending rows remain settleable, but never appear in Wilson metrics."""
    return _legacy_rows(ledger, system) + active_bets(ledger, system)


def _prospective(bets: Iterable[dict[str, Any]]) -> dict[str, Any]:
    rows = list(bets)
    settled = [row for row in rows if row.get("status") == "SETTLED"]
    decided = [row for row in settled if row.get("result") != "Refunded"]
    hits = sum(row.get("result") in {"Won", "Half Won"} for row in decided)
    pnl = round(sum(_number(row.get("pnl")) or 0.0 for row in settled), 2)
    turnover = round(sum(_number(row.get("stake")) or 0.0 for row in settled), 2)
    interval = wilson95(hits, len(decided))
    return {
        "hits": hits, "decided": len(decided), "pushes": len(settled) - len(decided),
        "hit_rate": hits / len(decided) if decided else None,
        "wilson95": list(interval) if interval else None,
        "pnl": pnl, "turnover": turnover, "roi": pnl / turnover if turnover else None,
        "pending": sum(row.get("status") == "PENDING" for row in rows),
        "settled": len(settled),
    }


def recompute_namespace(ledger: dict[str, Any], system: str) -> dict[str, Any]:
    ns = ensure_namespace(ledger, system)
    bets = active_bets(ledger, system)
    grouped: dict[str, list[dict[str, Any]]] = {}
    for row in bets:
        grouped.setdefault(str(row.get("frozen_condition_signature") or ""), []).append(row)
    for signature, frozen in ns["conditions"].items():
        if isinstance(frozen, dict):
            frozen["prospective"] = _prospective(grouped.get(signature, []))
    metrics = _prospective(bets)
    open_stake = sum(_number(row.get("stake")) or 0.0 for row in bets if row.get("status") == "PENDING")
    metrics.update({
        "portfolio": portfolio_name(system), "strategy": STRATEGY, "display_name": DISPLAY_NAME,
        "activation_at": ns["activation_at"], "cutover_at": ns["cutover_at"],
        "starting_bankroll": STARTING_BANKROLL, "fixed_stake": FIXED_STAKE,
        "fixture_stake_cap": FIXTURE_STAKE_CAP, "fixture_market_cap": FIXTURE_MARKET_CAP,
        "cash": STARTING_BANKROLL + metrics["pnl"] - open_stake,
        "equity": STARTING_BANKROLL + metrics["pnl"], "open_stake": open_stake,
        "conditions": ns["conditions"], "retired_v1": ns["retired_v1"],
        # Existing presentation adapters use these historical field names.
        # Keep aliases in the Wilson namespace rather than calculating from
        # legacy rows or allowing v1 data into prospective results.
        "n_pending": metrics["pending"], "n_settled": metrics["settled"],
        "n_voided": sum(row.get("status") == "VOIDED" for row in bets),
        "n_decided": metrics["decided"],
    })
    ns["stats"] = metrics
    return metrics


def migrate_ledger(ledger: dict[str, Any], system: str, *, now: str | None = None) -> dict[str, Any]:
    """Explicit idempotent migration entry point used by runtime loaders/tests."""
    return ensure_namespace(ledger, system, now=now)


def choose_admission(
    system: str, market: str, selected: dict[str, Any], candidates: Iterable[dict[str, Any],
    ], *, stage_at: str,
) -> tuple[dict[str, Any] | None, str]:
    """Select one exact market condition by raw safety margin, fail closed."""
    selected_sig = _selection_signature(market, selected)
    odds = _number(selected.get("odds"))
    if selected_sig is None:
        return None, "selected_line_or_side_invalid"
    if odds is None or odds <= 1:
        return None, "selected_odds_invalid_or_missing"
    grouped: dict[str, list[tuple[dict[str, Any], dict[str, Any], dict[str, Any]]]] = {}
    for candidate in candidates:
        if str(candidate.get("market") or "") != market or _selection_signature(market, candidate) != selected_sig:
            continue
        signature, definition = condition_signature(system, candidate)
        history = _historical(candidate, definition, stage_at)
        if history is not None:
            grouped.setdefault(signature, []).append((candidate, definition, history))
    eligible: list[dict[str, Any]] = []
    for signature, rows in grouped.items():
        baseline = rows[0][2]
        # Duplicate discovery rows for one condition must agree exactly.  A
        # disagreement is never resolved by cherry-picking a higher hit rate.
        if any(
            row[2]["hits"] != baseline["hits"] or row[2]["decided"] != baseline["decided"]
            or row[2]["artifact"] != baseline["artifact"] for row in rows[1:]
        ):
            continue
        arithmetic = admission_arithmetic(baseline["hits"], baseline["decided"], odds)
        if arithmetic is None or not arithmetic["passes"]:
            continue
        eligible.append({
            "signature": signature, "definition": rows[0][1], "history": baseline,
            "arithmetic": arithmetic, "candidate": rows[0][0],
            "safety_margin": arithmetic["wilson95_lower_raw"] - arithmetic["required_rate_raw"],
        })
    if not grouped:
        return None, "no_frozen_historical_condition"
    if not eligible:
        return None, "wilson_gate_not_passed"
    eligible.sort(key=lambda row: (
        -row["safety_margin"], -row["history"]["decided"],
        -row["arithmetic"]["wilson95_lower_raw"], row["signature"],
    ))
    return eligible[0], "wilson_pass"


def commit_bet(
    ledger: dict[str, Any], system: str, watch: dict[str, Any], market: str,
    selected: dict[str, Any], admission: dict[str, Any], *, now: str,
    market_label: str, selected_label: str, selected_role: str | None, selected_line: float,
) -> dict[str, Any] | None:
    ns = ensure_namespace(ledger, system, now=now)
    if market not in {"HDC", "HIL", "CHL"}:
        return None
    fixture = str(watch.get("match_id") or "")
    existing = active_bets(ledger, system)
    fixture_rows = [row for row in existing if str(row.get("match_id") or "") == fixture]
    if any(str(row.get("code") or row.get("market") or "") == market for row in fixture_rows):
        return None
    if len(fixture_rows) >= FIXTURE_MARKET_CAP or sum(_number(row.get("stake")) or 0 for row in fixture_rows) + FIXED_STAKE > FIXTURE_STAKE_CAP:
        return None
    signature = admission["signature"]
    frozen = ns["conditions"].get(signature)
    if frozen is None:
        frozen = {
            "signature": signature, "frozen_at": now, "definition": copy.deepcopy(admission["definition"]),
            "historical_evidence": copy.deepcopy(admission["history"]),
            "admission_arithmetic": copy.deepcopy(admission["arithmetic"]), "prospective": {},
        }
        ns["conditions"][signature] = frozen
    bid = f"{fixture}|{market}|{DECISION_STAGE}|{STRATEGY}"
    if any(str(row.get("bet_id") or "") == bid for row in fixture_rows):
        return None
    arithmetic = copy.deepcopy(admission["arithmetic"])
    return {
        "bet_id": bid, "portfolio": portfolio_name(system), "strategy": STRATEGY,
        "strategy_name": DISPLAY_NAME, "match_id": fixture, "league": watch.get("league"),
        "home": watch.get("home"), "away": watch.get("away"),
        "kickoff": watch.get("kickoff") or watch.get("kickoff_hkt"),
        "code": market, "market": market, "market_label": market_label, "side": selected.get("side"),
        "line": selected.get("line", selected.get("condition")), "condition": selected.get("line", selected.get("condition")),
        "selected_side": selected.get("side"), "selected_line": selected_line,
        "selected_role": selected_role, "label": selected_label,
        "odds": arithmetic["actual_decimal_odds_raw"], "stake": FIXED_STAKE,
        "stage": DECISION_STAGE, "first_stage": DECISION_STAGE, "status": "PENDING",
        "simulation_only": True, "real_betting_enabled": False, "created_at": now,
        "admission_at": now, "frozen_condition_signature": signature,
        "frozen_condition_definition": copy.deepcopy(admission["definition"]),
        "frozen_historical_evidence": copy.deepcopy(admission["history"]),
        "wilson_admission": arithmetic,
        "history": [{"ts": now, "stage": DECISION_STAGE, "action": "Wilson 模擬注建立",
                     "reason": "首次原生賽前 T-5；凍結歷史證據 Wilson 門檻通過"}],
    }
