#!/usr/bin/env python3
"""Read-only historical backtest for true opening, T-30 and T-5.

The two data layers are deliberately kept separate:

1. Odds Radar supplies immutable external opening quotes plus T30/T5 quotes.
   Its lower-odds side is treated as a market benchmark, not a trained model.
2. Footbreak/Crown supply graded model predictions at T-30 and T-5.

Legacy ``首預`` snapshots are audited for lead-time dispersion but are never
relabeled as true opening predictions.
"""

from __future__ import annotations

import argparse
import collections
import datetime as dt
import hashlib
import json
import math
import random
import sqlite3
import statistics
from typing import Any, Iterable


DEFAULT_FB_URI = "file:/var/lib/footbreak/learning/predictions.sqlite?mode=ro"
DEFAULT_RADAR_URI = "file:/opt/odds-radar/data/data.db?mode=ro"
STAGES = ("initial", "T30", "T5")
MODEL_STAGES = ("T-30", "T-5")
MARKETS = ("AH", "OU", "COU")
MODEL_MARKETS = ("HDC", "HIL", "CHL")


def ro_connect(uri: str) -> sqlite3.Connection:
    db = sqlite3.connect(uri, uri=True)
    db.row_factory = sqlite3.Row
    db.execute("PRAGMA query_only=ON")
    db.execute("BEGIN")
    return db


def parse_time(value: Any) -> dt.datetime | None:
    if value is None:
        return None
    if isinstance(value, (int, float)):
        return dt.datetime.fromtimestamp(float(value) / 1000, dt.timezone.utc)
    text = str(value).strip().replace("Z", "+00:00")
    try:
        parsed = dt.datetime.fromisoformat(text)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=dt.timezone.utc)
    return parsed.astimezone(dt.timezone.utc)


def number(value: Any) -> float | None:
    try:
        parsed = float(value)
    except (TypeError, ValueError):
        return None
    return parsed if math.isfinite(parsed) else None


def probability(value: Any) -> float | None:
    parsed = number(value)
    if parsed is None:
        return None
    if 1 < parsed <= 100:
        parsed /= 100
    return parsed if 0 <= parsed <= 1 else None


def line_number(value: Any) -> float | None:
    if value is None:
        return None
    text = str(value).strip()
    try:
        if "/" in text:
            parts = [float(part.strip()) for part in text.split("/") if part.strip()]
            parsed = sum(parts) / len(parts) if parts else None
        else:
            parsed = float(text)
    except ValueError:
        return None
    if parsed is None or not math.isfinite(parsed):
        return None
    quarter = round(parsed * 4) / 4
    return quarter if abs(parsed - quarter) < 1e-8 else None


def side_key(value: Any, market: str) -> str | None:
    text = str(value or "").strip().upper()
    if market in {"OU", "HIL", "COU", "CHL"}:
        return {"H": "O", "O": "O", "L": "U", "U": "U"}.get(text)
    if market in {"AH", "HDC"}:
        return {"H": "H", "A": "A"}.get(text)
    return None


def target_parts(target: Any, market: str) -> tuple[float | None, str | None]:
    text = str(target or "")
    if "|" not in text:
        return line_number(text), None
    condition, side = text.rsplit("|", 1)
    return line_number(condition), side_key(side, market)


def split_line(value: float) -> list[float]:
    if abs(value * 2 - round(value * 2)) < 1e-9:
        return [value]
    return [value - 0.25, value + 0.25]


def half_outcome(diff: float) -> int:
    if diff > 1e-9:
        return 1
    if diff < -1e-9:
        return -1
    return 0


def aggregate_outcomes(outcomes: list[int]) -> str:
    if len(outcomes) == 1:
        return {1: "win", 0: "push", -1: "loss"}[outcomes[0]]
    total = sum(outcomes)
    if outcomes[0] == outcomes[1] == 1:
        return "win"
    if total == 1:
        return "half_win"
    if total == 0:
        return "push"
    if total == -1:
        return "half_loss"
    return "loss"


