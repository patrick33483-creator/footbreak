"""Auditable Asian-totals quarter-line settlement profiles.

The Wilson history grades half wins as hits and half losses as misses.  These
profiles retain the frozen probability mass needed to convert that binary
history into payout-adjusted admission odds.
"""
from __future__ import annotations

import hashlib
import json
import math
from typing import Any


SCHEMA_VERSION = 2
MAX_GOALS = 12


def _canonical_hash(payload: dict[str, Any]) -> str:
    raw = json.dumps(
        payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"),
    )
    return hashlib.sha256(raw.encode()).hexdigest()


def _poisson(k: int, mean: float) -> float:
    return math.exp(k * math.log(mean) - mean - math.lgamma(k + 1))


def _dixon_coles_pmf(lh: float, la: float, rho: float) -> list[float]:
    values = [0.0] * (2 * MAX_GOALS + 1)
    total = 0.0
    for home in range(MAX_GOALS + 1):
        ph = _poisson(home, lh)
        for away in range(MAX_GOALS + 1):
            pa = _poisson(away, la)
            if home == 0 and away == 0:
                tau = 1.0 - lh * la * rho
            elif home == 0 and away == 1:
                tau = 1.0 + lh * rho
            elif home == 1 and away == 0:
                tau = 1.0 + la * rho
            elif home == 1 and away == 1:
                tau = 1.0 - rho
            else:
                tau = 1.0
            probability = max(0.0, ph * pa * tau)
            values[home + away] += probability
            total += probability
    return [value / total for value in values] if total > 0 else []


def _poisson_total_pmf(mean: float) -> list[float]:
    values = [_poisson(total, mean) for total in range(2 * MAX_GOALS + 1)]
    scale = sum(values)
    return [value / scale for value in values] if scale > 0 else []


def _quarter_fraction(line: float) -> float | None:
    fraction = abs(line) - math.floor(abs(line))
    if abs(fraction - 0.25) <= 1e-8:
        return 0.25
    if abs(fraction - 0.75) <= 1e-8:
        return 0.75
    return None


def _profile_core(
    pmf: list[float], *, line: float, side: str, method: str,
    source: dict[str, Any],
) -> dict[str, Any] | None:
    fraction = _quarter_fraction(line)
    side = str(side or "").upper()
    if not pmf or fraction is None or side not in {"H", "L"}:
        return None
    boundary = math.floor(line) if fraction < 0.5 else math.ceil(line)
    boundary_probability = pmf[boundary] if 0 <= boundary < len(pmf) else 0.0
    boundary_is_hit = (
        (fraction > 0.5 and side == "H")
        or (fraction < 0.5 and side == "L")
    )
    if side == "H":
        hit_probability = (
            sum(pmf[boundary:]) if boundary_is_hit
            else sum(pmf[boundary + 1:])
        )
    else:
        hit_probability = (
            sum(pmf[:boundary + 1]) if boundary_is_hit
            else sum(pmf[:boundary])
        )
    miss_probability = max(0.0, 1.0 - hit_probability)
    if boundary_is_hit:
        if hit_probability <= 0 or boundary_probability > hit_probability + 1e-9:
            return None
        win_fraction = 1.0 - 0.5 * boundary_probability / hit_probability
        loss_fraction = 1.0
        boundary_result = "half_win"
    else:
        if miss_probability <= 0 or boundary_probability > miss_probability + 1e-9:
            return None
        win_fraction = 1.0
        loss_fraction = 1.0 - 0.5 * boundary_probability / miss_probability
        boundary_result = "half_loss"
    core = {
        "schema_version": SCHEMA_VERSION,
        "method": method,
        "market": "HIL",
        "side": side,
        "line": line,
        "boundary_total": boundary,
        "boundary_result": boundary_result,
        "boundary_probability_raw": boundary_probability,
        "model_binary_hit_probability_raw": hit_probability,
        "win_fraction_raw": win_fraction,
        "loss_fraction_raw": loss_fraction,
        "source": source,
    }
    return {**core, "profile_hash": _canonical_hash(core)}


def from_dixon_coles(
    *, line: Any, side: Any, lh: Any, la: Any, rho: Any,
) -> dict[str, Any] | None:
    try:
        line_value = float(line)
        lh_value, la_value, rho_value = float(lh), float(la), float(rho)
    except (TypeError, ValueError):
        return None
    if (
        not all(math.isfinite(value) for value in (
            line_value, lh_value, la_value, rho_value,
        ))
        or lh_value <= 0 or la_value <= 0 or abs(rho_value) > 0.25
    ):
        return None
    source = {
        "kind": "dixon_coles_parameters",
        "lh": lh_value,
        "la": la_value,
        "rho": rho_value,
        "max_goals": MAX_GOALS,
    }
    return _profile_core(
        _dixon_coles_pmf(lh_value, la_value, rho_value),
        line=line_value, side=str(side), method="native_t5_dixon_coles_goal_pmf",
        source=source,
    )


def _fair_decimal(profile: dict[str, Any]) -> float:
    hit = float(profile["model_binary_hit_probability_raw"])
    win_fraction = float(profile["win_fraction_raw"])
    loss_fraction = float(profile["loss_fraction_raw"])
    return 1.0 + ((1.0 - hit) * loss_fraction) / (hit * win_fraction)


