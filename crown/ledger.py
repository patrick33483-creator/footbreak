"""Idempotent Crown watch ledger.  It can create simulations, never real bets."""
from __future__ import annotations

import os
from datetime import datetime, timedelta, timezone
from typing import Any

from analysis.learning_store import LearningStore

from .common import iso_hkt
from .config import Settings

STAGES = {"首預": 1, "T-30": 2, "T-5": 3}
PREDICTION_ERA = "2026-08-10-every-crown-fixture-v3"
PREDICTION_SCHEMA_VERSION = 2


def completed_stages(watch: dict[str, Any], matching_version: str) -> set[str]:
    """Refresh stale first looks once without replaying T-30/T-5 decisions."""
    done = {
        str(row.get("stage"))
        for row in watch.get("stages", [])
        # A provider/mapping outage is not a completed prediction.  Keep the
        # stage eligible for a later recovery pass; sync_prediction updates
        # the same stage row idempotently and still cannot duplicate a bet.
        if row.get("stage") and row.get("status") != "DATA_MISSING"
    }
    if (
        watch.get("matching_version") != matching_version
        and not done.intersection({"T-30", "T-5"})
    ):
        done.discard("首預")
    return done


def stage_for(minutes_to_kickoff: float, sweep: bool, done: set[str]) -> str | None:
    if sweep:
        return "首預" if "首預" not in done else None
    if 0 < minutes_to_kickoff <= 10 and "T-5" not in done:
        return "T-5"
    if 20 <= minutes_to_kickoff <= 40 and "T-30" not in done:
        return "T-30"
    return None


def _market_predictions(candidates: list[dict[str, Any]]) -> list[dict[str, Any]]:
    grouped: dict[str, list[dict[str, Any]]] = {}
    for candidate in candidates or []:
        code = str(candidate.get("code") or "")
        if code in {"HDC", "HIL", "CHL"}:
            grouped.setdefault(code, []).append(candidate)
    output = []
    for code, rows in grouped.items():
        best = max(rows, key=lambda row: float(row.get("prob") or 0))
        output.append({
            "code": code,
            "market": best.get("market"),
            "condition": best.get("condition"),
            "line": best.get("line"),
            "side": best.get("side"),
            "label": best.get("label"),
            "odds": best.get("odds"),
            "probability": best.get("prob"),
            "source": best.get("reference") or "pinnapi_exact_line",
            "provider": best.get("provider") or "Crown",
        })
    return sorted(output, key=lambda row: row["code"])


def _snapshot(prediction: dict[str, Any], stage: str) -> dict[str, Any]:
    snapshot = {key: prediction.get(key) for key in (
        "match_id", "league", "home", "away", "kickoff_hkt", "mins_to_ko", "status", "verdict",
        "conviction", "no_bet_reason", "pick", "shadow_pick", "shadow_status",
        "shadow_no_bet_reason", "lead_view", "market_sources", "hkjc_match_id",
        "titan_match_id", "pinnapi_event_id", "source_snapshot_at", "execution",
        "outcome", "forecast", "probability", "likely_score", "prediction_source",
        "probabilities", "baseline_low_confidence", "edge_reference_status", "edge_reference_note",
        "pinnapi_corner_event_id", "pinnapi_corner_source_at", "pinnapi_corner_timestamp_inferred",
        "matching_version", "crown_quote_cached_forecast_only", "crown_cached_source_at",
    )} | {
        "prediction_era": PREDICTION_ERA,
        "schema_version": PREDICTION_SCHEMA_VERSION,
        "stage": stage,
        "ts": iso_hkt(),
        "market_predictions": _market_predictions(
            prediction.get("forecast_candidates") or prediction.get("candidates") or []
        ),
    }
    return snapshot


def _bet_id(prediction: dict[str, Any]) -> str:
    pick = prediction["pick"]
    return f"{prediction['match_id']}|{pick['code']}|{pick['condition']}|{pick['side']}"


def _shadow_bet_id(prediction: dict[str, Any]) -> str:
    pick = prediction["shadow_pick"]
    return (
        f"shadow|{prediction['match_id']}|{pick['code']}|"
        f"{pick['condition']}|{pick['side']}"
    )


def _record_learning_snapshot(
    prediction: dict[str, Any], snapshot: dict[str, Any]
) -> dict[str, Any] | None:
    path = os.environ.get("LEARNING_DB_PATH")
    if not path:
        return None
    payload = {key: value for key, value in snapshot.items() if key != "ts"}
    with LearningStore(path) as store:
        return store.record_snapshot(
            "crown",
            str(prediction["match_id"]),
            str(snapshot["stage"]),
            snapshot["ts"],
            str(prediction["kickoff_hkt"]),
            payload,
            model_version=PREDICTION_ERA,
            schema_version=str(PREDICTION_SCHEMA_VERSION),
        )