def settle(
    market: str,
    line: float,
    side: str,
    home_score: int,
    away_score: int,
    corners_total: int | None,
) -> str | None:
    if market == "AH":
        outcomes = []
        for half in split_line(line):
            adjusted = home_score + half - away_score
            outcomes.append(half_outcome(adjusted if side == "H" else -adjusted))
        return aggregate_outcomes(outcomes)
    total = corners_total if market == "COU" else home_score + away_score
    if total is None:
        return None
    outcomes = [
        half_outcome(total - half if side == "O" else half - total)
        for half in split_line(line)
    ]
    return aggregate_outcomes(outcomes)


def unit_return(status: str, odds: float) -> float:
    return {
        "win": odds - 1,
        "half_win": (odds - 1) / 2,
        "push": 0,
        "half_loss": -0.5,
        "loss": -1,
    }[status]


def status_target(status: str) -> float:
    return {
        "win": 1.0,
        "half_win": 0.75,
        "push": 0.5,
        "half_loss": 0.25,
        "loss": 0.0,
    }[status]


def percentile(values: list[float], q: float) -> float | None:
    if not values:
        return None
    ordered = sorted(values)
    pos = (len(ordered) - 1) * q
    lo, hi = math.floor(pos), math.ceil(pos)
    if lo == hi:
        return ordered[lo]
    return ordered[lo] * (hi - pos) + ordered[hi] * (pos - lo)


def wilson(hits: int, decided: int, z: float = 1.959963984540054) -> list[float | None]:
    if decided <= 0:
        return [None, None]
    p = hits / decided
    den = 1 + z * z / decided
    centre = p + z * z / (2 * decided)
    margin = z * math.sqrt(
        p * (1 - p) / decided + z * z / (4 * decided * decided)
    )
    return [(centre - margin) / den, (centre + margin) / den]


def stable_seed(label: str) -> int:
    return int(hashlib.sha256(label.encode("utf-8")).hexdigest()[:8], 16)


def summarize(rows: Iterable[dict[str, Any]], label: str) -> dict[str, Any]:
    rows = list(rows)
    settlements = collections.Counter(row["settlement"] for row in rows)
    decided = (
        settlements["win"]
        + settlements["half_win"]
        + settlements["half_loss"]
        + settlements["loss"]
    )
    hits = settlements["win"] + settlements["half_win"]
    returns = [float(row["return"]) for row in rows]
    probabilities = [
        (float(row["probability"]), float(row["target"]))
        for row in rows
        if row.get("probability") is not None and row.get("target") is not None
    ]
    brier = (
        statistics.mean((p - target) ** 2 for p, target in probabilities)
        if probabilities
        else None
    )
    log_loss = None
    if probabilities:
        eps = 1e-12
        log_loss = statistics.mean(
            -(target * math.log(min(max(p, eps), 1 - eps))
              + (1 - target) * math.log(min(max(1 - p, eps), 1 - eps)))
            for p, target in probabilities
        )

    fixture_returns: dict[str, list[float]] = collections.defaultdict(list)
    for row in rows:
        fixture_returns[str(row["fixture_id"])].append(float(row["return"]))
    fixture_means = [statistics.mean(values) for values in fixture_returns.values()]
    rng = random.Random(stable_seed(label))
    bootstrap: list[float] = []
    if fixture_means:
        n = len(fixture_means)
        for _ in range(5000):
            bootstrap.append(
                statistics.mean(fixture_means[rng.randrange(n)] for __ in range(n))
            )

    equity = peak = max_drawdown = 0.0
    ordered = sorted(
        rows,
        key=lambda row: (
            str(row.get("kickoff") or ""),
            str(row["fixture_id"]),
        ),
    )
    for row in ordered:
        equity += float(row["return"])
        peak = max(peak, equity)
        max_drawdown = max(max_drawdown, peak - equity)

    interval = wilson(hits, decided)
    return {
        "bets": len(rows),
        "unique_fixtures": len(fixture_returns),
        "settlements": dict(sorted(settlements.items())),
        "hit_rate_ex_push": round(hits / decided, 6) if decided else None,
        "hit_wilson_95": [
            round(value, 6) if value is not None else None for value in interval
        ],
        "unit_pnl": round(sum(returns), 4),
        "roi": round(statistics.mean(returns), 6) if returns else None,
        "roi_fixture_bootstrap_95": [
            round(percentile(bootstrap, 0.025), 6),
            round(percentile(bootstrap, 0.975), 6),
        ] if bootstrap else [None, None],
        "mean_odds": round(
            statistics.mean(float(row["odds"]) for row in rows), 4
        ) if rows else None,
        "probability_rows": len(probabilities),
        "brier": round(brier, 6) if brier is not None else None,
        "log_loss": round(log_loss, 6) if log_loss is not None else None,
        "max_drawdown_units": round(max_drawdown, 4),
    }


