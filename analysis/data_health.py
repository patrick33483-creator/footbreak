#!/usr/bin/env python3
"""皇冠＋足破 · 資料完整率及錯誤分層報告(唯讀診斷)。

This module is a strictly read-only diagnostic.  It opens the immutable
learning SQLite database in SQLite read-only mode, never writes to it, never
touches a prediction, grade, result, ledger, stake, or notification, and never
trains or applies a model.  Its only outputs are aggregate JSON artifacts.

Counting policy
---------------
* The **primary sample is the unique fixture** (``system`` + ``fixture_id``).
  首預 / T-30 / T-5 rows for one match are the *same* fixture and are never
  counted as independent fixtures.
* Stage rows and market rows are preserved as **secondary reference context**
  only, and are always labelled as such in the artifact.
* Every metric is aggregated from raw rows (``sum(x) / count``).  A mean of
  per-slice means is never computed.
* Only immutable pre-kickoff snapshots are eligible wherever model
  performance is evaluated.  Quarantined post-kickoff attempts are counted as
  a data-quality issue and excluded from every metric.
* No result field ever enters a feature/slice key.  Slice keys come from the
  pre-kickoff payload only (market, stage, league, selection side,
  confidence).
"""
from __future__ import annotations

import argparse
import fcntl
import json
import math
import os
import sqlite3
import tempfile
from collections import Counter, defaultdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Sequence

SCHEMA_VERSION = 2
REPORT_NAME = "data_health"
SYSTEMS: tuple[str, ...] = ("footbreak", "crown")
COMPARABLE_MODEL_VERSION = {
    "footbreak": "2026-08-10-market-learning-v2",
    "crown": "2026-08-12-hkjc-corner-forecast-v4",
}
MARKETS: tuple[str, ...] = ("HDC", "HIL", "CHL")
STAGES: tuple[str, ...] = ("首預", "T-30", "T-5")
STAGE_ORDER = {stage: index for index, stage in enumerate(STAGES)}

# Result-blind rule for the separate primary diagnostic: keep the latest
# available immutable pre-kickoff stage for each fixture/market/selection.
PRIMARY_STAGE_PRIORITY: tuple[str, ...] = ("T-5", "T-30", "首預")

# Accuracy, Brier and log loss are aggregated over graded *prediction rows*.
# Unique fixtures are only the sample-size basis; the same fixture contributes
# several correlated stage rows, so these labels must never be conflated.
METRIC_UNIT_ALL_STAGES = "graded_prediction_rows"
METRIC_UNIT_LATEST_STAGE = "graded_prediction_rows_latest_stage_per_fixture_market"
SAMPLE_BASIS = "unique_fixtures"

# Minimum unique fixtures before a slice may be read as anything other than
# "insufficient".  Recommendations are never derived below this threshold.
MIN_UNIQUE_FIXTURES = 30

# Existing production grace policy, mirrored (not re-defined) here:
#   crown.common.SETTLE_AFTER_SECONDS = 105 * 60
#   crown.prediction_history._CORNER_RESULT_RETRY_DAYS = 7
SETTLE_GRACE_SECONDS = 105 * 60
CORNER_RETRY_DAYS = 7

CONFIDENCE_BUCKETS: tuple[tuple[str, float | None, float | None], ...] = (
    ("<50", None, 50.0),
    ("50-57", 50.0, 58.0),
    ("58-64", 58.0, 65.0),
    ("65-74", 65.0, 75.0),
    (">=75", 75.0, None),
)
UNKNOWN = "__未知__"

MARKET_LABELS = {"HDC": "讓球", "HIL": "入球大小", "CHL": "總角球大小"}
SIDE_LABELS = {"H": "主隊", "A": "客隊", "L": "大", "S": "細", "O": "大", "U": "細"}

# Feature families checked for HIL v4 diagnostics.  Each entry lists the
# pre-kickoff payload paths that make the family usable.  Result fields are
# deliberately absent: a result field can never become a feature.
FEATURE_FAMILIES: tuple[dict[str, Any], ...] = (
    {
        "id": "market_line_price",
        "label": "盤口線及賠率",
        "paths": (("market", "line_or_condition"), ("market", "odds")),
        "critical": True,
    },
    {
        "id": "model_probability",
        "label": "模型機率",
        "paths": (("market", "probability"),),
        "critical": True,
    },
    {
        "id": "stage_movement",
        "label": "階段間盤口移動",
        "paths": (("payload", "movement"),),
        "critical": False,
    },
    {
        "id": "sharp_reference",
        "label": "銳利盤參考",
        "paths": (("payload", "sharp_reference_available"), ("payload", "market_sources")),
        "critical": False,
    },
    {
        "id": "league_context",
        "label": "聯賽情境",
        "paths": (("payload", "league"),),
        "critical": True,
    },
    {
        "id": "weather",
        "label": "天氣",
        "paths": (("payload", "info", "weather"),),
        "critical": False,
    },
    {
        "id": "team_news",
        "label": "陣容／傷患消息",
        "paths": (("payload", "info", "news"),),
        "critical": False,
    },
    {
        "id": "hk_pool_movement",
        "label": "香港彩池走勢",
        "paths": (("payload", "info", "hk_lines"), ("payload", "info", "hk_max_move_pct")),
        "critical": False,
    },
    {
        "id": "wdl_context",
        "label": "主客和機率情境",
        "paths": (("payload", "outcome"),),
        "critical": False,
    },
    {
        "id": "corner_independent_source",
        "label": "角球獨立資料源",
        "paths": (("payload", "corner_source"),),
        "critical": False,
    },
)


# ────────────────────────── small numeric helpers ──────────────────────────


def finite(value: Any) -> float | None:
    """Return a finite float, or None for missing/NaN/Infinity/garbage."""
    if isinstance(value, bool) or value is None or value == "":
        return None
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if math.isfinite(number) else None


def is_nonfinite_number(value: Any) -> bool:
    """True only when the value is numeric-looking but NaN or Infinity."""
    if isinstance(value, bool) or value is None:
        return False
    if isinstance(value, (int, float)):
        return not math.isfinite(float(value))
    if isinstance(value, str):
        text = value.strip().lower().lstrip("+-")
        return text in {"nan", "inf", "infinity"}
    return False


def parse_line(value: Any) -> float | None:
    """Parse a whole or split Asian line ('0.5', '0/0.5') without inventing data."""
    direct = finite(value)
    if direct is not None:
        return direct
    if not isinstance(value, str) or "/" not in value:
        return None
    parts = [finite(part.strip()) for part in value.split("/")]
    if len(parts) != 2 or any(part is None for part in parts):
        return None
    return (float(parts[0]) + float(parts[1])) / 2.0


def parse_timestamp(value: Any) -> datetime | None:
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except (TypeError, ValueError):
        return None
    if parsed.tzinfo is None:
        return parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def ratio(numerator: int, denominator: int) -> float | None:
    """Coverage ratio from raw counts.  None when there is nothing to divide."""
    if denominator <= 0:
        return None
    return round(numerator / denominator, 6)


def wilson_interval(successes: int, total: int, z: float = 1.959963984540054) -> list[float] | None:
    """Wilson score 95% interval for a hit rate; None when undefined."""
    if total <= 0 or successes < 0 or successes > total:
        return None
    proportion = successes / total
    denominator = 1.0 + (z * z) / total
    centre = proportion + (z * z) / (2 * total)
    spread = z * math.sqrt((proportion * (1 - proportion) + (z * z) / (4 * total)) / total)
    low = (centre - spread) / denominator
    high = (centre + spread) / denominator
    return [round(max(0.0, low), 6), round(min(1.0, high), 6)]


