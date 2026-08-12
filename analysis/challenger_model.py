#!/usr/bin/env python3
"""Isolated, leakage-safe market-probability challenger evaluation.

This module intentionally has no live prediction, staking, ledger, or alerting
imports.  It trains deterministic regularized logistic regressions only from
the immutable learning database, writes a candidate report, and never applies
its probabilities.  The model is evaluated separately for each
``(system, market)`` pair only after enough unique fixtures exist.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import math
import os
import tempfile
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable

from analysis.learning_store import LearningStore

MODEL_NAME = "deterministic_l2_logistic_market_challenger"
MODEL_VERSION = "challenger-logit-v1"
FEATURE_SCHEMA_VERSION = "pre_kickoff-market-v1"
HIL_MODEL_VERSION = "challenger-logit-hil-v2"
HIL_FEATURE_SCHEMA_VERSION = "pre_kickoff-hil-market-v2"
HIL_V3_MODEL_VERSION = "challenger-logit-hil-v3-frozen-prospective"
HIL_V3_STATE_SCHEMA_VERSION = 1
STAGE_RANK = {"首預": 1.0, "T-30": 2.0, "T-5": 3.0}
MARKETS = ("HDC", "HIL", "CHL")

# A model needs 70 chronological training fixtures and a 30-fixture locked
# holdout.  Count fixtures, never decisions or repeated stage rows.
MIN_FIXTURES = 100
MIN_TRAIN_FIXTURES = 70
MIN_HOLDOUT_FIXTURES = 30
MIN_BRIER_IMPROVEMENT = 0.01
MIN_LOG_LOSS_IMPROVEMENT = 0.0001
MAX_ACCURACY_DECLINE = 0.02
L2 = 3.0
LEARNING_RATE = 0.15
ITERATIONS = 800
CLIP = 1e-6
HIL_L2 = 12.0
HIL_CALIBRATION_FRACTION = 0.80
HIL_BLEND_WEIGHTS = tuple(item / 20.0 for item in range(21))
HIL_V3_MIN_PROSPECTIVE_FIXTURES = 30
HIL_V3_MIN_WALK_FORWARD_TRAIN_FIXTURES = 70
HIL_V3_WALK_FORWARD_FOLDS = 3
# This deliberately small, declared-before-selection grid is the whole v3
# search space.  It is selected from pre-freeze, pre-v2-holdout history only.
HIL_V3_SPECS = (
    {"id": "market_anchor", "l2": 12.0, "blend_weight": 0.0},
    {"id": "conservative_25", "l2": 12.0, "blend_weight": 0.25},
    {"id": "conservative_50", "l2": 20.0, "blend_weight": 0.50},
)

# This is intentionally a whitelist.  Result fields in an accidentally
# malformed payload cannot enter the model even if present in SQLite.
NUMERIC_FEATURES = (
    "base_probability",
    "market_line",
    "market_odds",
    "stage_rank",
    "prior_stage_probability",
    "stage_probability_delta",
    "stage_changed",
    "footbreak_final_lh",
    "footbreak_final_la",
    "footbreak_final_total",
    "footbreak_final_supremacy",
    "footbreak_final_mu",
    "footbreak_final_rho",
    "footbreak_now_lh",
    "footbreak_now_la",
    "footbreak_now_total",
    "footbreak_now_supremacy",
    "footbreak_movement_total",
    "footbreak_movement_supremacy",
    "footbreak_movement_corners",
    "footbreak_hk_max_move_pct",
    "footbreak_hk_n_lines_moved",
    "crown_outcome_home",
    "crown_outcome_draw",
    "crown_outcome_away",
    "neutral_venue",
)
CATEGORICAL_FEATURES = ("league", "selection_side", "stage")

# Crown HIL has a compact, market-native feature set.  The generic schema
# contains several Footbreak-only fields which are systematically missing in
# Crown snapshots; carrying their missing indicators and sparse league dummies
# into a 156-fixture HIL fit made the v1 model needlessly flexible.  v2 keeps
# only fields persisted before kickoff for the selected HIL quote, its earlier
# stage quote, and one low-dimensional WDL context value when it is available.
HIL_NUMERIC_FEATURES = (
    "base_probability",
    "market_line",
    "market_odds",
    "market_implied_probability",
    "stage_rank",
    "prior_stage_probability",
    "stage_probability_delta",
    "stage_line_delta",
    "stage_odds_delta",
    "stage_implied_probability_delta",
    "stage_changed",
    "stage_line_changed",
    "stage_price_changed",
    "crown_outcome_draw",
)
HIL_CATEGORICAL_FEATURES = ("selection_side", "stage")


def _canonical(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False)


def _sha256(value: Any) -> str:
    return hashlib.sha256(_canonical(value).encode("utf-8")).hexdigest()


def _finite(value: Any) -> float | None:
    try:
        value = float(value)
    except (TypeError, ValueError):
        return None
    return value if math.isfinite(value) else None


def _nested(value: Any, *keys: str) -> Any:
    for key in keys:
        if not isinstance(value, dict):
            return None
        value = value.get(key)
    return value


def _market_prediction(row: dict[str, Any]) -> dict[str, Any]:
    """Find the immutable pre-kickoff market record matching this grade."""
    for candidate in _nested(row, "payload", "market_predictions") or []:
        if isinstance(candidate, dict) and str(candidate.get("code")) == str(row.get("market")):
            return candidate
    return {}


def _line(value: Any) -> float | None:
    """Parse a persisted whole or split Asian line without creating new data."""
    direct = _finite(value)
    if direct is not None:
        return direct
    if not isinstance(value, str) or "/" not in value:
        return None
    parts = [_finite(part.strip()) for part in value.split("/")]
    if len(parts) != 2 or any(part is None for part in parts):
        return None
    return (float(parts[0]) + float(parts[1])) / 2


def chronological_fixture_split(rows: Iterable[dict[str, Any]]) -> tuple[set[str], set[str], dict[str, Any]]:
    """Split fixtures chronologically, preserving every stage in its fixture."""
    kickoff_by_fixture: dict[str, datetime] = {}
    for row in rows:
        fixture = str(row["match_id"])
        kickoff = row["kickoff"]
        kickoff_by_fixture[fixture] = min(kickoff_by_fixture.get(fixture, kickoff), kickoff)
    ordered = sorted(kickoff_by_fixture, key=lambda fixture: (kickoff_by_fixture[fixture], fixture))
    if len(ordered) < 2:
        return set(ordered), set(), {
            "train_period": _period(ordered, kickoff_by_fixture),
            "holdout_period": None,
            "holdout_start": None,
        }
    cut = max(1, min(len(ordered) - 1, int(len(ordered) * 0.70)))
    train, holdout = set(ordered[:cut]), set(ordered[cut:])
    return train, holdout, {
        "train_period": _period(ordered[:cut], kickoff_by_fixture),
        "holdout_period": _period(ordered[cut:], kickoff_by_fixture),
        "holdout_start": kickoff_by_fixture[ordered[cut]].isoformat(),
    }


def _period(ids: list[str], dates: dict[str, datetime]) -> dict[str, str] | None:
    if not ids:
        return None
    values = [dates[item] for item in ids]
    return {"start": min(values).isoformat(), "end": max(values).isoformat()}


def build_feature_rows(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Build strictly pre-kickoff feature dictionaries without result fields."""
    previous: dict[tuple[str, str], dict[str, float | None]] = {}
    ordered = sorted(
        rows,
        key=lambda row: (
            row["kickoff"], str(row["match_id"]), STAGE_RANK.get(str(row.get("stage")), 0.0),
            row["predicted_at"], str(row["target_key"]),
        ),
    )
    output = []
    for row in ordered:
        payload = row.get("payload") if isinstance(row.get("payload"), dict) else {}
        market = _market_prediction(row)
        _, separator, target_side = str(row.get("target_key") or "").rpartition("|")
        fixture_market = (str(row["match_id"]), str(row["market"]))
        probability = _finite(row.get("probability"))
        if probability is None:
            continue
        line = _line(market.get("line"))
        if line is None:
            line = _line(market.get("condition"))
        if line is None:
            line = _line(str(row.get("target_key") or "").partition("|")[0])
        odds = _finite(market.get("odds"))
        implied_probability = (
            1.0 / odds if odds is not None and odds > 1.0 else None
        )
        prior = previous.get(fixture_market)
        prior_probability = prior.get("probability") if prior else None
        prior_line = prior.get("line") if prior else None
        prior_odds = prior.get("odds") if prior else None
        prior_implied_probability = prior.get("implied_probability") if prior else None
        final, now = payload.get("final"), payload.get("now")
        movement, info = payload.get("movement"), payload.get("info")
        outcome = payload.get("outcome")
        values: dict[str, float | None] = {
            "base_probability": probability,
            "market_line": line,
            "market_odds": odds,
            "market_implied_probability": implied_probability,
            "stage_rank": STAGE_RANK.get(str(row.get("stage"))),
            "prior_stage_probability": prior_probability,
            "stage_probability_delta": (
                None if prior_probability is None else probability - prior_probability
            ),
            "stage_line_delta": None if line is None or prior_line is None else line - prior_line,
            "stage_odds_delta": None if odds is None or prior_odds is None else odds - prior_odds,
            "stage_implied_probability_delta": (
                None
                if implied_probability is None or prior_implied_probability is None
                else implied_probability - prior_implied_probability
            ),
            "stage_changed": (
                None
                if prior_probability is None
                else float(abs(probability - prior_probability) > 1e-12)
            ),
            "stage_line_changed": (
                None if line is None or prior_line is None else float(abs(line - prior_line) > 1e-12)
            ),
            "stage_price_changed": (
                None if odds is None or prior_odds is None else float(abs(odds - prior_odds) > 1e-12)
            ),
            "footbreak_final_lh": _finite(_nested(final, "lh")),
            "footbreak_final_la": _finite(_nested(final, "la")),
            "footbreak_final_total": _finite(_nested(final, "total")),
            "footbreak_final_supremacy": _finite(_nested(final, "supremacy")),
            "footbreak_final_mu": _finite(_nested(final, "mu")),
            "footbreak_final_rho": _finite(_nested(final, "rho")),
            "footbreak_now_lh": _finite(_nested(now, "lh")),
            "footbreak_now_la": _finite(_nested(now, "la")),
            "footbreak_now_total": _finite(_nested(now, "total")),
            "footbreak_now_supremacy": _finite(_nested(now, "supremacy")),
            "footbreak_movement_total": _finite(_nested(movement, "d_total")),
            "footbreak_movement_supremacy": _finite(_nested(movement, "d_sup")),
            "footbreak_movement_corners": _finite(_nested(movement, "d_corners")),
            "footbreak_hk_max_move_pct": _finite(_nested(info, "hk_max_move_pct")),
            "footbreak_hk_n_lines_moved": _finite(_nested(info, "hk_n_lines_moved")),
            "crown_outcome_home": _finite(_nested(outcome, "home")),
            "crown_outcome_draw": _finite(_nested(outcome, "draw")),
            "crown_outcome_away": _finite(_nested(outcome, "away")),
            "neutral_venue": _finite(payload.get("neutral")),
        }
        categories = {
            "league": str(payload.get("league") or "__MISSING__"),
            "selection_side": str(market.get("side") or (target_side if separator else "__MISSING__")),
            "stage": str(row.get("stage") or "__MISSING__"),
        }
        output.append({
            **row,
            "numeric": values,
            "categorical": categories,
        })
        previous[fixture_market] = {
            "probability": probability,
            "line": line,
            "odds": odds,
            "implied_probability": implied_probability,
        }
    return output


