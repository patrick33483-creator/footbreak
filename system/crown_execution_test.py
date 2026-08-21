"""Fail-closed Footbreak × Crown execution simulation.

This module consumes only persisted local Crown quote artifacts.  It does not
import the Crown engine and never performs network or provider work; a missing
or ambiguous local artifact is a durable rejection, not a fallback price.
"""
from __future__ import annotations

import copy
import json
import math
import os
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Iterable

from analysis.granular_conditions import MARKET_LABELS, MARKETS, _role, match_upcoming
from analysis.wilson_portfolio import _audit_selection, _native_t5, _selected
from analysis.wilson_validation import (
    DECISION_STAGE, EDGE_BUFFER, FIXED_STAKE, FIXTURE_MARKET_CAP,
    FIXTURE_STAKE_CAP, STARTING_BANKROLL, STRATEGY, admission_arithmetic,
    ensure_namespace, matching_admissions,
)

NAMESPACE = "footbreak_crown_execution_test"
PORTFOLIO = NAMESPACE
DISPLAY_NAME = "足破×皇冠執行測試倉（模擬）"
CROWN_SOURCE = "titan007-crown-id-3"
AUDIT_LIMIT = 1600
FRESHNESS_SECONDS = 120.0
HKT = timezone(timedelta(hours=8))


def _num(value: Any) -> float | None:
    try:
        result = float(value)
    except (TypeError, ValueError):
        return None
    return result if math.isfinite(result) else None


def _time(value: Any) -> datetime | None:
    number = _num(value)
    if number is not None and number > 0:
        if number >= 10_000_000_000:
            number /= 1000
        try:
            return datetime.fromtimestamp(number, timezone.utc)
        except (OverflowError, OSError, ValueError):
            return None
    try:
        parsed = datetime.fromisoformat(str(value or "").replace("Z", "+00:00"))
    except (TypeError, ValueError):
        return None
    return parsed.replace(tzinfo=timezone.utc) if parsed.tzinfo is None else parsed.astimezone(timezone.utc)


def _iso_now() -> str:
    return datetime.now(HKT).isoformat(timespec="seconds")


def _freshness_seconds() -> float:
    try:
        configured = float(os.environ.get("FOOTBREAK_CROWN_EXECUTION_MAX_AGE_SECONDS", FRESHNESS_SECONDS))
    except ValueError:
        configured = FRESHNESS_SECONDS
    return max(1.0, min(600.0, configured))


def evidence_path() -> Path:
    explicit = os.environ.get("FOOTBREAK_CROWN_EXECUTION_EVIDENCE_PATH")
    if explicit:
        return Path(explicit)
    state_dir = os.environ.get("CROWN_STATE_DIR", "/var/lib/footbreak/crown")
    return Path(state_dir) / "footbreak-execution-evidence.json"


def _load_local_crown_cards() -> tuple[list[dict[str, Any]], str | None]:
    """Read bounded persisted state only; never repair it by calling Crown."""
    path = evidence_path()
    try:
        if path.stat().st_size > 8_000_000:
            return [], "crown_local_evidence_too_large"
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError, json.JSONDecodeError):
        return [], "crown_local_evidence_unavailable"
    if not isinstance(payload, list):
        return [], "crown_local_evidence_invalid"
    return [row for row in payload if isinstance(row, dict)], None


def ensure_namespace(ledger: dict[str, Any], *, now: str | None = None) -> dict[str, Any]:
    ns = ledger.get(NAMESPACE)
    if ns is None:
        ns = {}
        ledger[NAMESPACE] = ns
    if not isinstance(ns, dict):
        raise ValueError("cross-book namespace must be an object")
    ns.setdefault("schema_version", 1)
    ns.setdefault("display_name", DISPLAY_NAME)
    ns.setdefault("activation_at", now or _iso_now())
    ns.setdefault("starting_bankroll", STARTING_BANKROLL)
    ns.setdefault("fixed_stake", FIXED_STAKE)
    ns.setdefault("fixture_stake_cap", FIXTURE_STAKE_CAP)
    ns.setdefault("fixture_market_cap", FIXTURE_MARKET_CAP)
    ns.setdefault("bets", [])
    ns.setdefault("audit", [])
    ns.setdefault("notifications", {"sent": []})
    if not isinstance(ns["bets"], list) or not isinstance(ns["audit"], list):
        raise ValueError("cross-book namespace collections must be arrays")
    return ns


