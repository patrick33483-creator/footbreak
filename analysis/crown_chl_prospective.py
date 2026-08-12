#!/usr/bin/env python3
"""Crown CHL (總角球大小) frozen prospective-only challenger validation.

This module is deliberately isolated:

* It never writes a live probability, pick, official/shadow ledger, stake,
  or notification, and it never auto-applies anything.
* It is completely separate from the 資料健康 (data-health) report: neither
  module imports the other, and they share no state file or artifact.
* Its executable model state lives only in a private ``0600`` file under
  ``/var/lib/footbreak/challenger``; the public challenger report carries
  status, counts, and metrics only.

Counting policy
---------------
The **primary prospective unit is one deterministic row per unique fixture**,
never every stage.  The primary-stage rule is predeclared here, before any
result is observed, and is frozen into the state file at cutoff:

    T-5  >  T-30  >  首預

that is, the latest available immutable pre-kickoff CHL snapshot for the
fixture.  Stage-specific metrics are reported separately as *correlated*
secondary diagnostics and must never be added together as independent samples.
"""
from __future__ import annotations

import json
import math
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable, Sequence

from analysis.challenger_model import (
    CLIP,
    _line as parse_line,
    MAX_ACCURACY_DECLINE,
    MIN_BRIER_IMPROVEMENT,
    MIN_LOG_LOSS_IMPROVEMENT,
    TrainOnlyEncoder,
    _canonical,
    _encoder_from_state,
    _encoder_state,
    _finite,
    _period,
    _sha256,
    atomic_create_state,
    build_feature_rows,
    chronological_fixture_split,
    fit_logistic,
    predict,
    probability_metrics,
)

MARKET = "CHL"
STATE_KIND = "crown_chl_frozen_prospective_shadow"
STATE_SCHEMA_VERSION = 1
MODEL_VERSION = "crown-chl-frozen-prospective-v1"
FEATURE_SCHEMA_VERSION = "pre_kickoff-chl-team-corner-v1"

# Predeclared, result-blind primary-stage rule.  Documented in the module
# docstring, frozen into the state, and surfaced in the public report.
PRIMARY_STAGE_PRIORITY: tuple[str, ...] = ("T-5", "T-30", "首預")
STAGES: tuple[str, ...] = ("首預", "T-30", "T-5")

MIN_PROSPECTIVE_FIXTURES = 30
STRONG_SAMPLE_FIXTURES = 100
WALK_FORWARD_FOLDS = 3
MIN_WALK_FORWARD_TRAIN_FIXTURES = 40
TEAM_FEATURE_MIN_COVERAGE = 0.80
TEAM_FEATURE_L2 = 12.0

CHAMPION_STRATEGY = "market_favourite"
BENCHMARK_STRATEGY = "closing_reference"
DECLARED_STRATEGIES: tuple[dict[str, Any], ...] = (
    {
        "id": CHAMPION_STRATEGY,
        "label": "現行 HKJC 去水市場方向",
        "role": "champion",
        "fits_model": False,
    },
    {
        "id": "always_under",
        "label": "永遠買細(under)基準",
        "role": "baseline",
        "fits_model": False,
    },
    {
        "id": BENCHMARK_STRATEGY,
        "label": "T-5／收盤方向參考",
        "role": "benchmark_only",
        "fits_model": False,
    },
    {
        "id": "team_corner_feature",
        "label": "球隊角球特徵候選",
        "role": "candidate",
        "fits_model": True,
    },
)

# Strictly pre-kickoff team-corner inputs.  If they are genuinely absent the
# candidate fails closed with ``insufficient_feature_coverage``; they are never
# invented, imputed from post-match data, or back-filled from a result.
TEAM_CORNER_PATHS: tuple[tuple[str, tuple[str, ...]], ...] = (
    ("home_corners_for_avg", ("team_corners", "home_for_avg")),
    ("away_corners_for_avg", ("team_corners", "away_for_avg")),
    ("home_corners_against_avg", ("team_corners", "home_against_avg")),
    ("away_corners_against_avg", ("team_corners", "away_against_avg")),
    ("team_corner_sample_matches", ("team_corners", "sample_matches")),
)
TEAM_NUMERIC_FEATURES: tuple[str, ...] = tuple(name for name, _ in TEAM_CORNER_PATHS) + (
    "base_probability",
    "market_line",
    "market_odds",
)
TEAM_CATEGORICAL_FEATURES: tuple[str, ...] = ("selection_side",)

UNDER_SIDES = frozenset({"S", "U", "L-", "細", "小", "under", "UNDER", "Under"})
OVER_SIDES = frozenset({"L", "O", "大", "over", "OVER", "Over"})


# ────────────────────────── helpers ──────────────────────────


def _nested(value: Any, path: Sequence[str]) -> Any:
    for key in path:
        if not isinstance(value, dict):
            return None
        value = value.get(key)
    return value