def direction_path_backtest(
    observations: list[dict[str, Any]],
    common_keys: set[tuple[str, str, str]],
) -> dict[str, Any]:
    by_identity: dict[
        tuple[str, str, str], dict[str, dict[str, Any]]
    ] = collections.defaultdict(dict)
    for row in observations:
        identity = (
            str(row["fixture_id"]),
            str(row["provider"]),
            str(row["market"]),
        )
        if identity in common_keys:
            by_identity[identity][str(row["stage"])] = row

    grouped: dict[tuple[str, str, str], list[dict[str, Any]]] = (
        collections.defaultdict(list)
    )
    fixture_rows = []
    for (fixture_id, provider, market), stage_rows in by_identity.items():
        if not set(STAGES).issubset(stage_rows):
            continue
        initial = stage_rows["initial"]
        t30 = stage_rows["T30"]
        t5 = stage_rows["T5"]
        path = f'{initial["side"]}→{t30["side"]}→{t5["side"]}'
        bet_row = dict(t5)
        bet_row["direction_path"] = path
        grouped[(provider, market, path)].append(bet_row)
        fixture_rows.append({
            "fixture_id": fixture_id,
            "kickoff": t5["kickoff"],
            "provider": provider,
            "market": market,
            "direction_path": path,
            "initial": {
                "side": initial["side"],
                "line": initial["line"],
                "odds": initial["odds"],
            },
            "T30": {
                "side": t30["side"],
                "line": t30["line"],
                "odds": t30["odds"],
            },
            "T5": {
                "side": t5["side"],
                "line": t5["line"],
                "odds": t5["odds"],
            },
            "T5_settlement": t5["settlement"],
            "T5_unit_return": round(float(t5["return"]), 6),
        })

    conditions = {}
    for (provider, market, path), rows in sorted(grouped.items()):
        ordered = sorted(
            rows,
            key=lambda row: (
                str(row.get("kickoff") or ""),
                str(row["fixture_id"]),
            ),
        )
        cut = max(1, min(len(ordered) - 1, math.floor(len(ordered) * 0.7)))
        discovery = ordered[:cut] if len(ordered) > 1 else ordered
        holdout = ordered[cut:] if len(ordered) > 1 else []
        label = f"{provider}_{market}_{path}"
        discovery_summary = summarize(discovery, f"path-discovery:{label}")
        holdout_summary = summarize(holdout, f"path-holdout:{label}")
        all_summary = summarize(ordered, f"path-all:{label}")
        discovery_roi = discovery_summary["roi"]
        holdout_roi = holdout_summary["roi"]
        holdout_ci_low = holdout_summary["roi_fixture_bootstrap_95"][0]
        conditions[label] = {
            "provider": provider,
            "market": market,
            "direction_path": path,
            "decision_stage": "T5",
            "bet_stage": "T5",
            "chronological_split": "earliest 70% discovery / latest 30% holdout",
            "all": all_summary,
            "discovery": discovery_summary,
            "holdout": holdout_summary,
            "screening": {
                "minimum_full_sample_30": len(ordered) >= 30,
                "minimum_holdout_sample_10": len(holdout) >= 10,
                "positive_roi_both_periods": (
                    discovery_roi is not None
                    and holdout_roi is not None
                    and discovery_roi > 0
                    and holdout_roi > 0
                ),
                "holdout_roi_ci_above_zero": (
                    holdout_ci_low is not None and holdout_ci_low > 0
                ),
            },
        }

    return {
        "definition": {
            "direction": "lower decimal-odds side at each stage",
            "path": "initial direction → T30 direction → T5 direction",
            "decision_stage": "T5 because the full path is only known at T5",
            "settlement": "T5 side, line and decimal odds",
            "validation": "chronological 70/30 split within each fixed path",
            "promotion_rule": (
                "at least 30 full observations, at least 10 holdout observations, "
                "positive discovery and holdout ROI, and holdout ROI 95% lower "
                "bound above zero"
            ),
        },
        "condition_count": len(conditions),
        "conditions": conditions,
        "fixture_rows": sorted(
            fixture_rows,
            key=lambda row: (
                row["kickoff"],
                row["provider"],
                row["market"],
                row["fixture_id"],
            ),
        ),
    }