def confidence_bucket(value: Any) -> str:
    conviction = finite(value)
    if conviction is None:
        return UNKNOWN
    for label, low, high in CONFIDENCE_BUCKETS:
        if (low is None or conviction >= low) and (high is None or conviction < high):
            return label
    return UNKNOWN


# ────────────────────────── read-only source access ──────────────────────────


class ReadOnlyLearningSource:
    """Read-only accessor over the immutable learning SQLite database.

    The connection is opened with ``mode=ro`` so that even a programming
    mistake cannot write, migrate, or vacuum the production database.
    """

    def __init__(self, path: str | Path) -> None:
        self.path = Path(path)
        uri = f"file:{self.path.as_posix()}?mode=ro"
        self._connection = sqlite3.connect(uri, uri=True, timeout=30)
        self._connection.row_factory = sqlite3.Row
        self._connection.execute("PRAGMA query_only = 1")

    def __enter__(self) -> "ReadOnlyLearningSource":
        return self

    def __exit__(self, *_: Any) -> None:
        self.close()

    def close(self) -> None:
        if self._connection is not None:
            self._connection.close()
            self._connection = None  # type: ignore[assignment]

    def snapshot_rows(
        self, system: str, model_version: str | None = None,
    ) -> list[sqlite3.Row]:
        """Canonical pre-kickoff snapshot per (fixture, stage), deterministic order."""
        return self._connection.execute(
            """
            WITH ranked AS (
                SELECT snapshot_id, fixture_id, stage, attempt, generated_at,
                       kickoff, payload_json, model_version, schema_version,
                       ROW_NUMBER() OVER (
                           PARTITION BY fixture_id, stage
                           ORDER BY generated_at DESC, snapshot_id DESC
                       ) AS snapshot_rank
                FROM prediction_snapshots
                WHERE system = ? AND pre_kickoff = 1
                  AND (? IS NULL OR model_version = ?)
                  AND NOT EXISTS (
                      SELECT 1
                      FROM stage_snapshot_reconciliations AS reconciliation
                      WHERE reconciliation.system = prediction_snapshots.system
                        AND reconciliation.fixture_id = prediction_snapshots.fixture_id
                        AND reconciliation.stage = prediction_snapshots.stage
                        AND reconciliation.canonical_snapshot_id
                            != prediction_snapshots.snapshot_id
                  )
            )
            SELECT * FROM ranked WHERE snapshot_rank = 1
            ORDER BY kickoff, fixture_id, stage, snapshot_id
            """,
            (system, model_version, model_version),
        ).fetchall()

    def stage_attempt_counts(self, system: str) -> list[sqlite3.Row]:
        """Unreconciled active duplicate stage keys, never market selections."""
        return self._connection.execute(
            """
            SELECT fixture_id, stage, COUNT(*) AS attempts
            FROM prediction_snapshots
            WHERE system = ? AND pre_kickoff = 1
              AND NOT EXISTS (
                  SELECT 1
                  FROM stage_snapshot_reconciliations AS reconciliation
                  WHERE reconciliation.system = prediction_snapshots.system
                    AND reconciliation.fixture_id = prediction_snapshots.fixture_id
                    AND reconciliation.stage = prediction_snapshots.stage
                    AND reconciliation.canonical_snapshot_id
                        != prediction_snapshots.snapshot_id
              )
            GROUP BY fixture_id, stage
            HAVING COUNT(*) > 1
            ORDER BY fixture_id, stage
            """,
            (system,),
        ).fetchall()

    def quarantined_counts(self, system: str) -> tuple[int, int]:
        row = self._connection.execute(
            """
            SELECT COUNT(*) AS rows_total,
                   COUNT(DISTINCT fixture_id) AS fixtures
            FROM prediction_snapshots
            WHERE system = ? AND pre_kickoff = 0
            """,
            (system,),
        ).fetchone()
        return int(row["rows_total"] or 0), int(row["fixtures"] or 0)

    def result_rows(self, system: str) -> list[sqlite3.Row]:
        """Latest immutable result attempt per fixture."""
        return self._connection.execute(
            """
            WITH ranked AS (
                SELECT fixture_id, home_score, away_score, home_corners,
                       away_corners, terminal_status, provenance_json,
                       observed_at,
                       ROW_NUMBER() OVER (
                           PARTITION BY fixture_id
                           ORDER BY result_attempt DESC, result_id DESC
                       ) AS result_rank
                FROM results
                WHERE system = ?
            )
            SELECT * FROM ranked WHERE result_rank = 1
            ORDER BY fixture_id
            """,
            (system,),
        ).fetchall()

    def grade_rows(
        self, system: str, model_version: str | None = None,
    ) -> list[sqlite3.Row]:
        """Latest grade revision per (pre-kickoff snapshot, market, target)."""
        return self._connection.execute(
            """
            WITH ranked_snapshots AS (
                SELECT snapshot_id, fixture_id, stage,
                       ROW_NUMBER() OVER (
                           PARTITION BY fixture_id, stage
                           ORDER BY generated_at DESC, snapshot_id DESC
                       ) AS snapshot_rank
                FROM prediction_snapshots
                WHERE system = ? AND pre_kickoff = 1
                  AND (? IS NULL OR model_version = ?)
                  AND NOT EXISTS (
                      SELECT 1
                      FROM stage_snapshot_reconciliations AS reconciliation
                      WHERE reconciliation.system = prediction_snapshots.system
                        AND reconciliation.fixture_id = prediction_snapshots.fixture_id
                        AND reconciliation.stage = prediction_snapshots.stage
                        AND reconciliation.canonical_snapshot_id
                            != prediction_snapshots.snapshot_id
                  )
            ),
            ranked_grades AS (
                SELECT grade_id, snapshot_id, market, target, state, metrics_json,
                       ROW_NUMBER() OVER (
                           PARTITION BY snapshot_id, market, target
                           ORDER BY grade_attempt DESC, grade_id DESC
                       ) AS grade_rank
                FROM grades
            )
            SELECT s.fixture_id, s.stage, g.market, g.target, g.state,
                   g.metrics_json, g.grade_id
            FROM ranked_snapshots AS s
            JOIN ranked_grades AS g
              ON g.snapshot_id = s.snapshot_id AND g.grade_rank = 1
            WHERE s.snapshot_rank = 1
            ORDER BY s.fixture_id, s.stage, g.market, g.target, g.grade_id
            """,
            (system, model_version, model_version),
        ).fetchall()


# ────────────────────────── row normalisation ──────────────────────────


def _payload(raw: str) -> tuple[dict[str, Any], bool]:
    try:
        payload = json.loads(raw)
    except (TypeError, ValueError):
        return {}, True
    if not isinstance(payload, dict):
        return {}, True
    return payload, False


def _metrics(raw: str) -> dict[str, Any]:
    try:
        metrics = json.loads(raw)
    except (TypeError, ValueError):
        return {}
    return metrics if isinstance(metrics, dict) else {}


