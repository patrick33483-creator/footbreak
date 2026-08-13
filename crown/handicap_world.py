"""Isolated Crown HDC three-stage simulation portfolio (讓球世界).

This module deliberately does not inspect official/shadow bets, prediction
history, learning stores or notification state.  It records an immutable
source signal once a newly persisted T-5 snapshot satisfies the strict
three-stage rule, then creates strategy-child legs in its own ledger namespace.
"""
from __future__ import annotations

import math
from datetime import datetime
from typing import Any

from .common import HKT, iso_hkt, parse_time


PORTFOLIO = "handicap_world"
STARTING_BANKROLL = 50_000.0
FIXED_STAKE = 1_000.0
KELLY_FRACTION = 1.0 / 3.0
KELLY_CAP_PCT = 0.04
MIN_ODDS = 1.70
STAGES = ("首預", "T-30", "T-5")
INDEPENDENT_PROBABILITY_SOURCES = {"pinnapi_exact_full_match"}


def default_state() -> dict[str, Any]:
    return {
        "schema_version": 1,
        "portfolio": PORTFOLIO,
        "simulation_only": True,
        "starting_bankroll": STARTING_BANKROLL,
        "fixed_stake": FIXED_STAKE,
        "kelly_policy": {
            "formula": "max(0,(p*odds-1)/(odds-1))/3",
            "fraction": KELLY_FRACTION,
            "cap_pct_of_current_equity": KELLY_CAP_PCT,
            "probability_requirement": "independent_prekickoff_t5_reference_only",
            "excluded_probability_sources": ["crown_full_market_no_vig"],
        },
        "signals": [],
        "bets": [],
        "audit": [],
        "stats": {},
    }


def ensure_state(ledger: dict[str, Any]) -> dict[str, Any]:
    current = ledger.get(PORTFOLIO)
    if not isinstance(current, dict):
        current = default_state()
        ledger[PORTFOLIO] = current
    defaults = default_state()
    for key, value in defaults.items():
        current.setdefault(key, value)
    for key in ("signals", "bets", "audit"):
        if not isinstance(current.get(key), list):
            current[key] = []
    if not isinstance(current.get("stats"), dict):
        current["stats"] = {}
    return current


def _finite(value: Any, *, minimum: float | None = None) -> float | None:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    if not math.isfinite(number) or (minimum is not None and number < minimum):
        return None
    return number


def _team(value: Any) -> str | None:
    text = " ".join(str(value or "").split()).casefold()
    return text or None


def _stage_identity(watch: dict[str, Any], stage: dict[str, Any]) -> tuple[str, str, str, str] | None:
    fields = ("match_id", "kickoff_hkt", "home", "away")
    values = tuple(str(stage.get(field) or "").strip() for field in fields)
    if not all(values):
        return None
    if str(watch.get("match_id") or "").strip() != values[0]:
        return None
    watch_values = (
        str(watch.get("match_id") or "").strip(),
        str(watch.get("kickoff_hkt") or watch.get("kickoff") or "").strip(),
        str(watch.get("home") or "").strip(),
        str(watch.get("away") or "").strip(),
    )
    return values if values == watch_values else None


def _market(stage: dict[str, Any]) -> dict[str, Any] | None:
    rows = [
        row for row in (stage.get("market_predictions") or [])
        if isinstance(row, dict) and str(row.get("code") or "") == "HDC"
    ]
    # A persisted duplicate HDC selection is ambiguous; fail closed rather
    # than choosing latest/best after kickoff risk has become unknowable.
    return rows[0] if len(rows) == 1 else None


def _selection(stage: dict[str, Any], market: dict[str, Any]) -> dict[str, Any] | None:
    side = str(market.get("side") or "")
    home_line = _finite(market.get("line", market.get("condition")))
    if side not in {"H", "A"} or home_line is None:
        return None
    raw_team = stage.get("home") if side == "H" else stage.get("away")
    team = _team(raw_team)
    if not team:
        return None
    # HDC raw line is a home-team line.  Compare the selected team's own line
    # so a reversed feed cannot look like a stable selection by token alone.
    selected_line = home_line if side == "H" else -home_line
    return {
        "side": side,
        "team_key": team,
        "selected_team": str(raw_team).strip(),
        "home_line": home_line,
        "selected_line": selected_line,
        "market": market,
    }


