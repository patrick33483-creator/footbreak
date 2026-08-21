"""Fail-closed 皇冠 × 馬會 execution-only simulation.

The Crown deadline worker reads a bounded persisted Footbreak ledger artifact
only.  It never imports Footbreak providers or makes a remote call, so a
missing exact HKJC quote simply rejects the reciprocal simulation.
"""
from __future__ import annotations

import copy
import json
import math
import os
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Iterable

from analysis import bilateral_decision as bilateral
from analysis.granular_conditions import MARKET_LABELS, MARKETS
from analysis.wilson_portfolio import _audit_selection, _native_t5, _selected
from analysis.wilson_validation import (
    DECISION_STAGE, FIXED_STAKE, FIXTURE_MARKET_CAP, FIXTURE_STAKE_CAP,
    STARTING_BANKROLL, admission_arithmetic,
    matching_admissions, formal_registry_candidates, match_formal_registry,
)
from .common import iso_hkt, parse_time

NAMESPACE = "crown_hkjc_execution_test"
PORTFOLIO = NAMESPACE
DISPLAY_NAME = "皇冠×馬會執行測試倉（模擬）"
STRATEGY = "crown-hkjc-execution-test-v1"
CROWN_SOURCE = "titan007-crown-id-3"
HKJC_SOURCE = {"hkjc_public_board", "hkjc-current-board"}
FRESHNESS_SECONDS = 120.0


def _number(value: Any) -> float | None:
    try:
        out = float(value)
    except (TypeError, ValueError):
        return None
    return out if math.isfinite(out) else None


def _time(value: Any) -> datetime | None:
    if (number := _number(value)) is not None and number > 0:
        if number >= 10_000_000_000:
            number /= 1000
        try:
            return datetime.fromtimestamp(number, timezone.utc)
        except (OSError, OverflowError, ValueError):
            return None
    return parse_time(str(value or ""))


def _hkjc_evidence_path() -> Path:
    return Path(os.environ.get(
        "CROWN_HKJC_EXECUTION_EVIDENCE_PATH",
        "/opt/footbreak/system/sim_ledger.json",
    ))


def _load_footbreak_ledger() -> tuple[dict[str, Any] | None, str | None]:
    path = _hkjc_evidence_path()
    try:
        if path.stat().st_size > 16_000_000:
            return None, "hkjc_local_evidence_too_large"
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError, json.JSONDecodeError):
        return None, "hkjc_local_evidence_unavailable"
    return (payload, None) if isinstance(payload, dict) else (None, "hkjc_local_evidence_invalid")


def prefetch_bridge(watch: dict[str, Any], *, now: str | None = None) -> dict[str, Any]:
    """Persist the exact Footbreak identity bridge at Crown T-30, locally."""
    source, error = _load_footbreak_ledger()
    hkjc_id = str(watch.get("hkjc_match_id") or "")
    kickoff = _time(watch.get("kickoff"))
    candidate = ((source or {}).get("watch") or {}).get(hkjc_id)
    status, reason = "RESOLVED", None
    if error:
        status, reason = "UNAVAILABLE", error
    elif not hkjc_id or not isinstance(candidate, dict):
        status, reason = "UNAVAILABLE", "hkjc_fixture_identity_missing_or_ambiguous"
    elif kickoff is None or _time(candidate.get("kickoff")) != kickoff:
        status, reason = "UNAVAILABLE", "hkjc_fixture_kickoff_identity_mismatch"
    bridge = {
        "at": now or iso_hkt(), "status": status, "reason": reason,
        "counterpart_book": "hkjc", "hkjc_match_id": hkjc_id,
        "kickoff": watch.get("kickoff"),
    }
    watch.setdefault("counterpart_bridges", {})["hkjc"] = bridge
    return bridge