def radar_backtest(db: sqlite3.Connection) -> dict[str, Any]:
    query = """
    SELECT q.match_id,q.stage,q.provider,q.market,q.line_key,q.selection,
           q.decimal_odds,q.is_main,q.captured_at,q.origin,
           m.kickoff_utc,
           COALESCE(rr.home_score,r.home_score) AS home_score,
           COALESCE(rr.away_score,r.away_score) AS away_score,
           rr.corners_total AS official_corners,
           rr.source AS research_result_source
    FROM research_timeline_snapshots q
    JOIN matches m ON m.id=q.match_id
    LEFT JOIN research_results rr ON rr.match_id=m.id
    LEFT JOIN results r ON r.match_id=m.id
    WHERE q.stage IN ('initial','T30','T5')
      AND q.provider IN ('hkjc','pinnacle')
      AND q.market IN ('AH','OU','COU')
    ORDER BY q.match_id,q.provider,q.market,q.stage,q.line_key,q.selection,
             q.captured_at
    """
    diagnostics = collections.Counter()
    quotes: dict[tuple[str, str, str, str, float, str], dict[str, Any]] = {}
    metadata: dict[str, dict[str, Any]] = {}
    for row in db.execute(query):
        kickoff = number(row["kickoff_utc"])
        captured = number(row["captured_at"])
        if kickoff is None or captured is None or captured >= kickoff:
            diagnostics["not_strictly_pre_kickoff"] += 1
            continue
        stage = str(row["stage"])
        origin = str(row["origin"] or "")
        if stage == "initial" and origin != "external_opening":
            diagnostics["initial_not_external_opening"] += 1
            continue
        if stage != "initial" and origin != "live_observation":
            diagnostics["checkpoint_not_live_observation"] += 1
            continue
        market = str(row["market"])
        line = line_number(row["line_key"])
        side = side_key(row["selection"], market)
        odds = number(row["decimal_odds"])
        if line is None or side is None or odds is None or odds <= 1:
            diagnostics["invalid_quote"] += 1
            continue
        match_id = str(row["match_id"])
        key = (
            match_id,
            str(row["provider"]).lower(),
            market,
            stage,
            line,
            side,
        )
        candidate = {
            "odds": odds,
            "is_main": int(row["is_main"] or 0),
            "captured_at": captured,
        }
        old = quotes.get(key)
        if old is None or candidate["captured_at"] > old["captured_at"]:
            quotes[key] = candidate
        metadata[match_id] = {
            "kickoff": int(kickoff),
            "home_score": row["home_score"],
            "away_score": row["away_score"],
            "corners_total": (
                row["official_corners"]
                if row["research_result_source"] == "hkjc_official"
                else None
            ),
        }

    paired: dict[tuple[str, str, str, str, float], dict[str, dict[str, Any]]] = (
        collections.defaultdict(dict)
    )
    for (match_id, provider, market, stage, line, side), quote in quotes.items():
        paired[(match_id, provider, market, stage, line)][side] = quote

    candidates: dict[tuple[str, str, str, str], list[dict[str, Any]]] = (
        collections.defaultdict(list)
    )
    for (match_id, provider, market, stage, line), sides in paired.items():
        required = ("H", "A") if market == "AH" else ("O", "U")
        if any(side not in sides for side in required):
            diagnostics["incomplete_two_sided_line"] += 1
            continue
        candidates[(match_id, provider, market, stage)].append({
            "line": line,
            "sides": sides,
            "main_score": sum(sides[side]["is_main"] for side in required),
        })

    primary: dict[tuple[str, str, str, str], dict[str, Any]] = {}
    selection_coverage = collections.Counter()
    for key, groups in candidates.items():
        selection_coverage["paired_stage_markets"] += 1
        best_score = max(group["main_score"] for group in groups)
        if best_score > 0:
            best = [group for group in groups if group["main_score"] == best_score]
            if len(best) != 1:
                diagnostics["ambiguous_main_paired_line"] += 1
                continue
            primary[key] = best[0]
            selection_coverage["unique_main_stage_markets"] += 1
            continue
        if len(groups) == 1:
            primary[key] = groups[0]
            selection_coverage["single_paired_line_without_main"] += 1
            continue
        diagnostics["no_main_multiple_paired_lines"] += 1

    observations: list[dict[str, Any]] = []
    for (match_id, provider, market, stage), group in primary.items():
        meta = metadata[match_id]
        if meta["home_score"] is None or meta["away_score"] is None:
            diagnostics["missing_result"] += 1
            continue
        if market == "COU" and meta["corners_total"] is None:
            diagnostics["missing_official_corner_result"] += 1
            continue
        sides = ("H", "A") if market == "AH" else ("O", "U")
        left, right = group["sides"][sides[0]], group["sides"][sides[1]]
        if abs(left["odds"] - right["odds"]) < 1e-12:
            diagnostics["equal_odds_no_market_lean"] += 1
            continue
        selected = sides[0] if left["odds"] < right["odds"] else sides[1]
        opposite = sides[1] if selected == sides[0] else sides[0]
        selected_odds = group["sides"][selected]["odds"]
        opposite_odds = group["sides"][opposite]["odds"]
        denominator = 1 / selected_odds + 1 / opposite_odds
        no_vig_probability = (1 / selected_odds) / denominator
        status = settle(
            market,
            group["line"],
            selected,
            int(meta["home_score"]),
            int(meta["away_score"]),
            (
                int(meta["corners_total"])
                if meta["corners_total"] is not None
                else None
            ),
        )
        if status is None:
            continue
        observations.append({
            "fixture_id": match_id,
            "kickoff": meta["kickoff"],
            "provider": provider,
            "market": market,
            "stage": stage,
            "line": group["line"],
            "side": selected,
            "odds": selected_odds,
            "probability": no_vig_probability,
            "target": status_target(status),
            "settlement": status,
            "return": unit_return(status, selected_odds),
        })

    all_available = {}
    for provider in ("hkjc", "pinnacle"):
        for market in MARKETS:
            for stage in STAGES:
                label = f"{provider}_{market}_{stage}"
                subset = [
                    row for row in observations
                    if row["provider"] == provider
                    and row["market"] == market
                    and row["stage"] == stage
                ]
                all_available[label] = summarize(subset, f"all:{label}")

    common_keys = set()
    by_identity: dict[tuple[str, str, str], set[str]] = collections.defaultdict(set)
    for row in observations:
        by_identity[
            (row["fixture_id"], row["provider"], row["market"])
        ].add(row["stage"])
    for identity, stages in by_identity.items():
        if set(STAGES).issubset(stages):
            common_keys.add(identity)

    same_fixture = {}
    for provider in ("hkjc", "pinnacle"):
        for market in MARKETS:
            for stage in STAGES:
                label = f"{provider}_{market}_{stage}"
                subset = [
                    row for row in observations
                    if row["provider"] == provider
                    and row["market"] == market
                    and row["stage"] == stage
                    and (row["fixture_id"], provider, market) in common_keys
                ]
                same_fixture[label] = summarize(subset, f"common:{label}")

    direction_paths = direction_path_backtest(observations, common_keys)

    return {
        "definition": {
            "role": "market benchmark, not a trained prediction model",
            "pick": "lower decimal-odds side on the unique paired main line",
            "probability": "two-sided de-vig implied probability",
            "initial": "origin=external_opening only",
            "T30_T5": "origin=live_observation only and strictly pre-kickoff",
            "settlement": "Asian quarter-line returns; COU requires hkjc_official corner total",
        },
        "coverage": dict(selection_coverage),
        "diagnostics": dict(sorted(diagnostics.items())),
        "scorable_observations": len(observations),
        "three_stage_common_identities": len(common_keys),
        "all_available": all_available,
        "same_fixture_all_three_stages": same_fixture,
        "direction_paths": direction_paths,
    }