def _stage_timestamp(stage: dict[str, Any]) -> Any:
    return parse_time(stage.get("ts") or stage.get("source_snapshot_at"))


def _evidence_time(value: Any):
    """Accept persisted ISO timestamps or provider epoch timestamps."""
    parsed = parse_time(value)
    if parsed is not None:
        return parsed
    number = _finite(value)
    if number is None or number <= 0:
        return None
    try:
        return datetime.fromtimestamp(number, HKT)
    except (OverflowError, OSError, ValueError):
        return None


def _audit_key(watch: dict[str, Any], t5: dict[str, Any]) -> str:
    return "|".join((
        PORTFOLIO, "audit", str(watch.get("match_id") or ""),
        str(t5.get("ts") or t5.get("source_snapshot_at") or ""),
    ))


def _append_audit(world: dict[str, Any], key: str, status: str, reason: str, watch: dict[str, Any]) -> None:
    if any(row.get("audit_id") == key for row in world["audit"]):
        return
    world["audit"].append({
        "audit_id": key,
        "status": status,
        "reason": reason,
        "match_id": str(watch.get("match_id") or ""),
        "home": watch.get("home"),
        "away": watch.get("away"),
        "kickoff": watch.get("kickoff_hkt") or watch.get("kickoff"),
        "recorded_at": iso_hkt(),
        "simulation_only": True,
    })
    world["audit"] = world["audit"][-200:]


def _current_equity(world: dict[str, Any], as_of) -> float:
    """Only include results persisted no later than the source T-5 timestamp."""
    pnl = 0.0
    for bet in world["bets"]:
        if bet.get("strategy") != "conservative_kelly" or bet.get("status") != "SETTLED":
            continue
        settled_at = parse_time(bet.get("settled_at"))
        if settled_at is None or settled_at > as_of:
            continue
        value = _finite(bet.get("pnl"))
        if value is not None:
            pnl += value
    return max(0.0, round(STARTING_BANKROLL + pnl, 2))


def _kelly_leg(
    signal: dict[str, Any],
    selection: dict[str, Any],
    probability: float,
    probability_source: str,
    equity: float,
) -> dict[str, Any] | None:
    odds = float(selection["market"]["odds"])
    if equity <= 0:
        return None
    full = max(0.0, (probability * odds - 1.0) / (odds - 1.0))
    used = full * KELLY_FRACTION
    stake = min(equity * used, equity * KELLY_CAP_PCT)
    stake = round(max(0.0, stake), 2)
    if stake <= 0:
        return None
    return {
        "strategy": "conservative_kelly",
        "stake": stake,
        "model_prob": probability,
        "probability_source": probability_source,
        "kelly_full": round(full, 8),
        "kelly_fraction": KELLY_FRACTION,
        "kelly_used": round(used, 8),
        "kelly_cap_pct": KELLY_CAP_PCT,
        "kelly_equity_at_entry": equity,
        "formula": "max(0,(p*odds-1)/(odds-1))/3; stake=min(equity*kelly_used,equity*4%)",
        "parent_signal_id": signal["signal_id"],
    }


