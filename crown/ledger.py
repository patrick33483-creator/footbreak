"""Idempotent Crown watch ledger.  It can create simulations, never real bets."""
from __future__ import annotations

import math
import os
from datetime import datetime, timedelta, timezone
from typing import Any

from analysis.learning_store import LearningStore

from .common import HKT, iso_hkt, parse_time
from .config import Settings
from .condition_portfolio import FIXED_STAKE, STARTING_BANKROLL, STRATEGY, evaluate_new_t5

STAGES = {"首預": 1, "T-30": 2, "T-5": 3}
PREDICTION_ERA = "2026-08-12-hkjc-corner-forecast-v4"
PREDICTION_SCHEMA_VERSION = 2


def completed_stages(
    watch: dict[str, Any],
    matching_version: str,
    prediction_era: str | None = None,
) -> set[str]:
    """Refresh stale first looks once without replaying T-30/T-5 decisions."""
    done = {
        str(row.get("stage"))
        for row in watch.get("stages", [])
        # A provider/mapping outage is not a completed prediction.  Keep the
        # stage eligible for a later recovery pass; sync_prediction updates
        # the same stage row idempotently and still cannot duplicate a bet.
        if (
            row.get("stage")
            and row.get("status") != "DATA_MISSING"
            # Schema-v2 rows explicitly record selected-odds availability.
            # A fixture-level baseline forecast with no priced market is only
            # an attempt, not a completed 首預/T-30/T-5 market prediction.
            # Keep legacy rows (which have no odds_status field) compatible,
            # but retry current rows until an auditable pre-kickoff market
            # quote is persisted.
            and (
                row.get("odds_status") is None
                or (
                    row.get("odds_status") == "available"
                    and bool(row.get("market_predictions"))
                )
            )
        )
    }
    if (
        (
            watch.get("matching_version") != matching_version
            or (
                prediction_era is not None
                and watch.get("prediction_era") != prediction_era
            )
        )
        and not done.intersection({"T-30", "T-5"})
    ):
        done.discard("首預")
    return done


def stage_for(minutes_to_kickoff: float, sweep: bool, done: set[str]) -> str | None:
    if sweep:
        return "首預" if "首預" not in done else None
    # A known card can reach a timed window when a prior board sweep failed,
    # was delayed, or discovered it just after that sweep started.  Do not let
    # a later T-30/T-5 snapshot become the first persisted decision: it makes
    # the three-stage history incomplete and the dashboard misleadingly says
    # it is merely waiting for T-30.  The tick has enough local identity to
    # recover 首預 without another fixture-list request; the next tick can
    # process the still-due timed stage.
    if (
        0 < minutes_to_kickoff <= 40
        and "首預" not in done
        and "T-5" not in done
    ):
        return "首預"
    if 0 < minutes_to_kickoff <= 10 and "T-5" not in done:
        return "T-5"
    if 20 <= minutes_to_kickoff <= 40 and "T-30" not in done:
        return "T-30"
    return None


def _observed_before_kickoff(value: Any, kickoff: Any) -> bool:
    """Accept only a finite quote observation that predates this fixture."""
    kickoff_at = parse_time(str(kickoff or ""))
    if kickoff_at is None:
        return False
    try:
        observed_number = float(value)
    except (TypeError, ValueError):
        observed_at = parse_time(str(value or ""))
    else:
        if not math.isfinite(observed_number) or observed_number <= 0:
            return False
        # Provider rows normally use epoch seconds; tolerate milliseconds only
        # for an otherwise valid immutable observation.
        if observed_number >= 10_000_000_000:
            observed_number /= 1000
        try:
            observed_at = datetime.fromtimestamp(observed_number, HKT)
        except (OverflowError, OSError, ValueError):
            return False
    return observed_at is not None and observed_at < kickoff_at


