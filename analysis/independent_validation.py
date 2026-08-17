"""Shared, append-only independent-validation portfolio primitives.

The old granular-condition portfolio is deliberately never mutated.  Each
system gets a separate ``independent_validation`` namespace alongside its
legacy ledger rows; new bets reference frozen discovery conditions there.
"""
from __future__ import annotations

import hashlib
import json
import math
import copy
from datetime import datetime, timezone
from typing import Any, Iterable

SCHEMA_VERSION = 1
STARTING_BANKROLL = 50_000.0
FIXED_STAKE = 250.0
FIXTURE_STAKE_CAP = 500.0
FIXTURE_MARKET_CAP = 2
DECISION_STAGE = "T-5"
STRATEGY = "independent-validation-v1"
AUDIT_LIMIT = 1_600
CONDITION_AUDIT_LIMIT = 400
DIAGNOSTIC_EVALUATION_LIMIT = 600

# These codes are intentionally coarse. They describe the admission decision
# without carrying a provider quote, candidate collection, team name, or other
# raw input into the public dashboard payload.
DIAGNOSTIC_LABELS = {
    "stage_not_eligible": "非原生／階段不符／開賽後",
    "selected_quote_invalid": "入選盤口、賠率、方向或來源時間戳無效",
    "no_granular_match": "沒有相符的細緻歷史條件",
    "historical_gate_not_passed": "沒有通過凍結歷史門檻（命中率＞60%、已判定≥20）",
    "conservative_selection_failed": "保守條件選擇未能完成",
    "same_market_conflict": "同市場已有衝突或重播紀錄",
    "fixture_cap_reached": "已達單場市場或注碼上限",
    "created": "已建立獨立驗證注",
    "other_rejection": "其他安全拒絕",
}

_DIAGNOSTIC_REASON_CODES = {
    "missing_new_t5_snapshot": "stage_not_eligible",
    "not_first_native_pre_kickoff_t5": "stage_not_eligible",
    "t5_safe_lead_not_met": "stage_not_eligible",
    "missing_fixture_context_for_public_condition_bet": "stage_not_eligible",
    "cached_discovery_ranking_unavailable": "no_granular_match",
    "selected_market_missing_or_ambiguous": "selected_quote_invalid",
    "selected_odds_invalid_or_missing": "selected_quote_invalid",
    "selected_line_or_side_invalid": "selected_quote_invalid",
    "selected_source_observation_invalid_or_missing": "selected_quote_invalid",
    "selected_quote_not_provably_pre_kickoff": "selected_quote_invalid",
    "no_granular_match": "no_granular_match",
    "no_historical_condition_above_60pct_with_20_decided": "historical_gate_not_passed",
    "no_conservative_candidate": "conservative_selection_failed",
    "idempotent_existing_market": "same_market_conflict",
    "idempotent_existing_bet": "same_market_conflict",
    "fixture_two_market_cap": "fixture_cap_reached",
    "fixture_stake_cap": "fixture_cap_reached",
    "independent_validation_candidate_frozen": "created",
}


def _number(value: Any) -> float | None:
    try:
        value = float(value)
    except (TypeError, ValueError):
        return None
    return value if math.isfinite(value) else None


def _iso_now() -> str:
    return datetime.now(timezone.utc).astimezone().isoformat(timespec="seconds")


def portfolio_name(system: str) -> str:
    return f"{system}_independent_validation"


