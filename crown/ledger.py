"""Idempotent Crown watch ledger.  It can create simulations, never real bets."""
from __future__ import annotations

from typing import Any

from .common import iso_hkt
from .config import Settings

STAGES = {"首預": 1, "T-30": 2, "T-5": 3}


def stage_for(minutes_to_kickoff: float, sweep: bool, done: set[str]) -> str | None:
    if sweep:
        return "首預" if "首預" not in done else None
    if 1 <= minutes_to_kickoff <= 10 and "T-5" not in done:
        return "T-5"
    if 20 <= minutes_to_kickoff <= 40 and "T-30" not in done:
        return "T-30"
    return None


def _snapshot(prediction: dict[str, Any], stage: str) -> dict[str, Any]:
    return {key: prediction.get(key) for key in (
        "match_id", "league", "home", "away", "kickoff_hkt", "mins_to_ko", "status", "verdict",
        "conviction", "no_bet_reason", "pick", "lead_view", "market_sources", "hkjc_match_id",
        "titan_match_id", "pinnapi_event_id", "source_snapshot_at", "execution",
    )} | {"stage": stage, "ts": iso_hkt()}


def _bet_id(prediction: dict[str, Any]) -> str:
    pick = prediction["pick"]
    return f"{prediction['match_id']}|{pick['code']}|{pick['condition']}|{pick['side']}"


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
    })
    watch.update({key: prediction.get(key) for key in ("league", "home", "away", "kickoff_hkt", "titan_match_id", "pinnapi_event_id", "hkjc_match_id")})
    watch["kickoff"] = prediction.get("kickoff_hkt")
    stage_rows = watch["stages"]
    existing = next((row for row in stage_rows if row.get("stage") == stage), None)
    if existing is None:
        stage_rows.append(_snapshot(prediction, stage))
        stage_rows.sort(key=lambda row: STAGES[row["stage"]])
    else:
        existing.update(_snapshot(prediction, stage))
    if stage != "T-5" or not prediction.get("pick"):
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
        "label": pick["label"], "odds": pick["odds"], "stake": pick["stake"], "model_prob": pick["prob"], "ev": pick["ev"],
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
