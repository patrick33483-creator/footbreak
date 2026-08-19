"""Crown v2 champion/challenger research ledger.

This module is intentionally side-effect free except for mutations of the
dedicated ``crown_v2_challenger`` namespace supplied by the caller.  It never
reads a provider, sends Telegram, changes v1 rows, changes stakes, or promotes
itself to an active strategy.
"""
from __future__ import annotations

import copy
import hashlib
import json
import math
from collections import defaultdict
from datetime import datetime, timezone
from typing import Any

from analysis.independent_validation import (
    FIXED_STAKE, implied_break_even, portfolio_name, validation_bets, wilson95,
)
from analysis.probability_research import (
    number as research_number, promotion_gate, score_rows, two_sided_no_vig,
)
from .common import HKT, iso_hkt, parse_time

STRATEGY = "crown-independent-validation-v2-challenger"
NAMESPACE = "crown_v2_challenger"
CUTOVER_AT = "2026-08-19T20:00:00+08:00"
DECISION_STAGE = "T-5"
MAX_MARKETS_PER_FIXTURE = 2
LEAGUE_PRIOR_FIXTURES = 30
PROMOTION_MIN_FIXTURES = 100
PROMOTION_STRONG_FIXTURES = 200
VARIANTS = ("no_league", "league_shrunk")
_ALLOWED_SOURCES = frozenset({"fallback", "model_only", "model-only", "none", "unavailable"})


def _number(value: Any) -> float | None:
    try:
        value = float(value)
    except (TypeError, ValueError):
        return None
    return value if math.isfinite(value) else None


def _before(value: Any, reference: Any) -> bool:
    at, boundary = parse_time(value), parse_time(reference)
    return at is not None and boundary is not None and at < boundary


def _after(value: Any, reference: Any) -> bool:
    at, boundary = parse_time(value), parse_time(reference)
    return at is not None and boundary is not None and at > boundary


def _v1_snapshot(
    ledger: dict[str, Any], *, activation_at: str,
) -> dict[str, Any]:
    """Copy the v1 benchmark at v2 activation, never at an implied past time."""
    rows = validation_bets(ledger, "crown")
    digest = hashlib.sha256(json.dumps(
        rows, ensure_ascii=False, sort_keys=True, separators=(",", ":"), default=str,
    ).encode("utf-8")).hexdigest()
    return {
        "read_only": True,
        "strategy": "independent-validation-v1",
        "portfolio": portfolio_name("crown"),
        "benchmark_snapshot_at_activation": activation_at,
        "policy_cutover_at": CUTOVER_AT,
        "bet_count_at_activation": len(rows),
        "bets_sha256_at_activation": digest,
        "stats_at_activation": copy.deepcopy(ledger.get("stats") or {}),
        "note": (
            "v1 失敗基準於 v2 namespace 啟用時唯讀封存；"
            "此快照不是追溯到 policy cutover 的重算，"
            "v2 不會回溯篩選、重分類或混入 v1 注單。"
        ),
    }


def ensure_namespace(
    ledger: dict[str, Any], *, now: str | None = None,
) -> dict[str, Any]:
    """Create v2 once with an immutable activation boundary and v1 snapshot."""
    namespace = ledger.get(NAMESPACE)
    if namespace is None:
        namespace = {}
        ledger[NAMESPACE] = namespace
    if not isinstance(namespace, dict):
        raise ValueError("crown v2 challenger namespace must be an object")
    if namespace.get("strategy") not in (None, STRATEGY):
        raise ValueError("crown v2 challenger strategy mismatch")
    namespace.setdefault("schema_version", 2)
    namespace.setdefault("strategy", STRATEGY)
    namespace.setdefault("cutover_at", CUTOVER_AT)
    if namespace["cutover_at"] != CUTOVER_AT:
        raise ValueError("crown v2 cutover is immutable")
    activation_at = now or iso_hkt()
    if namespace.get("activation_at") is None:
        namespace["activation_at"] = activation_at
    if parse_time(namespace.get("activation_at")) is None:
        raise ValueError("crown v2 activation boundary must be a valid timestamp")
    namespace.setdefault("created_at", namespace["activation_at"])
    namespace.setdefault("mode", "challenger_research_shadow")
    namespace.setdefault("real_betting_enabled", False)
    namespace.setdefault("kelly_enabled", False)
    namespace.setdefault("telegram_actionable_enabled", False)
    namespace.setdefault("fixed_stake", FIXED_STAKE)
    namespace.setdefault("fixture_market_cap", MAX_MARKETS_PER_FIXTURE)
    namespace.setdefault("research_bets", [])
    namespace.setdefault("dedupe_keys", [])
    namespace.setdefault("audit", [])
    namespace.setdefault("league_effect", {
        "status": "research_only_no_frozen_pre_cutover_evidence",
        "freeze_required_before": CUTOVER_AT,
        "prior_fixtures": LEAGUE_PRIOR_FIXTURES,
        "note": "未有凍結賽前證據時，不估計聯賽效應；只保留無聯賽基準和研究列。",
    })
    namespace.setdefault(
        "v1_frozen_benchmark",
        _v1_snapshot(ledger, activation_at=str(namespace["activation_at"])),
    )
    return namespace