def wilson_interval(successes: int, total: int, z: float = 1.959963984540054) -> list[float] | None:
    """Wilson score 95% interval for a hit rate; None when undefined."""
    if total <= 0 or successes < 0 or successes > total:
        return None
    proportion = successes / total
    denominator = 1.0 + (z * z) / total
    centre = proportion + (z * z) / (2 * total)
    spread = z * math.sqrt((proportion * (1 - proportion) + (z * z) / (4 * total)) / total)
    return [
        round(max(0.0, (centre - spread) / denominator), 6),
        round(min(1.0, (centre + spread) / denominator), 6),
    ]


def _market_prediction(row: dict[str, Any]) -> dict[str, Any]:
    for candidate in (_nested(row.get("payload"), ("market_predictions",)) or []):
        if isinstance(candidate, dict) and str(candidate.get("code")) == MARKET:
            return candidate
    return {}


def side_of(row: dict[str, Any]) -> str:
    market = _market_prediction(row)
    side = market.get("side")
    if side in (None, ""):
        side = str(row.get("target_key") or "").rpartition("|")[2]
    return str(side or "")


def is_under_side(side: str) -> bool | None:
    """True for the under/細 side, False for over/大, None when undecidable."""
    if side in UNDER_SIDES:
        return True
    if side in OVER_SIDES:
        return False
    return None


def primary_rows(rows: Iterable[dict[str, Any]]) -> list[dict[str, Any]]:
    """Return exactly one deterministic CHL row per unique fixture.

    The predeclared, result-blind rule keeps the latest available immutable
    pre-kickoff stage (T-5 > T-30 > 首預).  Ties inside one stage resolve on
    the later prediction time, then the target key, so the result is stable.
    """
    priority = {stage: index for index, stage in enumerate(PRIMARY_STAGE_PRIORITY)}
    best: dict[str, dict[str, Any]] = {}
    for row in rows:
        if str(row.get("market")) != MARKET:
            continue
        fixture = str(row["match_id"])
        rank = priority.get(str(row.get("stage")))
        if rank is None:
            continue
        current = best.get(fixture)
        key = (rank, -row["predicted_at"].timestamp(), str(row.get("target_key") or ""))
        if current is None or key < current["_order"]:
            best[fixture] = {**row, "_order": key}
    output = [
        {key: value for key, value in row.items() if key != "_order"}
        for row in best.values()
    ]
    output.sort(key=lambda row: (row["kickoff"], str(row["match_id"])))
    return output


def stage_rows(rows: Iterable[dict[str, Any]], stage: str) -> list[dict[str, Any]]:
    """One deterministic CHL row per fixture for a single stage."""
    best: dict[str, dict[str, Any]] = {}
    for row in rows:
        if str(row.get("market")) != MARKET or str(row.get("stage")) != stage:
            continue
        fixture = str(row["match_id"])
        key = (-row["predicted_at"].timestamp(), str(row.get("target_key") or ""))
        current = best.get(fixture)
        if current is None or key < current["_order"]:
            best[fixture] = {**row, "_order": key}
    output = [
        {key: value for key, value in row.items() if key != "_order"}
        for row in best.values()
    ]
    output.sort(key=lambda row: (row["kickoff"], str(row["match_id"])))
    return output


def team_feature_coverage(rows: Sequence[dict[str, Any]]) -> dict[str, Any]:
    """Coverage of genuinely present immutable pre-kickoff team-corner inputs."""
    fields: dict[str, dict[str, Any]] = {}
    for name, path in TEAM_CORNER_PATHS:
        present = sum(
            _finite(_nested(row.get("payload"), path)) is not None for row in rows
        )
        fields[name] = {
            "present_rows": present,
            "rows": len(rows),
            "coverage": round(present / len(rows), 6) if rows else 0.0,
        }
    minimum = min((item["coverage"] for item in fields.values()), default=0.0)
    return {
        "minimum_required_coverage": TEAM_FEATURE_MIN_COVERAGE,
        "minimum_observed_coverage": minimum,
        "eligible": bool(rows) and minimum >= TEAM_FEATURE_MIN_COVERAGE,
        "fields": dict(sorted(fields.items())),
        "policy": (
            "team corner features are used only when genuinely present before "
            "kickoff; they are never imputed or back-filled from results"
        ),
    }


def _team_feature_rows(rows: Sequence[dict[str, Any]]) -> list[dict[str, Any]]:
    """Attach the CHL team-corner numeric/categorical view to feature rows."""
    featured = build_feature_rows(list(rows))
    output = []
    for row in featured:
        payload = row.get("payload") if isinstance(row.get("payload"), dict) else {}
        numeric = dict(row["numeric"])
        for name, path in TEAM_CORNER_PATHS:
            numeric[name] = _finite(_nested(payload, path))
        output.append({**row, "numeric": numeric})
    return output


# ────────────────────────── strategy scoring ──────────────────────────