def matching_prediction(
    payload: dict[str, Any],
    market: str,
    target: Any,
) -> dict[str, Any] | None:
    target_line, target_side = target_parts(target, market)
    matches = []
    for item in payload.get("market_predictions") or []:
        if not isinstance(item, dict) or str(item.get("code")) != market:
            continue
        item_line = line_number(item.get("line", item.get("condition")))
        item_side = side_key(item.get("side"), market)
        if item_line == target_line and item_side == target_side:
            matches.append(item)
    return matches[0] if len(matches) == 1 else None


def model_backtest(db: sqlite3.Connection) -> dict[str, Any]:
    query = """
    WITH ranked_snapshots AS (
      SELECT s.*,
             ROW_NUMBER() OVER (
               PARTITION BY system,fixture_id,stage
               ORDER BY generated_at DESC,snapshot_id DESC
             ) AS snapshot_rank
      FROM prediction_snapshots s
      WHERE system IN ('footbreak','crown')
        AND stage IN ('T-30','T-5')
        AND pre_kickoff=1
        AND NOT EXISTS (
          SELECT 1 FROM stage_snapshot_reconciliations r
          WHERE r.system=s.system AND r.fixture_id=s.fixture_id
            AND r.stage=s.stage
            AND r.canonical_snapshot_id != s.snapshot_id
        )
    ),
    ranked_grades AS (
      SELECT g.*,
             ROW_NUMBER() OVER (
               PARTITION BY snapshot_id,market,target
               ORDER BY grade_attempt DESC,grade_id DESC
             ) AS grade_rank
      FROM grades g
      WHERE market IN ('HDC','HIL','CHL')
    )
    SELECT s.system,s.fixture_id,s.stage,s.generated_at,s.kickoff,
           s.payload_json,g.market,g.target,g.metrics_json
    FROM ranked_snapshots s
    JOIN ranked_grades g ON g.snapshot_id=s.snapshot_id AND g.grade_rank=1
    WHERE s.snapshot_rank=1 AND g.state='GRADED'
    ORDER BY s.system,s.fixture_id,s.stage,g.market,g.target
    """
    candidates: dict[tuple[str, str, str, str], list[dict[str, Any]]] = (
        collections.defaultdict(list)
    )
    diagnostics = collections.Counter()
    for row in db.execute(query):
        generated = parse_time(row["generated_at"])
        kickoff = parse_time(row["kickoff"])
        if generated is None or kickoff is None or generated >= kickoff:
            diagnostics["invalid_pre_kickoff_time"] += 1
            continue
        try:
            payload = json.loads(row["payload_json"])
            metrics = json.loads(row["metrics_json"])
        except (TypeError, ValueError):
            diagnostics["invalid_json"] += 1
            continue
        market = str(row["market"])
        prediction = matching_prediction(payload, market, row["target"])
        if prediction is None:
            diagnostics["no_unique_matching_prediction"] += 1
            continue
        odds = number(prediction.get("odds"))
        model_probability = probability(
            prediction.get("probability", metrics.get("probability"))
        )
        target = number(metrics.get("target"))
        if odds is None or odds <= 1:
            diagnostics["missing_or_invalid_odds"] += 1
            continue
        if target is None or not 0 <= target <= 1:
            diagnostics["missing_or_invalid_target"] += 1
            continue
        status = (
            "win" if target >= 0.999
            else "half_win" if target >= 0.749
            else "push" if target >= 0.499
            else "half_loss" if target >= 0.249
            else "loss"
        )
        line, side = target_parts(row["target"], market)
        key = (
            str(row["system"]),
            str(row["fixture_id"]),
            str(row["stage"]),
            market,
        )
        candidates[key].append({
            "fixture_id": str(row["fixture_id"]),
            "kickoff": kickoff.isoformat(),
            "system": str(row["system"]),
            "stage": str(row["stage"]),
            "market": market,
            "line": line,
            "side": side,
            "odds": odds,
            "probability": model_probability,
            "target": target,
            "settlement": status,
            "return": unit_return(status, odds),
        })

    observations = []
    for rows in candidates.values():
        signatures = {
            (
                row["line"],
                row["side"],
                row["odds"],
                row["probability"],
                row["target"],
            )
            for row in rows
        }
        if len(signatures) != 1:
            diagnostics["ambiguous_multiple_targets_excluded"] += 1
            continue
        observations.append(rows[0])

    all_available = {}
    for system in ("footbreak", "crown"):
        for market in MODEL_MARKETS:
            for stage in MODEL_STAGES:
                label = f"{system}_{market}_{stage}"
                subset = [
                    row for row in observations
                    if row["system"] == system
                    and row["market"] == market
                    and row["stage"] == stage
                ]
                all_available[label] = summarize(subset, f"model-all:{label}")

    by_identity: dict[tuple[str, str, str], set[str]] = collections.defaultdict(set)
    for row in observations:
        by_identity[
            (row["system"], row["fixture_id"], row["market"])
        ].add(row["stage"])
    common_keys = {
        identity
        for identity, stages in by_identity.items()
        if set(MODEL_STAGES).issubset(stages)
    }
    same_fixture = {}
    for system in ("footbreak", "crown"):
        for market in MODEL_MARKETS:
            for stage in MODEL_STAGES:
                label = f"{system}_{market}_{stage}"
                subset = [
                    row for row in observations
                    if row["system"] == system
                    and row["market"] == market
                    and row["stage"] == stage
                    and (system, row["fixture_id"], market) in common_keys
                ]
                same_fixture[label] = summarize(
                    subset, f"model-common:{label}"
                )

    return {
        "definition": {
            "stages": list(MODEL_STAGES),
            "canonical_snapshot": "latest unreconciled pre-kickoff snapshot per fixture-stage",
            "grade": "latest GRADED revision per snapshot-market-target",
            "dedupe": "exactly one unambiguous target per fixture-market-stage",
            "settlement": "stored grade target mapped to Asian quarter-line unit return",
            "legacy_initial": "excluded because 首預 lead time is not fixed",
        },
        "diagnostics": dict(sorted(diagnostics.items())),
        "scorable_observations": len(observations),
        "two_stage_common_identities": len(common_keys),
        "all_available": all_available,
        "same_fixture_T30_T5": same_fixture,
    }