def _admission_boundary(namespace: dict[str, Any]) -> datetime | None:
    """Return the later immutable policy/activation boundary for prospective rows."""
    policy_cutover = parse_time(CUTOVER_AT)
    activation = parse_time(namespace.get("activation_at"))
    if policy_cutover is None or activation is None:
        return None
    return max(policy_cutover, activation)


def _valid_quote(stage: dict[str, Any], market: str) -> tuple[dict[str, Any] | None, str]:
    selected = [
        item for item in (stage.get("market_predictions") or [])
        if isinstance(item, dict) and str(item.get("code") or "").upper() == market
    ]
    if len(selected) != 1:
        return None, "selected_market_missing_or_ambiguous"
    item = selected[0]
    odds = _number(item.get("odds"))
    line = _number(item.get("line", item.get("condition")))
    side = str(item.get("side") or "").upper()
    valid_sides = {"H", "A"} if market == "HDC" else {"H", "L"}
    source = str(item.get("quote_source") or item.get("source") or "").strip().lower()
    observed = item.get("observed_at")
    kickoff = stage.get("kickoff") or stage.get("kickoff_hkt")
    if odds is None or odds <= 1 or line is None or side not in valid_sides:
        return None, "selected_quote_invalid"
    if not source or source in _ALLOWED_SOURCES:
        return None, "missing_quote_provenance"
    if not _before(observed, kickoff):
        return None, "quote_not_provably_pre_kickoff"
    return item, "ok"


def _two_sided_baseline(
    stage: dict[str, Any], selected: dict[str, Any], fixture: str, market: str,
) -> dict[str, Any]:
    """Strictly require the matching two-sided observed Crown quote set."""
    kickoff = stage.get("kickoff") or stage.get("kickoff_hkt")
    quotes = []
    for key in ("two_sided_quotes", "market_quotes", "market_predictions"):
        value = stage.get(key)
        if isinstance(value, list):
            quotes.extend(item for item in value if isinstance(item, dict))
    # The helper matches fixture/market/line/observed/source exactly.  The
    # pre-kickoff validity check applies to both legs, not only selection.
    valid = [quote for quote in quotes if _before(quote.get("observed_at"), kickoff)]
    return two_sided_no_vig(selected, valid, fixture=fixture, market=market, kickoff=kickoff)


def _closing_clv(stage: dict[str, Any], selected: dict[str, Any]) -> dict[str, Any]:
    """Use only a persisted, same-side real pre-kickoff closing quote."""
    kickoff = stage.get("kickoff") or stage.get("kickoff_hkt")
    entries = stage.get("closing_quotes") or []
    if not isinstance(entries, list):
        return {"available": False, "reason": "closing_quote_evidence_unavailable", "value": None}
    matching = [
        item for item in entries if isinstance(item, dict)
        and str(item.get("code") or item.get("market") or "").upper() == str(selected.get("code") or "").upper()
        and str(item.get("side") or "").upper() == str(selected.get("side") or "").upper()
        and str(item.get("line", item.get("condition"))) == str(selected.get("line", selected.get("condition")))
        and str(item.get("quote_source") or item.get("source") or "") == str(selected.get("quote_source") or selected.get("source") or "")
        and _before(item.get("observed_at"), kickoff)
    ]
    if len(matching) != 1:
        return {"available": False, "reason": "same_market_side_line_pre_kickoff_closing_quote_unavailable", "value": None}
    entry, closing = _number(selected.get("odds")), _number(matching[0].get("odds"))
    if entry is None or closing is None or entry <= 1 or closing <= 1:
        return {"available": False, "reason": "closing_quote_invalid", "value": None}
    # Positive means the admitted price was longer than the verified close.
    return {"available": True, "value": round(entry / closing - 1, 6), "observed_at": matching[0].get("observed_at")}