def _grade_index(rows: Sequence[sqlite3.Row]) -> dict[tuple[str, str, str, str], dict[str, Any]]:
    index: dict[tuple[str, str, str, str], dict[str, Any]] = {}
    for row in rows:
        key = (
            str(row["fixture_id"]),
            str(row["stage"]),
            str(row["market"]),
            str(row["target"]),
        )
        index[key] = {
            "state": str(row["state"] or ""),
            "metrics": _metrics(row["metrics_json"]),
        }
    return index


def build_market_rows(
    snapshots: Sequence[sqlite3.Row],
    grades: Sequence[sqlite3.Row],
) -> tuple[list[dict[str, Any]], dict[str, int]]:
    """Flatten immutable pre-kickoff snapshots into one row per market pick.

    Returns the rows plus structural counters that describe malformed source
    data.  No result field is ever copied into a row's slice keys.
    """
    grade_index = _grade_index(grades)
    counters: Counter[str] = Counter()
    rows: list[dict[str, Any]] = []
    for snapshot in snapshots:
        payload, malformed = _payload(snapshot["payload_json"])
        fixture_id = str(snapshot["fixture_id"])
        stage = str(snapshot["stage"] or "")
        if malformed:
            counters["malformed_payload_rows"] += 1
        if stage not in STAGE_ORDER:
            counters["invalid_stage_rows"] += 1
        kickoff = parse_timestamp(snapshot["kickoff"])
        generated_at = parse_timestamp(snapshot["generated_at"])
        league = payload.get("league")
        conviction = payload.get("conviction")
        predictions = payload.get("market_predictions")
        if not isinstance(predictions, list):
            predictions = []
            if payload:
                counters["stage_rows_without_market_predictions"] += 1
        seen_keys: set[tuple[str, str]] = set()
        for prediction in predictions:
            if not isinstance(prediction, dict):
                counters["malformed_market_prediction_rows"] += 1
                continue
            code = str(prediction.get("code") or "")
            if code not in MARKETS:
                counters["unsupported_market_rows"] += 1
                continue
            side = prediction.get("side")
            condition = prediction.get("condition")
            line_raw = prediction.get("line", condition)
            target_key = f"{condition}|{side}"
            if (code, target_key) in seen_keys:
                counters["duplicate_market_keys_in_stage"] += 1
                continue
            seen_keys.add((code, target_key))
            grade = grade_index.get((fixture_id, stage, code, target_key), {})
            probability = finite(prediction.get("probability"))
            line = parse_line(line_raw)
            odds = finite(prediction.get("odds"))
            nonfinite = sum(
                is_nonfinite_number(prediction.get(field))
                for field in ("probability", "odds", "line", "condition")
            )
            if nonfinite:
                counters["nonfinite_prediction_values"] += nonfinite
            rows.append({
                "fixture_id": fixture_id,
                "stage": stage if stage in STAGE_ORDER else UNKNOWN,
                "kickoff": kickoff,
                "generated_at": generated_at,
                "market": code,
                "target_key": target_key,
                "league": str(league).strip() if isinstance(league, str) and league.strip() else UNKNOWN,
                "side": str(side) if side not in (None, "") else UNKNOWN,
                "confidence_bucket": confidence_bucket(conviction),
                "conviction": finite(conviction),
                "probability": probability,
                "probability_valid": probability is not None and 0.0 <= probability <= 1.0,
                "line": line,
                "odds": odds,
                "source": prediction.get("source"),
                "provider": prediction.get("provider"),
                "grade_state": grade.get("state", ""),
                "grade_metrics": grade.get("metrics", {}),
                "payload": payload,
            })
    rows.sort(key=lambda row: (
        row["kickoff"] or datetime.max.replace(tzinfo=timezone.utc),
        row["fixture_id"],
        STAGE_ORDER.get(row["stage"], 99),
        row["market"],
        row["target_key"],
    ))
    return rows, dict(counters)


# ────────────────────────── metric aggregation ──────────────────────────


def aggregate_metrics(rows: Sequence[dict[str, Any]]) -> dict[str, Any]:
    """Aggregate graded rows from raw values only — never a mean of means.

    ``brier`` and ``log_loss`` are recomputed from the immutable probability
    and settlement target so that a stored, rounded, or non-finite metric can
    never distort the aggregate.
    """
    decided = hits = pushes = 0
    brier_total = 0.0
    brier_rows = 0
    log_loss_total = 0.0
    log_loss_rows = 0
    graded_rows = 0
    for row in rows:
        if row.get("grade_state") != "GRADED":
            continue
        graded_rows += 1
        metrics = row.get("grade_metrics") or {}
        hit = metrics.get("hit")
        if hit is None:
            pushes += 1
        else:
            decided += 1
            hits += int(bool(hit))
        probability = row.get("probability")
        target = finite(metrics.get("target"))
        if (
            probability is None
            or not 0.0 <= probability <= 1.0
            or target is None
            or not 0.0 <= target <= 1.0
        ):
            continue
        clipped = min(1.0 - 1e-6, max(1e-6, probability))
        brier_total += (clipped - target) ** 2
        brier_rows += 1
        log_loss_total += -(target * math.log(clipped) + (1 - target) * math.log(1 - clipped))
        log_loss_rows += 1
    accuracy = round(hits / decided, 6) if decided else None
    return {
        "graded_rows": graded_rows,
        "decided_rows": decided,
        "hits": hits,
        "pushes": pushes,
        "accuracy": accuracy,
        "accuracy_ci95": wilson_interval(hits, decided) if decided else None,
        "brier": round(brier_total / brier_rows, 6) if brier_rows else None,
        "brier_rows": brier_rows,
        "log_loss": round(log_loss_total / log_loss_rows, 6) if log_loss_rows else None,
        "log_loss_rows": log_loss_rows,
    }


def has_repeated_stage_rows(rows: Sequence[dict[str, Any]]) -> bool:
    """True when one fixture contributes several graded rows to one selection.

    Those rows are the same bet measured at 首預/T-30/T-5 and are therefore
    correlated repeated measures, not independent samples.  Two *different*
    markets on the same fixture are not counted here: they are distinct bets.
    """
    seen: set[tuple[str, str, str]] = set()
    for row in rows:
        if row.get("grade_state") != "GRADED":
            continue
        key = (str(row["fixture_id"]), str(row["market"]), str(row["target_key"]))
        if key in seen:
            return True
        seen.add(key)
    return False


def latest_stage_rows(rows: Sequence[dict[str, Any]]) -> list[dict[str, Any]]:
    """Keep one row per fixture/market/selection: the latest pre-kickoff stage.

    This is the separate *primary* diagnostic unit.  Because a fixture no
    longer contributes several correlated stage rows to the same market and
    selection, metrics computed over these rows are close to one-per-fixture
    per market rather than a repeated-measures aggregate.  The rule is
    result-blind: it looks only at the stage name and the prediction time.
    """
    priority = {stage: index for index, stage in enumerate(PRIMARY_STAGE_PRIORITY)}
    best: dict[tuple[str, str, str], tuple[tuple, dict[str, Any]]] = {}
    for row in rows:
        rank = priority.get(str(row["stage"]))
        if rank is None:
            # An unknown stage cannot be ordered, so it is kept on its own key
            # instead of being silently promoted over a real stage.
            rank = len(priority)
        generated = row.get("generated_at")
        order = (
            rank,
            -(generated.timestamp() if generated is not None else float("-inf")),
            str(row["stage"]),
        )
        key = (str(row["fixture_id"]), str(row["market"]), str(row["target_key"]))
        current = best.get(key)
        if current is None or order < current[0]:
            best[key] = (order, row)
    output = [row for _order, row in best.values()]
    output.sort(key=lambda row: (
        row["kickoff"] or datetime.max.replace(tzinfo=timezone.utc),
        row["fixture_id"],
        row["market"],
        row["target_key"],
    ))
    return output