def sync_prediction(ledger: dict[str, Any], prediction: dict[str, Any], config: Settings) -> list[str]:
    """Repeated calls are a no-op for a created simulation and notification key."""
    ledger.setdefault("bets", [])
    ledger.setdefault("shadow_bets", [])
    ledger.setdefault("watch", {})
    stage = prediction.get("stage")
    if stage not in STAGES:
        return []
    match_id = str(prediction["match_id"])
    watch = ledger["watch"].setdefault(match_id, {
        "match_id": match_id, "league": prediction.get("league"), "home": prediction.get("home"), "away": prediction.get("away"),
        "kickoff": prediction.get("kickoff_hkt"), "titan_match_id": prediction.get("titan_match_id"),
        "pinnapi_event_id": prediction.get("pinnapi_event_id"), "hkjc_match_id": prediction.get("hkjc_match_id"), "stages": [],
        "matching_version": prediction.get("matching_version"),
    })
    watch.update({key: prediction.get(key) for key in (
        "league", "home", "away", "kickoff_hkt", "titan_match_id",
        "pinnapi_event_id", "hkjc_match_id", "matching_version",
    )})
    watch["kickoff"] = prediction.get("kickoff_hkt")
    stage_rows = watch["stages"]
    existing = next((row for row in stage_rows if row.get("stage") == stage), None)
    snapshot = _snapshot(prediction, stage)
    learning = _record_learning_snapshot(prediction, snapshot)
    if learning:
        snapshot.update({
            "learning_snapshot_id": learning["snapshot_id"],
            "learning_attempt": learning["attempt"],
            "learning_pre_kickoff": learning["pre_kickoff"],
            "learning_payload_sha256": learning["payload_sha256"],
        })
        if not learning["pre_kickoff"]:
            return []
    if existing is None:
        stage_rows.append(snapshot)
        stage_rows.sort(key=lambda row: STAGES[row["stage"]])
    else:
        existing.update(snapshot)
    if stage == "T-5" and prediction.get("shadow_pick"):
        shadow_bets = ledger["shadow_bets"]
        # Shadow decisions are also one-per-fixture and idempotent. They are
        # deliberately stored outside ledger["bets"], which remains the only
        # official simulation, learning and notification portfolio.
        if not any(str(bet.get("match_id")) == match_id for bet in shadow_bets):
            shadow_id = _shadow_bet_id(prediction)
            shadow_pick = prediction["shadow_pick"]
            shadow_bets.append({
                "bet_id": shadow_id, "match_id": match_id,
                "league": prediction.get("league"), "home": prediction.get("home"),
                "away": prediction.get("away"), "kickoff": prediction.get("kickoff_hkt"),
                "titan_match_id": prediction.get("titan_match_id"),
                "pinnapi_event_id": prediction.get("pinnapi_event_id"),
                "hkjc_match_id": prediction.get("hkjc_match_id"),
                "market": shadow_pick["market"], "code": shadow_pick["code"],
                "condition": shadow_pick["condition"], "side": shadow_pick["side"],
                "label": shadow_pick["label"], "line": shadow_pick.get("line"),
                "odds": shadow_pick["odds"], "stake": shadow_pick["stake"],
                "model_prob": shadow_pick["prob"], "ev": None,
                "provider": shadow_pick.get("provider"), "source": shadow_pick.get("source"),
                "bookmaker": shadow_pick.get("bookmaker"),
                "reference": shadow_pick.get("reference"),
                "reference_provider": shadow_pick.get("reference_provider"),
                "confidence_only": True, "shadow_only": True, "portfolio": "shadow",
                "conviction": shadow_pick.get("conviction"),
                "first_stage": "T-5", "stage": "T-5", "status": "PENDING",
                "simulation_only": True, "real_betting_enabled": False,
                "created_at": iso_hkt(),
                "history": [{
                    "ts": iso_hkt(), "stage": "T-5",
                    "action": "影子注建立", "bet_id": shadow_id,
                    "reason": "confidence-only；固定 2% 虛擬本金；不計 EV",
                }],
            })
    if stage != "T-5" or not prediction.get("pick"):
        return []
    # One final simulation decision per Crown prediction fixture.  A retry or
    # a concurrent recalculation may move market/line, but must not append a
    # second T-5 bet for the same match.
    if any(str(bet.get("match_id")) == match_id for bet in ledger["bets"]):
        return []
    bid = _bet_id(prediction)
    if any(str(bet.get("bet_id")) == bid for bet in ledger["bets"]):
        return []
    pick = prediction["pick"]
    # No external order path exists: this is permanently a simulated row.
    ledger["bets"].append({
        "bet_id": bid, "match_id": match_id, "league": prediction.get("league"), "home": prediction.get("home"),
        "away": prediction.get("away"), "kickoff": prediction.get("kickoff_hkt"), "titan_match_id": prediction.get("titan_match_id"),
        "pinnapi_event_id": prediction.get("pinnapi_event_id"), "hkjc_match_id": prediction.get("hkjc_match_id"),
        "market": pick["market"], "code": pick["code"], "condition": pick["condition"], "side": pick["side"],
        "label": pick["label"], "line": pick.get("line"), "odds": pick["odds"], "stake": pick["stake"],
        "model_prob": pick["prob"], "ev": pick.get("ev"), "provider": pick.get("provider"),
        "source": pick.get("source"), "bookmaker": pick.get("bookmaker"),
        "reference": pick.get("reference"), "reference_provider": pick.get("reference_provider"),
        "confidence_only": bool(pick.get("confidence_only")),
        "conviction": prediction.get("conviction"), "first_stage": "T-5", "stage": "T-5", "status": "PENDING",
        "simulation_only": True, "real_betting_enabled": False, "created_at": iso_hkt(),
        "history": [{"ts": iso_hkt(), "stage": "T-5", "action": "模擬注建立", "bet_id": bid}],
    })
    return [bid]


