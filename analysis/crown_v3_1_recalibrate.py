#!/usr/bin/env python3
"""Crown V3.1: genuine retraining plus probability calibration, strict time holdout.

Read-only research script.  It streams to production, reads the persisted Crown
prediction history, rebuilds fixture-stage canonical snapshots exactly like the
V3 audit (analysis/crown_v3_backtest.py), engineers primary-side oriented
features, retrains a scikit-learn GradientBoostingClassifier on the discovery
split, learns Isotonic and Platt calibrators on the selection split, and scores
an untouched holdout.

The target is a FIXED primary side per market (HDC home, HIL over), so the model
chooses direction on its own: it predicts p(primary side wins), backs the primary
side when p >= 0.5 and the opposite side otherwise.  The persisted V2 maximum-EV
lead is only a comparison baseline, never the label.

Nothing is written anywhere except the JSON report path given on the command
line.  The upstream V2 runtime (crown/opening_model.py) is never imported,
modified or deployed.
"""
from __future__ import annotations

import argparse
import bisect
import hashlib
import json
import math
import statistics
from collections import Counter, defaultdict, deque
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Iterable, Sequence

SCHEMA_VERSION = "crown-v3-1-recalibrate-v1"
STAGES = ("首預", "T-30", "T-5")
MARKETS = ("HDC", "HIL")
GRADES = ("full_win", "half_win", "push", "half_loss", "full_loss")
HITS = {"full_win", "half_win"}
LABELS = {"full_win": 1.0, "half_win": 0.5, "half_loss": 0.0, "full_loss": 0.0}
BUCKET_EDGES = (
    ("lt_0.40", -math.inf, 0.40),
    ("0.40_0.50", 0.40, 0.50),
    ("0.50_0.55", 0.50, 0.55),
    ("0.55_0.60", 0.55, 0.60),
    ("0.60_0.65", 0.60, 0.65),
    ("ge_0.65", 0.65, math.inf),
)
DECISION_THRESHOLD = 0.55
PRIMARY_DIRECTION = {"HDC": "home", "HIL": "over"}
OPPOSITE_DIRECTION = {"HDC": "away", "HIL": "under"}
SEED = 42
RECENT_WINDOW = 10
HKT = timezone(timedelta(hours=8))
EPS = 1e-10
CLAMP = 1e-6

# --------------------------------------------------------------------------- #
# Modelling backend: scikit-learn when importable, deterministic pure-Python
# fallback otherwise (production hosts may not ship scikit-learn and this audit
# is not allowed to pip install anything).
# --------------------------------------------------------------------------- #
try:  # pragma: no cover - environment dependent
    import sklearn
    from sklearn.ensemble import GradientBoostingClassifier
    from sklearn.isotonic import IsotonicRegression
    from sklearn.linear_model import LogisticRegression

    SKLEARN_VERSION: str | None = sklearn.__version__
except Exception:  # pragma: no cover - environment dependent
    SKLEARN_VERSION = None


def _logit(p: float) -> float:
    p = min(max(p, CLAMP), 1.0 - CLAMP)
    return math.log(p / (1.0 - p))


def _sigmoid(z: float) -> float:
    if z >= 0:
        return 1.0 / (1.0 + math.exp(-z))
    value = math.exp(z)
    return value / (1.0 + value)


class PureGradientBoosting:
    """Deterministic histogram gradient boosted trees (logistic loss, stdlib only).

    Used only when scikit-learn is unavailable on the host.  Features are binned
    once into quantile bins, and every node split is found from per-feature bin
    histograms, which keeps the cost linear in rows per node.
    """

    def __init__(self, n_estimators: int = 120, learning_rate: float = 0.08, max_depth: int = 3,
                 min_samples_leaf: int = 20, max_bins: int = 16) -> None:
        self.n_estimators = n_estimators
        self.learning_rate = learning_rate
        self.max_depth = max_depth
        self.min_samples_leaf = min_samples_leaf
        self.max_bins = max_bins
        self.trees: list[Any] = []
        self.base_score = 0.0
        self.bin_edges: list[list[float]] = []
        self.feature_importances_: list[float] = []

    def _fit_bins(self, X: Sequence[Sequence[float]]) -> None:
        self.bin_edges = []
        for feature in range(len(X[0])):
            values = sorted({row[feature] for row in X})
            if len(values) <= 1:
                self.bin_edges.append([])
            elif len(values) <= self.max_bins:
                self.bin_edges.append([(values[i] + values[i + 1]) / 2.0 for i in range(len(values) - 1)])
            else:
                step = len(values) / float(self.max_bins)
                cuts = []
                for index in range(1, self.max_bins):
                    position = min(len(values) - 1, max(1, int(round(index * step))))
                    cuts.append((values[position - 1] + values[position]) / 2.0)
                self.bin_edges.append(sorted(set(cuts)))

    def _binned(self, X: Sequence[Sequence[float]]) -> list[list[int]]:
        return [[bisect.bisect_left(self.bin_edges[feature], row[feature]) for feature in range(len(row))]
                for row in X]

    def _leaf(self, grad: float, hess: float) -> float:
        return grad / (hess + 1.0)

    def _build(self, indices: list[int], binned: Sequence[Sequence[int]], grads: list[float],
               hess: list[float], depth: int, gains: list[float]) -> Any:
        parent_g = sum(grads[i] for i in indices)
        parent_h = sum(hess[i] for i in indices)
        if depth >= self.max_depth or len(indices) < 2 * self.min_samples_leaf:
            return {"leaf": self._leaf(parent_g, parent_h)}
        parent_score = parent_g * parent_g / (parent_h + 1.0)
        best = None
        for feature, edges in enumerate(self.bin_edges):
            if not edges:
                continue
            width = len(edges) + 1
            hist_g = [0.0] * width
            hist_h = [0.0] * width
            hist_n = [0] * width
            for i in indices:
                slot = binned[i][feature]
                hist_g[slot] += grads[i]
                hist_h[slot] += hess[i]
                hist_n[slot] += 1
            left_g = left_h = 0.0
            left_n = 0
            for slot in range(width - 1):
                left_g += hist_g[slot]
                left_h += hist_h[slot]
                left_n += hist_n[slot]
                right_n = len(indices) - left_n
                if left_n < self.min_samples_leaf or right_n < self.min_samples_leaf:
                    continue
                right_g = parent_g - left_g
                right_h = parent_h - left_h
                gain = left_g * left_g / (left_h + 1.0) + right_g * right_g / (right_h + 1.0) - parent_score
                key = (gain, -feature, -slot)
                if best is None or key > best[0]:
                    best = (key, feature, slot, gain)
        if best is None or best[3] <= 0:
            return {"leaf": self._leaf(parent_g, parent_h)}
        _, feature, slot, gain = best
        gains[feature] += gain
        left = [i for i in indices if binned[i][feature] <= slot]
        right = [i for i in indices if binned[i][feature] > slot]
        return {
            "feature": feature,
            "threshold": self.bin_edges[feature][slot],
            "left": self._build(left, binned, grads, hess, depth + 1, gains),
            "right": self._build(right, binned, grads, hess, depth + 1, gains),
        }

    @staticmethod
    def _apply(tree: Any, row: Sequence[float]) -> float:
        node = tree
        while "leaf" not in node:
            node = node["left"] if row[node["feature"]] <= node["threshold"] else node["right"]
        return node["leaf"]

    def fit(self, X: Sequence[Sequence[float]], y: Sequence[float], sample_weight: Sequence[float] | None = None):
        weights = list(sample_weight) if sample_weight is not None else [1.0] * len(y)
        total = sum(weights) or 1.0
        mean = min(max(sum(w * t for w, t in zip(weights, y)) / total, CLAMP), 1.0 - CLAMP)
        self.base_score = _logit(mean)
        self._fit_bins(X)
        binned = self._binned(X)
        scores = [self.base_score] * len(y)
        gains = [0.0] * (len(X[0]) if X else 0)
        self.trees = []
        for _ in range(self.n_estimators):
            probabilities = [_sigmoid(score) for score in scores]
            grads = [weights[i] * (y[i] - probabilities[i]) for i in range(len(y))]
            hess = [weights[i] * max(probabilities[i] * (1 - probabilities[i]), 1e-6) for i in range(len(y))]
            tree = self._build(list(range(len(y))), binned, grads, hess, 0, gains)
            self.trees.append(tree)
            for i in range(len(y)):
                scores[i] += self.learning_rate * self._apply(tree, X[i])
        magnitude = sum(gains) or 1.0
        self.feature_importances_ = [value / magnitude for value in gains]
        return self

    def predict_proba_positive(self, X: Sequence[Sequence[float]]) -> list[float]:
        output = []
        for row in X:
            score = self.base_score
            for tree in self.trees:
                score += self.learning_rate * self._apply(tree, row)
            output.append(_sigmoid(score))
        return output