def legacy_lead_time_audit(db: sqlite3.Connection) -> list[dict[str, Any]]:
    grouped: dict[str, list[float]] = collections.defaultdict(list)
    for row in db.execute(
        """
        SELECT system,generated_at,kickoff
        FROM prediction_snapshots
        WHERE system IN ('footbreak','crown') AND stage='首預' AND pre_kickoff=1
        """
    ):
        generated = parse_time(row["generated_at"])
        kickoff = parse_time(row["kickoff"])
        if generated and kickoff and generated < kickoff:
            grouped[str(row["system"])].append(
                (kickoff - generated).total_seconds() / 60
            )
    output = []
    for system, values in sorted(grouped.items()):
        values.sort()
        output.append({
            "system": system,
            "n": len(values),
            "minimum_minutes": round(values[0], 2),
            "median_minutes": round(statistics.median(values), 2),
            "maximum_minutes": round(values[-1], 2),
        })
    return output


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--footbreak-db", default=DEFAULT_FB_URI)
    parser.add_argument("--radar-db", default=DEFAULT_RADAR_URI)
    args = parser.parse_args()
    footbreak = ro_connect(args.footbreak_db)
    radar = ro_connect(args.radar_db)
    try:
        report = {
            "mode": "read_only",
            "methodology_version": "three-stage-historical-v1",
            "generated_at_utc": dt.datetime.now(dt.timezone.utc).isoformat(),
            "interpretation": {
                "initial": "Odds Radar true external opening market benchmark",
                "T30_T5_market": "Odds Radar checkpoint market benchmark",
                "T30_T5_models": "Footbreak and Crown graded model predictions",
                "not_available": (
                    "No immutable as-of-opening team-feature model predictions exist; "
                    "therefore no historical trained opening model is claimed."
                ),
            },
            "legacy_first_prediction_lead_time": legacy_lead_time_audit(footbreak),
            "market_timeline": radar_backtest(radar),
            "models": model_backtest(footbreak),
            "statistical_notes": [
                "Wilson intervals describe hit-rate uncertainty only.",
                "ROI intervals use deterministic fixture-level bootstrap resampling.",
                "Stage variants are exploratory and are not independent confirmations.",
                "A positive point estimate is not promoted when its 95% ROI interval crosses zero.",
            ],
        }
        print(json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True))
    finally:
        footbreak.rollback()
        radar.rollback()
        footbreak.close()
        radar.close()


if __name__ == "__main__":
    main()