class TrainOnlyEncoder:
    """Median/scaling/categorical encoder whose fit sees only train rows."""

    def __init__(
        self,
        numeric_features: tuple[str, ...] = NUMERIC_FEATURES,
        categorical_features: tuple[str, ...] = CATEGORICAL_FEATURES,
    ) -> None:
        self.numeric_features = numeric_features
        self.categorical_features = categorical_features
        self.medians: dict[str, float] = {}
        self.scales: dict[str, float] = {}
        self.categories: dict[str, tuple[str, ...]] = {}
        self.feature_names: list[str] = []

    def fit(self, rows: list[dict[str, Any]]) -> "TrainOnlyEncoder":
        names = ["intercept"]
        for feature in self.numeric_features:
            observed = sorted(
                float(row["numeric"][feature])
                for row in rows
                if row["numeric"].get(feature) is not None
            )
            if observed:
                middle = len(observed) // 2
                median = observed[middle] if len(observed) % 2 else (observed[middle - 1] + observed[middle]) / 2
                spread = math.sqrt(sum((item - median) ** 2 for item in observed) / len(observed))
                self.medians[feature] = median
                self.scales[feature] = spread if spread > 1e-9 else 1.0
            else:
                self.medians[feature], self.scales[feature] = 0.0, 1.0
            names.extend((feature, f"{feature}__missing"))
        for feature in self.categorical_features:
            counts = Counter(str(row["categorical"].get(feature) or "__MISSING__") for row in rows)
            # Categories that occur once are represented by the deterministic
            # unknown baseline, avoiding a high-cardinality fixture proxy.
            kept = tuple(sorted(value for value, count in counts.items() if count >= 2))
            self.categories[feature] = kept
            names.extend(f"{feature}={value}" for value in kept)
        self.feature_names = names
        return self

    def transform_one(self, row: dict[str, Any]) -> list[float]:
        values = [1.0]
        for feature in self.numeric_features:
            raw = row["numeric"].get(feature)
            missing = raw is None
            value = self.medians[feature] if missing else float(raw)
            values.extend(((value - self.medians[feature]) / self.scales[feature], float(missing)))
        for feature in self.categorical_features:
            observed = str(row["categorical"].get(feature) or "__MISSING__")
            values.extend(float(observed == category) for category in self.categories[feature])
        return values

    def coverage(self, rows: list[dict[str, Any]]) -> dict[str, dict[str, int | float]]:
        return {
            feature: {
                "present": sum(row["numeric"].get(feature) is not None for row in rows),
                "total": len(rows),
                "coverage": round(
                    sum(row["numeric"].get(feature) is not None for row in rows) / len(rows), 6
                ) if rows else 0.0,
            }
            for feature in self.numeric_features
        }