def _market_predictions(
    candidates: list[dict[str, Any]],
    kickoff: Any,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """Persist only auditable pre-kickoff selected market observations.

    Fixture-level forecasts remain available in the stage metadata.  A
    scoreable market row, however, must never be created without an exact
    line/side, decimal odds, and an observation that predates kickoff.
    """
    grouped: dict[str, list[dict[str, Any]]] = {}
    for candidate in candidates or []:
        code = str(candidate.get("code") or "")
        if code in {"HDC", "HIL", "CHL"}:
            grouped.setdefault(code, []).append(candidate)
    output: list[dict[str, Any]] = []
    rejected: list[dict[str, Any]] = []
    for code, rows in grouped.items():
        best = max(rows, key=lambda row: float(row.get("prob") or 0))
        odds = best.get("odds")
        try:
            odds_valid = float(odds) > 1.0
        except (TypeError, ValueError):
            odds_valid = False
        observed_at = best.get("observed_at") or best.get("source_at")
        raw_line = best.get("line")
        try:
            line_valid = math.isfinite(float(raw_line))
        except (TypeError, ValueError):
            line_valid = False
        side_valid = best.get("side") in {
            "HDC": {"H", "A"},
            "HIL": {"H", "L"},
            "CHL": {"H", "L"},
        }[code]
        timestamp_valid = _observed_before_kickoff(observed_at, kickoff)
        if not (odds_valid and line_valid and side_valid and timestamp_valid):
            reason = (
                "selected_quote_unavailable"
                if not odds_valid
                else "selected_line_or_side_unavailable"
                if not (line_valid and side_valid)
                else "selected_quote_not_observed_pre_kickoff"
            )
            rejected.append({
                "code": code,
                "line": raw_line,
                "side": best.get("side"),
                "reason": reason,
            })
            continue
        output.append({
            "code": code,
            "market": best.get("market"),
            "condition": best.get("condition"),
            "line": best.get("line"),
            "side": best.get("side"),
            "label": best.get("label"),
            "odds": odds,
            "odds_status": "available",
            "odds_reason": None,
            # Crown price rows carry source_at as epoch seconds.  Preserve it
            # exactly; a missing observed timestamp remains explicit.
            "observed_at": observed_at,
            "probability": best.get("prob"),
            # A probability is usable by a strategy only when its immutable
            # source is retained.  In particular, Crown's own no-vig market
            # probability must never be reused as Kelly p against its price.
            "probability_source": best.get("reference"),
            "probability_observed_at": best.get("probability_observed_at"),
            # Keep the historical learning-source field backward compatible;
            # quote_source is the provider evidence for the selected price.
            "source": best.get("reference") or "pinnapi_exact_line",
            "quote_source": best.get("source") or best.get("reference") or "pinnapi_exact_line",
            "provider": best.get("provider") or "Crown",
        })
    return sorted(output, key=lambda row: row["code"]), rejected


def _selected_odds_journal(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return [{
        "code": row.get("code"), "line": row.get("line", row.get("condition")),
        "side": row.get("side"), "odds": row.get("odds"),
        "odds_status": row.get("odds_status"), "reason": row.get("odds_reason"),
        "source": row.get("quote_source") or row.get("source"),
        "provider": row.get("provider"),
        "observed_at": row.get("observed_at"),
    } for row in rows]


def _snapshot(prediction: dict[str, Any], stage: str) -> dict[str, Any]:
    market_predictions, market_prediction_rejections = _market_predictions(
        prediction.get("forecast_candidates") or prediction.get("candidates") or [],
        prediction.get("kickoff_hkt"),
    )
    odds_journal = _selected_odds_journal(market_predictions)
    unavailable_quotes = [
        row for row in odds_journal
        if row.get("odds_status") != "available"
    ]
    all_selected_quotes_available = bool(odds_journal) and not unavailable_quotes
    snapshot = {key: prediction.get(key) for key in (
        "match_id", "league", "home", "away", "kickoff_hkt", "mins_to_ko", "status", "verdict",
        "conviction", "no_bet_reason", "pick", "lead_view", "market_sources", "hkjc_match_id",
        "titan_match_id", "pinnapi_event_id", "source_snapshot_at", "execution",
        "outcome", "forecast", "probability", "likely_score", "prediction_source",
        "probabilities", "baseline_low_confidence", "edge_reference_status", "edge_reference_note",
        "sharp_reference_available", "source_status", "pinnapi_source_at",
        "pinnapi_timestamp_inferred", "pinnapi_timestamp_basis",
        "pinnapi_corner_event_id", "pinnapi_corner_source_at", "pinnapi_corner_timestamp_inferred",
        "matching_version", "crown_quote_cached_forecast_only", "crown_cached_source_at",
    )} | {
        "prediction_era": PREDICTION_ERA,
        "schema_version": PREDICTION_SCHEMA_VERSION,
        "stage": stage,
        "ts": iso_hkt(),
        "market_predictions": market_predictions,
        # This is non-market metadata, so a missing quote remains auditable
        # without becoming a scoreable history row.  Historic rows are not
        # rewritten by this gate or by the recovery-overlay workflow.
        "market_prediction_rejections": market_prediction_rejections,
        "selected_odds_journal": odds_journal,
        # A partial journal must not make the complete stage appear priced.
        # Each market still retains its own explicit missing reason below.
        "odds_status": "available" if all_selected_quotes_available else "missing",
        "odds_reason": (
            None if all_selected_quotes_available
            else (
                "one_or_more_selected_quotes_unavailable"
                if odds_journal else "no_selected_market_quote"
            )
        ),
    }
    # Persist only compact, verifiable provider provenance for the immutable
    # source-health report.  It distinguishes an unmatched PinnAPI fixture
    # from a matched-but-unavailable quote without making PinnAPI a prerequisite
    # for Crown's pure forecast path.
    if not snapshot.get("source_status"):
        if snapshot.get("sharp_reference_available") is True:
            snapshot["source_status"] = "pinnapi_live"
        elif not snapshot.get("pinnapi_event_id"):
            snapshot["source_status"] = "pinnapi_fixture_unmatched"
        elif snapshot.get("edge_reference_status") == "unavailable":
            snapshot["source_status"] = "pinnapi_live_unavailable"
        else:
            snapshot["source_status"] = "pinnapi_status_unobserved"
    return snapshot


def _bet_id(match_id: str, market: str) -> str:
    return f"{match_id}|{market}|T-5|{STRATEGY}"


def condition_bets(ledger: dict[str, Any]) -> list[dict[str, Any]]:
    """Return only rows belonging to the active fixed-stake portfolio.

    Old ledger rows remain readable until the explicit reset is performed, but
    must not appear in the new portfolio's statistics or settlement queue.
    """
    return [
        bet for bet in (ledger.get("bets") or [])
        if isinstance(bet, dict)
        and bet.get("portfolio") == "condition_simulation"
        and bet.get("strategy") == STRATEGY
    ]


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
    """Persist a stage and create only newly-observed eligible T-5 condition bets."""
    ledger.setdefault("bets", [])
    ledger.setdefault("watch", {})
    stage = prediction.get("stage")
    if stage not in STAGES:
        return []
    match_id = str(prediction["match_id"])
    watch = ledger["watch"].setdefault(match_id, {
        "match_id": match_id, "league": prediction.get("league"), "home": prediction.get("home"),
        "away": prediction.get("away"), "kickoff": prediction.get("kickoff_hkt"),
        "titan_match_id": prediction.get("titan_match_id"), "pinnapi_event_id": prediction.get("pinnapi_event_id"),
        "hkjc_match_id": prediction.get("hkjc_match_id"), "stages": [],
        "matching_version": prediction.get("matching_version"), "prediction_era": PREDICTION_ERA,
        "discovered_at": prediction.get("discovered_at") or iso_hkt(),
    })
    watch.update({key: prediction.get(key) for key in (
        "league", "home", "away", "kickoff_hkt", "titan_match_id", "pinnapi_event_id", "hkjc_match_id", "matching_version",
    )})
    watch["prediction_era"] = PREDICTION_ERA
    watch["kickoff"] = prediction.get("kickoff_hkt")
    if not watch.get("discovered_at"):
        watch["discovered_at"] = prediction.get("discovered_at") or iso_hkt()
    stage_rows = watch["stages"]
    existing = next((row for row in stage_rows if row.get("stage") == stage), None)
    existing_had_quote = bool(
        existing
        and existing.get("odds_status") == "available"
        and existing.get("market_predictions")
    )
    snapshot = _snapshot(prediction, stage)
    learning = _record_learning_snapshot(prediction, snapshot)
    if learning:
        snapshot.update({
            "learning_snapshot_id": learning["snapshot_id"], "learning_attempt": learning["attempt"],
            "learning_pre_kickoff": learning["pre_kickoff"], "learning_payload_sha256": learning["payload_sha256"],
        })
        if not learning["pre_kickoff"]:
            return []
    if existing is None:
        stage_rows.append(snapshot)
        stage_rows.sort(key=lambda row: STAGES[row["stage"]])
    else:
        existing.update(snapshot)
    # A T-5 that first persisted with no auditable selected quote stays due.
    # When a later, still-pre-kickoff retry supplies that evidence, it may be
    # evaluated exactly once as a newly eligible decision.  It is the same
    # immutable stage row (updated in place), never a fabricated/backfilled
    # second T-5 stage.  Existing priced T-5 stages remain idempotent.
    retry_with_new_quote = (
        stage == "T-5"
        and existing is not None
        and not existing_had_quote
        and snapshot.get("odds_status") == "available"
        and bool(snapshot.get("market_predictions"))
    )
    if stage != "T-5" or (existing is not None and not retry_with_new_quote):
        return []
    created, audit = evaluate_new_t5(ledger, watch, config)
    snapshot["condition_simulation"] = {"strategy": STRATEGY, "stage": "T-5", "audit": audit}
    audit_rows = ledger.setdefault("condition_simulation_audit", [])
    audit_rows.extend([{"match_id": match_id, **item} for item in audit])
    ledger["condition_simulation_audit"] = audit_rows[-1600:]
    ledger["bets"].extend(created)
    if created:
        ledger.setdefault("log", []).extend([
            {"ts": bet["created_at"], "action": "條件模擬注建立", "bet_id": bet["bet_id"],
             "match_id": match_id, "market": bet["market_label"], "condition": bet["condition_label"]}
            for bet in created
        ])
    return [str(bet["bet_id"]) for bet in created]

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
    peak = bankroll
    max_drawdown = 0.0
    max_drawdown_pct = 0.0
    for point in curve:
        equity = float(point["equity"])
        peak = max(peak, equity)
        drawdown = max(0.0, peak - equity)
        max_drawdown = max(max_drawdown, drawdown)
        if peak > 0:
            max_drawdown_pct = max(max_drawdown_pct, drawdown / peak)

    return {
        "n_pending": len(pending), "n_voided": sum(bet.get("status") == "VOIDED" for bet in bets), "n_settled": len(settled),
        "open_stake": round(sum(float(bet.get("stake") or 0) for bet in pending), 2),
        "open_pct": round(sum(float(bet.get("stake") or 0) for bet in pending) / bankroll, 4) if bankroll else 0,
        "pnl": pnl, "turnover": turnover, "roi": round(pnl / turnover, 4) if turnover else None,
        "n_decided": len(decided), "hits": hits, "hit_rate": round(hits / len(decided), 4) if decided else None,
        "equity": round(bankroll + pnl, 2), "by_market": by_market, "curve": curve,
        "res_counts": res_counts, "max_drawdown": round(max_drawdown, 2),
        "max_drawdown_pct": round(max_drawdown_pct, 6),
    }


def recompute_stats(ledger: dict[str, Any], config: Settings) -> dict[str, Any]:
    """Compute statistics for the sole fixed-stake condition portfolio."""
    del config
    ledger.setdefault("bets", [])
    bets = condition_bets(ledger)
    ledger["bankroll"] = STARTING_BANKROLL
    base = _portfolio_stats(bets, STARTING_BANKROLL)
    base.update({
        "strategy": STRATEGY, "starting_bankroll": STARTING_BANKROLL,
        "fixed_stake": FIXED_STAKE, "entry_rule": "T-5 only; historical GRADED condition accuracy >60%; decided >=10",
    })
    ledger["stats"] = base
    # Retired keys are tolerated if read from old state but are never created,
    # displayed, settled, or included in statistics.
    return base

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