def slice_summary(
    dimension: str,
    key: str,
    label: str,
    rows: Sequence[dict[str, Any]],
    total_unique_fixtures: int,
    metric_unit: str = METRIC_UNIT_ALL_STAGES,
) -> dict[str, Any]:
    fixtures = {row["fixture_id"] for row in rows}
    graded_fixtures = {row["fixture_id"] for row in rows if row.get("grade_state") == "GRADED"}
    metrics = aggregate_metrics(rows)
    unique_fixtures = len(graded_fixtures)
    sufficient = unique_fixtures >= MIN_UNIQUE_FIXTURES
    return {
        "dimension": dimension,
        "key": key,
        "label": label,
        "unique_fixtures": unique_fixtures,
        "unique_fixtures_all_states": len(fixtures),
        "rows": len(rows),
        "sample_status": "sufficient" if sufficient else "insufficient",
        "small_sample": not sufficient,
        "minimum_unique_fixtures": MIN_UNIQUE_FIXTURES,
        "coverage_share": ratio(unique_fixtures, total_unique_fixtures) or 0.0,
        # Metrics aggregate graded prediction rows, not fixtures.  Unique
        # fixtures are only the sample-size basis used for the threshold.
        "sample_basis": SAMPLE_BASIS,
        "metric_unit": metric_unit,
        "correlated_stage_rows": has_repeated_stage_rows(rows),
        **metrics,
    }


def _label_for(dimension: str, key: str) -> str:
    if key == UNKNOWN:
        return "未知／缺失"
    if dimension == "market":
        return MARKET_LABELS.get(key, key)
    if dimension == "direction":
        return SIDE_LABELS.get(key, key)
    return key


def build_slices(
    rows: Sequence[dict[str, Any]],
    total_unique_fixtures: int,
    metric_unit: str = METRIC_UNIT_ALL_STAGES,
) -> dict[str, list[dict[str, Any]]]:
    """Deterministically ordered error slices over pre-kickoff keys only."""
    dimensions = {
        "market": lambda row: row["market"],
        "stage": lambda row: row["stage"],
        "league": lambda row: row["league"],
        "direction": lambda row: row["side"],
        "confidence": lambda row: row["confidence_bucket"],
    }
    # Markets and stages always appear, even at zero rows, so the dashboard
    # never silently drops a market that stopped producing data.
    always_present = {"market": MARKETS, "stage": STAGES}
    output: dict[str, list[dict[str, Any]]] = {}
    for dimension, getter in dimensions.items():
        grouped: dict[str, list[dict[str, Any]]] = defaultdict(list)
        for key in always_present.get(dimension, ()):
            grouped[key] = []
        for row in rows:
            grouped[str(getter(row))].append(row)
        summaries = [
            slice_summary(
                dimension, key, _label_for(dimension, key), items,
                total_unique_fixtures, metric_unit,
            )
            for key, items in grouped.items()
        ]
        summaries.sort(key=_slice_order(dimension))
        output[dimension] = summaries
    return output


def _slice_order(dimension: str):
    if dimension == "market":
        order = {market: index for index, market in enumerate(MARKETS)}
        return lambda item: (order.get(item["key"], 99), item["key"])
    if dimension == "stage":
        return lambda item: (STAGE_ORDER.get(item["key"], 99), item["key"])
    if dimension == "confidence":
        order = {label: index for index, (label, _, _) in enumerate(CONFIDENCE_BUCKETS)}
        return lambda item: (order.get(item["key"], 99), item["key"])
    # League and direction: most unique fixtures first, then a stable key.
    return lambda item: (-item["unique_fixtures"], -item["rows"], item["key"])


# ────────────────────────── completeness ──────────────────────────


def _has_value(container: Any, *path: str) -> bool:
    value = container
    for key in path:
        if not isinstance(value, dict):
            return False
        value = value.get(key)
    if value is None or value == "" or value == [] or value == {}:
        return False
    return True


def _feature_family_present(row: dict[str, Any], family: dict[str, Any]) -> bool:
    for scope, *path in family["paths"]:
        if scope == "market":
            field = path[0]
            if field == "line_or_condition":
                if row.get("line") is not None:
                    return True
            elif row.get(field) is not None:
                return True
        elif scope == "payload":
            if _has_value(row.get("payload"), *path):
                return True
    return False


def completeness_for(
    rows: Sequence[dict[str, Any]],
    results: dict[str, sqlite3.Row],
    now: datetime,
    *,
    market: str | None = None,
) -> dict[str, Any]:
    """Completeness counters for a system, or for one market inside it."""
    subset = [row for row in rows if market is None or row["market"] == market]
    fixtures = sorted({row["fixture_id"] for row in subset})
    stage_keys = {(row["fixture_id"], row["stage"]) for row in subset}
    graded = sum(row["grade_state"] == "GRADED" for row in subset)
    excluded = sum(
        bool(row["grade_state"]) and row["grade_state"] != "GRADED" for row in subset
    )
    pending = sum(not row["grade_state"] for row in subset)

    settle_due_fixtures = sorted({
        row["fixture_id"]
        for row in subset
        if row["kickoff"] is not None
        and (now - row["kickoff"]).total_seconds() >= SETTLE_GRACE_SECONDS
    })
    with_result = [fixture for fixture in settle_due_fixtures if fixture in results]
    stale_unresolved = [fixture for fixture in settle_due_fixtures if fixture not in results]

    corner_rows = [row for row in subset if row["market"] == "CHL"]
    corner_due_fixtures = sorted({
        row["fixture_id"]
        for row in corner_rows
        if row["kickoff"] is not None
        and (now - row["kickoff"]).total_seconds() >= SETTLE_GRACE_SECONDS
    })
    corner_graded_fixtures = {
        row["fixture_id"] for row in corner_rows if row["grade_state"] == "GRADED"
    }
    corner_covered = [f for f in corner_due_fixtures if f in corner_graded_fixtures]
    corner_missing = [f for f in corner_due_fixtures if f not in corner_graded_fixtures]
    corner_stale = [
        fixture
        for fixture in corner_missing
        if any(
            row["fixture_id"] == fixture
            and row["kickoff"] is not None
            and (now - row["kickoff"]).total_seconds() > CORNER_RETRY_DAYS * 86400
            for row in corner_rows
        )
    ]

    missing = {
        "probability": sum(not row["probability_valid"] for row in subset),
        "line": sum(row["line"] is None for row in subset),
        "odds": sum(row["odds"] is None for row in subset),
        "selection_side": sum(row["side"] == UNKNOWN for row in subset),
        "league": sum(row["league"] == UNKNOWN for row in subset),
        "stage": sum(row["stage"] == UNKNOWN for row in subset),
        "source": sum(not row.get("source") for row in subset),
        "provider": sum(not row.get("provider") for row in subset),
        "result": len(stale_unresolved),
        "corner_total": len(corner_missing),
    }

    grade_reasons = Counter(
        str((row["grade_metrics"] or {}).get("reason") or "unspecified")
        for row in subset
        if row["grade_state"] and row["grade_state"] != "GRADED"
    )
    result_sources = Counter(
        _result_source(results[fixture]) for fixture in with_result
    )

    return {
        "unique_fixtures": len(fixtures),
        "stage_rows": len(stage_keys),
        "prediction_rows": len(subset),
        "graded_rows": graded,
        "pending_rows": pending,
        "excluded_rows": excluded,
        "result": {
            "settle_due_fixtures": len(settle_due_fixtures),
            "fixtures_with_result": len(with_result),
            "coverage": ratio(len(with_result), len(settle_due_fixtures)),
            "stale_unresolved_fixtures": len(stale_unresolved),
            "grace_minutes": SETTLE_GRACE_SECONDS // 60,
        },
        "corner_result": {
            "corner_prediction_fixtures": len({row["fixture_id"] for row in corner_rows}),
            "settle_due_fixtures": len(corner_due_fixtures),
            "fixtures_with_corner_result": len(corner_covered),
            "coverage": ratio(len(corner_covered), len(corner_due_fixtures)),
            "missing_fixtures": len(corner_missing),
            "stale_beyond_retry_fixtures": len(corner_stale),
            "retry_days": CORNER_RETRY_DAYS,
        },
        "missing_or_invalid": dict(sorted(missing.items())),
        "exclusion_reasons": dict(sorted(grade_reasons.items())),
        "result_sources": dict(sorted(result_sources.items())),
    }