def _sigmoid(value: float) -> float:
    value = max(-35.0, min(35.0, value))
    return 1.0 / (1.0 + math.exp(-value))


def fit_logistic(
    rows: list[dict[str, Any]],
    *,
    numeric_features: tuple[str, ...] = NUMERIC_FEATURES,
    categorical_features: tuple[str, ...] = CATEGORICAL_FEATURES,
    l2: float = L2,
) -> tuple[TrainOnlyEncoder, list[float]]:
    """Fit deterministic full-batch L2 logistic regression with no dependencies."""
    encoder = TrainOnlyEncoder(numeric_features, categorical_features).fit(rows)
    matrix = [encoder.transform_one(row) for row in rows]
    target = [float(row["target"]) for row in rows]
    coefficients = [0.0] * len(encoder.feature_names)
    if not matrix:
        return encoder, coefficients
    for _ in range(ITERATIONS):
        gradient = [0.0] * len(coefficients)
        for values, actual in zip(matrix, target):
            predicted = _sigmoid(sum(weight * value for weight, value in zip(coefficients, values)))
            error = predicted - actual
            for index, value in enumerate(values):
                gradient[index] += error * value
        n_rows = len(matrix)
        for index in range(len(coefficients)):
            penalty = 0.0 if index == 0 else l2 * coefficients[index]
            coefficients[index] -= LEARNING_RATE * ((gradient[index] / n_rows) + penalty / n_rows)
    return encoder, coefficients


def predict(encoder: TrainOnlyEncoder, coefficients: list[float], rows: list[dict[str, Any]]) -> list[float]:
    return [
        min(1.0 - CLIP, max(CLIP, _sigmoid(sum(
            weight * value for weight, value in zip(coefficients, encoder.transform_one(row))
        ))))
        for row in rows
    ]


def probability_metrics(probabilities: list[float], rows: list[dict[str, Any]]) -> dict[str, int | float | None]:
    if not rows:
        return {"n": 0, "accuracy": None, "brier": None, "log_loss": None}
    briers, losses, hits = [], [], []
    for probability, row in zip(probabilities, rows):
        actual = float(row["target"])
        p = min(1.0 - CLIP, max(CLIP, probability))
        briers.append((p - actual) ** 2)
        losses.append(-(actual * math.log(p) + (1.0 - actual) * math.log(1.0 - p)))
        if actual != 0.5:
            hits.append(float((p >= 0.5) == (actual > 0.5)))
    return {
        "n": len(rows),
        "accuracy": round(sum(hits) / len(hits), 6) if hits else None,
        "brier": round(sum(briers) / len(briers), 6),
        "log_loss": round(sum(losses) / len(losses), 6),
    }


def _model_spec(system: str, market: str) -> dict[str, Any]:
    """Return the immutable feature/training policy for one system/market."""
    if system == "crown" and market == "HIL":
        return {
            "model_version": HIL_MODEL_VERSION,
            "feature_schema_version": HIL_FEATURE_SCHEMA_VERSION,
            "numeric_features": HIL_NUMERIC_FEATURES,
            "categorical_features": HIL_CATEGORICAL_FEATURES,
            "l2": HIL_L2,
            "calibration": "chronological_train_only_market_anchor_blend",
        }
    return {
        "model_version": MODEL_VERSION,
        "feature_schema_version": FEATURE_SCHEMA_VERSION,
        "numeric_features": NUMERIC_FEATURES,
        "categorical_features": CATEGORICAL_FEATURES,
        "l2": L2,
        "calibration": "none",
    }


def _chronological_prefix_split(
    rows: list[dict[str, Any]], fraction: float
) -> tuple[set[str], set[str]]:
    """Split only an already-training set into earlier fit and later calibration fixtures."""
    kickoff_by_fixture: dict[str, datetime] = {}
    for row in rows:
        fixture = str(row["match_id"])
        kickoff = row["kickoff"]
        kickoff_by_fixture[fixture] = min(kickoff_by_fixture.get(fixture, kickoff), kickoff)
    ordered = sorted(kickoff_by_fixture, key=lambda fixture: (kickoff_by_fixture[fixture], fixture))
    if len(ordered) < 2:
        return set(ordered), set()
    cut = max(1, min(len(ordered) - 1, int(len(ordered) * fraction)))
    return set(ordered[:cut]), set(ordered[cut:])