def _scored_pair(row: dict[str, Any], take_under: bool | None) -> tuple[float, float] | None:
    """Return (probability, target) for the direction this strategy takes.

    The stored probability/target always describe the *selected* side.  Taking
    the opposite side mirrors both, which keeps a 0.5 push at 0.5.
    """
    probability = _finite(row.get("probability"))
    target = _finite(row.get("target"))
    if probability is None or target is None:
        return None
    if not 0.0 <= probability <= 1.0 or not 0.0 <= target <= 1.0:
        return None
    selected_under = is_under_side(side_of(row))
    if take_under is None or selected_under is None or take_under == selected_under:
        return probability, target
    return 1.0 - probability, 1.0 - target


def _strategy_scores(
    strategy: str,
    rows: Sequence[dict[str, Any]],
    *,
    model: tuple[TrainOnlyEncoder, list[float]] | None = None,
) -> tuple[list[float], list[dict[str, Any]]] | None:
    """Return aligned probabilities and pseudo-rows for one strategy."""
    probabilities: list[float] = []
    scored: list[dict[str, Any]] = []
    if strategy == "team_corner_feature":
        if model is None:
            return None
        encoder, coefficients = model
        raw = predict(encoder, coefficients, list(rows))
        for probability, row in zip(raw, rows):
            target = _finite(row.get("target"))
            if target is None:
                return None
            probabilities.append(min(1.0 - CLIP, max(CLIP, probability)))
            scored.append({"target": target})
        return probabilities, scored
    take_under = True if strategy == "always_under" else None
    for row in rows:
        pair = _scored_pair(row, take_under)
        if pair is None:
            return None
        probability, target = pair
        probabilities.append(min(1.0 - CLIP, max(CLIP, probability)))
        scored.append({"target": target})
    return probabilities, scored


def _hits(
    probabilities: Sequence[float],
    scored: Sequence[dict[str, Any]],
    *,
    direction_based: bool,
) -> tuple[int, int]:
    """Count decided rows and hits for the direction the strategy actually takes.

    A declared direction strategy (market favourite, always under) wins when
    the side it took wins.  A fitted model has no declared side, so its
    direction is the side its own probability favours.  Pushes are excluded
    from the denominator, never silently counted as wins.
    """
    decided = hits = 0
    for probability, row in zip(probabilities, scored):
        target = float(row["target"])
        if target == 0.5:
            continue
        decided += 1
        if direction_based:
            hits += int(target > 0.5)
        else:
            hits += int((probability >= 0.5) == (target > 0.5))
    return hits, decided


def strategy_metrics(
    strategy: str,
    rows: Sequence[dict[str, Any]],
    *,
    model: tuple[TrainOnlyEncoder, list[float]] | None = None,
) -> dict[str, Any] | None:
    scores = _strategy_scores(strategy, rows, model=model)
    if scores is None:
        return None
    probabilities, scored = scores
    metrics = probability_metrics(probabilities, scored)
    declared = next(item for item in DECLARED_STRATEGIES if item["id"] == strategy)
    hits, decided = _hits(
        probabilities, scored, direction_based=not bool(declared["fits_model"])
    )
    return {
        **metrics,
        "unique_fixtures": len({str(row["match_id"]) for row in rows}),
        "decided_rows": decided,
        "hits": hits,
        "pushes": len(scored) - decided,
        # `accuracy` stays the probability-side score used by every gate in
        # this repository.  `hit_rate` is the direction the strategy took.
        "hit_rate": round(hits / decided, 6) if decided else None,
        "hit_rate_ci95": wilson_interval(hits, decided),
    }


# ────────────────────────── historical selection ──────────────────────────


def _fixture_order(rows: Sequence[dict[str, Any]]) -> tuple[list[str], dict[str, datetime]]:
    kickoffs: dict[str, datetime] = {}
    for row in rows:
        fixture = str(row["match_id"])
        kickoffs[fixture] = min(kickoffs.get(fixture, row["kickoff"]), row["kickoff"])
    return sorted(kickoffs, key=lambda fixture: (kickoffs[fixture], fixture)), kickoffs


def walk_forward_folds(rows: Sequence[dict[str, Any]]) -> list[tuple[set[str], set[str]]]:
    """Deterministic expanding-window fixture folds; never row folds."""
    ordered, _ = _fixture_order(rows)
    remaining = len(ordered) - MIN_WALK_FORWARD_TRAIN_FIXTURES
    if remaining < WALK_FORWARD_FOLDS:
        return []
    base, extra = divmod(remaining, WALK_FORWARD_FOLDS)
    start = MIN_WALK_FORWARD_TRAIN_FIXTURES
    folds: list[tuple[set[str], set[str]]] = []
    for index in range(WALK_FORWARD_FOLDS):
        size = base + (1 if index < extra else 0)
        validation = set(ordered[start:start + size])
        if not validation:
            return []
        folds.append((set(ordered[:start]), validation))
        start += size
    return folds