def ensure_namespace(ledger: dict[str, Any], system: str, *, now: str | None = None) -> dict[str, Any]:
    """Append versioned validation metadata without touching legacy records."""
    ledger.setdefault("bets", [])
    namespace = ledger.get("independent_validation")
    if namespace is None:
        namespace = {}
        ledger["independent_validation"] = namespace
    elif not isinstance(namespace, dict):
        # Never silently replace a malformed persisted namespace: doing so
        # would hide an operator-visible migration problem.
        raise ValueError("independent-validation namespace must be an object")
    actual_version = namespace.get("schema_version")
    actual_system = namespace.get("system")
    if actual_version is not None and actual_version != SCHEMA_VERSION:
        raise ValueError(f"unsupported independent-validation schema: {actual_version!r}")
    if actual_system is not None and actual_system != system:
        raise ValueError(f"independent-validation system mismatch: {actual_system!r}")
    namespace.setdefault("schema_version", SCHEMA_VERSION)
    namespace.setdefault("system", system)
    namespace.setdefault("validation_started_at", now or _iso_now())
    namespace.setdefault("starting_bankroll", STARTING_BANKROLL)
    namespace.setdefault("fixed_stake", FIXED_STAKE)
    namespace.setdefault("fixture_stake_cap", FIXTURE_STAKE_CAP)
    namespace.setdefault("fixture_market_cap", FIXTURE_MARKET_CAP)
    namespace.setdefault("conditions", {})
    namespace.setdefault("audit", [])
    namespace.setdefault("diagnostics", {"evaluations": {}})
    # This snapshot is written only at cutover, before active validation
    # recomputation replaces the conventional top-level public stats.  The
    # original legacy bet rows stay in ``ledger["bets"]`` untouched as well.
    namespace.setdefault("historical_discovery_archive", {
        "read_only": True,
        "legacy_bets_preserved": True,
        "legacy_stats_preserved": True,
        "legacy_bet_count": len([
            bet for bet in ledger.get("bets") or []
            if isinstance(bet, dict) and bet.get("portfolio") != portfolio_name(system)
        ]),
        "legacy_bankroll": copy.deepcopy(ledger.get("bankroll")),
        "legacy_stats": copy.deepcopy(ledger.get("stats") or {}),
    })
    if not isinstance(namespace["conditions"], dict):
        namespace["conditions"] = {}
    if not isinstance(namespace["audit"], list):
        namespace["audit"] = []
    diagnostics = namespace.get("diagnostics")
    if not isinstance(diagnostics, dict):
        namespace["diagnostics"] = diagnostics = {}
    if not isinstance(diagnostics.get("evaluations"), dict):
        diagnostics["evaluations"] = {}
    return namespace


def record_evaluation_diagnostics(
    namespace: dict[str, Any],
    fixture: str,
    stage: str,
    audit: Iterable[dict[str, Any]],
    *,
    now: str,
) -> None:
    """Store one bounded, replay-safe aggregate input per fixture/market/stage.

    The ledger keeps the key only to replace a replay of the same evaluation.
    The dashboard receives ``public_diagnostics`` below, never these keys or
    raw audit rows.
    """
    diagnostics = namespace.setdefault("diagnostics", {})
    evaluations = diagnostics.setdefault("evaluations", {})
    if not isinstance(evaluations, dict):
        diagnostics["evaluations"] = evaluations = {}
    for row in audit:
        if not isinstance(row, dict):
            continue
        market = str(row.get("market") or "*")
        reason = str(row.get("reason") or "")
        code = _DIAGNOSTIC_REASON_CODES.get(
            reason,
            "created" if str(row.get("status") or "").upper() == "CREATED" else "other_rejection",
        )
        # A digest remains deterministically keyed by fixture+market+stage
        # without retaining a provider identity in this diagnostic namespace.
        identity = json.dumps(
            [str(fixture or "_missing_fixture_"), market, str(stage or "_missing_stage_")],
            ensure_ascii=False,
            separators=(",", ":"),
        )
        key = hashlib.sha256(identity.encode("utf-8")).hexdigest()[:32]
        evaluations[key] = {"code": code, "updated_at": now}
    # Preserve insertion order: rewriting an existing key on replay does not
    # inflate the window or make the aggregate non-deterministic.
    while len(evaluations) > DIAGNOSTIC_EVALUATION_LIMIT:
        evaluations.pop(next(iter(evaluations)))


