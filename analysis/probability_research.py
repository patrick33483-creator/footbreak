"""Fail-closed, prospective probability research utilities.

These functions are deliberately data-only.  They never select a production
bet, alter a stake, send a notification, or treat missing evidence as zero.
"""
from __future__ import annotations
import math
from collections import defaultdict
from typing import Any, Iterable
from analysis.independent_validation import implied_break_even, wilson95

PRIOR_STRENGTH = 30
HIERARCHY_VERSION = "hierarchical-eb-beta-binomial-v1"
PROMOTION_MIN_FIXTURES = 100
PROMOTION_PREFERRED_FIXTURES = 200
CLV_COVERAGE_MINIMUM = .70


def number(value: Any) -> float | None:
    try: value = float(value)
    except (TypeError, ValueError): return None
    return value if math.isfinite(value) else None


def unavailable(reason: str) -> dict[str, Any]:
    return {"available": False, "reason": reason, "value": None}


def _truth(row: dict[str, Any]) -> int | None:
    """Read only a decided binary result; pushes/half results stay unavailable."""
    if any(row.get(key) for key in ("post_hoc", "post_hoc_backfill", "backfill", "exclude_from_simulation")):
        return None
    value = row.get("outcome", row.get("result", row.get("won")))
    if value is True or str(value).lower() in {"won", "win", "1", "true"}: return 1
    if value is False or str(value).lower() in {"lost", "loss", "0", "false"}: return 0
    return None


def _same(value: Any, expected: Any) -> bool:
    return expected in (None, "") or str(value) == str(expected)

_LEVEL_AXES = {
    "exact": ("system", "stage", "market", "path", "odds_tier", "direction", "role", "line_bucket", "league"),
    "no_league": ("system", "stage", "market", "path", "odds_tier", "direction", "role", "line_bucket"),
    "relaxed_line": ("system", "stage", "market", "path", "odds_tier", "direction", "role"),
    "market_prior": ("system", "stage", "market", "direction", "odds_tier"),
}

def _eligible_evidence(row: dict[str, Any], c: dict[str, Any], level: str) -> bool:
    if not isinstance(row, dict) or _truth(row) is None: return False
    axes = _LEVEL_AXES.get(level)
    return bool(axes) and all(_same(row.get(key), c.get(key)) for key in axes)


def _counts(evidence: Iterable[dict[str, Any]] | None, context: dict[str, Any], level: str) -> tuple[int, int] | None:
    """Count one immutable fixture-market per cohort or fail closed on conflict."""
    if evidence is None: return None
    fixture_markets: dict[str, tuple[Any, ...]] = {}
    for row in evidence:
        if _eligible_evidence(row, context, level):
            key = str(row.get("fixture_market_key") or "").strip()
            if not key:
                return None
            # Compare only retained axes. Deliberately relaxed dimensions must
            # de-duplicate, not cause an artificial path/line/league conflict.
            signature = tuple(row.get(name) for name in (*_LEVEL_AXES[level], "outcome"))
            previous = fixture_markets.get(key)
            if previous is not None and previous != signature:
                return None
            fixture_markets[key] = signature
    hits = sum(1 for signature in fixture_markets.values() if str(signature[-1]).lower() == "won")
    return hits, len(fixture_markets)


def hierarchical_estimate(context: dict[str, Any], evidence: Iterable[dict[str, Any]] | None, *, prior_strength: int = PRIOR_STRENGTH) -> dict[str, Any]:
    """Fixed four-level beta-binomial backoff with audit-friendly evidence.

    Missing evidence produces an explicit unavailable report rather than a
    synthetic market probability.  Every level excludes opposite direction,
    other systems/stages, pushes, post-hoc rows and rows without valid outcomes.
    """
    if prior_strength <= 0: raise ValueError("prior_strength must be positive")
    required = ("system", "stage", "market", "path", "odds_tier", "direction", "role")
    if any(context.get(key) in (None, "") for key in required):
        return {"available": False, "reason": "condition_signature_incomplete", "levels": []}
    if evidence is None:
        return {"available": False, "reason": "frozen_pre_admission_evidence_unavailable", "levels": []}
    levels: list[dict[str, Any]] = []
    prior_rate, prior_n = .5, prior_strength
    # Work broad-to-narrow so each narrower cell shrinks to the frozen broader posterior.
    for level in ("market_prior", "relaxed_line", "no_league", "exact"):
        count = _counts(evidence, context, level)
        if count is None:
            return {
                "available": False,
                "reason": f"fixture_market_duplicate_or_conflict_{level}",
                "levels": levels,
            }
        hits, n = count
        weight = n / (n + prior_strength)
        posterior = (hits + prior_strength * prior_rate) / (n + prior_strength)
        levels.append({
            "level": level, "raw_n": n, "raw_hits": hits,
            "raw_rate": round(hits / n, 6) if n else None,
            "prior_n": prior_n, "prior_rate": round(prior_rate, 6),
            "posterior_mean": round(posterior, 6), "wilson95": wilson95(hits, n),
            "actual_weight": round(weight, 6),
        })
        prior_rate, prior_n = posterior, n + prior_strength
    exact = levels[-1]
    return {"available": True, "version": HIERARCHY_VERSION, "prior_strength": prior_strength,
            "condition_signature": {key: context.get(key) for key in (*required, "league", "line_bucket")},
            "levels": levels, "exact_only_probability": exact["raw_rate"],
            "hierarchical_shrunk_probability": exact["posterior_mean"]}