def _result_source(row: sqlite3.Row) -> str:
    try:
        provenance = json.loads(row["provenance_json"])
    except (TypeError, ValueError):
        return "unparsable_provenance"
    if isinstance(provenance, dict):
        source = provenance.get("source")
        if isinstance(source, str) and source.strip():
            return source.strip()
    return "unspecified"


# ────────────────────────── issues ──────────────────────────


def _issue(code: str, severity: str, label: str, scope: str, count: int, detail: str = "") -> dict[str, Any]:
    return {
        "code": code,
        "severity": severity,
        "scope": scope,
        "label": label,
        "count": int(count),
        "detail": detail,
    }


def build_issues(
    completeness: dict[str, Any],
    by_market: dict[str, dict[str, Any]],
    structural: dict[str, int],
    quarantined_rows: int,
    quarantined_fixtures: int,
) -> list[dict[str, Any]]:
    issues: list[dict[str, Any]] = []
    overall = completeness

    if quarantined_rows:
        issues.append(_issue(
            "post_kickoff_quarantined_rows", "high", "開賽後才寫入的預測(已隔離,不計入評估)",
            "overall", quarantined_rows, f"涉及 {quarantined_fixtures} 場獨立賽事",
        ))
    for key, severity, label in (
        ("malformed_payload_rows", "high", "無法解析的預測 payload"),
        ("nonfinite_prediction_values", "high", "NaN／無限值欄位"),
        ("duplicate_market_keys_in_stage", "high", "同一階段重複的市場鍵"),
        ("invalid_stage_rows", "high", "階段值不合法"),
        ("malformed_market_prediction_rows", "warn", "格式錯誤的市場預測列"),
        ("stage_rows_without_market_predictions", "warn", "冇市場預測的階段列"),
        ("unsupported_market_rows", "info", "非 HDC／HIL／CHL 市場列"),
    ):
        count = int(structural.get(key) or 0)
        if count:
            issues.append(_issue(key, severity, label, "overall", count))

    stale = overall["result"]["stale_unresolved_fixtures"]
    if stale:
        issues.append(_issue(
            "stale_unresolved_results", "high", "過了結算寬限期仍然冇賽果",
            "overall", stale, f"寬限期 {overall['result']['grace_minutes']} 分鐘",
        ))
    corner_stale = overall["corner_result"]["stale_beyond_retry_fixtures"]
    if corner_stale:
        issues.append(_issue(
            "stale_missing_corner_results", "high", "超過重試期仍然缺角球賽果",
            "market:CHL", corner_stale, f"重試期 {CORNER_RETRY_DAYS} 日",
        ))
    corner_missing = overall["corner_result"]["missing_fixtures"]
    if corner_missing and corner_missing != corner_stale:
        issues.append(_issue(
            "missing_corner_results", "warn", "缺角球賽果(仍在重試期內)",
            "market:CHL", corner_missing - corner_stale,
        ))

    for market, payload in by_market.items():
        for field, severity, label in (
            ("probability", "high", "缺失或不合法機率"),
            ("line", "high", "缺失盤口線"),
            ("odds", "warn", "缺失賠率"),
            ("selection_side", "high", "缺失選擇方向"),
            ("source", "warn", "缺失資料來源標記"),
            ("provider", "info", "缺失供應商標記"),
            ("league", "warn", "缺失聯賽"),
            ("stage", "high", "缺失階段"),
        ):
            count = int(payload["missing_or_invalid"].get(field) or 0)
            if count:
                issues.append(_issue(
                    f"missing_{field}", severity, label, f"market:{market}", count,
                ))
    issues.sort(key=lambda item: (
        {"high": 0, "warn": 1, "info": 2}.get(item["severity"], 3),
        -item["count"],
        item["scope"],
        item["code"],
    ))
    return issues


# ────────────────────────── HIL v4 diagnostics ──────────────────────────


def feature_family_coverage(rows: Sequence[dict[str, Any]], market: str) -> list[dict[str, Any]]:
    subset = [row for row in rows if row["market"] == market]
    output = []
    for family in FEATURE_FAMILIES:
        present = sum(_feature_family_present(row, family) for row in subset)
        output.append({
            "id": family["id"],
            "label": family["label"],
            "critical": bool(family["critical"]),
            "rows": len(subset),
            "present_rows": present,
            "coverage": ratio(present, len(subset)),
        })
    output.sort(key=lambda item: (item["coverage"] if item["coverage"] is not None else -1.0, item["id"]))
    return output