class PureIsotonic:
    """Pool-adjacent-violators isotonic regression with linear interpolation."""

    def __init__(self) -> None:
        self.x: list[float] = []
        self.y: list[float] = []

    def fit(self, x: Sequence[float], y: Sequence[float], sample_weight: Sequence[float] | None = None):
        weights = list(sample_weight) if sample_weight is not None else [1.0] * len(y)
        merged: dict[float, list[float]] = {}
        for value, target, weight in zip(x, y, weights):
            slot = merged.setdefault(float(value), [0.0, 0.0])
            slot[0] += weight * target
            slot[1] += weight
        points = [(key, slot[0] / slot[1], slot[1]) for key, slot in sorted(merged.items())]
        blocks: list[list[float]] = []
        for position, value, weight in points:
            blocks.append([position, value, weight])
            while len(blocks) > 1 and blocks[-2][1] > blocks[-1][1] + 1e-15:
                b = blocks.pop()
                a = blocks.pop()
                weight_sum = a[2] + b[2]
                blocks.append([b[0], (a[1] * a[2] + b[1] * b[2]) / weight_sum, weight_sum])
        self.x = []
        self.y = []
        index = 0
        for block in blocks:
            while index < len(points) and points[index][0] <= block[0] + 1e-15:
                self.x.append(points[index][0])
                self.y.append(min(max(block[1], 0.0), 1.0))
                index += 1
        return self

    def predict(self, x: Sequence[float]) -> list[float]:
        output = []
        for value in x:
            value = float(value)
            if not self.x:
                output.append(0.5)
                continue
            if value <= self.x[0]:
                output.append(self.y[0])
                continue
            if value >= self.x[-1]:
                output.append(self.y[-1])
                continue
            position = bisect.bisect_left(self.x, value)
            x0, x1 = self.x[position - 1], self.x[position]
            y0, y1 = self.y[position - 1], self.y[position]
            span = x1 - x0
            output.append(y0 if span <= 0 else y0 + (y1 - y0) * (value - x0) / span)
        return output


class PurePlatt:
    """Weighted logistic regression of the raw logit onto the observed label."""

    def __init__(self, iterations: int = 200) -> None:
        self.a = 1.0
        self.b = 0.0
        self.iterations = iterations

    def fit(self, x: Sequence[float], y: Sequence[float], sample_weight: Sequence[float] | None = None):
        weights = list(sample_weight) if sample_weight is not None else [1.0] * len(y)
        features = [float(value) for value in x]
        self.a, self.b = 1.0, 0.0
        for _ in range(self.iterations):
            g_a = g_b = h_aa = h_ab = h_bb = 0.0
            for feature, target, weight in zip(features, y, weights):
                probability = _sigmoid(self.a * feature + self.b)
                residual = weight * (target - probability)
                curvature = weight * max(probability * (1 - probability), 1e-9)
                g_a += residual * feature
                g_b += residual
                h_aa += curvature * feature * feature
                h_ab += curvature * feature
                h_bb += curvature
            h_aa += 1e-6
            h_bb += 1e-6
            determinant = h_aa * h_bb - h_ab * h_ab
            if abs(determinant) < 1e-12:
                break
            step_a = (h_bb * g_a - h_ab * g_b) / determinant
            step_b = (h_aa * g_b - h_ab * g_a) / determinant
            self.a += step_a
            self.b += step_b
            if abs(step_a) + abs(step_b) < 1e-10:
                break
        return self

    def predict(self, x: Sequence[float]) -> list[float]:
        return [_sigmoid(self.a * float(value) + self.b) for value in x]


def backend_name() -> str:
    return "sklearn" if SKLEARN_VERSION else "pure_python_fallback"


def fit_classifier(X: Sequence[Sequence[float]], y: Sequence[float], weights: Sequence[float]):
    if SKLEARN_VERSION:
        model = GradientBoostingClassifier(
            random_state=SEED, n_estimators=200, learning_rate=0.05, max_depth=3,
            min_samples_leaf=20, subsample=1.0,
        )
        model.fit([list(row) for row in X], list(y), sample_weight=list(weights))
        return model
    return PureGradientBoosting().fit(X, y, weights)


def classifier_probabilities(model: Any, X: Sequence[Sequence[float]]) -> list[float]:
    if not X:
        return []
    if SKLEARN_VERSION:
        column = list(model.classes_).index(1)
        return [float(row[column]) for row in model.predict_proba([list(item) for item in X])]
    return model.predict_proba_positive(X)


def classifier_importances(model: Any) -> list[float]:
    return [float(value) for value in model.feature_importances_]


def fit_isotonic(raw: Sequence[float], labels: Sequence[float]):
    if SKLEARN_VERSION:
        model = IsotonicRegression(out_of_bounds="clip", y_min=0.0, y_max=1.0)
        model.fit(list(raw), list(labels))
        return model
    return PureIsotonic().fit(raw, labels)


def apply_isotonic(model: Any, raw: Sequence[float]) -> list[float]:
    if not raw:
        return []
    return [float(value) for value in model.predict(list(raw))]


def fit_platt(raw: Sequence[float], labels: Sequence[float]):
    features = [[_logit(value)] for value in raw]
    if SKLEARN_VERSION:
        expanded_x, expanded_y, expanded_w = [], [], []
        for row, label in zip(features, labels):
            for target, weight in ((1.0, label), (0.0, 1.0 - label)):
                if weight > 0:
                    expanded_x.append(row)
                    expanded_y.append(target)
                    expanded_w.append(weight)
        model = LogisticRegression(C=1e6, solver="lbfgs", max_iter=1000)
        model.fit(expanded_x, expanded_y, sample_weight=expanded_w)
        return model
    return PurePlatt().fit([row[0] for row in features], labels)