def _score_candidate(
    strategy: str,
    rows: Sequence[dict[str, Any]],
    folds: list[tuple[set[str], set[str]]],
    coverage: dict[str, Any],
) -> dict[str, Any]:
    """Walk-forward score one predeclared strategy; encoders fit train-only."""
    declared = next(item for item in DECLARED_STRATEGIES if item["id"] == strategy)
    base = {
        "id": strategy,
        "label": declared["label"],
        "role": declared["role"],
        "folds": [],
    }
    if strategy == "team_corner_feature" and not coverage["eligible"]:
        return {
            **base,
            "status": "insufficient_feature_coverage",
            "feature_coverage": coverage,
            "metrics": None,
            "champion_metrics": None,
        }
    candidate_probability: list[float] = []
    champion_probability: list[float] = []
    scored: list[dict[str, Any]] = []
    fold_counts: list[dict[str, int]] = []
    for train_ids, validation_ids in folds:
        train = [row for row in rows if str(row["match_id"]) in train_ids]
        validation = [row for row in rows if str(row["match_id"]) in validation_ids]
        if not train or not validation:
            continue
        model = None
        if strategy == "team_corner_feature":
            featured_train = _team_feature_rows(train)
            featured_validation = _team_feature_rows(validation)
            # Encoder medians/scales/categories see training fixtures only.
            model = fit_logistic(
                featured_train,
                numeric_features=TEAM_NUMERIC_FEATURES,
                categorical_features=TEAM_CATEGORICAL_FEATURES,
                l2=TEAM_FEATURE_L2,
            )
            candidate = _strategy_scores(strategy, featured_validation, model=model)
        else:
            candidate = _strategy_scores(strategy, validation)
        champion = _strategy_scores(CHAMPION_STRATEGY, validation)
        if candidate is None or champion is None:
            return {
                **base,
                "status": "unscorable_rows",
                "metrics": None,
                "champion_metrics": None,
            }
        candidate_probability.extend(candidate[0])
        champion_probability.extend(champion[0])
        scored.extend(champion[1])
        fold_counts.append({
            "train_fixtures": len(train_ids),
            "validation_fixtures": len(validation_ids),
            "validation_rows": len(validation),
        })
    if not scored:
        return {**base, "status": "unscorable_rows", "metrics": None, "champion_metrics": None}
    return {
        **base,
        "status": "scored",
        "folds": fold_counts,
        "metrics": probability_metrics(candidate_probability, scored),
        "champion_metrics": probability_metrics(champion_probability, scored),
    }


def select_strategy(rows: Sequence[dict[str, Any]]) -> dict[str, Any] | None:
    """Select from the predeclared grid using expanding walk-forward folds only.

    The benchmark-only closing reference can never be selected, and a
    candidate must beat the declared champion on *both* proper scores.
    """
    folds = walk_forward_folds(rows)
    if len(folds) != WALK_FORWARD_FOLDS:
        return None
    coverage = team_feature_coverage(rows)
    selectable = [
        item["id"] for item in DECLARED_STRATEGIES if item["role"] in {"champion", "baseline", "candidate"}
    ]
    candidates = [_score_candidate(strategy, rows, folds, coverage) for strategy in selectable]
    champion = next(item for item in candidates if item["id"] == CHAMPION_STRATEGY)
    eligible = [
        item for item in candidates
        if item["id"] != CHAMPION_STRATEGY
        and item["status"] == "scored"
        and item["metrics"] is not None
        and champion["metrics"] is not None
        and item["metrics"]["brier"] is not None
        and item["metrics"]["log_loss"] is not None
        and float(item["metrics"]["brier"]) < float(champion["metrics"]["brier"])
        and float(item["metrics"]["log_loss"]) < float(champion["metrics"]["log_loss"])
    ]
    selected = min(
        eligible or [champion],
        key=lambda item: (
            float(item["metrics"]["brier"]),
            float(item["metrics"]["log_loss"]),
            str(item["id"]),
        ),
    )
    return {
        "method": "predeclared_strategy_grid_expanding_window_walk_forward",
        "fold_count": len(folds),
        "minimum_walk_forward_train_fixtures": MIN_WALK_FORWARD_TRAIN_FIXTURES,
        "status": (
            "candidate_selected_with_both_walk_forward_proper_scores_improved"
            if selected["id"] != CHAMPION_STRATEGY
            else "champion_retained_no_walk_forward_proper_score_improvement"
        ),
        "selected_id": selected["id"],
        "team_feature_coverage": coverage,
        "candidates": candidates,
    }


def _state_version_hash(state: dict[str, Any]) -> str:
    copy = json.loads(_canonical(state))
    copy.get("frozen", {}).pop("version_hash", None)
    return _sha256(copy)