def hil_v4_diagnostics(
    rows: Sequence[dict[str, Any]],
    slices: dict[str, list[dict[str, Any]]],
    baseline: dict[str, Any],
) -> dict[str, Any]:
    """Conservative, diagnostic-only observations.  No model, no retraining.

    ``slices`` and ``baseline`` must come from the latest-stage-per-fixture
    primary diagnostic.  Repeated stage rows of the same fixture are correlated
    and must never be counted as independent evidence for a recommendation;
    the sufficiency threshold still counts unique fixtures.
    """
    families = feature_family_coverage(rows, "HIL")
    missing_families = [
        family for family in families
        if family["coverage"] is None or family["coverage"] < 0.5
    ]

    baseline_accuracy = baseline.get("accuracy")
    baseline_brier = baseline.get("brier")
    stable: list[dict[str, Any]] = []
    for dimension, items in slices.items():
        for item in items:
            if item["sample_status"] != "sufficient":
                continue
            accuracy_gap = (
                None if item["accuracy"] is None or baseline_accuracy is None
                else round(item["accuracy"] - baseline_accuracy, 6)
            )
            brier_gap = (
                None if item["brier"] is None or baseline_brier is None
                else round(item["brier"] - baseline_brier, 6)
            )
            if accuracy_gap is None and brier_gap is None:
                continue
            effect = max(
                0.0 if accuracy_gap is None else -accuracy_gap,
                0.0 if brier_gap is None else brier_gap,
            )
            if effect <= 0.0:
                continue
            # Rank by coverage AND effect size, never by raw noisy accuracy.
            stable.append({
                "dimension": dimension,
                "key": item["key"],
                "label": item["label"],
                "unique_fixtures": item["unique_fixtures"],
                "rows": item["rows"],
                "graded_rows": item["graded_rows"],
                "metric_unit": item.get("metric_unit", METRIC_UNIT_LATEST_STAGE),
                "correlated_stage_rows": item.get("correlated_stage_rows", False),
                "coverage_share": item["coverage_share"],
                "accuracy": item["accuracy"],
                "accuracy_ci95": item["accuracy_ci95"],
                "accuracy_gap_vs_baseline": accuracy_gap,
                "brier": item["brier"],
                "brier_gap_vs_baseline": brier_gap,
                "effect_size": round(effect, 6),
                "priority_score": round(effect * math.sqrt(item["coverage_share"] or 0.0), 6),
            })
    stable.sort(key=lambda item: (
        -item["priority_score"], -item["unique_fixtures"], item["dimension"], item["key"]
    ))

    recommendations: list[dict[str, Any]] = []
    for family in missing_families:
        if not family["critical"] and (family["coverage"] or 0.0) >= 0.2:
            continue
        recommendations.append({
            "id": f"feature_family:{family['id']}",
            "kind": "feature_coverage",
            "priority": "high" if family["critical"] else "medium",
            "title": f"HIL 缺少特徵族:{family['label']}",
            "detail": (
                f"HIL 預測列之中只有 {family['present_rows']}／{family['rows']} 行有呢個特徵族。"
                "先補資料覆蓋率,再考慮任何 v4 特徵設計。呢個只係覆蓋率觀察,唔代表因果。"
            ),
            "evidence": family,
        })
    for item in stable[:5]:
        recommendations.append({
            "id": f"slice:{item['dimension']}:{item['key']}",
            "kind": "weak_slice",
            "priority": "medium",
            "title": f"表現最弱且樣本足夠的切面:{item['label']}({item['dimension']})",
            "detail": (
                f"{item['unique_fixtures']} 場獨立賽事(≥{MIN_UNIQUE_FIXTURES} 場門檻),"
                f"命中率 {'—' if item['accuracy'] is None else round(item['accuracy'] * 100, 1)}%,"
                f"與整體差距 {item['accuracy_gap_vs_baseline']}。"
                "只係關聯觀察,並唔代表因果,亦唔構成任何自動調整。"
            ),
            "evidence": item,
        })
    recommendations.sort(key=lambda item: (
        {"high": 0, "medium": 1, "low": 2}.get(item["priority"], 3), item["id"]
    ))
    return {
        "scope": "HIL",
        "auto_apply": False,
        "retraining": False,
        "is_model": False,
        "minimum_unique_fixtures": MIN_UNIQUE_FIXTURES,
        "evidence_unit": METRIC_UNIT_LATEST_STAGE,
        "evidence_sample_basis": SAMPLE_BASIS,
        "evidence_uses_repeated_stage_rows": False,
        "baseline": baseline,
        "feature_families": families,
        "missing_feature_families": [family["id"] for family in missing_families],
        "worst_stable_slices": stable[:10],
        "recommendations": recommendations,
        "notes": [
            "本節只係診斷:唔會自動套用、唔會重訓、唔會改動任何預測或注碼。",
            f"樣本少於 {MIN_UNIQUE_FIXTURES} 場獨立賽事的切面唔會用嚟做任何建議。",
            "所有排序按資料覆蓋率＋效應量,唔係按原始命中率;所有觀察皆為關聯,非因果。",
            "證據一律取自「每場每市場最新階段」主要診斷:同一場嘅重複階段列彼此相關,"
            "絕對唔會當作獨立證據;門檻仍然以獨立賽事數計算。",
            "準確率／Brier／對數損失嘅單位係已結算預測列,唔係每場一行。",
        ],
    }


# ────────────────────────── report assembly ──────────────────────────


def build_system_report(
    source: ReadOnlyLearningSource,
    system: str,
    now: datetime,
) -> dict[str, Any]:
    model_version = COMPARABLE_MODEL_VERSION[system]
    # Match each dashboard's visible model era.  The two all-history counts
    # below are retained solely for reconciliation and never blended into
    # comparable metrics.
    snapshots = source.snapshot_rows(system, model_version)
    grades = source.grade_rows(system, model_version)
    all_history_snapshots = source.snapshot_rows(system)
    all_history_grades = source.grade_rows(system)
    results = {str(row["fixture_id"]): row for row in source.result_rows(system)}
    quarantined_rows, quarantined_fixtures = source.quarantined_counts(system)
    duplicate_stage_keys = source.stage_attempt_counts(system)

    rows, structural = build_market_rows(snapshots, grades)
    structural = dict(structural)
    structural["duplicate_stage_keys"] = len(duplicate_stage_keys)

    unique_fixtures = len({str(row["fixture_id"]) for row in snapshots})
    stage_rows = len(snapshots)

    overall = completeness_for(rows, results, now)
    overall["snapshot_unique_fixtures"] = unique_fixtures
    overall["snapshot_stage_rows"] = stage_rows
    overall["quarantined_post_kickoff_rows"] = quarantined_rows
    overall["quarantined_post_kickoff_fixtures"] = quarantined_fixtures
    overall["duplicate_stage_keys"] = len(duplicate_stage_keys)
    overall["structural_issues"] = dict(sorted(structural.items()))
    overall["all_history_audit"] = {
        "snapshot_rows": len(all_history_snapshots),
        "graded_rows": sum(
            str(row["state"] or "") == "GRADED" for row in all_history_grades
        ),
    }

    by_market = {
        market: completeness_for(rows, results, now, market=market)
        for market in MARKETS
    }
    graded_unique_fixtures = len({
        row["fixture_id"] for row in rows if row["grade_state"] == "GRADED"
    })
    slices = build_slices(rows, graded_unique_fixtures, METRIC_UNIT_ALL_STAGES)
    all_stage_metrics = aggregate_metrics(rows)
    baseline = {
        "unique_fixtures": graded_unique_fixtures,
        "rows": len(rows),
        **all_stage_metrics,
        "sample_basis": SAMPLE_BASIS,
        "metric_unit": METRIC_UNIT_ALL_STAGES,
        "correlated_stage_rows": has_repeated_stage_rows(rows),
        "sample_status": (
            "sufficient" if graded_unique_fixtures >= MIN_UNIQUE_FIXTURES else "insufficient"
        ),
    }

    # Separate primary diagnostic: one row per fixture/market/selection using
    # the latest immutable pre-kickoff stage, so repeated correlated stage
    # rows cannot inflate the evidence behind any recommendation.
    primary_rows = latest_stage_rows(rows)
    primary_graded_fixtures = len({
        row["fixture_id"] for row in primary_rows if row["grade_state"] == "GRADED"
    })
    primary_metrics = aggregate_metrics(primary_rows)
    primary_baseline = {
        "unique_fixtures": primary_graded_fixtures,
        "rows": len(primary_rows),
        **primary_metrics,
        "sample_basis": SAMPLE_BASIS,
        "metric_unit": METRIC_UNIT_LATEST_STAGE,
        "correlated_stage_rows": has_repeated_stage_rows(primary_rows),
        "sample_status": (
            "sufficient" if primary_graded_fixtures >= MIN_UNIQUE_FIXTURES else "insufficient"
        ),
    }
    primary_diagnostic = {
        "unit": METRIC_UNIT_LATEST_STAGE,
        "sample_basis": SAMPLE_BASIS,
        "stage_priority": list(PRIMARY_STAGE_PRIORITY),
        "stage_rule_declared_before_results": True,
        "correlated_stage_rows": primary_baseline["correlated_stage_rows"],
        "label": "主要診斷:每場每市場最新階段",
        "note": (
            "每場每市場每個方向只保留最新一個賽前階段(T-5 > T-30 > 首預),"
            "所以唔會有同一場嘅重複階段列。指標單位仍然係已結算預測列,"
            "一場如果有多個市場／方向,仍會貢獻多過一行。"
        ),
        "baseline": primary_baseline,
        "error_slices": build_slices(
            primary_rows, primary_graded_fixtures, METRIC_UNIT_LATEST_STAGE
        ),
    }
    issues = build_issues(overall, by_market, structural, quarantined_rows, quarantined_fixtures)
    issue_counts = Counter(item["severity"] for item in issues)

    if unique_fixtures == 0:
        status = "no_data"
    elif graded_unique_fixtures < MIN_UNIQUE_FIXTURES:
        status = "insufficient_data"
    elif issue_counts.get("high"):
        status = "degraded"
    elif issue_counts.get("warn"):
        status = "watch"
    else:
        status = "ok"

    return {
        "schema_version": SCHEMA_VERSION,
        "report": REPORT_NAME,
        "system": system,
        "scope": {
            "model_version": model_version,
            "pre_kickoff_only": True,
            "description": "目前模型版本；全歷史只保留於 all_history_audit 作稽核。",
        },
        "generated_at": now.isoformat(),
        "status": status,
        "policy": policy_block(),
        "definitions": definitions_block(),
        "completeness": {"overall": overall, "by_market": by_market},
        "issues": issues,
        "issue_counts": {
            "high": issue_counts.get("high", 0),
            "warn": issue_counts.get("warn", 0),
            "info": issue_counts.get("info", 0),
            "total": len(issues),
        },
        "metrics_policy": metrics_policy_block(),
        "baseline": baseline,
        "error_slices": slices,
        "primary_diagnostic": primary_diagnostic,
        "hil_v4_diagnostics": hil_v4_diagnostics(
            rows, primary_diagnostic["error_slices"], primary_baseline
        ),
    }