def platt_slope(model: Any) -> float | None:
    """Slope of the fitted Platt mapping in raw-logit space (must be positive)."""
    if SKLEARN_VERSION:
        try:
            coefficient = float(model.coef_[0][0])
        except Exception:
            return None
        classes = list(getattr(model, "classes_", []))
        # scikit-learn models the last class; flip when that is the negative label.
        if classes and classes[-1] in (0, 0.0):
            coefficient = -coefficient
        return coefficient
    return float(model.a)


def apply_platt(model: Any, raw: Sequence[float]) -> list[float]:
    if not raw:
        return []
    features = [[_logit(value)] for value in raw]
    if SKLEARN_VERSION:
        column = list(model.classes_).index(1.0) if 1.0 in list(model.classes_) else 1
        return [float(row[column]) for row in model.predict_proba(features)]
    return model.predict([row[0] for row in features])


# --------------------------------------------------------------------------- #
# Canonical data extraction (identical semantics to analysis/crown_v3_backtest)
# --------------------------------------------------------------------------- #
def _number(value: Any) -> float | None:
    try:
        result = float(value)
    except (TypeError, ValueError):
        return None
    return result if math.isfinite(result) else None


def parse_time(value: Any) -> datetime | None:
    if value is None:
        return None
    text = str(value).strip().replace("Z", "+00:00")
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=HKT)
    return parsed.astimezone(timezone.utc)


def file_state(path: Path) -> dict[str, Any]:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    stat = path.stat()
    return {"sha256": digest.hexdigest(), "size": stat.st_size, "mtime_ns": stat.st_mtime_ns}


def _fixture_id(row: dict[str, Any]) -> str:
    return str(row.get("titan_match_id") or row.get("match_id") or "").strip()


def _prediction(item: Any, index: int) -> dict[str, Any] | None:
    if not isinstance(item, dict):
        return None
    code = str(item.get("code") or "").upper()
    if code not in MARKETS:
        return None
    side = str(item.get("side") or "").upper()
    direction = ({"H": "home", "A": "away"} if code == "HDC" else
                 {"H": "over", "O": "over", "L": "under", "U": "under"}).get(side)
    probability = _number(item.get("probability", item.get("prob")))
    odds = _number(item.get("odds", item.get("decimal_odds")))
    line = _number(item.get("line", item.get("condition")))
    if direction is None or probability is None or not 0 < probability <= 1 or odds is None or odds <= 1 or line is None:
        return None
    return {
        "market": code,
        "direction": direction,
        "line": line,
        "odds": odds,
        "probability": probability,
        "ev": probability * odds - 1.0,
        "source_index": index,
    }


def market_candidates(row: dict[str, Any]) -> dict[str, list[dict[str, Any]]]:
    candidates: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for index, item in enumerate(row.get("market_predictions") or []):
        value = _prediction(item, index)
        if value is not None:
            candidates[value["market"]].append(value)
    return candidates


def market_leads(row: dict[str, Any]) -> dict[str, dict[str, Any]]:
    result = {}
    for market, values in market_candidates(row).items():
        result[market] = max(values, key=lambda x: (x["ev"], -x["source_index"]))
    return result


def _result_from_row(row: dict[str, Any]) -> dict[str, float] | None:
    if row.get("result_status") not in {"已核對", "已核實"}:
        return None
    detail = row.get("result_detail")
    if not isinstance(detail, dict):
        return None
    home = _number(detail.get("home_score"))
    away = _number(detail.get("away_score"))
    if home is None or away is None:
        score = str(row.get("score") or "")
        pieces = score.replace("：", "-").replace(":", "-").split("-")
        if len(pieces) == 2:
            home, away = _number(pieces[0]), _number(pieces[1])
    if home is None or away is None:
        return None
    return {"home_score": home, "away_score": away}


def canonicalize(rows: Sequence[Any]) -> tuple[list[dict[str, Any]], dict[str, int]]:
    canonical: dict[tuple[str, str], dict[str, Any]] = {}
    results: dict[str, tuple[str, dict[str, float]]] = {}
    diagnostics: Counter[str] = Counter()
    for raw in rows:
        if not isinstance(raw, dict):
            diagnostics["non_object_rows"] += 1
            continue
        fixture_id = _fixture_id(raw)
        result = _result_from_row(raw)
        if fixture_id and result is not None:
            result_key = str(raw.get("verified_at") or raw.get("predicted_at") or "")
            if fixture_id not in results or result_key > results[fixture_id][0]:
                results[fixture_id] = (result_key, result)
        stage = str(raw.get("stage") or "")
        if stage not in STAGES:
            diagnostics["unsupported_stage"] += 1
            continue
        if raw.get("post_hoc_backfill") or raw.get("exclude_from_primary_statistics") or raw.get("exclude_from_settlement"):
            diagnostics["excluded_audit_rows"] += 1
            continue
        predicted = parse_time(raw.get("predicted_at") or raw.get("ts"))
        kickoff = parse_time(raw.get("kickoff_hkt") or raw.get("kickoff"))
        if not fixture_id:
            diagnostics["missing_fixture_id"] += 1
            continue
        if predicted is None or kickoff is None or predicted >= kickoff:
            diagnostics["invalid_or_post_kickoff_snapshot"] += 1
            continue
        leads = market_leads(raw)
        if not leads:
            diagnostics["no_hdc_or_hil_lead"] += 1
            continue
        fingerprint = json.dumps(raw, ensure_ascii=False, sort_keys=True, separators=(",", ":"), default=str)
        item = {
            "row": raw, "fixture_id": fixture_id, "stage": stage, "predicted_at": predicted,
            "kickoff": kickoff, "leads": leads, "candidates": market_candidates(raw),
            "fingerprint": fingerprint,
        }
        key = (fixture_id, stage)
        old = canonical.get(key)
        if old is None or (predicted, fingerprint) > (old["predicted_at"], old["fingerprint"]):
            canonical[key] = item

    grouped: dict[str, dict[str, dict[str, Any]]] = defaultdict(dict)
    for (fixture_id, stage), item in canonical.items():
        grouped[fixture_id][stage] = item
    fixtures = []
    for fixture_id, stages in grouped.items():
        anchor = next((stages[s] for s in reversed(STAGES) if s in stages), None)
        if anchor is None or fixture_id not in results:
            diagnostics["fixtures_without_verified_result"] += 1
            continue
        kickoffs = {item["kickoff"] for item in stages.values()}
        if len(kickoffs) > 1:
            diagnostics["fixture_kickoff_mismatch"] += 1
        fixtures.append({
            "fixture_id": fixture_id,
            "kickoff": min(kickoffs),
            "home": anchor["row"].get("home"),
            "away": anchor["row"].get("away"),
            "league": anchor["row"].get("league") or anchor["row"].get("league_name") or anchor["row"].get("league_id"),
            "stages": stages,
            "result": results[fixture_id][1],
        })
    fixtures.sort(key=lambda x: (x["kickoff"], x["fixture_id"]))
    diagnostics["canonical_fixture_stage_snapshots"] = len(canonical)
    diagnostics["settled_canonical_fixtures"] = len(fixtures)
    return fixtures, dict(sorted(diagnostics.items()))