def _blend_probabilities(
    candidate: list[float], champion: list[float], weight: float
) -> list[float]:
    return [
        min(1.0 - CLIP, max(CLIP, weight * item + (1.0 - weight) * baseline))
        for item, baseline in zip(candidate, champion)
    ]


def calibrate_hil_market_anchor(
    train: list[dict[str, Any]], spec: dict[str, Any]
) -> dict[str, Any]:
    """Choose HIL shrinkage using only a chronological suffix of train fixtures.

    The selected probability is a bounded blend of an HIL-only model and the
    persisted Crown no-vig probability.  A non-zero model weight is allowed
    only when the later training suffix improves *both* proper scores.  This
    makes an unproven adjustment resolve to the unchanged market probability,
    rather than a more confident but less calibrated candidate.
    """
    fit_ids, calibration_ids = _chronological_prefix_split(train, HIL_CALIBRATION_FRACTION)
    fit_rows = [row for row in train if str(row["match_id"]) in fit_ids]
    calibration_rows = [row for row in train if str(row["match_id"]) in calibration_ids]
    common = {
        "method": "chronological_train_only_market_anchor_blend",
        "fit_fixtures": len(fit_ids),
        "calibration_fixtures": len(calibration_ids),
        "calibration_rows": len(calibration_rows),
        "weight_grid": list(HIL_BLEND_WEIGHTS),
    }
    if not fit_rows or not calibration_rows:
        return {
            **common,
            "selected_weight": 0.0,
            "status": "insufficient_inner_chronological_partition_market_anchor_retained",
        }
    encoder, coefficients = fit_logistic(
        fit_rows,
        numeric_features=spec["numeric_features"],
        categorical_features=spec["categorical_features"],
        l2=float(spec["l2"]),
    )
    raw = predict(encoder, coefficients, calibration_rows)
    champion = [float(row["probability"]) for row in calibration_rows]
    baseline = probability_metrics(champion, calibration_rows)
    eligible: list[tuple[float, dict[str, int | float | None]]] = []
    for weight in HIL_BLEND_WEIGHTS:
        metrics = probability_metrics(
            _blend_probabilities(raw, champion, weight), calibration_rows
        )
        if (
            metrics["brier"] is not None
            and metrics["log_loss"] is not None
            and baseline["brier"] is not None
            and baseline["log_loss"] is not None
            and float(metrics["brier"]) < float(baseline["brier"])
            and float(metrics["log_loss"]) < float(baseline["log_loss"])
        ):
            eligible.append((weight, metrics))
    if not eligible:
        return {
            **common,
            "selected_weight": 0.0,
            "status": "market_anchor_retained_no_inner_proper_score_improvement",
            "champion_metrics": baseline,
        }
    weight, metrics = min(
        eligible,
        key=lambda item: (
            float(item[1]["brier"]),
            float(item[1]["log_loss"]),
            item[0],
        ),
    )
    return {
        **common,
        "selected_weight": weight,
        "status": "nonzero_weight_selected_on_train_only_calibration",
        "champion_metrics": baseline,
        "selected_metrics": metrics,
    }


def _fixture_order(rows: list[dict[str, Any]]) -> tuple[list[str], dict[str, datetime]]:
    """Return unique fixtures chronologically, keeping stages attached later."""
    kickoff_by_fixture: dict[str, datetime] = {}
    for row in rows:
        fixture = str(row["match_id"])
        kickoff = row["kickoff"]
        kickoff_by_fixture[fixture] = min(kickoff_by_fixture.get(fixture, kickoff), kickoff)
    ordered = sorted(kickoff_by_fixture, key=lambda fixture: (kickoff_by_fixture[fixture], fixture))
    return ordered, kickoff_by_fixture


def _walk_forward_folds(rows: list[dict[str, Any]]) -> list[tuple[set[str], set[str]]]:
    """Produce deterministic expanding-window fixture folds, never row folds."""
    ordered, _ = _fixture_order(rows)
    remaining = len(ordered) - HIL_V3_MIN_WALK_FORWARD_TRAIN_FIXTURES
    if remaining < HIL_V3_WALK_FORWARD_FOLDS:
        return []
    base, extra = divmod(remaining, HIL_V3_WALK_FORWARD_FOLDS)
    start = HIL_V3_MIN_WALK_FORWARD_TRAIN_FIXTURES
    folds: list[tuple[set[str], set[str]]] = []
    for index in range(HIL_V3_WALK_FORWARD_FOLDS):
        size = base + (1 if index < extra else 0)
        validation = set(ordered[start:start + size])
        if not validation:
            return []
        folds.append((set(ordered[:start]), validation))
        start += size
    return folds


def _v3_specification(spec: dict[str, Any]) -> dict[str, Any]:
    """Join a declared v3 blend option to its compact immutable schema."""
    return {
        "id": str(spec["id"]),
        "l2": float(spec["l2"]),
        "blend_weight": float(spec["blend_weight"]),
        "numeric_features": HIL_NUMERIC_FEATURES,
        "categorical_features": HIL_CATEGORICAL_FEATURES,
    }