def build_state(rows: Sequence[dict[str, Any]], cutoff: datetime) -> dict[str, Any] | None:
    """Freeze the Crown CHL prospective model from pre-cutoff history only.

    Fixtures kicking off at exactly the cutoff are excluded, and the final
    chronological 30% of pre-cutoff history is withheld from every selection
    decision so an already-inspected holdout cannot be tuned on.
    """
    cutoff = cutoff.astimezone(timezone.utc)
    historical = [row for row in primary_rows(rows) if row["kickoff"] < cutoff]
    if not historical:
        return None
    selection_ids, excluded_ids, _ = chronological_fixture_split(historical)
    selection = [row for row in historical if str(row["match_id"]) in selection_ids]
    selected = select_strategy(selection)
    if selected is None:
        return None
    ordered, kickoffs = _fixture_order(selection)
    private_model: dict[str, Any] | None = None
    if selected["selected_id"] == "team_corner_feature":
        encoder, coefficients = fit_logistic(
            _team_feature_rows(selection),
            numeric_features=TEAM_NUMERIC_FEATURES,
            categorical_features=TEAM_CATEGORICAL_FEATURES,
            l2=TEAM_FEATURE_L2,
        )
        private_model = {
            "encoder": _encoder_state(encoder),
            "coefficients": [round(value, 12) for value in coefficients],
        }
    state: dict[str, Any] = {
        "schema_version": STATE_SCHEMA_VERSION,
        "kind": STATE_KIND,
        "created_at": cutoff.isoformat(),
        "freeze_cutoff": cutoff.isoformat(),
        "frozen": {
            "market": MARKET,
            "model_version": MODEL_VERSION,
            "feature_schema_version": FEATURE_SCHEMA_VERSION,
            "primary_stage_rule": list(PRIMARY_STAGE_PRIORITY),
            "primary_unit": "one_row_per_unique_fixture",
            "selected_strategy": selected["selected_id"],
            "minimum_prospective_fixtures": MIN_PROSPECTIVE_FIXTURES,
            "strong_sample_fixtures": STRONG_SAMPLE_FIXTURES,
        },
        "selection": {
            **selected,
            "historical_fixtures_before_cutoff": len(historical),
            "selection_fixtures": len(selection_ids),
            "excluded_recent_holdout_fixtures": len(excluded_ids),
            "selection_period": _period(ordered, kickoffs),
        },
        # Executable state never leaves the 0600 private file.
        "private_model": private_model,
    }
    state["frozen"]["version_hash"] = _state_version_hash(state)
    return state


def load_state(path: Path) -> dict[str, Any]:
    payload = json.loads(path.read_text(encoding="utf-8"))
    if (
        not isinstance(payload, dict)
        or payload.get("schema_version") != STATE_SCHEMA_VERSION
        or payload.get("kind") != STATE_KIND
        or _state_version_hash(payload) != (payload.get("frozen") or {}).get("version_hash")
    ):
        raise ValueError(f"invalid or altered frozen Crown CHL state: {path}")
    return payload


# ────────────────────────── prospective evaluation ──────────────────────────


# A settled Asian total resolves to one of five immutable targets.  The
# flat-stake return of one unit is fixed by that target and the price, so it is
# written out explicitly rather than inferred from a win/lose boolean.
_UNIT_RETURN = {
    1.00: lambda price: price - 1.0,          # Won
    0.75: lambda price: (price - 1.0) / 2.0,  # Half Won
    0.50: lambda _price: 0.0,                 # Refunded / push
    0.25: lambda _price: -0.5,                # Half Lost
    0.00: lambda _price: -1.0,                # Lost
}


def chosen_direction(
    strategy: str, row: dict[str, Any], probability: float | None = None
) -> tuple[bool | None, str | None]:
    """Return the under/over direction a strategy actually takes for one row.

    ``True`` means the under side, ``False`` the over side.  A fitted model has
    no declared side, so it keeps the stored selection only while its own
    probability favours it; otherwise it flips.
    """
    selected_under = is_under_side(side_of(row))
    if selected_under is None:
        return None, "direction_not_resolvable"
    if strategy == "always_under":
        return True, None
    if strategy == CHAMPION_STRATEGY:
        return selected_under, None
    if probability is None:
        return None, "model_probability_unavailable"
    return (selected_under if probability >= 0.5 else not selected_under), None


def _alternate_price(row: dict[str, Any], take_under: bool) -> float | None:
    """Find an immutable price genuinely quoted for the opposite direction.

    The selected side's own price is never reused for the other direction.  A
    sibling quote must match the same market and the same line before it can
    stand in, and explicit ``under_odds``/``over_odds``/``opposite_odds``
    fields are accepted only when they were persisted before kickoff.
    """
    selected = _market_prediction(row)
    line = parse_line(selected.get("line"))
    if line is None:
        line = parse_line(selected.get("condition"))
    for candidate in (_nested(row.get("payload"), ("market_predictions",)) or []):
        if not isinstance(candidate, dict) or candidate is selected:
            continue
        if str(candidate.get("code")) != MARKET:
            continue
        candidate_line = parse_line(candidate.get("line"))
        if candidate_line is None:
            candidate_line = parse_line(candidate.get("condition"))
        if line is None or candidate_line is None or abs(candidate_line - line) > 1e-9:
            continue
        if is_under_side(str(candidate.get("side") or "")) is not take_under:
            continue
        price = _finite(candidate.get("odds"))
        if price is not None and price > 1.0:
            return price
    explicit = selected.get("under_odds" if take_under else "over_odds")
    price = _finite(explicit)
    if price is not None and price > 1.0:
        return price
    price = _finite(selected.get("opposite_odds"))
    if price is not None and price > 1.0:
        return price
    return None