def _frozen_league_probability(
    namespace: dict[str, Any], market: str, league: str, probability: float | None,
) -> tuple[float | None, str]:
    """Use a market-only pooled effect; never create exact league × odds cells."""
    effect = namespace.get("league_effect")
    if not isinstance(effect, dict) or effect.get("status") != "frozen_pre_cutover_ready":
        return probability, "research_only_no_frozen_pre_cutover_evidence"
    if not _before(effect.get("frozen_at"), CUTOVER_AT):
        return probability, "research_only_invalid_league_evidence_time"
    markets = effect.get("markets")
    market_effect = markets.get(market) if isinstance(markets, dict) else None
    if not isinstance(market_effect, dict):
        return probability, "research_only_missing_market_league_evidence"
    global_probability = _number(market_effect.get("global_probability"))
    leagues = market_effect.get("leagues")
    league_effect = leagues.get(league) if isinstance(leagues, dict) else None
    league_probability = _number((league_effect or {}).get("probability"))
    league_n = _number((league_effect or {}).get("fixtures"))
    if (
        global_probability is None or not 0 <= global_probability <= 1
        or league_probability is None or not 0 <= league_probability <= 1
        or league_n is None or league_n < 0
    ):
        return probability, "research_only_invalid_league_evidence"
    weight = league_n / (league_n + LEAGUE_PRIOR_FIXTURES)
    # The selected pre-kickoff probability remains the no-league ablation.
    # League adjustment is a shrunken market-level effect, not an odds cell.
    base = probability if probability is not None else global_probability
    adjusted = max(0.0, min(1.0, base + weight * (league_probability - global_probability)))
    return round(adjusted, 6), "frozen_market_partial_pooling"