def ensure_namespace(ledger: dict[str, Any]) -> dict[str, Any]:
    ns = ledger.setdefault(NAMESPACE, {})
    if not isinstance(ns, dict):
        raise ValueError("crown reciprocal namespace must be an object")
    ns.setdefault("schema_version", 1)
    ns.setdefault("display_name", DISPLAY_NAME)
    ns.setdefault("activation_at", iso_hkt())
    ns.setdefault("starting_bankroll", STARTING_BANKROLL)
    ns.setdefault("fixed_stake", FIXED_STAKE)
    ns.setdefault("fixture_stake_cap", FIXTURE_STAKE_CAP)
    ns.setdefault("fixture_market_cap", FIXTURE_MARKET_CAP)
    ns.setdefault("bets", [])
    ns.setdefault("audit", [])
    return ns


def _audit(ns: dict[str, Any], fixture: str, market: str, reason: str, *, status="SKIPPED") -> None:
    ns["audit"] = (ns.get("audit") or []) + [{
        "ts": iso_hkt(), "match_id": fixture, "market": market,
        "status": status, "reason": reason,
    }]
    ns["audit"] = ns["audit"][-1600:]


def _exact_hkjc_quote(
    hkjc_id: str, market: str, side: str, line: float, stage_at: datetime, kickoff: datetime,
) -> tuple[dict[str, Any] | None, str | None]:
    source, error = _load_footbreak_ledger()
    if error:
        return None, error
    watch = (source.get("watch") or {}).get(hkjc_id) if isinstance(source, dict) else None
    if not isinstance(watch, dict) or str(watch.get("match_id") or "") != hkjc_id:
        return None, "hkjc_fixture_identity_missing_or_ambiguous"
    foot_kickoff = _time(watch.get("kickoff"))
    if foot_kickoff is None or abs((foot_kickoff - kickoff).total_seconds()) > 1:
        return None, "hkjc_fixture_kickoff_identity_mismatch"
    stages = [row for row in watch.get("stages") or [] if isinstance(row, dict) and row.get("stage") == "T-5"]
    if len(stages) != 1 or not _native_t5(watch, stages[0], _time):
        return None, "hkjc_exact_native_t5_missing"
    rows = []
    for row in stages[0].get("market_predictions") or []:
        if not isinstance(row, dict):
            continue
        qline = _number(row.get("line", row.get("condition")))
        if (str(row.get("code") or "").upper() == market
                and str(row.get("side") or "").upper() == side
                and qline is not None and abs(qline - line) <= 1e-8):
            rows.append(row)
    if len(rows) != 1:
        return None, "hkjc_exact_market_side_line_missing_or_ambiguous"
    row = rows[0]
    odds, observed = _number(row.get("odds")), _time(row.get("observed_at"))
    if odds is None or odds <= 1:
        return None, "hkjc_execution_odds_invalid_or_missing"
    if str(row.get("source") or "").strip().lower() not in HKJC_SOURCE:
        return None, "hkjc_execution_source_invalid_or_missing"
    if observed is None:
        return None, "hkjc_execution_timestamp_missing"
    if observed >= kickoff or observed > stage_at:
        return None, "hkjc_execution_post_kickoff_or_post_decision"
    if (stage_at - observed).total_seconds() > FRESHNESS_SECONDS:
        return None, "hkjc_execution_quote_stale_at_t5"
    return {"odds": odds, "observed_at": row.get("observed_at"),
            "source": row.get("source"), "fixture_identity": {"hkjc_match_id": hkjc_id}}, None


def _active_existing_admission(
    ledger: dict[str, Any], admission: dict[str, Any], execution_odds: float,
) -> tuple[dict[str, Any] | None, str | None]:
    """Consume an existing Crown Wilson version; never freeze/revise it here."""
    signature = str(admission.get("signature") or "")
    frozen = ((ledger.get("wilson_validation") or {}).get("conditions") or {}).get(signature)
    active = frozen.get("active_evidence") if isinstance(frozen, dict) else None
    if not isinstance(active, dict):
        return None, "active_wilson_condition_unavailable"
    arithmetic = admission_arithmetic(int(active.get("cumulative_hits", 0)),
                                     int(active.get("cumulative_decided", 0)), execution_odds)
    if arithmetic is None:
        return None, "active_evidence_arithmetic_invalid"
    updated = copy.deepcopy(admission)
    updated["history"] = {**copy.deepcopy(admission.get("history") or {}),
                          "hits": int(active["cumulative_hits"]),
                          "decided": int(active["cumulative_decided"]),
                          "evidence_version": active.get("version"),
                          "evidence_hash": active.get("evidence_hash")}
    updated["arithmetic"] = arithmetic
    updated["evidence_version"], updated["evidence_hash"] = active.get("version"), active.get("evidence_hash")
    return updated, None