def _leg(signal: dict[str, Any], selection: dict[str, Any], strategy: dict[str, Any]) -> dict[str, Any]:
    market = selection["market"]
    strategy_name = str(strategy["strategy"])
    leg_id = f"{signal['signal_id']}|{strategy_name}"
    return {
        "bet_id": leg_id,
        "parent_signal_id": signal["signal_id"],
        "portfolio": PORTFOLIO,
        "strategy": strategy_name,
        "match_id": signal["match_id"],
        "league": signal.get("league"),
        "home": signal["home"],
        "away": signal["away"],
        "kickoff": signal["kickoff"],
        "titan_match_id": signal.get("titan_match_id"),
        "pinnapi_event_id": signal.get("pinnapi_event_id"),
        "hkjc_match_id": signal.get("hkjc_match_id"),
        "market": "皇冠讓球",
        "code": "HDC",
        "condition": f"{selection['home_line']:g}",
        "line": selection["home_line"],
        "selected_line": selection["selected_line"],
        "side": selection["side"],
        "selected_team": selection["selected_team"],
        "label": f"皇冠讓球 · {selection['selected_team']} {selection['selected_line']:+g}",
        "odds": float(market["odds"]),
        "stake": float(strategy["stake"]),
        "model_prob": strategy.get("model_prob"),
        "probability_source": strategy.get("probability_source"),
        "status": "PENDING",
        "stage": "T-5",
        "simulation_only": True,
        "real_betting_enabled": False,
        "created_at": iso_hkt(),
        "terms": {
            key: value for key, value in strategy.items()
            if key not in {"strategy", "stake", "parent_signal_id"}
        },
        "history": [{
            "ts": iso_hkt(),
            "stage": "T-5",
            "action": "讓球世界策略注建立",
            "strategy": strategy_name,
            "parent_signal_id": signal["signal_id"],
            "reason": "strict_three_stage_hdc_same_team_direction_exact_line",
        }],
    }