def _portfolio_stats(bets: list[dict[str, Any]], bankroll: float) -> dict[str, Any]:
    settled = [bet for bet in bets if bet.get("status") == "SETTLED"]
    pending = [bet for bet in bets if bet.get("status") == "PENDING"]
    pnl = round(sum(float(bet.get("pnl") or 0) for bet in settled), 2)
    turnover = round(sum(float(bet.get("stake") or 0) for bet in settled), 2)
    decided = [bet for bet in settled if bet.get("result") != "Refunded"]
    hits = sum(bet.get("result") in {"Won", "Half Won"} for bet in decided)
    by_market: dict[str, dict[str, Any]] = {}
    for bet in settled:
        market = str(bet.get("market") or bet.get("code") or "其他")
        row = by_market.setdefault(
            market, {"n": 0, "stake": 0.0, "pnl": 0.0, "hit": 0, "dec": 0}
        )
        row["n"] += 1
        row["stake"] += float(bet.get("stake") or 0)
        row["pnl"] += float(bet.get("pnl") or 0)
        if bet.get("result") != "Refunded":
            row["dec"] += 1
            row["hit"] += int(bet.get("result") in {"Won", "Half Won"})
    for row in by_market.values():
        row["stake"] = round(row["stake"], 2)
        row["pnl"] = round(row["pnl"], 2)
        row["roi"] = round(row["pnl"] / row["stake"], 4) if row["stake"] else None
        row["hit_rate"] = round(row["hit"] / row["dec"], 4) if row["dec"] else None

    result_names = ("Won", "Half Won", "Refunded", "Half Lost", "Lost")
    res_counts = {name: sum(bet.get("result") == name for bet in settled) for name in result_names}
    running_equity = bankroll
    curve = []
    for bet in sorted(settled, key=lambda row: str(row.get("settled_at") or row.get("created_at") or "")):
        bet_pnl = float(bet.get("pnl") or 0)
        running_equity += bet_pnl
        curve.append({
            "ts": bet.get("settled_at") or bet.get("created_at"),
            "label": f"{bet.get('home', '')} v {bet.get('away', '')}".strip(),
            "pnl": round(bet_pnl, 2),
            "equity": round(running_equity, 2),
        })

    return {
        "n_pending": len(pending), "n_voided": sum(bet.get("status") == "VOIDED" for bet in bets), "n_settled": len(settled),
        "open_stake": round(sum(float(bet.get("stake") or 0) for bet in pending), 2),
        "open_pct": round(sum(float(bet.get("stake") or 0) for bet in pending) / bankroll, 4) if bankroll else 0,
        "pnl": pnl, "turnover": turnover, "roi": round(pnl / turnover, 4) if turnover else None,
        "n_decided": len(decided), "hits": hits, "hit_rate": round(hits / len(decided), 4) if decided else None,
        "equity": round(bankroll + pnl, 2), "by_market": by_market, "curve": curve,
        "res_counts": res_counts,
    }


def _bet_time(bet: dict[str, Any]) -> datetime | None:
    value = bet.get("created_at") or bet.get("kickoff")
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=timezone(timedelta(hours=8)))
        return parsed.astimezone(timezone.utc)
    except (TypeError, ValueError):
        return None