def split_kickoff_cohorts(fixtures: Sequence[dict[str, Any]]) -> tuple[dict[str, list[dict[str, Any]]], dict[str, Any]]:
    """Chronological 60/20/20 split; an equal-kickoff cohort is indivisible."""
    cohorts: list[list[dict[str, Any]]] = []
    for fixture in sorted(fixtures, key=lambda x: (x["kickoff"], x["fixture_id"])):
        if not cohorts or cohorts[-1][0]["kickoff"] != fixture["kickoff"]:
            cohorts.append([])
        cohorts[-1].append(fixture)
    total = sum(len(group) for group in cohorts)
    targets = (total * 0.60, total * 0.80)
    cuts = []
    for target in targets:
        choices = []
        running = 0
        for index in range(len(cohorts) + 1):
            if index:
                running += len(cohorts[index - 1])
            choices.append((abs(running - target), index, running))
        cuts.append(min(choices, key=lambda x: (x[0], x[1]))[1])
    discovery_cut, selection_cut = cuts
    selection_cut = max(discovery_cut, selection_cut)
    parts = {
        "discovery": [row for group in cohorts[:discovery_cut] for row in group],
        "selection": [row for group in cohorts[discovery_cut:selection_cut] for row in group],
        "holdout": [row for group in cohorts[selection_cut:] for row in group],
    }
    fixture_part = {row["fixture_id"]: part for part, values in parts.items() for row in values}
    kickoff_parts: dict[str, set[str]] = defaultdict(set)
    for row in fixtures:
        kickoff_parts[row["kickoff"].isoformat()].add(fixture_part[row["fixture_id"]])
    assert all(len(value) == 1 for value in kickoff_parts.values())
    metadata = {
        "method": "chronological kickoff cohorts nearest to 60/20/20; equal kickoff is indivisible",
        "total_fixtures": total,
        "cohort_count": len(cohorts),
        "counts": {key: len(value) for key, value in parts.items()},
        "kickoff_ranges": {
            key: [value[0]["kickoff"].isoformat(), value[-1]["kickoff"].isoformat()] if value else [None, None]
            for key, value in parts.items()
        },
        "no_equal_kickoff_crosses_split": True,
    }
    return parts, metadata


def _split_quarter_line(line: float) -> tuple[float, ...]:
    doubled = line * 2
    if abs(doubled - round(doubled)) <= EPS:
        return (line,)
    quartered = line * 4
    if abs(quartered - round(quartered)) > EPS:
        raise ValueError(f"line is not an Asian quarter increment: {line}")
    return (line - 0.25, line + 0.25)


def settle(prediction: dict[str, Any], result: dict[str, float]) -> str:
    outcomes = []
    for line in _split_quarter_line(float(prediction["line"])):
        if prediction["market"] == "HDC":
            home_margin = result["home_score"] + line - result["away_score"]
            difference = home_margin if prediction["direction"] == "home" else -home_margin
        elif prediction["market"] == "HIL":
            total = result["home_score"] + result["away_score"]
            difference = total - line if prediction["direction"] == "over" else line - total
        else:
            raise ValueError(f"unsupported market: {prediction['market']}")
        outcomes.append(1 if difference > EPS else -1 if difference < -EPS else 0)
    if len(outcomes) == 1:
        return {1: "full_win", 0: "push", -1: "full_loss"}[outcomes[0]]
    if outcomes == [1, 1]:
        return "full_win"
    if sum(outcomes) == 1:
        return "half_win"
    if sum(outcomes) == 0:
        return "push"
    if sum(outcomes) == -1:
        return "half_loss"
    return "full_loss"


def unit_return(grade: str, odds: float) -> float:
    return {"full_win": odds - 1.0, "half_win": (odds - 1.0) / 2.0, "push": 0.0,
            "half_loss": -0.5, "full_loss": -1.0}[grade]


# --------------------------------------------------------------------------- #
# Feature engineering: fixed primary side, side-oriented features
# --------------------------------------------------------------------------- #
BASE_FEATURES = (
    "primary_probability",
    "opposite_probability",
    "probability_gap_primary_minus_opposite",
    "primary_odds",
    "opposite_odds",
    "odds_gap_primary_minus_opposite",
    "implied_primary_probability_devig",
    "primary_probability_minus_implied",
    "primary_probability_is_derived",
    "primary_line",
    "opposite_line",
    "primary_line_move_open_to_t30",
    "primary_line_move_t30_to_t5",
    "primary_odds_move_open_to_t30",
    "primary_odds_move_t30_to_t5",
    "opposite_odds_move_t30_to_t5",
    "hkjc_line_gap",
    "hkjc_odds_gap",
    "league_target_encoding",
    "kickoff_is_weekend",
    "kickoff_hour_bucket_hkt",
    "primary_side_recent_hit_rate",
    "home_recent_win_rate",
    "away_recent_win_rate",
    "recent_strength_diff",
)
NULLABLE_FEATURES = (
    "primary_probability",
    "opposite_probability",
    "probability_gap_primary_minus_opposite",
    "primary_odds",
    "opposite_odds",
    "odds_gap_primary_minus_opposite",
    "implied_primary_probability_devig",
    "primary_probability_minus_implied",
    "opposite_line",
    "primary_line_move_open_to_t30",
    "primary_line_move_t30_to_t5",
    "primary_odds_move_open_to_t30",
    "primary_odds_move_t30_to_t5",
    "opposite_odds_move_t30_to_t5",
    "hkjc_line_gap",
    "hkjc_odds_gap",
    "primary_side_recent_hit_rate",
    "home_recent_win_rate",
    "away_recent_win_rate",
    "recent_strength_diff",
)
FEATURE_NAMES = tuple(list(BASE_FEATURES) + [f"{name}__missing" for name in NULLABLE_FEATURES])


def _hkjc_value(row: dict[str, Any], keys: Sequence[str]) -> float | None:
    for key in keys:
        if key in row:
            value = _number(row.get(key))
            if value is not None:
                return value
    nested = row.get("hkjc")
    if isinstance(nested, dict):
        for key in ("line", "condition", "odds"):
            if key in nested and any(key in candidate for candidate in keys):
                value = _number(nested.get(key))
                if value is not None:
                    return value
    return None


def side_candidate(stage: dict[str, Any] | None, market: str, direction: str) -> dict[str, Any] | None:
    """Deterministic best candidate for one explicit market direction."""
    if stage is None:
        return None
    matches = [item for item in stage["candidates"].get(market, []) if item["direction"] == direction]
    if not matches:
        return None
    return max(matches, key=lambda x: (x["ev"], -x["source_index"]))


def _hour_bucket(hour: int) -> float:
    if hour < 6:
        return 0.0
    if hour < 12:
        return 1.0
    if hour < 18:
        return 2.0
    return 3.0


