"""Footbreak independent-validation portfolio (append-only, simulation-only)."""
from __future__ import annotations
import math
import sys
from datetime import datetime, timedelta, timezone
from typing import Any, Iterable
from pathlib import Path
ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
from analysis.granular_conditions import MARKET_LABELS, MARKETS, _role, match_upcoming, mine
from analysis.independent_validation import (AUDIT_LIMIT, DECISION_STAGE, FIXED_STAKE, STARTING_BANKROLL, STRATEGY, FIXTURE_MARKET_CAP, FIXTURE_STAKE_CAP, ensure_namespace, eligible, selection_signature, choose_candidate, conservative_key, attach_frozen_condition, existing_fixture_markets, portfolio_name, record_evaluation_diagnostics)
import json
HKT = timezone(timedelta(hours=8))
SYSTEM = "footbreak"
PORTFOLIO = portfolio_name(SYSTEM)
LOG_LIMIT = 100

def iso_hkt() -> str: return datetime.now(HKT).isoformat(timespec="seconds")
def parse_time(value: Any) -> datetime | None:
    try:
        result = datetime.fromisoformat(str(value or "").replace("Z", "+00:00")); return result.replace(tzinfo=HKT) if result.tzinfo is None else result
    except (TypeError, ValueError): return None
def _finite(value: Any) -> float | None:
    try: result = float(value)
    except (TypeError, ValueError): return None
    return result if math.isfinite(result) else None
def historical_rows_from_accuracy(history_path: Path) -> list[dict[str, Any]]:
    try: payload = json.loads(history_path.read_text(encoding="utf-8"))
    except (OSError, ValueError): return []
    return [{"match_id": match.get("match_id"), "kickoff": match.get("kickoff"), "predicted_at": stage.get("predicted_at"), "stage": stage.get("stage"), "market_grades": stage.get("market_grades") or []} for match in (payload.get("matches") or []) if isinstance(match, dict) for stage in (match.get("stages") or []) if isinstance(stage, dict)]
def _live_rows(watch: dict[str, Any]) -> list[dict[str, Any]]:
    return [{"match_id": str(watch.get("match_id") or ""), "stage": stage.get("stage"), "kickoff": watch.get("kickoff") or watch.get("kickoff_hkt"), "predicted_at": stage.get("ts") or stage.get("source_snapshot_at"), "market_predictions": stage.get("market_predictions") or []} for stage in (watch.get("stages") or []) if isinstance(stage, dict)]


def _valid_selected(stage: dict[str, Any], market: str) -> tuple[dict[str, Any] | None, str | None]:
    selected = [item for item in (stage.get("market_predictions") or []) if isinstance(item, dict) and str(item.get("code") or "").upper() == market]
    if len(selected) != 1:
        return None, "selected_market_missing_or_ambiguous"
    item = selected[0]
    odds, line = _finite(item.get("odds")), _finite(item.get("line", item.get("condition")))
    side = str(item.get("side") or "").upper()
    valid_sides = {"H", "A"} if market == "HDC" else {"H", "L"}
    kickoff = parse_time(stage.get("kickoff") or stage.get("kickoff_hkt"))
    observed_at = parse_time(item.get("observed_at"))
    if observed_at is None:
        numeric = _finite(item.get("observed_at"))
        if numeric is not None and numeric > 0:
            if numeric >= 10_000_000_000:
                numeric /= 1000
            try:
                observed_at = datetime.fromtimestamp(numeric, kickoff.tzinfo) if kickoff else None
            except (OverflowError, OSError, ValueError):
                observed_at = None
    if odds is None or odds <= 1:
        return None, "selected_odds_invalid_or_missing"
    if line is None or side not in valid_sides:
        return None, "selected_line_or_side_invalid"
    source = str(item.get("quote_source") or item.get("source") or "").strip().lower()
    if not source or source in {"none", "fallback", "model_only", "model-only", "unavailable"}:
        return None, "selected_source_observation_invalid_or_missing"
    if kickoff is None or observed_at is None or observed_at >= kickoff:
        return None, "selected_quote_not_provably_pre_kickoff"
    return item, None