def aligned_price_and_target(
    row: dict[str, Any], take_under: bool
) -> tuple[float | None, float | None, str | None]:
    """Return the immutable price and settled target for one chosen direction."""
    selected_under = is_under_side(side_of(row))
    if selected_under is None:
        return None, None, "direction_not_resolvable"
    target = _finite(row.get("target"))
    if target is None or target not in _UNIT_RETURN:
        return None, None, "unscorable_settlement_target"
    if take_under == selected_under:
        price = _finite(_market_prediction(row).get("odds"))
        if price is None or price <= 1.0:
            return None, None, "selected_side_price_unavailable"
        return price, target, None
    price = _alternate_price(row, take_under)
    if price is None:
        return None, None, "opposite_side_price_unavailable"
    # Mirroring the settled target is exact: Won<->Lost, Half Won<->Half Lost,
    # Refunded stays Refunded.
    return price, round(1.0 - target, 6), None


def _shadow_returns(
    rows: Sequence[dict[str, Any]],
    strategy: str,
    probabilities: Sequence[float] | None = None,
) -> dict[str, Any]:
    """Flat-stake shadow return for the direction the strategy actually takes.

    This fails closed.  Every scored row must have a price that was genuinely
    quoted for *that* direction and a target settled for *that* direction; a
    selected-side price is never reused for the opposite side.  Any gap makes
    the whole ROI ``null`` with a precise reason instead of a wrong number.

    It is *not* an edge or +EV claim.  Predicting HKJC outcomes from HKJC
    prices cannot demonstrate positive expected value, and CLV needs closing
    prices that the immutable store does not currently retain.
    """
    result: dict[str, Any] = {
        "strategy": strategy,
        "alignment": "strategy_direction_must_match_an_immutable_price_and_target",
        "rows": len(rows),
        "aligned_rows": 0,
        "direction_flips": 0,
        "odds_coverage": 0.0,
        "closing_odds_coverage": 0.0,
        "roi": None,
        "clv": None,
        "reason": None,
        "note": (
            "影子回報只係記錄,唔係優勢或 +EV。用 HKJC 賠率去預測 HKJC 賽果,"
            "本質上證明唔到正期望值。"
        ),
    }
    if not rows:
        result["reason"] = "no_prospective_rows"
        return result

    priced: list[tuple[float, float]] = []
    closing_available = 0
    first_reason: str | None = None
    flips = 0
    for index, row in enumerate(rows):
        probability = (
            probabilities[index]
            if probabilities is not None and index < len(probabilities)
            else None
        )
        take_under, reason = chosen_direction(strategy, row, probability)
        if take_under is None:
            first_reason = first_reason or (reason or "direction_not_resolvable")
            continue
        selected_under = is_under_side(side_of(row))
        if selected_under is not None and take_under != selected_under:
            flips += 1
        price, target, reason = aligned_price_and_target(row, take_under)
        if price is None or target is None:
            first_reason = first_reason or (reason or "unscorable_rows")
            continue
        priced.append((price, target))
        if take_under == selected_under:
            closing = _finite(_market_prediction(row).get("closing_odds"))
            if closing is not None and closing > 1.0:
                closing_available += 1

    result["aligned_rows"] = len(priced)
    result["direction_flips"] = flips
    result["odds_coverage"] = round(len(priced) / len(rows), 6)
    result["closing_odds_coverage"] = round(closing_available / len(rows), 6)
    if len(priced) < len(rows):
        # Fail closed: a partial book would silently change the denominator.
        result["reason"] = first_reason or "aligned_price_unavailable_for_every_row"
        return result

    staked = float(len(priced))
    returned = sum(_UNIT_RETURN[target](price) for price, target in priced)
    result["roi"] = round(returned / staked, 6)
    result["staked_units"] = round(staked, 6)
    if closing_available < len(rows):
        result["reason"] = "closing_odds_unavailable"
    else:  # pragma: no cover - production data has no closing prices yet
        moves = []
        for index, (price, _target) in enumerate(priced):
            closing = _finite(_market_prediction(rows[index]).get("closing_odds"))
            if closing and closing > 1.0:
                moves.append(math.log(price / closing))
        result["clv"] = round(sum(moves) / len(moves), 6) if moves else None
    return result


def _stage_diagnostics(rows: Sequence[dict[str, Any]]) -> list[dict[str, Any]]:
    """Per-stage metrics as correlated secondary diagnostics only."""
    output = []
    for stage in STAGES:
        subset = stage_rows(rows, stage)
        champion = strategy_metrics(CHAMPION_STRATEGY, subset) if subset else None
        under = strategy_metrics("always_under", subset) if subset else None
        output.append({
            "stage": stage,
            "unique_fixtures": len({str(row["match_id"]) for row in subset}),
            "rows": len(subset),
            "champion": champion,
            "always_under": under,
            "correlated_secondary_diagnostic": True,
            "note": "階段之間高度相關,唔可以相加當獨立樣本。",
        })
    return output