def bets(ledger: dict[str, Any]) -> list[dict[str, Any]]:
    ns = ledger.get(NAMESPACE)
    return list(ns.get("bets") or []) if isinstance(ns, dict) else []


def _append_audit(ns: dict[str, Any], now: str, fixture: str, row: dict[str, Any]) -> None:
    ns["audit"] = (ns.get("audit") or []) + [{"ts": now, "match_id": fixture, **row}]
    ns["audit"] = ns["audit"][-AUDIT_LIMIT:]


def _valid_side(market: str, side: Any) -> bool:
    return str(side or "").upper() in ({"H", "A"} if market == "HDC" else {"H", "L"})


def _crown_quote_for_exact_fixture(
    fixture: str, market: str, side: str, line: float, stage_at: datetime, kickoff: datetime,
) -> tuple[dict[str, Any] | None, str | None]:
    cards, error = _load_local_crown_cards()
    if error:
        return None, error
    # Cross-book identity is allowed only through the persisted Crown HKJC
    # bridge.  Title/team fuzzy matching is deliberately forbidden.
    # An empty local sidecar cannot establish that Crown had no same-fixture
    # market: it is a missing-native-T-5 collection/evidence state.  Once
    # cards exist, a missing or ambiguous bridge identity remains the genuine
    # fail-closed no-same-fixture outcome.
    if not cards:
        return None, "crown_native_t5_not_collected"
    cards = [row for row in cards if str(row.get("hkjc_match_id") or "") == fixture]
    if len(cards) != 1:
        return None, "crown_fixture_identity_missing_or_ambiguous"
    card = cards[0]
    card_kickoff = _time(card.get("kickoff_hkt") or card.get("kickoff"))
    if card_kickoff is None or abs((card_kickoff - kickoff).total_seconds()) > 1:
        return None, "crown_fixture_kickoff_identity_mismatch"
    journal = card.get("current_selected_odds_journal")
    if not isinstance(journal, list):
        return None, "crown_exact_quote_journal_missing"
    exact = []
    for row in journal:
        if not isinstance(row, dict) or str(row.get("code") or "").upper() != market:
            continue
        if str(row.get("side") or "").upper() != side:
            continue
        quote_line = _num(row.get("line", row.get("condition")))
        if quote_line is None or abs(quote_line - line) > 1e-8:
            continue
        exact.append(row)
    if len(exact) != 1:
        return None, "crown_exact_market_side_line_missing_or_ambiguous"
    quote = exact[0]
    odds = _num(quote.get("odds"))
    source = str(quote.get("source") or "").strip().lower()
    observed = _time(quote.get("observed_at"))
    if odds is None or odds <= 1:
        return None, "crown_execution_odds_invalid_or_missing"
    if source != CROWN_SOURCE:
        return None, "crown_execution_source_invalid_or_missing"
    if observed is None:
        return None, "crown_execution_timestamp_missing"
    if observed >= kickoff or observed > stage_at:
        return None, "crown_execution_post_kickoff_or_post_decision"
    if (stage_at - observed).total_seconds() > _freshness_seconds():
        return None, "crown_execution_quote_stale_at_t5"
    if str(quote.get("odds_status") or "available") != "available":
        return None, "crown_execution_quote_not_available"
    return {
        "odds": odds, "source": source, "observed_at": quote.get("observed_at"),
        "line": quote.get("line", quote.get("condition")), "side": side,
        "fixture_identity": {"hkjc_match_id": fixture, "crown_hkjc_match_id": fixture},
    }, None