def _audit_selection(market: str, item: dict[str, Any]) -> dict[str, Any]:
    side = str(item.get("side") or "").upper()
    line = _finite(item.get("line", item.get("condition")))
    role = _role(market, side, line)
    selected_line = -line if market == "HDC" and side == "A" and line is not None else line
    return {"selected_side": side, "selected_line": selected_line, "selected_odds": _finite(item.get("odds")), "selected_role": role,
            "selected_label": f"{MARKET_LABELS[market]} · {role or '—'}" + (f" {selected_line:g}" if selected_line is not None else "")}


def _native_t5(stage: dict[str, Any], kickoff: Any) -> bool:
    if stage.get("post_hoc_backfill") or stage.get("exclude_from_simulation"):
        return False
    saved, starts = parse_time(stage.get("ts") or stage.get("source_snapshot_at")), parse_time(kickoff)
    return saved is not None and starts is not None and saved < starts


def evaluate_new_t5(ledger: dict[str, Any], watch: dict[str, Any], history_path: Path | None = None, *, history_rows: Iterable[dict[str, Any]] | None = None, ranking: Iterable[dict[str, Any]] | None = None) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """Evaluate a first persisted native T-5 using a cached ranking when live.

    ``history_rows`` retains offline/test behaviour only. Production callers
    pass the persisted granular ranking, so deadline-bound ticks never mine.
    """
    fixture = str(watch.get("match_id") or "")
    now = iso_hkt()
    namespace = ensure_namespace(ledger, SYSTEM, now=now)

    def skipped(reason: str) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
        audit = [{"market": "*", "status": "SKIPPED", "reason": reason}]
        record_evaluation_diagnostics(namespace, fixture, DECISION_STAGE, audit, now=now)
        return [], audit

    stages = [row for row in (watch.get("stages") or []) if isinstance(row, dict)]
    current = next((row for row in stages if row.get("stage") == DECISION_STAGE), None)
    if not fixture or current is None:
        return skipped("missing_new_t5_snapshot")
    current = {**current, "kickoff": current.get("kickoff") or watch.get("kickoff") or watch.get("kickoff_hkt")}
    if not _native_t5(current, current.get("kickoff")):
        return skipped("not_first_native_pre_kickoff_t5")
    if not all(str(watch.get(field) or "").strip() for field in ("league", "home", "away")):
        return skipped("missing_fixture_context_for_public_condition_bet")
    if ranking is None:
        if history_rows is None:
            return skipped("cached_discovery_ranking_unavailable")
        rows = [row for row in history_rows if str(row.get("match_id") or row.get("history_key") or "") != fixture]
        ranking = mine(rows, system=SYSTEM)["ranking"]
    ranking = list(ranking or [])
    matches = match_upcoming(_live_rows(watch), ranking, system=SYSTEM, decision_stage=DECISION_STAGE).get(fixture, [])
    existing = existing_fixture_markets(ledger, SYSTEM, fixture)
    existing_markets = {str(bet.get("code") or bet.get("market") or "") for bet in existing}
    existing_stake = sum(float(bet.get("stake") or 0) for bet in existing)
    created, audit, proposals = [], [], []
    for market in MARKETS:
        selected, reason = _valid_selected(current, market)
        if selected is None:
            audit.append({"market": market, "market_label": MARKET_LABELS[market], "status": "SKIPPED", "reason": reason})
            continue
        quote_signature = selection_signature(market, _audit_selection(market, selected))
        market_matches = [
            item for item in matches
            if str(item.get("market") or "") == market
            and selection_signature(market, item) == quote_signature
        ]
        if not market_matches:
            # The cached ranking already excludes candidates that fail the
            # frozen >60% / >=20 admission gate. If a market survived that
            # gate but its exact live line/side does not match, report a true
            # granular mismatch; otherwise retain the established gate reason.
            market_ranked = any(
                str(item.get("market") or "") == market for item in ranking
                if isinstance(item, dict)
            )
            reason = (
                "no_granular_match" if market_ranked
                else "no_historical_condition_above_60pct_with_20_decided"
            )
            audit.append({"market": market, "market_label": MARKET_LABELS[market], "status": "SKIPPED", "reason": reason})
            continue
        candidates = [item for item in market_matches if eligible(item)]
        if not candidates:
            audit.append({"market": market, "market_label": MARKET_LABELS[market], "status": "SKIPPED", "reason": "no_historical_condition_above_60pct_with_20_decided"})
            continue
        if quote_signature is None:
            audit.append({"market": market, "market_label": MARKET_LABELS[market], "status": "SKIPPED", "reason": "selected_line_or_side_invalid"})
            continue
        if market in existing_markets:
            audit.append({"market": market, "market_label": MARKET_LABELS[market], "status": "SKIPPED", "reason": "idempotent_existing_market"})
            continue
        condition = choose_candidate(SYSTEM, candidates)
        if condition is None:
            audit.append({"market": market, "market_label": MARKET_LABELS[market], "status": "SKIPPED", "reason": "no_conservative_candidate"})
            continue
        proposals.append((market, selected, condition))
    # At most two markets and HK$500 per fixture. Candidate ordering is stable
    # and deliberately not based on raw accuracy.
    proposals.sort(key=lambda item: conservative_key(SYSTEM, item[2]))
    for market, selected, condition in proposals:
        if len(existing_markets) >= FIXTURE_MARKET_CAP:
            audit.append({"market": market, "market_label": MARKET_LABELS[market], "status": "SKIPPED", "reason": "fixture_two_market_cap"})
            continue
        if existing_stake + FIXED_STAKE > FIXTURE_STAKE_CAP + 1e-9:
            audit.append({"market": market, "market_label": MARKET_LABELS[market], "status": "SKIPPED", "reason": "fixture_stake_cap"})
            continue
        signature, frozen = attach_frozen_condition(namespace, SYSTEM, condition, now=now)
        bid = f"{fixture}|{market}|{DECISION_STAGE}|{STRATEGY}"
        if any(str(bet.get("bet_id") or "") == bid for bet in existing):
            audit.append({"market": market, "market_label": MARKET_LABELS[market], "status": "SKIPPED", "reason": "idempotent_existing_bet", "bet_id": bid})
            continue
        selection, baseline = _audit_selection(market, selected), frozen["discovery_baseline"]
        bet = {"bet_id": bid, "portfolio": portfolio_name(SYSTEM), "strategy": STRATEGY,
               "match_id": fixture, "league": watch.get("league"), "home": watch.get("home"), "away": watch.get("away"),
               "kickoff": watch.get("kickoff") or watch.get("kickoff_hkt"), "market": selected.get("market") or market,
               "market_label": MARKET_LABELS[market], "code": market, "condition": selected.get("line", selected.get("condition")),
               "line": selected.get("line", selected.get("condition")), "side": selected.get("side"), "odds": float(selected["odds"]),
               "stake": FIXED_STAKE, "stage": DECISION_STAGE, "first_stage": DECISION_STAGE, "status": "PENDING",
               "simulation_only": True, "real_betting_enabled": False, "created_at": now,
               "frozen_condition_signature": signature, "frozen_condition_definition": frozen["definition"],
               "discovery_baseline": baseline, "condition_label": baseline.get("label"), "condition_accuracy": baseline.get("accuracy"),
               "condition_hits": baseline.get("hits"), "condition_decided": baseline.get("decided"), "condition_badge": "歷史發現期（凍結）",
               "condition_odds_tier": baseline.get("odds_tier"), **selection,
               "label": selection["selected_label"],
               "history": [{"ts": now, "stage": DECISION_STAGE, "action": "獨立驗證注建立", "reason": "首次持久化原生賽前 T-5；凍結歷史發現基線", "condition": baseline.get("label")}]}
        created.append(bet); existing.append(bet); existing_markets.add(market); existing_stake += FIXED_STAKE
        audit.append({"market": market, "market_label": MARKET_LABELS[market], "status": "CREATED", "reason": "independent_validation_candidate_frozen", "bet_id": bid,
                      "condition_label": baseline.get("label"), "accuracy": baseline.get("accuracy"), "hits": baseline.get("hits"), "decided": baseline.get("decided"), "frozen_condition_signature": signature, **selection})
    namespace["audit"] = (namespace.get("audit") or [])[-AUDIT_LIMIT:]
    record_evaluation_diagnostics(namespace, fixture, DECISION_STAGE, audit, now=now)
    return created, audit