def _match_outcome_points(result: dict[str, float]) -> tuple[float, float]:
    if result["home_score"] > result["away_score"]:
        return 1.0, 0.0
    if result["home_score"] < result["away_score"]:
        return 0.0, 1.0
    return 0.5, 0.5


def _mean(values: Iterable[float]) -> float | None:
    values = [value for value in values if value is not None]
    return sum(values) / len(values) if values else None


def build_records(fixtures: Sequence[dict[str, Any]], market: str) -> list[dict[str, Any]]:
    """One record per fixture with a FIXED primary side as the target.

    The target is the primary side of the market itself (HDC home, HIL over), never
    the V2 maximum-EV lead, so the retrained model chooses direction independently.
    Rolling team history uses strictly earlier kickoffs only.
    """
    primary = PRIMARY_DIRECTION[market]
    opposite = OPPOSITE_DIRECTION[market]
    team_form: dict[str, deque[float]] = defaultdict(lambda: deque(maxlen=RECENT_WINDOW))
    team_primary: dict[str, deque[float]] = defaultdict(lambda: deque(maxlen=RECENT_WINDOW))
    records: list[dict[str, Any]] = []
    for fixture in sorted(fixtures, key=lambda x: (x["kickoff"], x["fixture_id"])):
        stages = fixture["stages"]
        t5 = stages.get("T-5")
        primary_pick = side_candidate(t5, market, primary)
        opposite_pick = side_candidate(t5, market, opposite)
        home = str(fixture.get("home") or "?")
        away = str(fixture.get("away") or "?")
        home_rate = _mean(team_form[home]) if team_form[home] else None
        away_rate = _mean(team_form[away]) if team_form[away] else None
        history_terms = list(team_primary[home]) + list(team_primary[away])
        primary_history = _mean(history_terms) if history_terms else None
        label: float | None = None
        primary_grade: str | None = None
        if primary_pick is not None or opposite_pick is not None:
            # The persisted line is home/over perspective for both sides, so the
            # primary-side label is derivable from whichever side is present.
            line_source = primary_pick if primary_pick is not None else opposite_pick
            line = float(line_source["line"])
            primary_grade = settle({"market": market, "direction": primary, "line": line}, fixture["result"])
            opposite_line = float(opposite_pick["line"]) if opposite_pick is not None else line
            opposite_grade = settle({"market": market, "direction": opposite, "line": opposite_line}, fixture["result"])
            label = LABELS.get(primary_grade)
            primary_probability = primary_pick["probability"] if primary_pick is not None else None
            opposite_probability = opposite_pick["probability"] if opposite_pick is not None else None
            derived = 0.0
            if primary_probability is None and opposite_probability is not None and 0 < opposite_probability < 1:
                # Two-way Asian market: the complementary side is a safe derivation.
                primary_probability = 1.0 - opposite_probability
                derived = 1.0
            primary_odds = primary_pick["odds"] if primary_pick is not None else None
            opposite_odds = opposite_pick["odds"] if opposite_pick is not None else None
            implied = None
            if primary_odds and opposite_odds:
                raw_primary = 1.0 / primary_odds
                raw_opposite = 1.0 / opposite_odds
                total = raw_primary + raw_opposite
                implied = raw_primary / total if total > 0 else None
            open_primary = side_candidate(stages.get("首預"), market, primary)
            t30_primary = side_candidate(stages.get("T-30"), market, primary)
            t30_opposite = side_candidate(stages.get("T-30"), market, opposite)
            kickoff_hkt = fixture["kickoff"].astimezone(HKT)
            row = t5["row"]
            hkjc_line = _hkjc_value(row, ("hkjc_line", "hkjc_condition", "hkjc_signal_line"))
            hkjc_odds = _hkjc_value(row, ("hkjc_odds", "hkjc_signal_odds", "hkjc_execution_odds"))
            strength_diff = None if home_rate is None or away_rate is None else home_rate - away_rate
            values = {
                "primary_probability": primary_probability,
                "opposite_probability": opposite_probability,
                "probability_gap_primary_minus_opposite": (
                    None if primary_probability is None or opposite_probability is None
                    else primary_probability - opposite_probability),
                "primary_odds": primary_odds,
                "opposite_odds": opposite_odds,
                "odds_gap_primary_minus_opposite": (
                    None if primary_odds is None or opposite_odds is None else primary_odds - opposite_odds),
                "implied_primary_probability_devig": implied,
                "primary_probability_minus_implied": (
                    None if implied is None or primary_probability is None else primary_probability - implied),
                "primary_probability_is_derived": derived,
                "primary_line": line,
                "opposite_line": None if opposite_pick is None else opposite_line,
                "primary_line_move_open_to_t30": (
                    None if open_primary is None or t30_primary is None else t30_primary["line"] - open_primary["line"]),
                "primary_line_move_t30_to_t5": (
                    None if t30_primary is None else line - t30_primary["line"]),
                "primary_odds_move_open_to_t30": (
                    None if open_primary is None or t30_primary is None else t30_primary["odds"] - open_primary["odds"]),
                "primary_odds_move_t30_to_t5": (
                    None if t30_primary is None or primary_odds is None else primary_odds - t30_primary["odds"]),
                "opposite_odds_move_t30_to_t5": (
                    None if t30_opposite is None or opposite_odds is None else opposite_odds - t30_opposite["odds"]),
                "hkjc_line_gap": None if hkjc_line is None else line - hkjc_line,
                "hkjc_odds_gap": None if hkjc_odds is None or primary_odds is None else primary_odds - hkjc_odds,
                "league_target_encoding": None,  # filled from discovery statistics later
                "kickoff_is_weekend": 1.0 if kickoff_hkt.weekday() >= 5 else 0.0,
                "kickoff_hour_bucket_hkt": _hour_bucket(kickoff_hkt.hour),
                "primary_side_recent_hit_rate": primary_history,
                "home_recent_win_rate": home_rate,
                "away_recent_win_rate": away_rate,
                "recent_strength_diff": strength_diff,
            }
            v2_lead = t5["leads"].get(market)
            v2_lead_grade = None
            if v2_lead is not None:
                v2_lead_grade = settle(v2_lead, fixture["result"])
            records.append({
                "fixture_id": fixture["fixture_id"],
                "kickoff": fixture["kickoff"],
                "market": market,
                "league": str(fixture.get("league") or "unknown"),
                "primary_direction": primary,
                "opposite_direction": opposite,
                "primary_line": line,
                "opposite_line": opposite_line,
                "primary_grade": primary_grade,
                "opposite_grade": opposite_grade,
                "label": label,
                "is_push": primary_grade == "push",
                "primary_odds": primary_odds,
                "opposite_odds": opposite_odds,
                "primary_return": None if primary_odds is None else unit_return(primary_grade, primary_odds),
                "opposite_return": None if opposite_odds is None else unit_return(opposite_grade, opposite_odds),
                "sides_available": [
                    name for name, value in (("primary", primary_pick), ("opposite", opposite_pick)) if value is not None
                ],
                "v2_primary_side_probability": primary_probability,
                "v2_primary_probability_is_derived": bool(derived),
                "v2_lead_direction": None if v2_lead is None else v2_lead["direction"],
                "v2_lead_probability": None if v2_lead is None else v2_lead["probability"],
                "v2_lead_odds": None if v2_lead is None else v2_lead["odds"],
                "v2_lead_grade": v2_lead_grade,
                "v2_lead_return": None if v2_lead is None else unit_return(v2_lead_grade, v2_lead["odds"]),
                "raw_values": values,
            })
        # Rolling history is updated only after the fixture has been emitted.
        home_points, away_points = _match_outcome_points(fixture["result"])
        team_form[home].append(home_points)
        team_form[away].append(away_points)
        if label is not None:
            team_primary[home].append(label)
            team_primary[away].append(label)
    return records