def _hkjc_selected(current: dict[str, Any], market: str, watch: dict[str, Any]) -> tuple[dict[str, Any] | None, str | None]:
    selected, reason = _selected(current, market, _time, fixture_kickoff=watch.get("kickoff"))
    if selected is None:
        return None, reason
    source = str(selected.get("source") or "").strip().lower()
    observed = _time(selected.get("observed_at"))
    stage_at = _time(current.get("ts") or current.get("source_snapshot_at"))
    if source not in {"hkjc_public_board", "hkjc-current-board"}:
        return None, "hkjc_signal_source_non_native_or_missing"
    if observed is None:
        return None, "hkjc_signal_timestamp_missing"
    if stage_at is None or observed > stage_at:
        return None, "hkjc_signal_post_decision_or_stage_timestamp_missing"
    if (stage_at - observed).total_seconds() > _freshness_seconds():
        return None, "hkjc_signal_quote_stale_at_t5"
    return selected, None


def _same_market_existing(rows: Iterable[dict[str, Any]], fixture: str, market: str, side: str, line: float, signature: str) -> bool:
    return any(
        str(row.get("match_id") or "") == fixture
        and str(row.get("code") or "") == market
        and str(row.get("side") or "").upper() == side
        and _num(row.get("line")) is not None and abs(float(_num(row.get("line"))) - line) <= 1e-8
        and str(row.get("frozen_condition_signature") or "") == signature
        for row in rows if isinstance(row, dict)
    )


def _active_existing_admission(
    ledger: dict[str, Any], admission: dict[str, Any], execution_odds: float,
) -> tuple[dict[str, Any] | None, str | None]:
    """Read an already frozen Wilson version without changing its chain."""
    signature = str(admission.get("signature") or "")
    frozen = ((ledger.get("wilson_validation") or {}).get("conditions") or {}).get(signature)
    active = frozen.get("active_evidence") if isinstance(frozen, dict) else None
    if not isinstance(active, dict):
        return None, "active_wilson_condition_unavailable"
    arithmetic = admission_arithmetic(
        int(active.get("cumulative_hits", 0)), int(active.get("cumulative_decided", 0)),
        execution_odds,
    )
    if arithmetic is None:
        return None, "active_evidence_arithmetic_invalid"
    updated = copy.deepcopy(admission)
    updated["history"] = {**copy.deepcopy(admission.get("history") or {}),
                          "hits": int(active["cumulative_hits"]),
                          "decided": int(active["cumulative_decided"]),
                          "evidence_version": active.get("version"),
                          "evidence_hash": active.get("evidence_hash")}
    updated["arithmetic"] = arithmetic
    updated["evidence_version"] = active.get("version")
    updated["evidence_hash"] = active.get("evidence_hash")
    return updated, None