def _closing_reference(rows: Sequence[dict[str, Any]]) -> dict[str, Any]:
    """Diagnostic-only T-5/closing benchmark, never used to score earlier stages."""
    t5 = stage_rows(rows, "T-5")
    all_fixtures = {str(row["match_id"]) for row in rows if str(row.get("market")) == MARKET}
    covered = {str(row["match_id"]) for row in t5}
    metrics = strategy_metrics(CHAMPION_STRATEGY, t5) if t5 else None
    return {
        "id": BENCHMARK_STRATEGY,
        "label": "T-5／收盤方向參考",
        "role": "benchmark_only",
        "benchmark_only": True,
        "excluded_from_promotion_gate": True,
        "available": bool(t5),
        "status": "available" if t5 else "unavailable_no_t5_snapshot",
        "covered_fixtures": len(covered),
        "fixtures_without_t5": len(all_fixtures - covered),
        "coverage": round(len(covered) / len(all_fixtures), 6) if all_fixtures else 0.0,
        "metrics": metrics,
        "note": (
            "只作參考基準。冇 T-5 快照嘅場次明確標示為不可用,"
            "亦絕對唔會用未來嘅 T-5 資訊去評分較早階段嘅決定。"
        ),
    }


def promotion_gate(
    champion: dict[str, Any],
    challenger: dict[str, Any],
    unique_fixtures: int,
    selected_strategy: str,
) -> dict[str, Any]:
    def delta(key: str) -> float | None:
        if champion.get(key) is None or challenger.get(key) is None:
            return None
        return round(float(challenger[key]) - float(champion[key]), 6)

    deltas = {"brier": delta("brier"), "log_loss": delta("log_loss"), "accuracy": delta("accuracy")}
    checks = {
        "minimum_prospective_fixtures": unique_fixtures >= MIN_PROSPECTIVE_FIXTURES,
        "identical_fixture_rows": (
            champion.get("n") == challenger.get("n") and int(champion.get("n") or 0) > 0
        ),
        "candidate_differs_from_champion": selected_strategy != CHAMPION_STRATEGY,
        "meaningful_brier_improvement": (
            deltas["brier"] is not None and deltas["brier"] <= -MIN_BRIER_IMPROVEMENT
        ),
        "log_loss_improved": (
            deltas["log_loss"] is not None and deltas["log_loss"] <= -MIN_LOG_LOSS_IMPROVEMENT
        ),
        "accuracy_not_materially_worse": (
            deltas["accuracy"] is not None and deltas["accuracy"] >= -MAX_ACCURACY_DECLINE
        ),
    }
    reasons = [name for name, passed in checks.items() if not passed]
    return {"checks": checks, "deltas": deltas, "passed": not reasons, "rejection_reasons": reasons}


def evaluate_prospective(rows: Sequence[dict[str, Any]], state: dict[str, Any]) -> dict[str, Any]:
    """Score only fixtures kicking off strictly after the immutable cutoff."""
    cutoff = datetime.fromisoformat(str(state["freeze_cutoff"])).astimezone(timezone.utc)
    frozen = state["frozen"]
    market_rows = [row for row in rows if str(row.get("market")) == MARKET]
    future = [row for row in market_rows if row["kickoff"] > cutoff]
    primary = primary_rows(future)
    fixtures = {str(row["match_id"]) for row in primary}
    selected_strategy = str(frozen["selected_strategy"])
    report: dict[str, Any] = {
        "market": MARKET,
        "model_version": frozen["model_version"],
        "state_version_hash": frozen["version_hash"],
        "freeze_cutoff": cutoff.isoformat(),
        "cutoff_boundary": "kickoff strictly after cutoff; exact cutoff excluded",
        "primary_unit": "one_row_per_unique_fixture",
        "primary_stage_rule": list(frozen["primary_stage_rule"]),
        "primary_stage_rule_declared_before_results": True,
        "selected_strategy": selected_strategy,
        "declared_strategies": [
            {key: item[key] for key in ("id", "label", "role")} for item in DECLARED_STRATEGIES
        ],
        "selection": public_selection(state["selection"]),
        "minimum_prospective_fixtures": MIN_PROSPECTIVE_FIXTURES,
        "strong_sample_fixtures": STRONG_SAMPLE_FIXTURES,
        "prospective_fixtures": len(fixtures),
        "prospective_rows": len(future),
        "remaining_fixtures": max(0, MIN_PROSPECTIVE_FIXTURES - len(fixtures)),
        "stage_diagnostics": _stage_diagnostics(future),
        "closing_reference": _closing_reference(future),
        "feature_coverage": team_feature_coverage(primary),
        "auto_apply": False,
        "retraining": False,
        "probability_artifact_written": False,
        "live_integration": "none",
    }
    coverage = report["feature_coverage"]
    if selected_strategy == "team_corner_feature" and not coverage["eligible"]:
        return {
            **report,
            "status": "insufficient_feature_coverage",
            "rejection_reasons": ["insufficient_feature_coverage"],
        }
    if len(fixtures) < MIN_PROSPECTIVE_FIXTURES:
        return {**report, "status": "prospective_shadow_collecting"}

    model = None
    scoring_rows: Sequence[dict[str, Any]] = primary
    if selected_strategy == "team_corner_feature":
        private = state.get("private_model") or {}
        model = (
            _encoder_from_state(private["encoder"]),
            [float(value) for value in private["coefficients"]],
        )
        scoring_rows = _team_feature_rows(primary)
    champion = strategy_metrics(CHAMPION_STRATEGY, primary)
    challenger = strategy_metrics(selected_strategy, scoring_rows, model=model)
    # The shadow return must follow the direction the selected strategy takes,
    # so a fitted model needs its own per-row probabilities to resolve flips.
    challenger_probabilities = None
    if model is not None:
        scores = _strategy_scores(selected_strategy, scoring_rows, model=model)
        challenger_probabilities = None if scores is None else scores[0]
    if champion is None or challenger is None:
        return {
            **report,
            "status": "prospective_tested_no_safe_upgrade",
            "rejection_reasons": ["unscorable_rows"],
        }
    always_under = strategy_metrics("always_under", primary)
    gate = promotion_gate(champion, challenger, len(fixtures), selected_strategy)
    return {
        **report,
        "status": (
            "candidate_passed_human_review_required"
            if gate["passed"] else "prospective_tested_no_safe_upgrade"
        ),
        "sample_warning": (
            "below_strong_sample" if len(fixtures) < STRONG_SAMPLE_FIXTURES else None
        ),
        "champion": {"strategy": CHAMPION_STRATEGY, "metrics": champion},
        "baselines": {"always_under": always_under},
        "challenger": {"strategy": selected_strategy, "metrics": challenger},
        "delta": gate["deltas"],
        "checks": gate["checks"],
        "rejection_reasons": gate["rejection_reasons"],
        "shadow_returns": _shadow_returns(
            primary, selected_strategy, challenger_probabilities
        ),
        "champion_shadow_returns": _shadow_returns(primary, CHAMPION_STRATEGY),
    }