def league_encoding(discovery: Sequence[dict[str, Any]], smoothing: float = 20.0) -> tuple[dict[str, float], float]:
    trained = [row for row in discovery if not row["is_push"] and row["label"] is not None]
    prior = _mean(row["label"] for row in trained) or 0.5
    groups: dict[str, list[float]] = defaultdict(list)
    for row in trained:
        groups[row["league"]].append(row["label"])
    encoding = {
        league: (sum(values) + smoothing * prior) / (len(values) + smoothing)
        for league, values in sorted(groups.items())
    }
    return encoding, prior


def design_matrix(records: Sequence[dict[str, Any]], medians: dict[str, float],
                  encoding: dict[str, float], prior: float) -> list[list[float]]:
    matrix = []
    for record in records:
        values = dict(record["raw_values"])
        values["league_target_encoding"] = encoding.get(record["league"], prior)
        row = []
        for name in BASE_FEATURES:
            value = values.get(name)
            row.append(medians.get(name, 0.0) if value is None else float(value))
        for name in NULLABLE_FEATURES:
            row.append(1.0 if values.get(name) is None else 0.0)
        matrix.append(row)
    return matrix


def feature_medians(records: Sequence[dict[str, Any]], encoding: dict[str, float], prior: float) -> dict[str, float]:
    medians = {}
    for name in BASE_FEATURES:
        if name == "league_target_encoding":
            values = [encoding.get(record["league"], prior) for record in records]
        else:
            values = [record["raw_values"][name] for record in records if record["raw_values"].get(name) is not None]
        medians[name] = float(statistics.median(values)) if values else 0.0
    return medians


# --------------------------------------------------------------------------- #
# Decision layer and metrics
# --------------------------------------------------------------------------- #
def wilson(hits: int, n: int, z: float = 1.959963984540054) -> list[float | None]:
    if not n:
        return [None, None]
    p = hits / n
    denominator = 1 + z * z / n
    center = (p + z * z / (2 * n)) / denominator
    margin = z * math.sqrt((p * (1 - p) + z * z / (4 * n)) / n) / denominator
    return [center - margin, center + margin]


def decide(record: dict[str, Any], primary_probability: float) -> dict[str, Any]:
    """Independent direction choice: primary when p >= 0.5, otherwise opposite."""
    if primary_probability + EPS >= 0.5:
        direction = record["primary_direction"]
        confidence = primary_probability
        grade = record["primary_grade"]
        odds = record["primary_odds"]
        payoff = record["primary_return"]
    else:
        direction = record["opposite_direction"]
        confidence = 1.0 - primary_probability
        grade = record["opposite_grade"]
        odds = record["opposite_odds"]
        payoff = record["opposite_return"]
    return {
        "direction": direction,
        "confidence": confidence,
        "grade": grade,
        "odds": odds,
        "return": payoff,
        "label": LABELS.get(grade),
        "is_push": grade == "push",
        "flips_v2_lead": (record["v2_lead_direction"] is not None and direction != record["v2_lead_direction"]),
    }


def v2_lead_decision(record: dict[str, Any]) -> dict[str, Any]:
    """The persisted production decision: the V2 maximum-EV lead direction."""
    grade = record["v2_lead_grade"]
    return {
        "direction": record["v2_lead_direction"],
        "confidence": record["v2_lead_probability"],
        "grade": grade,
        "odds": record["v2_lead_odds"],
        "return": record["v2_lead_return"],
        "label": LABELS.get(grade) if grade else None,
        "is_push": grade == "push",
        "flips_v2_lead": False,
    }


def _buckets(rows: Sequence[tuple[float, float]]) -> dict[str, Any]:
    output: dict[str, Any] = {}
    for name, low, high in BUCKET_EDGES:
        chosen = [(p, y) for p, y in rows if low <= p < high or (high == math.inf and p >= low)]
        output[name] = {
            "range": [None if low == -math.inf else low, None if high == math.inf else high],
            "n": len(chosen),
            "mean_probability": _mean(p for p, _ in chosen),
            "hit_rate_excluding_push": _mean(y for _, y in chosen),
        }
    return output


def metric_block(records: Sequence[dict[str, Any]], probabilities: Sequence[float],
                 decision_rule: Any = None) -> dict[str, Any]:
    """Calibration metrics on the primary-side label plus independent decision metrics."""
    rule = decision_rule or decide
    paired = [(record, min(max(float(p), CLAMP), 1.0 - CLAMP)) for record, p in zip(records, probabilities)]
    decided = [(record, p) for record, p in paired if not record["is_push"] and record["label"] is not None]
    rows = [(p, record["label"]) for record, p in decided]
    mean_probability = _mean(p for p, _ in rows)
    hit_rate = _mean(y for _, y in rows)
    brier = _mean((p - y) ** 2 for p, y in rows)
    log_loss = _mean(-(y * math.log(p) + (1 - y) * math.log(1 - p)) for p, y in rows)
    primary_hits = sum(1 for record, _ in decided if record["primary_grade"] in HITS)

    decisions = [(record, rule(record, p)) for record, p in paired]
    priced = [(record, choice) for record, choice in decisions if choice["return"] is not None]
    settled = [(record, choice) for record, choice in decisions if not choice["is_push"] and choice["label"] is not None]
    direction_counts = Counter(choice["direction"] for _, choice in decisions if choice["direction"])
    flips = [choice["flips_v2_lead"] for record, choice in decisions if record["v2_lead_direction"]]
    bets = [(record, choice) for record, choice in priced if choice["confidence"] is not None
            and choice["confidence"] + EPS >= DECISION_THRESHOLD]
    bet_settled = [(record, choice) for record, choice in bets if not choice["is_push"] and choice["label"] is not None]
    bet_hits = sum(1 for _, choice in bet_settled if choice["grade"] in HITS)
    returns = [choice["return"] for _, choice in bets]
    settled_hits = sum(1 for _, choice in settled if choice["grade"] in HITS)
    return {
        "n": len(decided),
        "n_including_push": len(paired),
        "push_n": len(paired) - len(decided),
        "mean_probability": mean_probability,
        "hit_rate_excluding_push": hit_rate,
        "primary_side_integer_hits_excluding_push": primary_hits,
        "brier": brier,
        "log_loss": log_loss,
        "calibration_gap": None if mean_probability is None or hit_rate is None else abs(mean_probability - hit_rate),
        "buckets": _buckets(rows),
        "reliability_diagram": [
            {
                "bin": name,
                "n": payload["n"],
                "mean_probability": payload["mean_probability"],
                "observed_hit_rate": payload["hit_rate_excluding_push"],
            }
            for name, payload in _buckets(rows).items()
        ],
        "decision": {
            "rule": "choose primary side when p(primary) >= 0.5, otherwise the opposite side; confidence = max(p, 1 - p)",
            "n_decisions": len(decisions),
            "n_priced_decisions": len(priced),
            "n_settled_excluding_push": len(settled),
            "direction_counts": dict(sorted(direction_counts.items())),
            "decision_accuracy_excluding_push": _mean(choice["label"] for _, choice in settled),
            "decision_integer_hit_rate_excluding_push": (settled_hits / len(settled)) if settled else None,
            "decision_wilson_95": wilson(settled_hits, len(settled)),
            "mean_confidence": _mean(choice["confidence"] for _, choice in decisions),
            "direction_flip_rate_vs_v2_lead": _mean(1.0 if value else 0.0 for value in flips),
            "n_compared_with_v2_lead": len(flips),
        },
        "roi_at_0.55_threshold": {
            "threshold": DECISION_THRESHOLD,
            "basis": "direction chosen by this model/probability, priced at the matching T-5 side odds",
            "n_bets_including_push": len(bets),
            "n_bets_excluding_push": len(bet_settled),
            "unit_pnl": sum(returns) if returns else 0.0,
            "roi": (sum(returns) / len(returns)) if returns else None,
            "hit_rate_excluding_push": _mean(choice["label"] for _, choice in bet_settled),
            "integer_hits": bet_hits,
            "wilson_hit_rate_95": wilson(bet_hits, len(bet_settled)),
            "direction_counts": dict(sorted(Counter(choice["direction"] for _, choice in bets).items())),
            "flip_rate_vs_v2_lead": _mean(1.0 if choice["flips_v2_lead"] else 0.0
                                          for record, choice in bets if record["v2_lead_direction"]),
        },
        "wilson": wilson(primary_hits, len(decided)),
        "primary_side_grade_counts": {
            grade: sum(1 for record, _ in paired if record["primary_grade"] == grade) for grade in GRADES
        },
    }