def metrics_policy_block() -> dict[str, Any]:
    """State the metric unit explicitly so nothing reads as one-per-fixture."""
    return {
        "sample_basis": SAMPLE_BASIS,
        "metric_unit": METRIC_UNIT_ALL_STAGES,
        "correlated_stage_rows": True,
        "primary_diagnostic_metric_unit": METRIC_UNIT_LATEST_STAGE,
        "stage_rows_are_reference_only": True,
        "metrics_are_one_per_fixture": False,
        "recommendation_evidence_unit": METRIC_UNIT_LATEST_STAGE,
        "note": (
            "準確率、Brier、對數損失都係將已結算預測列逐行相加得出。"
            "同一場賽事嘅首預／T-30／T-5 係高度相關嘅重複量度,唔可以當獨立樣本;"
            "獨立賽事只係樣本量基礎同門檻依據。想睇接近每場一行嘅數字,"
            "請用「主要診斷:每場每市場最新階段」。"
        ),
    }


def policy_block() -> dict[str, Any]:
    return {
        "read_only": True,
        "modifies_predictions": False,
        "modifies_bets_or_staking": False,
        "auto_apply": False,
        "retraining": False,
        "primary_sample": "unique_fixtures",
        "stage_rows_are_reference_only": True,
        "pre_kickoff_only": True,
        "result_fields_used_as_features": False,
        "minimum_unique_fixtures": MIN_UNIQUE_FIXTURES,
        "settle_grace_minutes": SETTLE_GRACE_SECONDS // 60,
        "corner_retry_days": CORNER_RETRY_DAYS,
    }


def definitions_block() -> dict[str, str]:
    return {
        "unique_fixtures": "獨立賽事(系統＋賽事識別碼)。首預／T-30／T-5 同一場只算一場,這是主要樣本單位。",
        "stage_rows": "階段列:每場每階段一行,只作次要參考,唔可以當獨立樣本。",
        "prediction_rows": "市場預測列:每場每階段每個市場方向一行,只作次要參考。",
        "graded_rows": "已結算並可評估的市場預測列。",
        "pending_rows": "未有結算紀錄的市場預測列。",
        "excluded_rows": "有結算紀錄但不適用(例如缺角球賽果)的市場預測列。",
        "result_coverage": "過了結算寬限期而有賽果的獨立賽事比例。",
        "corner_result_coverage": "有 CHL 預測、過了寬限期而有角球結算的獨立賽事比例。",
        "metric_unit": (
            "指標單位:已結算預測列(graded_prediction_rows)。"
            "同一場可以貢獻多於一行,而各行之間互相關聯。"
        ),
        "sample_basis": "樣本量基礎:獨立賽事。只用嚟定樣本夠唔夠,唔係指標嘅單位。",
        "correlated_stage_rows": "true 表示該切面包含同一場嘅多個階段列,彼此相關。",
        "primary_diagnostic": (
            "主要診斷:每場每市場每個方向只取最新賽前階段(T-5 > T-30 > 首預),"
            "消除重複階段列嘅相關性;HIL v4 建議只用這個單位做證據。"
        ),
        "accuracy": (
            "由原始命中列直接相加計出(hits ÷ decided),絕不平均再平均;"
            "單位係已結算預測列,唔係每場一行。"
        ),
        "brier": "由不可變機率同結算目標重算,逐列相加後除以列數。",
        "wilson_ci95": "命中率的 Wilson 95% 區間,只在有已判定列時計算。",
        "small_sample": f"獨立賽事少於 {MIN_UNIQUE_FIXTURES} 場即標示為樣本不足,不用作任何建議。",
    }


def unavailable_report(system: str, now: datetime, reason: str) -> dict[str, Any]:
    return {
        "schema_version": SCHEMA_VERSION,
        "report": REPORT_NAME,
        "system": system,
        "scope": {
            "model_version": COMPARABLE_MODEL_VERSION[system],
            "pre_kickoff_only": True,
            "description": "目前模型版本；全歷史只保留於 all_history_audit 作稽核。",
        },
        "generated_at": now.isoformat(),
        "status": "unavailable",
        "status_reason": reason,
        "policy": policy_block(),
        "definitions": definitions_block(),
        "completeness": {"overall": {}, "by_market": {}},
        "issues": [],
        "issue_counts": {"high": 0, "warn": 0, "info": 0, "total": 0},
        "metrics_policy": metrics_policy_block(),
        "baseline": {},
        "error_slices": {},
        "primary_diagnostic": {
            "unit": METRIC_UNIT_LATEST_STAGE,
            "sample_basis": SAMPLE_BASIS,
            "stage_priority": list(PRIMARY_STAGE_PRIORITY),
            "label": "主要診斷:每場每市場最新階段",
            "baseline": {},
            "error_slices": {},
        },
        "hil_v4_diagnostics": {
            "scope": "HIL",
            "auto_apply": False,
            "retraining": False,
            "is_model": False,
            "evidence_unit": METRIC_UNIT_LATEST_STAGE,
            "evidence_sample_basis": SAMPLE_BASIS,
            "evidence_uses_repeated_stage_rows": False,
            "recommendations": [],
            "notes": ["資料源不可用,唔會做任何建議。"],
        },
    }