def public_diagnostics(namespace: dict[str, Any] | None) -> dict[str, Any]:
    """Return only bounded Chinese aggregate diagnostics for public consumers."""
    diagnostics = (namespace or {}).get("diagnostics")
    evaluations = diagnostics.get("evaluations") if isinstance(diagnostics, dict) else {}
    counts = {code: 0 for code in DIAGNOSTIC_LABELS}
    if isinstance(evaluations, dict):
        for row in evaluations.values():
            if isinstance(row, dict):
                code = str(row.get("code") or "other_rejection")
                counts[code if code in counts else "other_rejection"] += 1
    return {
        "window_limit": DIAGNOSTIC_EVALUATION_LIMIT,
        "evaluated": sum(counts.values()),
        "labels": DIAGNOSTIC_LABELS.copy(),
        "counts": counts,
    }


def condition_definition(system: str, candidate: dict[str, Any]) -> dict[str, Any]:
    key = candidate.get("key") or []
    normalized_key = [str(value) for value in key] if isinstance(key, list) else [str(key)]
    # Persist all matcher-defining axes, including the canonical miner key.
    def axis(*names: str, prefixes: tuple[str, ...] = ()) -> Any:
        for name in names:
            value = candidate.get(name)
            if value not in (None, ""):
                return str(value)
        for prefix in prefixes:
            found = next(
                (value.split("=", 1)[1] for value in normalized_key if value.startswith(prefix)),
                None,
            )
            if found not in (None, ""):
                return found
        return None

    return {
        "version": str(candidate.get("version") or candidate.get("condition_version") or "granular-condition-v1"),
        "system": system,
        "market": str(candidate.get("market") or ""),
        "stage": str(candidate.get("decision_stage") or candidate.get("stage") or ""),
        "path": str(candidate.get("observed_path") or candidate.get("path") or ""),
        "direction": axis("direction", "selected_side", "side", prefixes=("direction=", "side=")),
        "role": axis("role", "selected_role", prefixes=("role=",)),
        "line_bucket": axis("line_bucket", "bucket", prefixes=("line_bucket=", "bucket=")),
        "odds_tier": str(candidate.get("odds_tier") or ""),
        "movement": axis("movement", prefixes=("movement=",)),
        "odds_trajectory": axis("odds_trajectory", "tier_path", prefixes=("odds_trajectory=", "tier_path=")),
        "miner_key": normalized_key,
    }


