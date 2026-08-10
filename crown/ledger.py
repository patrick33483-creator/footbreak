"""Idempotent Crown watch ledger.  It can create simulations, never real bets."""
from __future__ import annotations

import os
from typing import Any

from analysis.learning_store import LearningStore

from .common import iso_hkt
from .config import Settings

STAGES = {"首預": 1, "T-30": 2, "T-5": 3}
PREDICTION_ERA = "2026-08-10-market-learning-v2"
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
            "probability": best.get("prob"),
            "source": best.get("reference") or "pinnapi_exact_line",
            "provider": best.get("provider") or "Crown",
        })
    return sorted(output, key=lambda row: row["code"])


def _snapshot(prediction: dict[str, Any], stage: str) -> dict[str, Any]:
    snapshot = {key: prediction.get(key) for key in (
        "match_id", "league", "home", "away", "kickoff_hkt", "mins_to_ko", "status", "verdict",
        "conviction", "no_bet_reason", "pick", "lead_view", "market_sources", "hkjc_match_id",
        "titan_match_id", "pinnapi_event_id", "source_snapshot_at", "execution",
        "outcome", "forecast", "probability", "likely_score", "prediction_source",
        "pinnapi_corner_event_id", "pinnapi_corner_source_at", "pinnapi_corner_timestamp_inferred",
        "matching_version",
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
        "model_prob": pick["prob"], "ev": pick["ev"], "provider": pick.get("provider"),
        "source": pick.get("source"), "bookmaker": pick.get("bookmaker"),
        "reference": pick.get("reference"), "reference_provider": pick.get("reference_provider"),
        "conviction": prediction.get("conviction"), "first_stage": "T-5", "stage": "T-5", "status": "PENDING",
        "simulation_only": True, "real_betting_enabled": False, "created_at": iso_hkt(),
        "history": [{"ts": iso_hkt(), "stage": "T-5", "action": "模擬注建立", "bet_id": bid}],
    })
    return [bid]


def recompute_stats(ledger: dict[str, Any], config: Settings) -> dict[str, Any]:
    bets = ledger["bets"]
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
    running_equity = config.bankroll
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

    previous_staking = ((ledger.get("stats") or {}).get("staking") or {})
    stats = {
        "n_pending": len(pending), "n_voided": sum(bet.get("status") == "VOIDED" for bet in bets), "n_settled": len(settled),
        "open_stake": round(sum(float(bet.get("stake") or 0) for bet in pending), 2),
        "open_pct": round(sum(float(bet.get("stake") or 0) for bet in pending) / config.bankroll, 4) if config.bankroll else 0,
        "pnl": pnl, "turnover": turnover, "roi": round(pnl / turnover, 4) if turnover else None,
        "n_decided": len(decided), "hits": hits, "hit_rate": round(hits / len(decided), 4) if decided else None,
        "equity": round(config.bankroll + pnl, 2), "by_market": by_market, "curve": curve,
        "res_counts": res_counts,
        "daily_cap": config.bankroll, "open_cap": round(config.bankroll * 0.35, 2), "single_cap_pct": 0.04,
        "conf_floor": config.confidence_floor, "bet_stage": "T-5", "n_watch": len(ledger["watch"]),
        "n_stage_preds": sum(len(item.get("stages") or []) for item in ledger["watch"].values()),
        "staking": {
            "fraction": previous_staking.get("fraction", 1 / 3),
            "cap": previous_staking.get("cap", 0.04),
            "label": previous_staking.get("label", "階段一 · 建立樣本"),
            "level": previous_staking.get("level", 1),
            "n_settled": len(settled),
            "slope": previous_staking.get("slope"),
            "buckets": previous_staking.get("buckets", []),
            "perf": previous_staking.get("perf", {}),
            "demoted": previous_staking.get("demoted", False),
            "market_mult": previous_staking.get("market_mult", {"CHL": 0.5}),
        },
    }
    ledger["stats"] = stats
    return stats