def two_sided_no_vig(selected: dict[str, Any], quotes: Iterable[dict[str, Any]], *, fixture: str, market: str, kickoff: Any) -> dict[str, Any]:
    """Return selected-side no-vig probability only for one strict two-sided quote set."""
    wanted_side = str(selected.get("side") or "").upper()
    selected_line = selected.get("line", selected.get("condition"))
    source, observed = selected.get("quote_source") or selected.get("source"), selected.get("observed_at")
    required = [q for q in quotes if isinstance(q, dict) and str(q.get("fixture_id", q.get("match_id", fixture))) == str(fixture)
                and str(q.get("code", q.get("market", ""))).upper() == market
                and str(q.get("line", q.get("condition"))) == str(selected_line)
                and str(q.get("quote_source", q.get("source")) or "") == str(source or "")
                and str(q.get("observed_at") or "") == str(observed or "")]
    sides = {str(q.get("side") or "").upper(): number(q.get("odds")) for q in required}
    # HDC H/A; totals H/L. Exactly both directions and positive pre-kickoff validation is caller-owned.
    opposite = "A" if wanted_side == "H" and market == "HDC" else ("L" if wanted_side == "H" else "H")
    a, b = sides.get(wanted_side), sides.get(opposite)
    if a is None or b is None or a <= 1 or b <= 1 or len(sides) != 2:
        return unavailable("same_fixture_market_line_observed_source_two_sided_quote_unavailable")
    implied_a, implied_b = 1 / a, 1 / b
    return {"available": True, "value": round(implied_a / (implied_a + implied_b), 6),
            "overround": round(implied_a + implied_b - 1, 6), "source": source, "observed_at": observed}


def score_rows(rows: Iterable[dict[str, Any]], probability_key: str) -> dict[str, Any]:
    """Unique fixture+market scorecard. Missing probabilities never become zero."""
    unique: dict[tuple[str, str], dict[str, Any]] = {}
    for row in rows:
        if not isinstance(row, dict): continue
        unique.setdefault((str(row.get("match_id") or row.get("fixture_id") or ""), str(row.get("market") or row.get("code") or "")), row)
    records = list(unique.values())
    settled = [r for r in records if r.get("status") == "SETTLED" and _truth(r) is not None]
    probs = [number(r.get(probability_key)) for r in settled]
    if not settled or any(p is None for p in probs):
        probability = {"available": False, "brier": None, "log_loss": None, "calibration": None}
    else:
        targets = [_truth(r) for r in settled]
        clipped = [min(1 - 1e-12, max(1e-12, p)) for p in probs if p is not None]
        probability = {"available": True,
          "brier": round(sum((p-t)**2 for p,t in zip(probs,targets))/len(settled), 6),
          "log_loss": round(-sum(t*math.log(p)+(1-t)*math.log(1-p) for p,t in zip(clipped,targets))/len(settled), 6),
          "calibration": round(sum(probs)/len(probs)-sum(targets)/len(targets), 6)}
    pnl_rows = [r for r in records if r.get("status") == "SETTLED"]
    decided = [r for r in pnl_rows if _truth(r) is not None]
    hits = sum(_truth(r) or 0 for r in decided)
    stake = sum(number(r.get("stake")) or 0 for r in pnl_rows); pnl = sum(number(r.get("pnl")) or 0 for r in pnl_rows)
    return {"unique_fixtures": len(unique), "decided": len(decided), "hits": hits,
      "roi": round(pnl/stake, 6) if stake else None, "weighted_break_even": implied_break_even(decided),
      "hit_rate": round(hits / len(decided), 6) if decided else None,
      "wilson95": wilson95(hits, len(decided)), **probability}


def promotion_gate(metrics: dict[str, Any], baseline: dict[str, Any], *, clv_coverage: float | None, mean_clv: float | None) -> dict[str, Any]:
    reasons=[]; lower=(metrics.get("wilson95") or [None])[0]; be=metrics.get("weighted_break_even")
    if metrics.get("unique_fixtures",0) < PROMOTION_MIN_FIXTURES: reasons.append("unique_fixture_sample_below_100")
    if metrics.get("roi") is None or metrics["roi"] <= 0: reasons.append("roi_not_positive")
    if lower is None or be is None or lower <= be+.03: reasons.append("wilson_lower_not_above_weighted_break_even_plus_3pp")
    if not metrics.get("available") or not baseline.get("available"): reasons.append("market_baseline_probability_metrics_unavailable")
    elif metrics["brier"] > baseline["brier"] or metrics["log_loss"] > baseline["log_loss"]: reasons.append("not_noninferior_to_market_baseline")
    if clv_coverage is None or clv_coverage < CLV_COVERAGE_MINIMUM: reasons.append("clv_coverage_below_threshold_or_unavailable")
    if mean_clv is None or mean_clv < 0: reasons.append("mean_clv_negative_or_unavailable")
    return {"blocked": bool(reasons), "automatic_promotion": False, "human_review_required": True,
      "minimum_unique_fixtures": PROMOTION_MIN_FIXTURES, "preferred_unique_fixtures": PROMOTION_PREFERRED_FIXTURES,
      "clv_coverage_minimum": CLV_COVERAGE_MINIMUM, "reasons": reasons}