# --------------------------------------------------------------------------- #
# Report
# --------------------------------------------------------------------------- #
def _train_arrays(records: Sequence[dict[str, Any]], matrix: Sequence[Sequence[float]]):
    X, y, w = [], [], []
    for record, row in zip(records, matrix):
        if record["is_push"] or record["label"] is None:
            continue
        label = record["label"]
        for target, weight in ((1.0, label), (0.0, 1.0 - label)):
            if weight > 0:
                X.append(list(row))
                y.append(target)
                w.append(weight)
    return X, y, w


MONOTONICITY_GRID = tuple(round(0.02 + 0.04 * index, 4) for index in range(25))


def _monotonicity(mapped: Sequence[float]) -> bool:
    return all(mapped[index] <= mapped[index + 1] + 1e-12 for index in range(len(mapped) - 1))


def model_report(records_by_part: dict[str, list[dict[str, Any]]]) -> dict[str, Any]:
    discovery = records_by_part["discovery"]
    encoding, prior = league_encoding(discovery)
    medians = feature_medians(discovery, encoding, prior)
    matrices = {part: design_matrix(rows, medians, encoding, prior) for part, rows in records_by_part.items()}

    X, y, w = _train_arrays(discovery, matrices["discovery"])
    if not X:
        return {"error": "no training rows"}
    model = fit_classifier(X, y, w)
    raw = {part: classifier_probabilities(model, matrices[part]) for part in records_by_part}

    selection = records_by_part["selection"]
    selection_decided = [(record, p) for record, p in zip(selection, raw["selection"])
                         if not record["is_push"] and record["label"] is not None]
    isotonic_blocks: dict[str, Any] = {}
    platt_blocks: dict[str, Any] = {}
    guard: dict[str, Any] = {}
    if selection_decided:
        cal_raw = [p for _, p in selection_decided]
        cal_labels = [record["label"] for record, _ in selection_decided]
        isotonic = fit_isotonic(cal_raw, cal_labels)
        platt = fit_platt(cal_raw, cal_labels)
        isotonic_grid = apply_isotonic(isotonic, MONOTONICITY_GRID)
        platt_grid = apply_platt(platt, MONOTONICITY_GRID)
        slope = platt_slope(platt)
        platt_increasing = slope is not None and slope > 0 and _monotonicity(platt_grid)
        guard = {
            "requirement": "a calibrator must be non-decreasing in the raw probability, otherwise it would invert the direction decision",
            "isotonic_is_non_decreasing": _monotonicity(isotonic_grid),
            "platt_slope": slope,
            "platt_is_increasing": platt_increasing,
            "platt_rejected_and_scored_as_raw": not platt_increasing,
            "grid": list(MONOTONICITY_GRID),
            "isotonic_grid_mapped": isotonic_grid,
            "platt_grid_mapped": platt_grid,
        }
        for part in ("selection", "holdout"):
            isotonic_blocks[part] = metric_block(records_by_part[part], apply_isotonic(isotonic, raw[part]))
            platt_probabilities = apply_platt(platt, raw[part]) if platt_increasing else list(raw[part])
            platt_blocks[part] = metric_block(records_by_part[part], platt_probabilities)
        guard["isotonic_fit_n"] = len(cal_raw)
        guard["platt_fit_n"] = len(cal_raw)
        guard["fit_split"] = "selection"
        guard["holdout_never_used_for_fitting"] = True
    importances = dict(zip(FEATURE_NAMES, classifier_importances(model)))
    return {
        "raw": {part: metric_block(records_by_part[part], raw[part]) for part in records_by_part},
        "isotonic": isotonic_blocks,
        "platt": platt_blocks,
        "calibration_meta": guard,
        "training": {
            "discovery_rows_excluding_push": sum(1 for record in discovery
                                                 if not record["is_push"] and record["label"] is not None),
            "expanded_training_rows": len(X),
            "half_win_half_loss_weighting": "label 0.5 expanded into weighted 1/0 rows",
        },
        "feature_importance": {name: float(value) for name, value in sorted(importances.items())},
        "feature_medians_from_discovery": {name: float(value) for name, value in sorted(medians.items())},
        "league_target_encoding_prior": prior,
    }