def evaluate_new_t5(
    ledger: dict[str, Any], watch: dict[str, Any], stage: dict[str, Any],
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """Append v2 rows only after both policy cutover and first activation."""
    now = iso_hkt()
    namespace = ensure_namespace(ledger, now=now)
    fixture = str(watch.get("match_id") or "").strip()
    kickoff = stage.get("kickoff") or stage.get("kickoff_hkt") or watch.get("kickoff")
    stage_at = stage.get("ts") or stage.get("source_snapshot_at")
    admission_boundary = _admission_boundary(namespace)
    audit: list[dict[str, Any]] = []
    if (
        not fixture or stage.get("stage") != DECISION_STAGE
        or stage.get("post_hoc_backfill") or stage.get("exclude_from_simulation")
        or admission_boundary is None
        or not _after(stage_at, admission_boundary) or not _before(stage_at, kickoff)
    ):
        audit.append({
            "market": "*", "status": "SKIPPED",
            "reason": "v2_policy_or_activation_or_native_t5_not_eligible",
        })
        namespace["audit"] = (namespace["audit"] + audit)[-1600:]
        return [], audit
    if not all(str(watch.get(field) or "").strip() for field in ("league", "home", "away")):
        audit.append({"market": "*", "status": "SKIPPED", "reason": "missing_fixture_context"})
        namespace["audit"] = (namespace["audit"] + audit)[-1600:]
        return [], audit

    created: list[dict[str, Any]] = []
    dedupe = set(str(key) for key in namespace.get("dedupe_keys") or [])
    for market in ("HIL", "HDC"):
        selected, reason = _valid_quote(stage, market)
        if selected is None:
            audit.append({"market": market, "status": "SKIPPED", "reason": reason})
            continue
        odds = _number(selected.get("odds"))
        assert odds is not None
        if market == "HIL" and not (1.80 <= odds < 1.90):
            audit.append({"market": market, "status": "SKIPPED", "reason": "v2_hil_odds_gate_1_80_1_89_only"})
            continue
        if market == "HDC" and 1.80 <= odds < 1.90:
            audit.append({"market": market, "status": "SKIPPED", "reason": "v2_hdc_1_80_1_89_explicitly_ineligible"})
            continue
        if market == "HDC" and not (1.90 <= odds < 2.00):
            audit.append({"market": market, "status": "SKIPPED", "reason": "v2_hdc_odds_gate_1_90_1_99_research_only"})
            continue
        probability = _number(selected.get("probability"))
        if probability is not None and not 0 <= probability <= 1:
            probability = None
        no_vig = _two_sided_baseline(stage, selected, fixture, market)
        closing_clv = _closing_clv(stage, selected)
        league_probability, league_status = _frozen_league_probability(
            namespace, market, str(watch["league"]), probability,
        )
        for variant, value in (
            ("no_league", probability), ("league_shrunk", league_probability),
        ):
            key = f"{fixture}|{market}|{DECISION_STAGE}|{STRATEGY}|{variant}"
            if key in dedupe:
                audit.append({"market": market, "status": "SKIPPED", "reason": "v2_idempotent_existing_research_row", "variant": variant})
                continue
            row = {
                "research_id": key, "dedupe_key": key, "strategy": STRATEGY,
                "portfolio": "crown_v2_challenger_research", "variant": variant,
                "match_id": fixture, "league": watch["league"], "home": watch["home"],
                "away": watch["away"], "kickoff": kickoff, "stage": DECISION_STAGE,
                "first_native_pre_kickoff_t5": True, "created_at": now,
                "namespace_activation_at": namespace["activation_at"],
                "admission_boundary_at": admission_boundary.isoformat(),
                "market": market, "code": market, "side": selected["side"],
                "line": selected.get("line", selected.get("condition")),
                "condition": selected.get("line", selected.get("condition")),
                "odds": odds, "stake": FIXED_STAKE, "status": "PENDING",
                "research_only": True, "simulation_only": True,
                "real_betting_enabled": False, "kelly_enabled": False,
                "actionable_telegram": False, "quote_source": selected.get("quote_source") or selected.get("source"),
                "quote_observed_at": selected.get("observed_at"), "probability": value,
                "probability_available": value is not None,
                # The no-vig baseline is unavailable unless *both* Crown legs
                # share fixture, market, line, observed_at and source.
                "market_implied_probability": no_vig.get("value"),
                "market_implied_available": bool(no_vig.get("available")),
                "market_implied_reason": no_vig.get("reason"),
                "model_probability": probability,
                "shrunk_probability": league_probability if variant == "league_shrunk" else None,
                "break_even_probability": round(1 / odds, 6),
                "edge": round(value - 1 / odds, 6) if value is not None else None,
                "brier": None, "log_loss": None,
                "calibration_bucket": _calibration_bucket(value),
                "closing_line_value": closing_clv.get("value"),
                "clv_available": bool(closing_clv.get("available")),
                "clv_reason": closing_clv.get("reason"),
                "league_effect_status": league_status if variant == "league_shrunk" else "no_league_ablation",
                "odds_lane": "1.80-1.89" if odds < 1.90 else "1.90-1.99",
                "history": [{"ts": now, "action": "v2挑戰者研究列建立", "reason": "首次原生賽前 T-5；非正式推介，不發 Telegram"}],
            }
            namespace["research_bets"].append(row)
            namespace["dedupe_keys"].append(key)
            dedupe.add(key)
            created.append(row)
            audit.append({"market": market, "status": "CREATED", "reason": "v2_challenger_research_created", "variant": variant})
    namespace["dedupe_keys"] = namespace["dedupe_keys"][-10000:]
    namespace["audit"] = (namespace["audit"] + audit)[-1600:]
    return created, audit


def _probability_metrics(rows: list[dict[str, Any]]) -> dict[str, Any]:
    settled = [row for row in rows if row.get("status") == "SETTLED" and row.get("result") != "Refunded"]
    scored = [
        row for row in settled
        if _number(row.get("probability")) is not None
    ]
    if len(scored) != len(settled) or not settled:
        return {"available": False, "brier": None, "log_loss": None, "calibration_gap": None}
    probabilities = [_number(row["probability"]) for row in scored]
    targets = [1.0 if row.get("result") in {"Won", "Half Won"} else 0.0 for row in scored]
    brier = sum((probability - target) ** 2 for probability, target in zip(probabilities, targets)) / len(scored)
    clipped = [min(1 - 1e-12, max(1e-12, probability)) for probability in probabilities]
    log_loss = -sum(
        target * math.log(probability) + (1 - target) * math.log(1 - probability)
        for probability, target in zip(clipped, targets)
    ) / len(scored)
    return {
        "available": True, "brier": round(brier, 6), "log_loss": round(log_loss, 6),
        "calibration_gap": round(abs(sum(probabilities) / len(scored) - sum(targets) / len(scored)), 6),
    }


def _metrics(rows: list[dict[str, Any]]) -> dict[str, Any]:
    fixture_rows: dict[tuple[str, str], dict[str, Any]] = {}
    for row in rows:
        fixture_rows.setdefault((str(row.get("match_id") or ""), str(row.get("market") or "")), row)
    rows = list(fixture_rows.values())
    settled = [row for row in rows if row.get("status") == "SETTLED"]
    decided = [row for row in settled if row.get("result") != "Refunded"]
    hits = sum(row.get("result") in {"Won", "Half Won"} for row in decided)
    turnover = sum(_number(row.get("stake")) or 0 for row in settled)
    pnl = sum(_number(row.get("pnl")) or 0 for row in settled)
    hit_rate = hits / len(decided) if decided else None
    return {
        "unique_fixtures": len(fixture_rows), "settled_unique_fixtures": len(settled),
        "decided": len(decided), "hits": hits, "pnl": round(pnl, 2),
        "turnover": round(turnover, 2), "roi": round(pnl / turnover, 6) if turnover else None,
        "hit_rate": round(hit_rate, 6) if hit_rate is not None else None,
        "weighted_break_even": implied_break_even(decided),
        "wilson95": wilson95(hits, len(decided)),
        **_probability_metrics(rows),
    }


def _promotion(
    metrics: dict[str, Any], no_league: dict[str, Any], market_baseline: dict[str, Any],
    *, clv_coverage: float | None, mean_clv: float | None, v1: dict[str, Any],
) -> dict[str, Any]:
    reasons: list[str] = []
    if metrics["unique_fixtures"] < PROMOTION_MIN_FIXTURES:
        reasons.append("unique_fixture_sample_below_100")
    if metrics["roi"] is None or metrics["roi"] <= 0:
        reasons.append("roi_not_positive")
    if metrics["hit_rate"] is None or metrics["weighted_break_even"] is None or metrics["hit_rate"] <= metrics["weighted_break_even"] + .03:
        reasons.append("hit_rate_not_above_break_even_plus_3pp")
    lower = (metrics.get("wilson95") or [None])[0]
    if lower is None or metrics["weighted_break_even"] is None or lower <= metrics["weighted_break_even"] + .03:
        reasons.append("wilson_lower_not_above_break_even_plus_3pp")
    if not metrics["available"] or not no_league["available"]:
        reasons.append("probability_fields_missing_for_brier_logloss_calibration")
    elif (
        metrics["brier"] > no_league["brier"]
        or metrics["log_loss"] > no_league["log_loss"]
    ):
        reasons.append("league_variant_not_noninferior_to_no_league")
    # v1 did not persist probability per bet in the current schema.  Never
    # invent it merely to make a promotion gate pass.
    if not v1.get("probability_metrics_available"):
        reasons.append("v1_champion_probability_metrics_unavailable")
    # This shared gate also fail-closes when the strict market no-vig baseline
    # or pre-kickoff CLV evidence is missing.  It never promotes automatically.
    strict = promotion_gate(metrics, market_baseline, clv_coverage=clv_coverage, mean_clv=mean_clv)
    reasons.extend(reason for reason in strict["reasons"] if reason not in reasons)
    strict.update({"promotion_review_eligible": not reasons, "strong_sample_preferred": PROMOTION_STRONG_FIXTURES, "reasons": reasons})
    return strict


def _clv_metrics(rows: list[dict[str, Any]]) -> dict[str, Any]:
    unique: dict[tuple[str, str], dict[str, Any]] = {}
    for row in rows:
        unique.setdefault((str(row.get("match_id") or ""), str(row.get("market") or "")), row)
    values = [_number(row.get("closing_line_value")) for row in unique.values() if row.get("clv_available")]
    total = len(unique)
    return {
        "coverage": round(len(values) / total, 6) if total else None,
        "mean": round(sum(values) / len(values), 6) if values else None,
        "available": bool(values),
    }


def _calibration_bucket(probability: float | None) -> str | None:
    if probability is None or probability < 0 or probability > 1:
        return None
    low = math.floor(probability * 10) / 10
    return f"{low:.1f}-{min(1.0, low + .1):.1f}"


def _persist_row_probability_metrics(rows: list[dict[str, Any]]) -> None:
    """Persist outcome-dependent research metrics only after real settlement."""
    for row in rows:
        probability = _number(row.get("probability"))
        row["calibration_bucket"] = _calibration_bucket(probability)
        if row.get("status") != "SETTLED" or row.get("result") == "Refunded" or probability is None:
            row["brier"] = None
            row["log_loss"] = None
            continue
        target = 1.0 if row.get("result") in {"Won", "Half Won"} else 0.0
        clipped = min(1 - 1e-12, max(1e-12, probability))
        row["brier"] = round((probability - target) ** 2, 6)
        row["log_loss"] = round(-(target * math.log(clipped) + (1 - target) * math.log(1 - clipped)), 6)


def recompute(namespace: dict[str, Any], ledger: dict[str, Any]) -> dict[str, Any]:
    """Compute prospective-only v2 report; rows remain separate from v1."""
    rows = [row for row in namespace.get("research_bets") or [] if isinstance(row, dict)]
    _persist_row_probability_metrics(rows)
    admission_boundary = _admission_boundary(namespace)
    v1_rows = validation_bets(ledger, "crown")
    v1_probability = all(_number(row.get("probability")) is not None for row in v1_rows if row.get("status") == "SETTLED")
    v1 = {"strategy": "independent-validation-v1", "read_only": True, "probability_metrics_available": bool(v1_rows) and v1_probability}
    by_market: dict[str, dict[str, Any]] = {}
    league_odds_market: list[dict[str, Any]] = []
    for market in ("HIL", "HDC"):
        no_league = [row for row in rows if row.get("market") == market and row.get("variant") == "no_league"]
        league_rows = [row for row in rows if row.get("market") == market and row.get("variant") == "league_shrunk"]
        base, pooled = _metrics(no_league), _metrics(league_rows)
        # All three scorecards use the identical prospective fixture+market
        # unit.  Missing two-sided quotes stay unavailable rather than being
        # rebuilt from a one-sided price or from post-kickoff data.
        no_league_model = score_rows(no_league, "model_probability")
        league_shrunk_model = score_rows(league_rows, "shrunk_probability")
        market_no_vig = score_rows(no_league, "market_implied_probability")
        clv = _clv_metrics(league_rows)
        by_market[market] = {
            "no_league_ablation": base | {"probability_metrics": no_league_model},
            "league_shrunk": pooled | {"probability_metrics": league_shrunk_model},
            "market_no_vig_baseline": market_no_vig,
            "clv": clv,
            "promotion": _promotion(
                league_shrunk_model, no_league_model, market_no_vig,
                clv_coverage=clv["coverage"], mean_clv=clv["mean"], v1=v1,
            ),
        }
    groups: dict[tuple[str, str, str], list[dict[str, Any]]] = defaultdict(list)
    for row in rows:
        if row.get("variant") == "no_league":
            groups[(str(row.get("league") or "—"), str(row.get("odds_lane") or "—"), str(row.get("market") or "—"))].append(row)
    for (league, odds_lane, market), grouped in sorted(groups.items()):
        league_odds_market.append({
            "league": league, "odds_lane": odds_lane, "market": market,
            "prospective": _metrics(grouped),
            "promotion_gate_use": False,
            "note": "聯賽×賠率×市場只作彙總；細格不得作升級或入場門檻。",
        })
    report = {
        "title": "v2挑戰者研究中",
        "subtitle": "非正式推介",
        "strategy": STRATEGY, "cutover_at": CUTOVER_AT,
        "activation_at": namespace.get("activation_at"),
        "admission_boundary_at": admission_boundary.isoformat() if admission_boundary else None,
        "mode": "challenger_research_shadow", "real_betting_enabled": False,
        "kelly_enabled": False, "actionable_telegram_enabled": False,
        "primary_unit": "unique_fixture_per_market",
        "v1_frozen_benchmark": namespace.get("v1_frozen_benchmark"),
        "by_market": by_market, "league_odds_market": league_odds_market,
        "research_rows": len(rows), "research_unique_fixture_markets": sum(
            metrics["no_league_ablation"]["unique_fixtures"] for metrics in by_market.values()
        ),
    }
    namespace["stats"] = report
    return report


def research_bets(ledger: dict[str, Any]) -> list[dict[str, Any]]:
    """Return only v2 rows; never fall back to or reinterpret v1 bets."""
    namespace = ledger.get(NAMESPACE)
    rows = namespace.get("research_bets") if isinstance(namespace, dict) else []
    return [row for row in rows or [] if isinstance(row, dict) and row.get("strategy") == STRATEGY]