def _score_v3_spec(
    rows: list[dict[str, Any]], spec: dict[str, Any], folds: list[tuple[set[str], set[str]]]
) -> dict[str, Any]:
    """Walk-forward score one predeclared option without seeing later fixtures."""
    candidate_probability: list[float] = []
    champion_probability: list[float] = []
    scored_rows: list[dict[str, Any]] = []
    fold_counts: list[dict[str, int]] = []
    for train_ids, validation_ids in folds:
        train = [row for row in rows if str(row["match_id"]) in train_ids]
        validation = [row for row in rows if str(row["match_id"]) in validation_ids]
        if not train or not validation:
            continue
        encoder, coefficients = fit_logistic(
            train,
            numeric_features=tuple(spec["numeric_features"]),
            categorical_features=tuple(spec["categorical_features"]),
            l2=float(spec["l2"]),
        )
        raw = predict(encoder, coefficients, validation)
        champion = [float(row["probability"]) for row in validation]
        candidate_probability.extend(_blend_probabilities(raw, champion, float(spec["blend_weight"])))
        champion_probability.extend(champion)
        scored_rows.extend(validation)
        fold_counts.append({
            "train_fixtures": len(train_ids),
            "validation_fixtures": len(validation_ids),
            "validation_rows": len(validation),
        })
    return {
        "id": spec["id"],
        "l2": spec["l2"],
        "blend_weight": spec["blend_weight"],
        "folds": fold_counts,
        "metrics": probability_metrics(candidate_probability, scored_rows),
        "champion_metrics": probability_metrics(champion_probability, scored_rows),
    }


def _select_v3_spec(rows: list[dict[str, Any]]) -> dict[str, Any] | None:
    """Select from the fixed v3 grid using only expanding historical folds."""
    folds = _walk_forward_folds(rows)
    if len(folds) != HIL_V3_WALK_FORWARD_FOLDS:
        return None
    candidates = [
        _score_v3_spec(rows, _v3_specification(item), folds)
        for item in HIL_V3_SPECS
    ]
    baseline = next(item for item in candidates if item["id"] == "market_anchor")
    eligible = [
        item for item in candidates
        if item["metrics"]["brier"] is not None
        and item["metrics"]["log_loss"] is not None
        and baseline["metrics"]["brier"] is not None
        and baseline["metrics"]["log_loss"] is not None
        and float(item["metrics"]["brier"]) < float(baseline["metrics"]["brier"])
        and float(item["metrics"]["log_loss"]) < float(baseline["metrics"]["log_loss"])
    ]
    selected = min(
        eligible or [baseline],
        key=lambda item: (
            float(item["metrics"]["brier"]),
            float(item["metrics"]["log_loss"]),
            float(item["blend_weight"]),
            str(item["id"]),
        ),
    )
    return {
        "method": "predeclared_three_option_expanding_window_walk_forward",
        "fold_count": len(folds),
        "status": (
            "non_anchor_selected_with_both_walk_forward_proper_scores_improved"
            if selected["id"] != "market_anchor"
            else "market_anchor_retained_no_walk_forward_proper_score_improvement"
        ),
        "selected_id": selected["id"],
        "candidates": candidates,
    }


def _encoder_state(encoder: TrainOnlyEncoder) -> dict[str, Any]:
    return {
        "numeric_features": list(encoder.numeric_features),
        "categorical_features": list(encoder.categorical_features),
        "medians": encoder.medians,
        "scales": encoder.scales,
        "categories": {key: list(value) for key, value in encoder.categories.items()},
        "feature_names": encoder.feature_names,
    }


def _encoder_from_state(payload: dict[str, Any]) -> TrainOnlyEncoder:
    encoder = TrainOnlyEncoder(
        tuple(str(item) for item in payload["numeric_features"]),
        tuple(str(item) for item in payload["categorical_features"]),
    )
    encoder.medians = {str(key): float(value) for key, value in payload["medians"].items()}
    encoder.scales = {str(key): float(value) for key, value in payload["scales"].items()}
    encoder.categories = {
        str(key): tuple(str(item) for item in value)
        for key, value in payload["categories"].items()
    }
    encoder.feature_names = [str(item) for item in payload["feature_names"]]
    return encoder


def _state_version_hash(state: dict[str, Any]) -> str:
    """Hash the frozen private model state without recursively hashing itself."""
    copy = json.loads(_canonical(state))
    copy.get("frozen", {}).pop("version_hash", None)
    return _sha256(copy)


def build_hil_v3_state(rows: list[dict[str, Any]], cutoff: datetime) -> dict[str, Any] | None:
    """Freeze a Crown-HIL v3 model using only history strictly before cutoff.

    The final chronological 30% of the pre-cutoff history is deliberately
    excluded from selection.  That keeps the already-inspected v2 outer
    holdout out of every v3 hyperparameter/blend decision.
    """
    cutoff = cutoff.astimezone(timezone.utc)
    historical = [row for row in rows if row["kickoff"] < cutoff]
    featured = build_feature_rows(historical)
    selection_ids, excluded_ids, _ = chronological_fixture_split(featured)
    selection = [row for row in featured if str(row["match_id"]) in selection_ids]
    selected = _select_v3_spec(selection)
    if selected is None:
        return None
    selected_declared = next(
        item for item in HIL_V3_SPECS if item["id"] == selected["selected_id"]
    )
    spec = _v3_specification(selected_declared)
    encoder, coefficients = fit_logistic(
        selection,
        numeric_features=HIL_NUMERIC_FEATURES,
        categorical_features=HIL_CATEGORICAL_FEATURES,
        l2=float(spec["l2"]),
    )
    ordered_selection, selection_kickoffs = _fixture_order(selection)
    state: dict[str, Any] = {
        "schema_version": HIL_V3_STATE_SCHEMA_VERSION,
        "kind": "crown_hil_v3_frozen_prospective_shadow",
        "created_at": cutoff.isoformat(),
        "freeze_cutoff": cutoff.isoformat(),
        "frozen": {
            "model_version": HIL_V3_MODEL_VERSION,
            "feature_schema_version": HIL_FEATURE_SCHEMA_VERSION,
            "selected_spec": {
                "id": spec["id"],
                "l2": spec["l2"],
                "blend_weight": spec["blend_weight"],
            },
        },
        "selection": {
            **selected,
            "historical_fixtures_before_cutoff": len({str(row["match_id"]) for row in featured}),
            "selection_fixtures": len(selection_ids),
            "excluded_recent_holdout_fixtures": len(excluded_ids),
            "selection_period": _period(ordered_selection, selection_kickoffs),
        },
        # This executable model stays exclusively in the 0600 state file and
        # is never copied to a public challenger-status report.
        "private_model": {
            "encoder": _encoder_state(encoder),
            "coefficients": [round(value, 12) for value in coefficients],
        },
    }
    state["frozen"]["version_hash"] = _state_version_hash(state)
    return state