def evaluate_new_t5(
    ledger: dict[str, Any], watch: dict[str, Any], *, ranking: Iterable[dict[str, Any]] | None,
    now: str | None = None,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """Commit only exact, fresh cross-book simulations after a native Footbreak T-5."""
    now = now or _iso_now()
    ns = ensure_namespace(ledger, now=now)
    fixture = str(watch.get("match_id") or "")
    audit: list[dict[str, Any]] = []
    if not fixture or ranking is None:
        reason = "missing_fixture_or_frozen_ranking"
        audit.append({"market": "*", "status": "SKIPPED", "reason": reason})
        _append_audit(ns, now, fixture, audit[-1])
        return [], audit
    current_rows = [row for row in watch.get("stages") or [] if isinstance(row, dict) and row.get("stage") == DECISION_STAGE]
    current = current_rows[0] if len(current_rows) == 1 else None
    if current is None or not _native_t5(watch, current, _time):
        audit.append({"market": "*", "status": "SKIPPED", "reason": "not_first_native_pre_kickoff_t5"})
        _append_audit(ns, now, fixture, audit[-1]); return [], audit
    stage_at = _time(current.get("ts") or current.get("source_snapshot_at"))
    kickoff = _time(watch.get("kickoff") or current.get("kickoff"))
    decision_at = _time(now)
    if (
        stage_at is None or kickoff is None or stage_at >= kickoff
        or decision_at is None or decision_at >= kickoff
    ):
        audit.append({"market": "*", "status": "SKIPPED", "reason": "t5_decision_timestamp_invalid"})
        _append_audit(ns, now, fixture, audit[-1]); return [], audit
    current_rows_for_match = [{"match_id": fixture, "stage": DECISION_STAGE, "kickoff": watch.get("kickoff"),
                               "predicted_at": current.get("ts"), "market_predictions": current.get("market_predictions") or []}]
    matched = match_upcoming(current_rows_for_match, list(ranking), system="footbreak", decision_stage=DECISION_STAGE).get(fixture, [])
    proposed: list[dict[str, Any]] = []
    for market in MARKETS:
        hkjc, reason = _hkjc_selected(current, market, watch)
        if hkjc is None:
            audit.append({"market": market, "status": "SKIPPED", "reason": reason}); continue
        side = str(hkjc.get("side") or "").upper(); line = _num(hkjc.get("line", hkjc.get("condition")))
        if line is None or not _valid_side(market, side):
            audit.append({"market": market, "status": "SKIPPED", "reason": "hkjc_signal_line_or_side_invalid"}); continue
        admissions, reason = matching_admissions("footbreak", market, hkjc, matched, stage_at=str(current.get("ts") or now))
        if not admissions:
            audit.append({"market": market, "status": "SKIPPED", "reason": reason}); continue
        quote, reason = _crown_quote_for_exact_fixture(fixture, market, side, line, stage_at, kickoff)
        if quote is None:
            audit.append({"market": market, "status": "SKIPPED", "reason": reason}); continue
        for admission in admissions:
            signature = str(admission["signature"])
            # The normal Footbreak Wilson evaluator owns freezing/versioning of
            # the historical condition.  A cross-book price must never create
            # or revise that chain on its own.
            frozen_conditions = (ledger.get("wilson_validation") or {}).get("conditions") or {}
            if not isinstance(frozen_conditions.get(signature), dict):
                audit.append({"market": market, "status": "SKIPPED",
                              "reason": "active_wilson_condition_unavailable"})
                continue
            if _same_market_existing(ns["bets"], fixture, market, side, line, signature):
                audit.append({"market": market, "status": "SKIPPED", "reason": "idempotent_existing_exact_entry", "condition_number": (ledger.get("wilson_validation") or {}).get("conditions", {}).get(signature, {}).get("condition_number")})
                continue
            # The condition was selected by the native HKJC quote/tier above;
            # only Crown's exact execution price enters Wilson arithmetic.
            adjusted, reason = _active_existing_admission(ledger, admission, quote["odds"])
            if adjusted is None:
                audit.append({"market": market, "status": "SKIPPED", "reason": reason or "active_evidence_unavailable"}); continue
            if not adjusted["arithmetic"].get("passes"):
                audit.append({"market": market, "status": "MATCHED_NO_BET", "reason": "crown_wilson_gate_not_passed", "condition_number": (ledger.get("wilson_validation") or {}).get("conditions", {}).get(signature, {}).get("condition_number"), "wilson_admission": adjusted["arithmetic"]}); continue
            proposed.append({"market": market, "hkjc": hkjc, "quote": quote, "admission": adjusted, "line": line, "side": side})
            break
    created: list[dict[str, Any]] = []
    for item in proposed:
        if len([b for b in ns["bets"] if str(b.get("match_id") or "") == fixture]) >= FIXTURE_MARKET_CAP or sum(float(b.get("stake") or 0) for b in ns["bets"] if str(b.get("match_id") or "") == fixture) + FIXED_STAKE > FIXTURE_STAKE_CAP:
            audit.append({"market": item["market"], "status": "SKIPPED", "reason": "fixture_cap_reached"}); continue
        admission = item["admission"]; frozen = (ledger.get("wilson_validation") or {}).get("conditions", {}).get(admission["signature"], {})
        role, selected_line, label = _audit_selection(item["market"], item["hkjc"])
        bid = f"{fixture}|{item['market']}|{item['side']}|{item['line']:g}|{admission['signature']}|crown-execution-v1"
        if any(str(row.get("bet_id") or "") == bid for row in ns["bets"]):
            audit.append({"market": item["market"], "status": "SKIPPED", "reason": "idempotent_existing_exact_entry"}); continue
        bet = {
            "bet_id": bid, "portfolio": PORTFOLIO, "strategy": "footbreak-crown-execution-test-v1", "strategy_name": DISPLAY_NAME,
            "match_id": fixture, "league": watch.get("league"), "home": watch.get("home"), "away": watch.get("away"), "kickoff": watch.get("kickoff"), "fixture_id": watch.get("fixture_id"),
            "fixture_identity": item["quote"]["fixture_identity"], "code": item["market"], "market": item["market"], "market_label": MARKET_LABELS[item["market"]], "side": item["side"], "line": item["line"], "condition": item["line"], "selected_role": role, "selected_line": selected_line,
            "hkjc_signal_odds": _num(item["hkjc"].get("odds")), "hkjc_signal_source": item["hkjc"].get("source"), "hkjc_signal_observed_at": item["hkjc"].get("observed_at"),
            "crown_execution_odds": item["quote"]["odds"], "crown_execution_source": item["quote"]["source"], "crown_execution_observed_at": item["quote"]["observed_at"], "odds": item["quote"]["odds"],
            "stake": FIXED_STAKE, "stage": DECISION_STAGE, "status": "PENDING", "simulation_only": True, "real_betting_enabled": False, "first_native_pre_kickoff_t5": True, "created_at": now, "decision_at": now,
            "frozen_condition_signature": admission["signature"], "condition_number": frozen.get("condition_number"), "evidence_version": admission.get("evidence_version"), "evidence_hash": admission.get("evidence_hash"), "frozen_historical_evidence": copy.deepcopy(admission.get("history")), "wilson_admission": copy.deepcopy(admission["arithmetic"]),
            "history": [{"ts": now, "stage": DECISION_STAGE, "action": "足破×皇冠模擬注建立", "reason": "原生 HKJC T-5 訊號、皇冠相同盤口新鮮報價及 Wilson 閘門通過"}],
        }
        ns["bets"].append(bet); created.append(bet)
        audit.append({"market": item["market"], "status": "CREATED", "reason": "exact_cross_book_execution_committed", "bet_id": bid, "condition_number": bet["condition_number"], "wilson_admission": bet["wilson_admission"]})
    for row in audit:
        _append_audit(ns, now, fixture, row)
    recompute(ledger)
    return created, audit


def recompute(ledger: dict[str, Any]) -> dict[str, Any]:
    ns = ensure_namespace(ledger)
    rows = ns["bets"]
    settled = [row for row in rows if row.get("status") in {"SETTLED", "VOIDED"}]
    pending = [row for row in rows if row.get("status") == "PENDING"]
    decided = [row for row in settled if row.get("result") != "Refunded"]
    hits = sum(row.get("result") in {"Won", "Half Won"} for row in decided)
    pnl = round(sum(_num(row.get("pnl")) or 0.0 for row in settled), 2)
    turnover = round(sum(_num(row.get("stake")) or 0.0 for row in settled), 2)
    stats = {"portfolio": PORTFOLIO, "display_name": DISPLAY_NAME, "starting_bankroll": STARTING_BANKROLL, "fixed_stake": FIXED_STAKE, "fixture_stake_cap": FIXTURE_STAKE_CAP, "fixture_market_cap": FIXTURE_MARKET_CAP, "n_pending": len(pending), "n_settled": len(settled), "n_decided": len(decided), "hits": hits, "hit_rate": hits / len(decided) if decided else None, "pushes": len(settled) - len(decided), "pnl": pnl, "turnover": turnover, "roi": pnl / turnover if turnover else None, "open_stake": round(sum(_num(row.get("stake")) or 0 for row in pending), 2), "cash": STARTING_BANKROLL + pnl - sum(_num(row.get("stake")) or 0 for row in pending), "equity": STARTING_BANKROLL + pnl,
             "res_counts": {name: sum(row.get("result") == name for row in settled) for name in ("Won", "Half Won", "Refunded", "Half Lost", "Lost")},
             "rejections": {}}
    for row in ns.get("audit") or []:
        if isinstance(row, dict) and row.get("status") != "CREATED":
            reason = str(row.get("reason") or "unknown")
            stats["rejections"][reason] = stats["rejections"].get(reason, 0) + 1
    ns["stats"] = stats
    return stats