def condition_signature(system: str, candidate: dict[str, Any]) -> tuple[str, dict[str, Any]]:
    definition = condition_definition(system, candidate)
    encoded = json.dumps(definition, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()[:24], definition


def discovery_baseline(candidate: dict[str, Any]) -> dict[str, Any]:
    total = candidate.get("total") if isinstance(candidate.get("total"), dict) else {}
    return {
        "hits": int(total.get("hits") or 0),
        "decided": int(total.get("decided") or 0),
        "pushes": int(total.get("pushes") or 0),
        "accuracy": _number(total.get("accuracy")),
        "wilson95": total.get("wilson95"),
        "specificity": int(candidate.get("specificity") or 0),
        "label": candidate.get("label"),
        "odds_tier": candidate.get("odds_tier"),
    }


def eligible(candidate: dict[str, Any]) -> bool:
    total = candidate.get("total") if isinstance(candidate.get("total"), dict) else {}
    return int(total.get("decided") or 0) >= 20 and (_number(total.get("accuracy")) or 0) > .60


def conservative_key(system: str, candidate: dict[str, Any]) -> tuple[Any, ...]:
    """Lower tuple wins: Wilson first, then sample, broad definitions, signature."""
    total = candidate.get("total") if isinstance(candidate.get("total"), dict) else {}
    wilson = total.get("wilson95")
    lower = _number(wilson[0]) if isinstance(wilson, (list, tuple)) and wilson else None
    sig, _ = condition_signature(system, candidate)
    # If no usable Wilson exists, do not substitute raw accuracy: sample size
    # and lower specificity are the only conservative fallback evidence.
    return (0 if lower is not None else 1, -(lower or 0.0), -int(total.get("decided") or 0), int(candidate.get("specificity") or 0), sig)


def choose_candidate(system: str, candidates: Iterable[dict[str, Any]]) -> dict[str, Any] | None:
    choices = [item for item in candidates if eligible(item)]
    return min(choices, key=lambda item: conservative_key(system, item)) if choices else None


def selection_signature(market: str, item: dict[str, Any]) -> tuple[str, float] | None:
    side = str(item.get("selected_side") or item.get("side") or "").upper()
    line = _number(item.get("selected_line", item.get("line", item.get("condition"))))
    valid = {"H", "A"} if market == "HDC" else {"H", "L"}
    if side not in valid or line is None:
        return None
    if "selected_line" not in item and market == "HDC" and side == "A":
        line = -line
    return side, round(line, 8)


def validation_bets(ledger: dict[str, Any], system: str) -> list[dict[str, Any]]:
    portfolio = portfolio_name(system)
    return [bet for bet in ledger.get("bets") or [] if isinstance(bet, dict) and bet.get("portfolio") == portfolio and bet.get("strategy") == STRATEGY]


def existing_fixture_markets(ledger: dict[str, Any], system: str, fixture: str) -> list[dict[str, Any]]:
    return [bet for bet in validation_bets(ledger, system) if str(bet.get("match_id") or "") == fixture]


def attach_frozen_condition(namespace: dict[str, Any], system: str, candidate: dict[str, Any], *, now: str) -> tuple[str, dict[str, Any]]:
    signature, definition = condition_signature(system, candidate)
    conditions = namespace["conditions"]
    frozen = conditions.get(signature)
    if not isinstance(frozen, dict):
        frozen = {
            "signature": signature,
            "frozen_at": now,
            "definition": definition,
            "discovery_baseline": discovery_baseline(candidate),
            "prospective": {},
            "audit": [{
                "ts": now,
                "action": "frozen_discovery_baseline_created",
                "reason": "first_independent_validation_admission",
            }],
        }
        conditions[signature] = frozen
    elif not isinstance(frozen.get("audit"), list):
        frozen["audit"] = []
    frozen["audit"] = frozen.get("audit", [])[-CONDITION_AUDIT_LIMIT:]
    # Never update definition/baseline after the first persisted use.
    return signature, frozen


def implied_break_even(bets: Iterable[dict[str, Any]]) -> float | None:
    weighted, weight = 0.0, 0.0
    for bet in bets:
        if bet.get("result") == "Refunded":
            continue
        odds, stake = _number(bet.get("odds")), _number(bet.get("stake"))
        if odds is None or odds <= 1 or stake is None or stake <= 0:
            continue
        weighted += stake / odds
        weight += stake
    return weighted / weight if weight else None


def prospective_metrics(bets: Iterable[dict[str, Any]]) -> dict[str, Any]:
    settled = [bet for bet in bets if bet.get("status") == "SETTLED"]
    pushes = sum(bet.get("result") == "Refunded" for bet in settled)
    decided = [bet for bet in settled if bet.get("result") != "Refunded"]
    hits = sum(bet.get("result") in {"Won", "Half Won"} for bet in decided)
    pnl = round(sum(_number(bet.get("pnl")) or 0.0 for bet in settled), 2)
    turnover = round(sum(_number(bet.get("stake")) or 0.0 for bet in settled), 2)
    accuracy = hits / len(decided) if decided else None
    roi = pnl / turnover if turnover else None
    breakeven = implied_break_even(decided)
    if len(decided) < 30:
        status = "驗證中"
    elif roi is not None and roi > 0 and accuracy is not None and breakeven is not None and accuracy > breakeven + .03:
        status = "已驗證"
    else:
        status = "觀察"
    return {
        "decided": len(decided), "hits": hits, "pushes": pushes,
        "pnl": pnl, "turnover": turnover, "roi": round(roi, 6) if roi is not None else None,
        "accuracy": round(accuracy, 6) if accuracy is not None else None,
        "weighted_implied_break_even": round(breakeven, 6) if breakeven is not None else None,
        "status": status,
    }


def recompute_namespace(ledger: dict[str, Any], system: str) -> dict[str, Any]:
    ns = ensure_namespace(ledger, system)
    bets = validation_bets(ledger, system)
    by_signature: dict[str, list[dict[str, Any]]] = {}
    for bet in bets:
        signature = str(bet.get("frozen_condition_signature") or "")
        if signature:
            by_signature.setdefault(signature, []).append(bet)
    for signature, frozen in ns["conditions"].items():
        if isinstance(frozen, dict):
            frozen["prospective"] = prospective_metrics(by_signature.get(signature, []))
    aggregate = prospective_metrics(bets)
    settled = [bet for bet in bets if bet.get("status") == "SETTLED"]
    pending = [bet for bet in bets if bet.get("status") == "PENDING"]
    decided = [bet for bet in settled if bet.get("result") != "Refunded"]
    by_market: dict[str, dict[str, Any]] = {}
    for bet in settled:
        market = str(bet.get("market_label") or bet.get("market") or bet.get("code") or "其他")
        item = by_market.setdefault(market, {"n": 0, "stake": 0.0, "pnl": 0.0, "hit": 0, "dec": 0})
        item["n"] += 1
        item["stake"] += _number(bet.get("stake")) or 0.0
        item["pnl"] += _number(bet.get("pnl")) or 0.0
        if bet.get("result") != "Refunded":
            item["dec"] += 1
            item["hit"] += int(bet.get("result") in {"Won", "Half Won"})
    for item in by_market.values():
        item["stake"] = round(item["stake"], 2)
        item["pnl"] = round(item["pnl"], 2)
        item["roi"] = round(item["pnl"] / item["stake"], 6) if item["stake"] else None
        item["hit_rate"] = round(item["hit"] / item["dec"], 6) if item["dec"] else None
    equity = STARTING_BANKROLL
    curve = []
    for bet in sorted(settled, key=lambda row: str(row.get("settled_at") or row.get("created_at") or "")):
        bet_pnl = _number(bet.get("pnl")) or 0.0
        equity += bet_pnl
        curve.append({
            "ts": bet.get("settled_at") or bet.get("created_at"),
            "label": f"{bet.get('home') or ''} v {bet.get('away') or ''}".strip(),
            "pnl": round(bet_pnl, 2),
            "equity": round(equity, 2),
        })
    open_stake = round(sum((_number(bet.get("stake")) or 0.0) for bet in pending), 2)
    aggregate.update({
        "portfolio": portfolio_name(system), "strategy": STRATEGY,
        "starting_bankroll": STARTING_BANKROLL, "fixed_stake": FIXED_STAKE,
        "fixture_stake_cap": FIXTURE_STAKE_CAP, "fixture_market_cap": FIXTURE_MARKET_CAP,
        "equity": round(STARTING_BANKROLL + aggregate["pnl"], 2),
        "cash": round(STARTING_BANKROLL + aggregate["pnl"] - open_stake, 2),
        "n_pending": len(pending),
        "n_settled": len(settled), "n_voided": sum(b.get("status") == "VOIDED" for b in bets),
        "n_decided": len(decided), "hit_rate": aggregate["accuracy"],
        "open_stake": open_stake,
        "open_pct": round(open_stake / STARTING_BANKROLL, 6),
        "by_market": by_market, "curve": curve,
        "res_counts": {
            label: sum(bet.get("result") == label for bet in settled)
            for label in ("Won", "Half Won", "Refunded", "Half Lost", "Lost")
        },
        "conditions": ns["conditions"],
        "historical_discovery_archive": ns["historical_discovery_archive"],
    })
    ns["stats"] = aggregate
    return aggregate