PUBLIC_TOP_LEVEL_KEYS = (
    "schema_version", "report", "system", "generated_at", "status", "status_reason",
    "scope", "policy", "definitions", "completeness", "issues", "issue_counts",
    "metrics_policy", "baseline", "error_slices", "primary_diagnostic",
    "hil_v4_diagnostics",
)


def public_view(report: dict[str, Any]) -> dict[str, Any]:
    """Aggregate-only projection.  No fixture ids, payloads, or secrets."""
    return {key: report[key] for key in PUBLIC_TOP_LEVEL_KEYS if key in report}


def build_reports(
    learning_db: Path | None,
    now: datetime | None = None,
) -> dict[str, dict[str, Any]]:
    moment = (now or datetime.now(timezone.utc)).astimezone(timezone.utc)
    if learning_db is None or not Path(learning_db).is_file():
        reason = "learning_database_missing"
        return {system: unavailable_report(system, moment, reason) for system in SYSTEMS}
    reports: dict[str, dict[str, Any]] = {}
    with ReadOnlyLearningSource(learning_db) as source:
        for system in SYSTEMS:
            reports[system] = build_system_report(source, system, moment)
    return reports


def audit_summary(report: dict[str, Any]) -> dict[str, Any]:
    """Compact, private-row-free summary for the production audit."""
    overall = (report.get("completeness") or {}).get("overall") or {}
    result = overall.get("result") or {}
    corner = overall.get("corner_result") or {}
    baseline = report.get("baseline") or {}
    primary = report.get("primary_diagnostic") or {}
    primary_baseline = primary.get("baseline") or {}
    diagnostics = report.get("hil_v4_diagnostics") or {}
    return {
        "schema_version": report.get("schema_version"),
        "system": report.get("system"),
        "scope": report.get("scope") or {},
        "generated_at": report.get("generated_at"),
        "status": report.get("status"),
        "status_reason": report.get("status_reason"),
        "counts": {
            "unique_fixtures": overall.get("unique_fixtures", 0),
            "stage_rows": overall.get("stage_rows", 0),
            "prediction_rows": overall.get("prediction_rows", 0),
            "graded_rows": overall.get("graded_rows", 0),
            "pending_rows": overall.get("pending_rows", 0),
            "excluded_rows": overall.get("excluded_rows", 0),
            "duplicate_stage_keys": overall.get("duplicate_stage_keys", 0),
            "quarantined_post_kickoff_rows": overall.get("quarantined_post_kickoff_rows", 0),
        },
        "coverage": {
            "result": result.get("coverage"),
            "corner_result": corner.get("coverage"),
            "stale_unresolved_fixtures": result.get("stale_unresolved_fixtures", 0),
            "stale_missing_corner_fixtures": corner.get("stale_beyond_retry_fixtures", 0),
        },
        "baseline": {
            "unique_fixtures": baseline.get("unique_fixtures", 0),
            "accuracy": baseline.get("accuracy"),
            "brier": baseline.get("brier"),
            "log_loss": baseline.get("log_loss"),
            "sample_status": baseline.get("sample_status"),
            # Stated so an audit reader never treats these as one-per-fixture.
            "metric_unit": baseline.get("metric_unit", METRIC_UNIT_ALL_STAGES),
            "sample_basis": baseline.get("sample_basis", SAMPLE_BASIS),
            "correlated_stage_rows": bool(baseline.get("correlated_stage_rows", False)),
        },
        "primary_diagnostic_baseline": {
            "metric_unit": primary.get("unit", METRIC_UNIT_LATEST_STAGE),
            "sample_basis": primary_baseline.get("sample_basis", SAMPLE_BASIS),
            "correlated_stage_rows": bool(
                primary_baseline.get("correlated_stage_rows", False)
            ),
            "unique_fixtures": primary_baseline.get("unique_fixtures", 0),
            "graded_rows": primary_baseline.get("graded_rows", 0),
            "accuracy": primary_baseline.get("accuracy"),
            "brier": primary_baseline.get("brier"),
            "log_loss": primary_baseline.get("log_loss"),
            "sample_status": primary_baseline.get("sample_status"),
        },
        "metrics_policy": {
            key: (report.get("metrics_policy") or {}).get(key)
            for key in (
                "sample_basis",
                "metric_unit",
                "correlated_stage_rows",
                "primary_diagnostic_metric_unit",
                "metrics_are_one_per_fixture",
                "recommendation_evidence_unit",
            )
        },
        "issue_counts": report.get("issue_counts") or {},
        "top_issues": [
            {key: issue.get(key) for key in ("code", "severity", "scope", "count")}
            for issue in (report.get("issues") or [])[:5]
        ],
        "top_recommendations": [
            {key: item.get(key) for key in ("id", "kind", "priority", "title")}
            for item in (diagnostics.get("recommendations") or [])[:5]
        ],
        "recommendation_evidence_unit": diagnostics.get(
            "evidence_unit", METRIC_UNIT_LATEST_STAGE
        ),
        "auto_apply": False,
        "retraining": False,
    }


# ────────────────────────── output ──────────────────────────


def atomic_write(path: Path, payload: dict[str, Any], mode: int = 0o600) -> None:
    """Write JSON atomically: temp file, fsync, chmod, then rename."""
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temp_name = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(payload, handle, ensure_ascii=False, indent=2, sort_keys=False)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.chmod(temp_name, mode)
        os.replace(temp_name, path)
    finally:
        if os.path.exists(temp_name):
            os.unlink(temp_name)


def run(
    learning_db: Path | None,
    out: Path | None,
    public_paths: dict[str, Path] | None = None,
    now: datetime | None = None,
) -> dict[str, dict[str, Any]]:
    reports = build_reports(learning_db, now=now)
    if out is not None:
        atomic_write(out, {
            "schema_version": SCHEMA_VERSION,
            "report": REPORT_NAME,
            "generated_at": next(iter(reports.values()))["generated_at"],
            "systems": reports,
        })
    for system, path in (public_paths or {}).items():
        atomic_write(path, public_view(reports[system]), mode=0o644)
    return reports


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--learning-db",
        type=Path,
        default=Path("/var/lib/footbreak/learning/predictions.sqlite"),
    )
    parser.add_argument(
        "--out",
        type=Path,
        default=Path("/var/lib/footbreak/data-health/latest.json"),
    )
    parser.add_argument(
        "--public-footbreak", type=Path, default=Path("/var/www/footbreak/data-health.json")
    )
    parser.add_argument(
        "--public-crown", type=Path, default=Path("/var/www/crown/data-health.json")
    )
    parser.add_argument(
        "--lock", type=Path, default=Path("/var/lock/footbreak-data-health.lock")
    )
    args = parser.parse_args()

    public = {}
    if args.public_footbreak:
        public["footbreak"] = args.public_footbreak
    if args.public_crown:
        public["crown"] = args.public_crown

    args.lock.parent.mkdir(parents=True, exist_ok=True)
    with args.lock.open("w", encoding="utf-8") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        reports = run(args.learning_db, args.out, public)
    print(json.dumps({
        system: audit_summary(report) for system, report in reports.items()
    }, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