def _load_v3_state(path: Path) -> dict[str, Any]:
    payload = json.loads(path.read_text(encoding="utf-8"))
    if (
        not isinstance(payload, dict)
        or payload.get("schema_version") != HIL_V3_STATE_SCHEMA_VERSION
        or payload.get("kind") != "crown_hil_v3_frozen_prospective_shadow"
        or not isinstance(payload.get("private_model"), dict)
        or _state_version_hash(payload) != payload.get("frozen", {}).get("version_hash")
    ):
        raise ValueError(f"invalid or altered frozen HIL v3 state: {path}")
    return payload


def atomic_create_state(path: Path, payload: dict[str, Any], mode: int = 0o600) -> bool:
    """Atomically create a freeze state once; never replace a concurrent one."""
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            json.dump(payload, handle, ensure_ascii=False, indent=2)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.chmod(temporary, mode)
        try:
            os.link(temporary, path)
        except FileExistsError:
            return False
        return True
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def _v3_public_selection(selection: dict[str, Any]) -> dict[str, Any]:
    """Expose audit status, never private coefficients or encoder contents."""
    return {
        "method": selection["method"],
        "fold_count": selection["fold_count"],
        "status": selection["status"],
        "selected_id": selection["selected_id"],
        "historical_fixtures_before_cutoff": selection["historical_fixtures_before_cutoff"],
        "selection_fixtures": selection["selection_fixtures"],
        "excluded_recent_holdout_fixtures": selection["excluded_recent_holdout_fixtures"],
        "candidates": [
            {
                "id": item["id"],
                "l2": item["l2"],
                "blend_weight": item["blend_weight"],
                "metrics": item["metrics"],
                "champion_metrics": item["champion_metrics"],
            }
            for item in selection["candidates"]
        ],
    }


def evaluate_hil_v3_prospective(rows: list[dict[str, Any]], state: dict[str, Any]) -> dict[str, Any]:
    """Score only fixture stages strictly after a state’s immutable cutoff."""
    cutoff = datetime.fromisoformat(str(state["freeze_cutoff"])).astimezone(timezone.utc)
    future = build_feature_rows([row for row in rows if row["kickoff"] > cutoff])
    fixture_ids = {str(row["match_id"]) for row in future}
    frozen = state["frozen"]
    report: dict[str, Any] = {
        "model_version": frozen["model_version"],
        "state_version_hash": frozen["version_hash"],
        "freeze_cutoff": cutoff.isoformat(),
        "selected_spec": {
            "id": frozen["selected_spec"]["id"],
            "blend_weight": frozen["selected_spec"]["blend_weight"],
        },
        "selection": _v3_public_selection(state["selection"]),
        "minimum_prospective_fixtures": HIL_V3_MIN_PROSPECTIVE_FIXTURES,
        "prospective_fixtures": len(fixture_ids),
        "prospective_rows": len(future),
        "remaining_fixtures": max(0, HIL_V3_MIN_PROSPECTIVE_FIXTURES - len(fixture_ids)),
        "grouping": "unique_fixture_with_all_pre_kickoff_stages_preserved",
        "auto_apply": False,
        "probability_artifact_written": False,
    }
    if len(fixture_ids) < HIL_V3_MIN_PROSPECTIVE_FIXTURES:
        return {**report, "status": "prospective_shadow_collecting"}
    encoder = _encoder_from_state(state["private_model"]["encoder"])
    coefficients = [float(value) for value in state["private_model"]["coefficients"]]
    raw = predict(encoder, coefficients, future)
    champion_probability = [float(row["probability"]) for row in future]
    challenger_probability = _blend_probabilities(
        raw, champion_probability, float(frozen["selected_spec"]["blend_weight"])
    )
    champion = probability_metrics(champion_probability, future)
    challenger = probability_metrics(challenger_probability, future)
    gate = promotion_gate(champion, challenger, len(fixture_ids))
    return {
        **report,
        "status": (
            "candidate_passed_human_review_required"
            if gate["passed"] else "prospective_tested_no_safe_upgrade"
        ),
        "champion": {"metrics": champion},
        "challenger": {"metrics": challenger, "raw_model_metrics": probability_metrics(raw, future)},
        "delta": gate["deltas"],
        "checks": gate["checks"],
        "rejection_reasons": gate["rejection_reasons"],
    }