def _market_over_share(mean: float, line: float) -> float | None:
    over = _profile_core(
        _poisson_total_pmf(mean), line=line, side="H",
        method="native_t5_market_implied_poisson", source={},
    )
    under = _profile_core(
        _poisson_total_pmf(mean), line=line, side="L",
        method="native_t5_market_implied_poisson", source={},
    )
    if over is None or under is None:
        return None
    over_inverse = 1.0 / _fair_decimal(over)
    under_inverse = 1.0 / _fair_decimal(under)
    return over_inverse / (over_inverse + under_inverse)


def _mean_for_over_share(target: float, line: float) -> float | None:
    if not math.isfinite(target) or not 0.0 < target < 1.0:
        return None
    low, high = 0.05, 10.0
    low_share, high_share = (
        _market_over_share(low, line),
        _market_over_share(high, line),
    )
    if (
        low_share is None or high_share is None
        or not low_share <= target <= high_share
    ):
        return None
    for _ in range(80):
        middle = (low + high) / 2.0
        share = _market_over_share(middle, line)
        if share is None:
            return None
        if share < target:
            low = middle
        else:
            high = middle
    return (low + high) / 2.0


def from_two_sided_market(
    *, line: Any, side: Any, over_odds: Any, under_odds: Any,
) -> dict[str, Any] | None:
    """Infer a Poisson total from one complete two-sided quarter line.

    The fitted mean matches the observed no-vig ratio of the two Asian
    settlement-aware fair prices.  No neighbouring 2.5 or 3.0 line is needed.
    """
    try:
        line_value = float(line)
        over_value, under_value = float(over_odds), float(under_odds)
    except (TypeError, ValueError):
        return None
    if (
        _quarter_fraction(line_value) is None
        or not all(math.isfinite(value) for value in (
            line_value, over_value, under_value,
        ))
        or over_value <= 1.0 or under_value <= 1.0
    ):
        return None
    target = (1.0 / over_value) / (
        (1.0 / over_value) + (1.0 / under_value)
    )
    mean = _mean_for_over_share(target, line_value)
    if mean is None:
        return None
    source = {
        "kind": "two_sided_market_implied_poisson",
        "over_odds": over_value,
        "under_odds": under_value,
        "fitted_total_mean": mean,
        "max_goals": MAX_GOALS,
    }
    return _profile_core(
        _poisson_total_pmf(mean), line=line_value, side=str(side),
        method="native_t5_market_implied_poisson", source=source,
    )


def from_no_vig_probability(
    *, line: Any, side: Any, selected_probability: Any,
) -> dict[str, Any] | None:
    """Recover a profile from a persisted same-stage no-vig market share.

    Legacy Crown stage rows retained the selected side's normalized two-sided
    probability even when they did not retain the opposite raw quote.  This
    remains decision-time market evidence and does not use a result, later
    stage, or later price.
    """
    try:
        line_value = float(line)
        probability = float(selected_probability)
    except (TypeError, ValueError):
        return None
    side_value = str(side or "").upper()
    if (
        _quarter_fraction(line_value) is None
        or side_value not in {"H", "L"}
        or not math.isfinite(probability)
        or not 0.0 < probability < 1.0
    ):
        return None
    target = probability if side_value == "H" else 1.0 - probability
    mean = _mean_for_over_share(target, line_value)
    if mean is None:
        return None
    source = {
        "kind": "selected_market_no_vig_probability",
        "selected_probability": probability,
        "fitted_total_mean": mean,
        "max_goals": MAX_GOALS,
    }
    return _profile_core(
        _poisson_total_pmf(mean), line=line_value, side=side_value,
        method="native_market_no_vig_probability", source=source,
    )


def validate(
    profile: Any, *, market: str | None = None, side: str | None = None,
    line: Any = None,
) -> dict[str, Any] | None:
    """Recompute a profile from its frozen inputs and compare every field."""
    if not isinstance(profile, dict) or profile.get("schema_version") != SCHEMA_VERSION:
        return None
    source = profile.get("source")
    if not isinstance(source, dict):
        return None
    method = profile.get("method")
    if method == "native_t5_dixon_coles_goal_pmf":
        expected = from_dixon_coles(
            line=profile.get("line"), side=profile.get("side"),
            lh=source.get("lh"), la=source.get("la"), rho=source.get("rho"),
        )
    elif method == "native_t5_market_implied_poisson":
        expected = from_two_sided_market(
            line=profile.get("line"), side=profile.get("side"),
            over_odds=source.get("over_odds"), under_odds=source.get("under_odds"),
        )
    elif method == "native_market_no_vig_probability":
        expected = from_no_vig_probability(
            line=profile.get("line"), side=profile.get("side"),
            selected_probability=source.get("selected_probability"),
        )
    else:
        return None
    if expected is None or profile != expected:
        return None
    try:
        requested_line = float(line) if line is not None else None
    except (TypeError, ValueError):
        return None
    if (
        market is not None and str(market).upper() != "HIL"
        or side is not None and str(side).upper() != expected["side"]
        or requested_line is not None
        and abs(requested_line - float(expected["line"])) > 1e-8
    ):
        return None
    return dict(expected)
