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


def evaluate_all(store: LearningStore) -> dict[str, Any]:
    systems: dict[str, Any] = {}
    for system in ("footbreak", "crown"):
        rows, diagnostics = store.challenger_rows(system)
        tests = {market: evaluate_market(rows, system, market, diagnostics) for market in MARKETS}
        systems[system] = {
            "tests": tests,
            "review_required": any(
                item["status"] == "candidate_passed_human_review_required"
                for item in tests.values()
            ),
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


def run(learning_db: Path, output_path: Path, public_paths: list[Path] | None = None) -> dict[str, Any]:
    if not learning_db.is_file():
        raise FileNotFoundError(f"immutable learning database does not exist: {learning_db}")
    with LearningStore(learning_db) as store:
        report = evaluate_all(store)
    atomic_write(output_path, report, 0o600)
    for path in public_paths or []:
        atomic_write(path, report, 0o644)
    return report


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--learning-db", type=Path, required=True)
    parser.add_argument("--out", type=Path, default=Path("/var/lib/footbreak/challenger/latest.json"))
    parser.add_argument("--public", type=Path, action="append", default=[])
    args = parser.parse_args()
    report = run(args.learning_db, args.out, args.public)
    print(json.dumps({
        "generated_at": report["generated_at"],
        "review_required": report["review_required"],
        "mode": report["policy"]["mode"],
    }, ensure_ascii=False))


if __name__ == "__main__":
    main()