def _same_period_comparison(
    official_bets: list[dict[str, Any]],
    shadow_bets: list[dict[str, Any]],
    bankroll: float,
) -> dict[str, Any] | None:
    dated_shadow = [(bet, _bet_time(bet)) for bet in shadow_bets]
    starts = [ts for _, ts in dated_shadow if ts is not None]
    if not starts:
        return None
    start = min(starts)
    official_period = [
        bet for bet in official_bets
        if (ts := _bet_time(bet)) is not None and ts >= start
    ]
    shadow_period = [
        bet for bet, ts in dated_shadow
        if ts is not None and ts >= start
    ]
    return {
        "period_start": start.isoformat(),
        "definition": "from_first_shadow_bet",
        "official_total_bets": len(official_period),
        "shadow_total_bets": len(shadow_period),
        "official": _portfolio_stats(official_period, bankroll),
        "shadow": _portfolio_stats(shadow_period, bankroll),
    }


def recompute_stats(ledger: dict[str, Any], config: Settings) -> dict[str, Any]:
    bets = ledger.setdefault("bets", [])
    shadow_bets = ledger.setdefault("shadow_bets", [])
    watch = ledger.setdefault("watch", {})
    base = _portfolio_stats(bets, config.bankroll)
    previous_staking = ((ledger.get("stats") or {}).get("staking") or {})
    stats = {
        **base,
        "daily_cap": config.bankroll, "open_cap": round(config.bankroll * 0.35, 2), "single_cap_pct": 0.04,
        "conf_floor": config.confidence_floor, "bet_stage": "T-5", "n_watch": len(watch),
        "n_stage_preds": sum(len(item.get("stages") or []) for item in watch.values()),
        "staking": {
            "fraction": previous_staking.get("fraction", 1 / 3),
            "cap": previous_staking.get("cap", 0.04),
            "label": previous_staking.get("label", "階段一 · 建立樣本"),
            "level": previous_staking.get("level", 1),
            "n_settled": base["n_settled"],
            "slope": previous_staking.get("slope"),
            "buckets": previous_staking.get("buckets", []),
            "perf": previous_staking.get("perf", {}),
            "demoted": previous_staking.get("demoted", False),
            "market_mult": previous_staking.get("market_mult", {"CHL": 0.5}),
        },
    }
    ledger["stats"] = stats
    ledger["shadow_stats"] = {
        **_portfolio_stats(shadow_bets, config.bankroll),
        "conf_floor": config.confidence_floor,
        "bet_stage": "T-5",
        "single_cap_pct": 0.02,
        "mode": "confidence_only_shadow",
        "excluded_from_official": True,
        "comparison": _same_period_comparison(bets, shadow_bets, config.bankroll),
    }
    return stats


def market_entry_thresholds(
    ledger: dict[str, Any],
    code: str,
    config: Settings,
    *,
    min_samples: int = 30,
) -> dict[str, Any]:
    """Return a conservative, performance-driven threshold for one market.

    Small samples never change policy.  Once the market has enough settled
    observations, poor ROI or probability calibration can only tighten entry;
    this function never loosens the configured production floor.
    """
    settled = [
        bet for bet in (ledger.get("bets") or [])
        if bet.get("status") == "SETTLED" and str(bet.get("code") or "") == code
    ]
    decided = [bet for bet in settled if bet.get("result") != "Refunded"]
    stake = sum(float(bet.get("stake") or 0) for bet in settled)
    profit = sum(float(bet.get("pnl") or 0) for bet in settled)
    roi = profit / stake if stake else None
    hit_gap = None
    comparable = [
        bet for bet in decided
        if bet.get("model_prob", bet.get("prob")) is not None
    ]
    if comparable:
        hit = sum(
            1.0 if bet.get("result") == "Won"
            else 0.5 if bet.get("result") in {"Half Won", "Half Lost"}
            else 0.0
            for bet in comparable
        )
        predicted = sum(
            float(bet.get("model_prob", bet.get("prob")))
            for bet in comparable
        )
        hit_gap = hit / len(comparable) - predicted / len(comparable)

    edge_add = 0.0
    confidence_add = 0.0
    reason = "insufficient_market_sample"
    if len(settled) >= min_samples:
        reason = "market_performance_stable"
        if (roi is not None and roi <= -0.10) or (
            hit_gap is not None and hit_gap <= -0.12
        ):
            edge_add, confidence_add = 0.02, 4.0
            reason = "severe_market_underperformance"
        elif (roi is not None and roi < 0) or (
            hit_gap is not None and hit_gap < -0.08
        ):
            edge_add, confidence_add = 0.01, 2.0
            reason = "market_underperformance"

    return {
        "code": code,
        "n_settled": len(settled),
        "min_samples": min_samples,
        "roi": round(roi, 6) if roi is not None else None,
        "hit_gap": round(hit_gap, 6) if hit_gap is not None else None,
        "base_edge": config.min_edge,
        "base_confidence": config.confidence_floor,
        "min_edge": round(config.min_edge + edge_add, 6),
        "confidence_floor": round(config.confidence_floor + confidence_add, 1),
        "reason": reason,
    }