def promotion_gate(champion: dict[str, Any], challenger: dict[str, Any], holdout_fixtures: int) -> dict[str, Any]:
    def delta(key: str) -> float | None:
        if champion.get(key) is None or challenger.get(key) is None:
            return None
        return round(float(challenger[key]) - float(champion[key]), 6)
    deltas = {"brier": delta("brier"), "log_loss": delta("log_loss"), "accuracy": delta("accuracy")}
    checks = {
        "minimum_holdout_fixtures": holdout_fixtures >= MIN_HOLDOUT_FIXTURES,
        "identical_holdout_rows": champion.get("n") == challenger.get("n") and int(champion.get("n") or 0) > 0,
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


def evaluate_market(rows: list[dict[str, Any]], system: str, market: str, diagnostics: dict[str, int]) -> dict[str, Any]:
    scoped = [row for row in rows if str(row.get("market")) == market]
    fixtures = {str(row["match_id"]) for row in scoped}
    spec = _model_spec(system, market)
    common = {
        "system": system,
        "market": market,
        "model_name": MODEL_NAME,
        "model_version": spec["model_version"],
        "feature_schema_version": spec["feature_schema_version"],
        "numeric_features": list(spec["numeric_features"]),
        "categorical_features": list(spec["categorical_features"]),
        "l2": spec["l2"],
        "model_scope": "system_market",
        "eligible_fixtures": len(fixtures),
        "eligible_rows": len(scoped),
        "source_diagnostics": diagnostics,
        "fallback": "champion_unchanged_no_pooled_or_cross_system_fallback",
        "auto_apply": False,
    }
    if len(fixtures) < MIN_FIXTURES:
        return {
            **common,
            "status": "insufficient_data",
            "required_fixtures": MIN_FIXTURES,
            "remaining_fixtures": MIN_FIXTURES - len(fixtures),
            "rejection_reasons": ["minimum_eligible_fixtures"],
            "separate_model_trained": False,
        }
    featured = build_feature_rows(scoped)
    train_ids, holdout_ids, periods = chronological_fixture_split(featured)
    train = [row for row in featured if str(row["match_id"]) in train_ids]
    holdout = [row for row in featured if str(row["match_id"]) in holdout_ids]
    if len(train_ids) < MIN_TRAIN_FIXTURES or len(holdout_ids) < MIN_HOLDOUT_FIXTURES:
        return {
            **common, **periods,
            "status": "insufficient_chronological_partition",
            "train_fixtures": len(train_ids),
            "holdout_fixtures": len(holdout_ids),
            "rejection_reasons": ["minimum_train_or_holdout_fixtures"],
            "separate_model_trained": False,
        }
    encoder, coefficients = fit_logistic(
        train,
        numeric_features=spec["numeric_features"],
        categorical_features=spec["categorical_features"],
        l2=float(spec["l2"]),
    )
    champion_probability = [float(row["probability"]) for row in holdout]
    raw_challenger_probability = predict(encoder, coefficients, holdout)
    calibration: dict[str, Any] | None = None
    if spec["calibration"] != "none":
        calibration = calibrate_hil_market_anchor(train, spec)
        challenger_probability = _blend_probabilities(
            raw_challenger_probability,
            champion_probability,
            float(calibration["selected_weight"]),
        )
    else:
        challenger_probability = raw_challenger_probability
    champion = probability_metrics(champion_probability, holdout)
    challenger = probability_metrics(challenger_probability, holdout)
    raw_challenger = probability_metrics(raw_challenger_probability, holdout)
    gate = promotion_gate(champion, challenger, len(holdout_ids))
    coefficient_rows = [
        {"feature": name, "coefficient": round(weight, 8), "absolute_importance": round(abs(weight), 8)}
        for name, weight in zip(encoder.feature_names, coefficients)
    ]
    coefficient_rows.sort(key=lambda row: (-float(row["absolute_importance"]), str(row["feature"])))
    artifact = {
        "model_name": MODEL_NAME,
        "model_version": spec["model_version"],
        "feature_schema_version": spec["feature_schema_version"],
        "system": system,
        "market": market,
        "numeric_features": list(spec["numeric_features"]),
        "categorical_features": list(spec["categorical_features"]),
        "l2": spec["l2"],
        "calibration": calibration,
        "train_fixtures": sorted(train_ids),
        "feature_names": encoder.feature_names,
        "medians": encoder.medians,
        "scales": encoder.scales,
        "categories": encoder.categories,
        "coefficients": [round(value, 12) for value in coefficients],
        "training_source": [
            (str(row["match_id"]), row["stage"], row["predicted_at"].isoformat(), row["target_key"])
            for row in train
        ],
    }
    version_hash = _sha256(artifact)
    status = "candidate_passed_human_review_required" if gate["passed"] else "tested_no_safe_upgrade"
    return {
        **common, **periods,
        "status": status,
        "separate_model_trained": True,
        "train_fixtures": len(train_ids),
        "holdout_fixtures": len(holdout_ids),
        "train_rows": len(train),
        "holdout_rows": len(holdout),
        "train_feature_coverage": encoder.coverage(train),
        "holdout_feature_coverage": encoder.coverage(holdout),
        "champion": {"metrics": champion},
        "challenger": {
            "metrics": challenger,
            "raw_model_metrics": raw_challenger,
            "probability_artifact_written": False,
        },
        "delta": gate["deltas"],
        "checks": gate["checks"],
        "rejection_reasons": gate["rejection_reasons"],
        "coefficient_importance": coefficient_rows,
        "calibration": calibration,
        "model_version_hash": version_hash,
    }


def evaluate_all(
    store: LearningStore,
    *,
    hil_v3_state_path: Path | None = None,
    chl_state_path: Path | None = None,
    now: datetime | None = None,
) -> dict[str, Any]:
    # Imported here rather than at module scope: the Crown CHL prospective
    # module depends on this module's fitting helpers, and it must stay an
    # optional, isolated add-on to the existing evaluation.
    from analysis import crown_chl_prospective

    systems: dict[str, Any] = {}
    crown_hil_v3: dict[str, Any] | None = None
    crown_chl: dict[str, Any] | None = None
    for system in ("footbreak", "crown"):
        rows, diagnostics = store.challenger_rows(system)
        tests = {market: evaluate_market(rows, system, market, diagnostics) for market in MARKETS}
        if system == "crown":
            state_path = hil_v3_state_path
            if state_path is None:
                # Some legacy callers embed the ordinary v1/v2 report inside a
                # separate rolling-backtest document.  They intentionally do
                # not own a persistent challenger-state directory, so v3 must
                # not silently freeze/retrain there.
                state = None
                crown_hil_v3 = {
                    "status": "prospective_shadow_collecting",
                    "freeze_cutoff": None,
                    "minimum_prospective_fixtures": HIL_V3_MIN_PROSPECTIVE_FIXTURES,
                    "prospective_fixtures": 0,
                    "prospective_rows": 0,
                    "remaining_fixtures": HIL_V3_MIN_PROSPECTIVE_FIXTURES,
                    "reason": "persistent_state_path_required_for_v3_freeze",
                    "auto_apply": False,
                    "probability_artifact_written": False,
                }
            elif state_path.exists():
                state = _load_v3_state(state_path)
            else:
                freeze_at = (now or datetime.now(timezone.utc)).astimezone(timezone.utc)
                state = build_hil_v3_state(
                    [row for row in rows if str(row.get("market")) == "HIL"], freeze_at
                )
                if state is not None:
                    atomic_create_state(state_path, state)
                    state = _load_v3_state(state_path)
            if state_path is not None and state is None:
                crown_hil_v3 = {
                    "status": "prospective_shadow_collecting",
                    "freeze_cutoff": None,
                    "minimum_prospective_fixtures": HIL_V3_MIN_PROSPECTIVE_FIXTURES,
                    "prospective_fixtures": 0,
                    "prospective_rows": 0,
                    "remaining_fixtures": HIL_V3_MIN_PROSPECTIVE_FIXTURES,
                    "reason": "insufficient_pre_freeze_history_for_three_walk_forward_folds",
                    "auto_apply": False,
                    "probability_artifact_written": False,
                }
            elif state is not None:
                crown_hil_v3 = evaluate_hil_v3_prospective(
                    [row for row in rows if str(row.get("market")) == "HIL"], state
                )
            tests["HIL"]["prospective_v3"] = crown_hil_v3
            # Crown-only CHL frozen prospective shadow.  It is isolated from
            # HIL v3, from the data-health report, and from every live path.
            try:
                crown_chl = crown_chl_prospective.resolve(
                    [row for row in rows if str(row.get("market")) == "CHL"],
                    chl_state_path,
                    now=now,
                )
            except (OSError, ValueError) as exc:
                crown_chl = crown_chl_prospective.collecting_report(
                    f"state_unavailable:{type(exc).__name__}"
                )
            tests["CHL"]["prospective_chl"] = crown_chl
        systems[system] = {
            "tests": tests,
            "review_required": any(
                item["status"] == "candidate_passed_human_review_required"
                for item in tests.values()
            ) or (crown_hil_v3 or {}).get("status") == "candidate_passed_human_review_required"
            or (crown_chl or {}).get("status") == "candidate_passed_human_review_required",
        }
    return {
        "schema_version": 1,
        "generated_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "policy": {
            "mode": "daily_train_evaluate_candidate_report_only",
            "runtime_prediction_integration": "not_enabled",
            "reason": (
                "Historical payload coverage differs by system and the challenger is "
                "not allowed to alter champion probabilities, picks, ledgers, staking, or alerts."
            ),
            "separation": "one deterministic model per system and market; no pooled fallback",
            "split": "chronological 70/30 by fixture; all stages of a fixture stay together",
            "fit_scope": "encoders, medians, scales, and categories fit on train fixtures only",
            "HIL_v2": (
                "Crown HIL uses a compact line/price/stage-movement schema and "
                "a chronological train-only market-anchor calibration; a nonzero "
                "blend weight requires both inner Brier and log-loss improvement."
            ),
            "HIL_v3": (
                "Crown HIL v3 is a frozen prospective shadow only: one small "
                "predeclared blend grid is selected by expanding walk-forward "
                "folds before a private immutable cutoff, then only strictly "
                "later fixture stages count toward a 30-unique-fixture review."
            ),
            "CHL_prospective": (
                "Crown CHL is a frozen prospective shadow only: the primary "
                "unit is one deterministic row per unique fixture using the "
                "predeclared T-5 > T-30 > 首預 stage rule, strategies are "
                "selected by expanding walk-forward folds strictly before an "
                "immutable cutoff, and stage metrics are correlated secondary "
                "diagnostics only."
            ),
            "post_kickoff": "quarantined snapshots excluded",
            "result_fields": "not in feature whitelist",
            "auto_apply": False,
            "notification_policy": "human_review_only_after_all_promotion_gates_pass",
        },
        "systems": systems,
        "review_required": any(system["review_required"] for system in systems.values()),
    }