def collecting_report(reason: str, cutoff: datetime | None = None) -> dict[str, Any]:
    """Safe placeholder when no frozen state can exist yet."""
    return {
        "market": MARKET,
        "status": "prospective_shadow_collecting",
        "freeze_cutoff": cutoff.isoformat() if cutoff else None,
        "primary_unit": "one_row_per_unique_fixture",
        "primary_stage_rule": list(PRIMARY_STAGE_PRIORITY),
        "selected_strategy": None,
        "minimum_prospective_fixtures": MIN_PROSPECTIVE_FIXTURES,
        "strong_sample_fixtures": STRONG_SAMPLE_FIXTURES,
        "prospective_fixtures": 0,
        "prospective_rows": 0,
        "remaining_fixtures": MIN_PROSPECTIVE_FIXTURES,
        "reason": reason,
        "auto_apply": False,
        "retraining": False,
        "probability_artifact_written": False,
        "live_integration": "none",
    }


def public_selection(selection: dict[str, Any]) -> dict[str, Any]:
    """Audit-visible selection evidence; never encoder state or coefficients."""
    return {
        "method": selection["method"],
        "fold_count": selection["fold_count"],
        "minimum_walk_forward_train_fixtures": selection["minimum_walk_forward_train_fixtures"],
        "status": selection["status"],
        "selected_id": selection["selected_id"],
        "historical_fixtures_before_cutoff": selection["historical_fixtures_before_cutoff"],
        "selection_fixtures": selection["selection_fixtures"],
        "excluded_recent_holdout_fixtures": selection["excluded_recent_holdout_fixtures"],
        "team_feature_coverage": selection["team_feature_coverage"],
        "candidates": [
            {
                key: item.get(key)
                for key in ("id", "label", "role", "status", "metrics", "champion_metrics")
            }
            for item in selection["candidates"]
        ],
    }


def resolve(
    rows: Sequence[dict[str, Any]],
    state_path: Path | None,
    now: datetime | None = None,
) -> dict[str, Any]:
    """Load or freeze the state once, then report the prospective window.

    A subsequent run loads the existing state byte-for-byte and never
    retrains while the prospective window is still collecting.
    """
    if state_path is None:
        return collecting_report("persistent_state_path_required_for_chl_freeze")
    market_rows = [row for row in rows if str(row.get("market")) == MARKET]
    if state_path.exists():
        state = load_state(state_path)
    else:
        freeze_at = (now or datetime.now(timezone.utc)).astimezone(timezone.utc)
        state = build_state(market_rows, freeze_at)
        if state is None:
            # No cutoff is recorded: nothing was frozen, and embedding the
            # attempt time would make an otherwise identical report differ
            # between runs.
            return collecting_report(
                "insufficient_pre_freeze_history_for_three_walk_forward_folds"
            )
        state_path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        atomic_create_state(state_path, state)
        state = load_state(state_path)
    return evaluate_prospective(market_rows, state)