def record_new_t5(ledger: dict[str, Any], watch: dict[str, Any]) -> list[str]:
    """Create eligible fixed/Kelly child legs once, or retain an auditable skip.

    This is intentionally called only after a newly persisted T-5 stage.  It
    remains idempotent when a process replay calls it again.
    """
    world = ensure_state(ledger)
    stages = watch.get("stages") if isinstance(watch.get("stages"), list) else []
    t5_rows = [row for row in stages if isinstance(row, dict) and row.get("stage") == "T-5"]
    if len(t5_rows) != 1:
        if t5_rows:
            _append_audit(world, _audit_key(watch, t5_rows[-1]), "REJECTED", "duplicate_or_ambiguous_t5_stage", watch)
        return []
    t5 = t5_rows[0]
    audit_id = _audit_key(watch, t5)

    selected: list[tuple[dict[str, Any], dict[str, Any]]] = []
    identity = None
    for name in STAGES:
        rows = [row for row in stages if isinstance(row, dict) and row.get("stage") == name]
        if len(rows) != 1:
            _append_audit(world, audit_id, "REJECTED", f"duplicate_or_missing_{name}_stage", watch)
            return []
        stage = rows[0]
        stage_identity = _stage_identity(watch, stage)
        if stage_identity is None or (identity is not None and stage_identity != identity):
            _append_audit(world, audit_id, "REJECTED", "fixture_identity_mismatch", watch)
            return []
        identity = stage_identity
        market = _market(stage)
        selection = _selection(stage, market) if market else None
        if selection is None:
            _append_audit(world, audit_id, "REJECTED", f"invalid_or_ambiguous_hdc_{name}", watch)
            return []
        selected.append((stage, selection))

    first = selected[0][1]
    if any(
        row["team_key"] != first["team_key"]
        or row["selected_line"] != first["selected_line"]
        for _, row in selected[1:]
    ):
        _append_audit(world, audit_id, "REJECTED", "selected_team_direction_or_exact_line_mismatch", watch)
        return []
    t5_at = _stage_timestamp(t5)
    kickoff = parse_time(t5.get("kickoff_hkt"))
    if t5_at is None or kickoff is None or t5_at >= kickoff:
        _append_audit(world, audit_id, "REJECTED", "t5_not_provably_prekickoff", watch)
        return []
    t5_selection = selected[-1][1]
    odds = _finite(t5_selection["market"].get("odds"), minimum=MIN_ODDS)
    if odds is None:
        _append_audit(world, audit_id, "REJECTED", "invalid_or_below_1_70_t5_selected_odds", watch)
        return []

    selected_line_quarters = int(round(first["selected_line"] * 4))
    signal_id = "|".join((
        PORTFOLIO, str(watch.get("match_id") or ""), first["team_key"],
        str(selected_line_quarters), str(t5.get("ts") or t5.get("source_snapshot_at") or ""),
    ))
    existing = next((row for row in world["signals"] if row.get("signal_id") == signal_id), None)
    if existing is not None:
        return []

    probability = _finite(t5_selection["market"].get("probability"))
    probability_source = str(t5_selection["market"].get("probability_source") or "")
    probability_at = _evidence_time(t5_selection["market"].get("probability_observed_at"))
    independent_probability = (
        probability is not None
        and 0 < probability < 1
        and probability_source in INDEPENDENT_PROBABILITY_SOURCES
        and probability_at is not None
        and probability_at <= t5_at
        and probability_at < kickoff
    )
    kelly_reason = None
    if not independent_probability:
        if probability is None or not (0 < probability < 1):
            kelly_reason = "independent_t5_probability_unavailable"
        elif probability_source not in INDEPENDENT_PROBABILITY_SOURCES:
            kelly_reason = "probability_source_not_independent"
        elif probability_at is None:
            kelly_reason = "independent_t5_probability_timestamp_unavailable"
        else:
            kelly_reason = "independent_probability_not_provably_prekickoff"
    signal = {
        "signal_id": signal_id,
        "portfolio": PORTFOLIO,
        "match_id": str(watch.get("match_id") or ""),
        "league": watch.get("league"),
        "home": str(watch.get("home") or ""),
        "away": str(watch.get("away") or ""),
        "kickoff": str(watch.get("kickoff_hkt") or watch.get("kickoff") or ""),
        "titan_match_id": watch.get("titan_match_id"),
        "pinnapi_event_id": watch.get("pinnapi_event_id"),
        "hkjc_match_id": watch.get("hkjc_match_id"),
        "source_stage": "T-5",
        "source_t5_at": t5_at.isoformat(),
        "market": "HDC",
        "side": first["side"],
        "selected_team": first["selected_team"],
        "selected_line": first["selected_line"],
        "home_line": first["home_line"],
        "odds": odds,
        "eligibility": {
            "rule": "Crown HDC only; exactly one 首預/T-30/T-5; same selected team/direction and exact selected-side numeric handicap; T-5 odds >=1.70; pre-kickoff",
            "stages": list(STAGES),
            "min_t5_odds": MIN_ODDS,
        },
        "kelly": {
            "status": "READY" if independent_probability else "SKIPPED",
            "reason": kelly_reason,
            "probability": probability if independent_probability else None,
            "probability_source": probability_source or None,
            "probability_observed_at": probability_at.isoformat() if probability_at else None,
        },
        "child_bet_ids": [],
        "created_at": iso_hkt(),
        "simulation_only": True,
    }
    world["signals"].append(signal)

    fixed = _leg(signal, t5_selection, {
        "strategy": "fixed_stake",
        "stake": FIXED_STAKE,
        "formula": "fixed HK$1,000 = 2% of HK$50,000 starting bankroll; never rolling balance",
        "parent_signal_id": signal_id,
    })
    world["bets"].append(fixed)
    signal["child_bet_ids"].append(fixed["bet_id"])
    created = [fixed["bet_id"]]

    if independent_probability:
        kelly = _kelly_leg(
            signal, t5_selection, float(probability), probability_source,
            _current_equity(world, t5_at),
        )
        if kelly is None:
            signal["kelly"].update({"status": "SKIPPED", "reason": "nonpositive_kelly_or_zero_equity"})
        else:
            leg = _leg(signal, t5_selection, kelly)
            world["bets"].append(leg)
            signal["child_bet_ids"].append(leg["bet_id"])
            signal["kelly"]["status"] = "CREATED"
            created.append(leg["bet_id"])

    _append_audit(world, audit_id, "ELIGIBLE", signal["kelly"]["reason"] or "both_strategies_created", watch)
    return created
