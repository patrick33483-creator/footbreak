"""Leakage-safe shadow calibration tests for HDC, HIL and CHL predictions."""
from __future__ import annotations

import math
from typing import Any

from analysis.time_order_backtest import split_ids

MIN_MARKET_MATCHES = 100
MIN_HOLDOUT_MATCHES = 30
MIN_BRIER_IMPROVEMENT = 0.01
MAX_ACCURACY_DECLINE = 0.02
ALPHAS = (0.60, 0.70, 0.80, 0.90, 1.00, 1.10, 1.20)


def _clip(value: float) -> float:
    return min(.999999, max(.000001, value))


def recalibrate(probability: float, alpha: float) -> float:
    p = _clip(probability)
    return 1.0 / (1.0 + math.exp(-alpha * math.log(p / (1.0 - p))))


def _latest_market(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    latest: dict[tuple[str, str], dict[str, Any]] = {}
    for row in rows:
        key = (str(row["match_id"]), str(row["market"]))
        old = latest.get(key)
        rank = (str(row.get("kickoff")), str(row.get("predicted_at") or ""))
        old_rank = (
            str((old or {}).get("kickoff")),
            str((old or {}).get("predicted_at") or ""),
        )
        if old is None or rank > old_rank:
            latest[key] = row
    return list(latest.values())


def _metrics(rows: list[dict[str, Any]], alpha: float) -> dict[str, Any]:
    if not rows:
        return {"n": 0, "accuracy": None, "brier": None, "log_loss": None}
    briers, losses, hits = [], [], []
    for row in rows:
        p = recalibrate(float(row["probability"]), alpha)
        y = float(row["target"])
        briers.append((p - y) ** 2)
        losses.append(-(y * math.log(p) + (1.0 - y) * math.log(1.0 - p)))
        if y != .5:
            hits.append(int((p >= .5) == (y > .5)))
    return {
        "n": len(rows),
        "accuracy": round(sum(hits) / len(hits), 6) if hits else None,
        "brier": round(sum(briers) / len(briers), 6),
        "log_loss": round(sum(losses) / len(losses), 6),
    }


def market_test(rows: list[dict[str, Any]], market: str) -> dict[str, Any]:
    eligible = _latest_market([row for row in rows if row.get("market") == market])
    unique_matches = len({row["match_id"] for row in eligible})
    if unique_matches < MIN_MARKET_MATCHES:
        return {
            "market": market,
            "status": "waiting_for_100_verified_matches",
            "eligible_matches": unique_matches,
            "required_matches": MIN_MARKET_MATCHES,
            "remaining_matches": MIN_MARKET_MATCHES - unique_matches,
            "auto_promote": False,
        }
    train_ids, holdout_ids, cutoff = split_ids(eligible)
    train = [row for row in eligible if row["match_id"] in train_ids]
    holdout = [row for row in eligible if row["match_id"] in holdout_ids]
    alpha = min(ALPHAS, key=lambda value: _metrics(train, value)["brier"])
    baseline = _metrics(holdout, 1.0)
    challenger = _metrics(holdout, alpha)
    brier_delta = round(challenger["brier"] - baseline["brier"], 6)
    accuracy_delta = (
        round(challenger["accuracy"] - baseline["accuracy"], 6)
        if challenger["accuracy"] is not None and baseline["accuracy"] is not None
        else None
    )
    log_loss_delta = round(challenger["log_loss"] - baseline["log_loss"], 6)
    checks = {
        "holdout_matches": len(holdout_ids) >= MIN_HOLDOUT_MATCHES,
        "brier_improvement": brier_delta <= -MIN_BRIER_IMPROVEMENT,
        "accuracy_not_materially_worse": (
            accuracy_delta is not None and accuracy_delta >= -MAX_ACCURACY_DECLINE
        ),
        "log_loss_not_worse": log_loss_delta <= 0,
    }
    return {
        "market": market,
        "status": "candidate_passed_human_review_required"
        if all(checks.values()) else "tested_no_safe_upgrade",
        "eligible_matches": unique_matches,
        "train_matches": len(train_ids),
        "holdout_matches": len(holdout_ids),
        "holdout_start": cutoff,
        "champion": {"calibration_alpha": 1.0, "metrics": baseline},
        "challenger": {"calibration_alpha": alpha, "metrics": challenger},
        "delta": {
            "brier": brier_delta,
            "accuracy": accuracy_delta,
            "log_loss": log_loss_delta,
        },
        "checks": checks,
        "auto_promote": False,
    }


def all_market_tests(rows: list[dict[str, Any]]) -> dict[str, Any]:
    tests = {market: market_test(rows, market) for market in ("HDC", "HIL", "CHL")}
    return {
        "design": "chronological 70/30; calibration selected on train only; locked holdout",
        "tests": tests,
        "auto_promote": False,
        "review_required": any(
            item["status"] == "candidate_passed_human_review_required"
            for item in tests.values()
        ),
    }