def _native_crown_signal(
    current: dict[str, Any], market: str, watch: dict[str, Any], stage_at: datetime,
) -> tuple[dict[str, Any] | None, str | None]:
    """Read one Crown-native, fresh T-5 signal without source substitution."""
    signal, reason = _selected(current, market, _time, fixture_kickoff=watch.get("kickoff"))
    if signal is None:
        return None, reason
    source = str(signal.get("quote_source") or signal.get("source") or "").strip().lower()
    observed = _time(signal.get("observed_at"))
    if source != CROWN_SOURCE:
        return None, "crown_signal_source_non_native_or_missing"
    if observed is None:
        return None, "crown_signal_timestamp_missing"
    if observed > stage_at:
        return None, "crown_signal_post_decision"
    if (stage_at - observed).total_seconds() > FRESHNESS_SECONDS:
        return None, "crown_signal_quote_stale_at_t5"
    return signal, None


def evaluate_new_t5(
    ledger: dict[str, Any], watch: dict[str, Any], *, ranking: Iterable[dict[str, Any]] | None,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    ns, created = ensure_namespace(ledger), []
    bilateral.ensure_namespace(ns)
    fixture = str(watch.get("match_id") or "")
    t5 = [row for row in watch.get("stages") or [] if isinstance(row, dict) and row.get("stage") == DECISION_STAGE]
    current = t5[0] if len(t5) == 1 else None
    kickoff = _time(watch.get("kickoff"))
    stage_at = _time((current or {}).get("ts") or (current or {}).get("source_snapshot_at"))
    decision_at = iso_hkt()
    decision_time = _time(decision_at)
    if (
        not fixture or current is None
        or not _native_t5(watch, current, _time)
        or not kickoff or not stage_at or stage_at >= kickoff
        or decision_time is None or decision_time >= kickoff
    ):
        _audit(ns, fixture, "*", "not_first_native_pre_kickoff_t5_or_ranking_missing"); return [], ns["audit"][-1:]
    hkjc_id = str(watch.get("hkjc_match_id") or "")
    stage_rows = [{"match_id": fixture, "stage": DECISION_STAGE, "kickoff": watch.get("kickoff"),
                   "predicted_at": current.get("ts"), "market_predictions": current.get("market_predictions") or []}]
    formal_candidates = formal_registry_candidates(ledger, "crown", now=decision_at)
    if not formal_candidates:
        _audit(ns, fixture, "*", "formal_condition_registry_unavailable"); return [], ns["audit"][-1:]
    matched = match_formal_registry(
        stage_rows, formal_candidates, system="crown", decision_stage=DECISION_STAGE,
    ).get(fixture, [])
    conditions = (ledger.get("wilson_validation") or {}).get("conditions") or {}
    for market in MARKETS:
        signal, reason = _native_crown_signal(current, market, watch, stage_at)
        if signal is None:
            _audit(ns, fixture, market, reason or "crown_signal_missing"); continue
        side, line = str(signal.get("side") or "").upper(), _number(signal.get("line", signal.get("condition")))
        if line is None:
            _audit(ns, fixture, market, "crown_signal_line_invalid"); continue
        admissions, reason = matching_admissions("crown", market, signal, matched, stage_at=str(current.get("ts") or ""))
        if not admissions:
            _audit(ns, fixture, market, reason or "crown_condition_not_matched"); continue
        quote, quote_reason = _exact_hkjc_quote(hkjc_id, market, side, line, stage_at, kickoff)
        for admission in admissions:
            signature = str(admission.get("signature") or "")
            if not isinstance(conditions.get(signature), dict):
                _audit(ns, fixture, market, "active_wilson_condition_unavailable"); continue
            frozen = conditions[signature]
            native_adjusted, native_reason = _active_existing_admission(
                ledger, admission, _number(signal.get("odds")) or 0,
            )
            if native_adjusted is None:
                _audit(ns, fixture, market, native_reason or "active_evidence_unavailable"); continue
            counterpart_status = "AVAILABLE" if quote is not None else "UNAVAILABLE"
            bilateral.append_counterpart_attempt(ns, {
                "system": "crown", "match_id": fixture, "hkjc_match_id": hkjc_id,
                "market": market, "side": side, "line": line,
                "stage_at": stage_at.isoformat(), "counterpart_book": "hkjc",
                "counterpart_status": counterpart_status, "counterpart_reason": quote_reason,
                "counterpart_quote": quote.get("odds") if quote else None,
                "counterpart_observed_at": quote.get("observed_at") if quote else None,
            })
            native_odds = _number(signal.get("odds")) or 0.0
            counterpart_odds = _number(quote.get("odds")) if quote else None
            chosen_odds = max(native_odds, counterpart_odds or 0.0)
            minimum = _number((native_adjusted.get("arithmetic") or {}).get(
                "minimum_acceptable_odds_raw",
            ))
            qualifies = minimum is not None and chosen_odds + 1e-12 >= minimum
            if quote is None:
                decision, chosen_book = "COUNTERPART_UNAVAILABLE", ("crown" if qualifies else None)
            elif qualifies:
                decision = "PAPER_SIMULATION"
                chosen_book = "hkjc" if counterpart_odds and counterpart_odds > native_odds else "crown"
            else:
                decision, chosen_book = "NO_BET_LOW_ODDS", None
            did = bilateral.decision_id(
                system="crown", fixture=fixture, market=market, side=side,
                line=line, condition_signature=signature,
                evidence_version=native_adjusted.get("evidence_version"),
            )
            decision_row, _ = bilateral.persist_decision(ns, {
                "decision_id": did, "system": "crown", "fixture": fixture,
                "market": market, "side": side, "line": line,
                "condition_signature": signature, "condition_number": frozen.get("condition_number"),
                "evidence_version": native_adjusted.get("evidence_version"),
                "evidence_hash": native_adjusted.get("evidence_hash"),
                "signal_book": "crown", "signal_quote": native_odds,
                "signal_observed_at": signal.get("observed_at"),
                "counterpart_book": "hkjc", "counterpart_status": counterpart_status,
                "counterpart_quote": counterpart_odds,
                "counterpart_observed_at": quote.get("observed_at") if quote else None,
                "counterpart_reason": quote_reason, "minimum_odds": minimum,
                "chosen_execution_book": chosen_book,
                "chosen_execution_odds": chosen_odds if chosen_book else None,
                "decision": decision, "created_at": decision_at,
                "stage_at": stage_at.isoformat(), "kickoff": watch.get("kickoff"),
                "league": watch.get("league"), "home": watch.get("home"), "away": watch.get("away"),
                "freshness_seconds": FRESHNESS_SECONDS,
            })
            _audit(ns, fixture, market, decision, status="DECISION")
            if quote is None:
                continue
            bid = f"{fixture}|{market}|{side}|{line:g}|{signature}|hkjc-execution-v1"
            if any(str(row.get("bet_id") or "") == bid for row in ns["bets"]):
                _audit(ns, fixture, market, "idempotent_existing_exact_entry"); break
            adjusted, reason = _active_existing_admission(ledger, admission, quote["odds"])
            if adjusted is None or not adjusted["arithmetic"].get("passes"):
                _audit(ns, fixture, market, "hkjc_wilson_gate_not_passed"); continue
            fixture_rows = [row for row in ns["bets"] if str(row.get("match_id") or "") == fixture]
            if len(fixture_rows) >= FIXTURE_MARKET_CAP or sum(float(row.get("stake") or 0) for row in fixture_rows) + FIXED_STAKE > FIXTURE_STAKE_CAP:
                _audit(ns, fixture, market, "fixture_cap_reached"); continue
            role, selected_line, _ = _audit_selection(market, signal)
            bet = {"bet_id": bid, "portfolio": PORTFOLIO, "strategy": STRATEGY, "strategy_name": DISPLAY_NAME,
                   "match_id": fixture, "hkjc_match_id": hkjc_id, "league": watch.get("league"), "home": watch.get("home"), "away": watch.get("away"), "kickoff": watch.get("kickoff"),
                   "code": market, "market": market, "market_label": MARKET_LABELS[market], "side": side, "line": line, "condition": line, "selected_role": role, "selected_line": selected_line,
                   "crown_signal_odds": _number(signal.get("odds")), "crown_signal_source": signal.get("quote_source") or signal.get("source"), "crown_signal_observed_at": signal.get("observed_at"),
                   "hkjc_execution_odds": quote["odds"], "hkjc_execution_source": quote["source"], "hkjc_execution_observed_at": quote["observed_at"], "fixture_identity": {**quote["fixture_identity"], "crown_match_id": fixture}, "odds": quote["odds"],
                   "stake": FIXED_STAKE, "status": "PENDING", "simulation_only": True, "real_betting_enabled": False, "stage": DECISION_STAGE, "created_at": decision_at, "decision_at": decision_at,
                   "frozen_condition_signature": signature, "condition_number": frozen.get("condition_number"), "evidence_version": adjusted.get("evidence_version"), "evidence_hash": adjusted.get("evidence_hash"),
                   "frozen_historical_evidence": copy.deepcopy(adjusted.get("history")), "wilson_admission": copy.deepcopy(adjusted["arithmetic"]),
                   "bilateral_decision_id": decision_row["decision_id"]}
            ns["bets"].append(bet); created.append(bet); _audit(ns, fixture, market, "exact_cross_book_execution_committed", status="CREATED"); break
    recompute(ledger)
    return created, ns["audit"][-20:]


def recompute(ledger: dict[str, Any]) -> dict[str, Any]:
    ns = ensure_namespace(ledger); rows = ns["bets"]
    settled = [row for row in rows if row.get("status") in {"SETTLED", "VOIDED"}]
    pending = [row for row in rows if row.get("status") == "PENDING"]
    decided = [row for row in settled if row.get("result") != "Refunded"]
    pnl = round(sum(_number(row.get("pnl")) or 0 for row in settled), 2)
    turnover = round(sum(_number(row.get("stake")) or 0 for row in settled), 2)
    ns["stats"] = {"portfolio": PORTFOLIO, "display_name": DISPLAY_NAME,
                   "starting_bankroll": STARTING_BANKROLL, "fixed_stake": FIXED_STAKE,
                   "fixture_stake_cap": FIXTURE_STAKE_CAP, "fixture_market_cap": FIXTURE_MARKET_CAP,
                   "n_pending": len(pending), "n_settled": len(settled),
                   "n_decided": len(decided), "hits": sum(row.get("result") in {"Won", "Half Won"} for row in decided),
                   "pushes": len(settled) - len(decided), "pnl": pnl, "turnover": turnover,
                   "roi": pnl / turnover if turnover else None,
                   "open_stake": round(sum(_number(row.get("stake")) or 0 for row in pending), 2),
                   "cash": STARTING_BANKROLL + pnl - sum(_number(row.get("stake")) or 0 for row in pending),
                   "equity": STARTING_BANKROLL + pnl,
                   "res_counts": {name: sum(row.get("result") == name for row in settled)
                                  for name in ("Won", "Half Won", "Refunded", "Half Lost", "Lost")},
                   "rejections": {}}
    for row in ns.get("audit") or []:
        if row.get("status") != "CREATED":
            reason = str(row.get("reason") or "unknown")
            ns["stats"]["rejections"][reason] = ns["stats"]["rejections"].get(reason, 0) + 1
    return ns["stats"]