def atomic_write(path: Path, payload: dict[str, Any], mode: int) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            json.dump(payload, handle, ensure_ascii=False, indent=2)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.chmod(temporary, mode)
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


# Keys that must never reach a world-readable artifact.  The private report
# under /var/lib/footbreak keeps them for the operator audit.
PRIVATE_TEST_KEYS = ("coefficient_importance", "private_model", "training_source")


def public_report(report: dict[str, Any]) -> dict[str, Any]:
    """Strip encoder state, coefficients, and raw rows from the public copy."""
    payload = json.loads(json.dumps(report, ensure_ascii=False))
    for system in (payload.get("systems") or {}).values():
        for test in ((system or {}).get("tests") or {}).values():
            if not isinstance(test, dict):
                continue
            for key in PRIVATE_TEST_KEYS:
                test.pop(key, None)
            for nested_key in ("prospective_v3", "prospective_chl"):
                nested = test.get(nested_key)
                if isinstance(nested, dict):
                    for key in PRIVATE_TEST_KEYS:
                        nested.pop(key, None)
    return payload


def run(
    learning_db: Path,
    output_path: Path,
    public_paths: list[Path] | None = None,
    *,
    hil_v3_state_path: Path | None = None,
    chl_state_path: Path | None = None,
    now: datetime | None = None,
) -> dict[str, Any]:
    if not learning_db.is_file():
        raise FileNotFoundError(f"immutable learning database does not exist: {learning_db}")
    state_path = hil_v3_state_path or output_path.parent / "crown_hil_v3_state.json"
    corner_state_path = chl_state_path or output_path.parent / "crown_chl_state.json"
    with LearningStore(learning_db) as store:
        report = evaluate_all(
            store,
            hil_v3_state_path=state_path,
            chl_state_path=corner_state_path,
            now=now,
        )
    atomic_write(output_path, report, 0o600)
    public = public_report(report)
    for path in public_paths or []:
        atomic_write(path, public, 0o644)
    return report


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--learning-db", type=Path, required=True)
    parser.add_argument("--out", type=Path, default=Path("/var/lib/footbreak/challenger/latest.json"))
    parser.add_argument("--public", type=Path, action="append", default=[])
    parser.add_argument(
        "--hil-v3-state", type=Path,
        default=Path("/var/lib/footbreak/challenger/crown_hil_v3_state.json"),
    )
    parser.add_argument(
        "--chl-state", type=Path,
        default=Path("/var/lib/footbreak/challenger/crown_chl_state.json"),
    )
    args = parser.parse_args()
    report = run(
        args.learning_db,
        args.out,
        args.public,
        hil_v3_state_path=args.hil_v3_state,
        chl_state_path=args.chl_state,
    )
    print(json.dumps({
        "generated_at": report["generated_at"],
        "review_required": report["review_required"],
        "mode": report["policy"]["mode"],
    }, ensure_ascii=False))


if __name__ == "__main__":
    main()