def build_report(payload: dict[str, Any], input_state: dict[str, Any]) -> dict[str, Any]:
    fixtures, diagnostics = canonicalize(payload.get("rows") or [])
    parts, split_metadata = split_kickoff_cohorts(fixtures)
    models: dict[str, Any] = {}
    baselines: dict[str, Any] = {}
    counts: dict[str, Any] = {}
    coverage: dict[str, Any] = {}
    importance: dict[str, Any] = {}
    for market in MARKETS:
        key = market.lower()
        # Rolling history is built once over the full chronological history and then
        # partitioned, so a record only ever sees strictly earlier kickoffs.
        every = build_records(fixtures, market)
        membership = {row["fixture_id"]: part for part, values in parts.items() for row in values}
        records_by_part: dict[str, list[dict[str, Any]]] = {part: [] for part in parts}
        for record in every:
            records_by_part[membership[record["fixture_id"]]].append(record)
        counts[key] = {part: len(rows) for part, rows in records_by_part.items()}
        coverage[key] = {
            "both_sides_present": sum(1 for row in every if len(row["sides_available"]) == 2),
            "primary_side_only": sum(1 for row in every if row["sides_available"] == ["primary"]),
            "opposite_side_only_probability_derived": sum(1 for row in every if row["sides_available"] == ["opposite"]),
            "primary_line_disagrees_with_opposite_line": sum(
                1 for row in every if row["opposite_line"] is not None
                and abs(row["opposite_line"] - row["primary_line"]) > EPS),
        }
        report = model_report(records_by_part)
        importance[key] = report.pop("feature_importance", {})
        models[key] = report
        baselines[key] = {
            part: metric_block(rows, [
                record["v2_primary_side_probability"] if record["v2_primary_side_probability"] is not None else 0.5
                for record in rows
            ])
            for part, rows in records_by_part.items()
        }
        baselines[key]["production_lead_decision"] = {
            part: metric_block(
                rows,
                [record["v2_primary_side_probability"] if record["v2_primary_side_probability"] is not None else 0.5
                 for record in rows],
                decision_rule=lambda record, probability: v2_lead_decision(record),
            )
            for part, rows in records_by_part.items()
        }
    return {
        "schema_version": SCHEMA_VERSION,
        "scope": {
            "kind": "V3.1 genuine retraining with independent direction choice and probability calibration",
            "retrains_model": True,
            "production_untouched": True,
            "decision_stage": "T-5",
            "target_definition": (
                "one row per fixture and market with a FIXED primary side: HDC home side, HIL over side. "
                "Label 1 for a full win, 0.5 for a half win, 0 for a half or full loss on that primary side; "
                "pushes are excluded from calibration metrics and from training. "
                "Both T-5 sides are read from the candidate list; the V2 maximum-EV lead is never the target."
            ),
            "direction_decision": (
                "the model outputs p(primary side wins); it picks the primary side when p >= 0.5 and the opposite "
                "side otherwise, with confidence max(p, 1 - p) priced at that side's T-5 odds and settled with "
                "exact Asian handicap/total rules"
            ),
            "v2_baseline_definition": (
                "persisted production V2 probability of the same primary side (complement of the opposite side when "
                "only that side was published), scored against the same primary-side label; the production "
                "maximum-EV lead decision is reported separately under v2_baseline.<market>.production_lead_decision"
            ),
            "calibration_fit_split": "selection only",
            "calibration_monotonicity_guard": (
                "a calibrator that is not non-decreasing would invert the direction decision, so isotonic "
                "monotonicity is verified on a grid and a Platt fit with non-positive slope is rejected and scored raw"
            ),
            "holdout_usage": "final scoring only; never used for training, calibration or feature statistics",
            "roi_definition": (
                "one unit per bet when the chosen-direction confidence >= 0.55, priced at the chosen side's T-5 "
                "decimal odds; Asian half win/loss and push settled exactly"
            ),
            "median_impute_source": "discovery split medians plus explicit missingness flags",
        },
        "split": split_metadata,
        "diagnostics": {
            **diagnostics,
            "records_per_market_and_split": counts,
            "side_coverage_per_market": coverage,
        },
        "features": {
            "used": list(FEATURE_NAMES),
            "base": list(BASE_FEATURES),
            "nullable_with_missingness_flag": list(NULLABLE_FEATURES),
            "orientation": "all price/line features are primary-side oriented or side-neutral; no feature encodes the V2 chosen direction",
            "importance": importance,
        },
        "reproducibility": {
            "input_sha256_before": input_state.get("sha256"),
            "input_sha256_after": input_state.get("sha256_after"),
            "input_file_state": {key: value for key, value in input_state.items() if key != "sha256_after"},
            "seed": SEED,
            "sklearn_version": SKLEARN_VERSION,
            "model_backend": backend_name(),
            "deterministic_ties": True,
            "report_has_no_wall_clock_field": True,
        },
        "models": models,
        "v2_baseline": baselines,
    }


def _fmt(value: Any) -> str:
    return "NA" if value is None else f"{value:.4f}"


def _summary(report: dict[str, Any]) -> str:
    lines = [
        "Crown V3.1 recalibration summary",
        f"backend={report['reproducibility']['model_backend']} sklearn={report['reproducibility']['sklearn_version']}",
        f"input_sha256={report['reproducibility']['input_sha256_before']}",
        f"split={report['split']['counts']}",
    ]
    for market in ("hdc", "hil"):
        model = report["models"].get(market, {})
        for variant in ("raw", "isotonic", "platt"):
            block = (model.get(variant) or {}).get("holdout")
            if block:
                lines.append(
                    f"{market}/{variant}: holdout n={block['n']} brier={_fmt(block['brier'])} "
                    f"logloss={_fmt(block['log_loss'])} gap={_fmt(block['calibration_gap'])} "
                    f"dec_hit={_fmt(block['decision']['decision_integer_hit_rate_excluding_push'])} "
                    f"flip={_fmt(block['decision']['direction_flip_rate_vs_v2_lead'])} "
                    f"bets={block['roi_at_0.55_threshold']['n_bets_including_push']} "
                    f"roi55={_fmt(block['roi_at_0.55_threshold']['roi'])}"
                )
        baseline = (report["v2_baseline"].get(market) or {}).get("holdout")
        if baseline:
            lines.append(
                f"{market}/v2_baseline: holdout n={baseline['n']} brier={_fmt(baseline['brier'])} "
                f"logloss={_fmt(baseline['log_loss'])} gap={_fmt(baseline['calibration_gap'])} "
                f"bets={baseline['roi_at_0.55_threshold']['n_bets_including_push']} "
                f"roi55={_fmt(baseline['roi_at_0.55_threshold']['roi'])}"
            )
        production = ((report["v2_baseline"].get(market) or {}).get("production_lead_decision") or {}).get("holdout")
        if production:
            lines.append(
                f"{market}/v2_production_lead: holdout bets={production['roi_at_0.55_threshold']['n_bets_including_push']} "
                f"roi55={_fmt(production['roi_at_0.55_threshold']['roi'])} "
                f"dec_hit={_fmt(production['decision']['decision_integer_hit_rate_excluding_push'])}"
            )
        guard = (report["models"].get(market) or {}).get("calibration_meta") or {}
        if guard:
            lines.append(
                f"{market}/calibration_guard: isotonic_increasing={guard.get('isotonic_is_non_decreasing')} "
                f"platt_slope={_fmt(guard.get('platt_slope'))} platt_rejected={guard.get('platt_rejected_and_scored_as_raw')}"
            )
    return "\n".join(lines)


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--input", required=True, type=Path)
    parser.add_argument("--output", required=True, type=Path)
    args = parser.parse_args(argv)
    before = file_state(args.input)
    with args.input.open("r", encoding="utf-8") as handle:
        payload = json.load(handle)
    if not isinstance(payload, dict):
        raise SystemExit("prediction history root must be an object")
    report = build_report(payload, before)
    after = file_state(args.input)
    if before != after:
        raise SystemExit("read-only integrity check failed: input changed during analysis")
    report["reproducibility"]["input_sha256_after"] = after["sha256"]
    report["reproducibility"]["read_only_input_hash_unchanged"] = True
    args.output.write_text(json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    print(_summary(report))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
