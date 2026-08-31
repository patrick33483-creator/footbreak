#!/usr/bin/env python3
"""Strict, read-only and reproducible Crown V3 decision/calibration backtest.

V3 selects decision rules over persisted upstream probabilities.  It does not
retrain, refit, or otherwise alter the upstream 70/30 model weights.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import math
import random
from collections import Counter, defaultdict
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Iterable, Sequence

STAGES = ("首預", "T-30", "T-5")
MARKETS = ("HDC", "HIL")
CONSISTENCIES = ("any", "t30_same_market_direction", "all_three_same_market_direction")
GRADES = ("full_win", "half_win", "push", "half_loss", "full_loss")
HITS = {"full_win", "half_win"}
DEFAULT_PROB_MINS = (0.0, 0.50, 0.52, 0.55, 0.58, 0.60, 0.62, 0.65)
DEFAULT_EV_MINS = (-1.0, 0.0, 0.02, 0.05, 0.08, 0.10, 0.15)
DEFAULT_ODDS_MINS = (1.0, 1.70, 1.75, 1.80, 1.85, 1.90, 2.0)
EPS = 1e-10


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
    # Production history is HKT when an old value has no explicit offset.
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone(timedelta(hours=8)))
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
    direction = ({"H": "home", "A": "away"} if code == "HDC" else {"H": "over", "O": "over", "L": "under", "U": "under"}).get(side)
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
        "label": item.get("label"),
    }


def market_leads(row: dict[str, Any]) -> dict[str, dict[str, Any]]:
    """Return a deterministic maximum-EV lead independently for HDC and HIL."""
    candidates: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for index, item in enumerate(row.get("market_predictions") or []):
        value = _prediction(item, index)
        if value is not None:
            candidates[value["market"]].append(value)
    result = {}
    for market, values in candidates.items():
        # Earlier payload order is the final tie break, matching legacy V2.
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
        # Some old verified rows retain only the display score.
        score = str(row.get("score") or "")
        pieces = score.replace("：", "-").replace(":", "-").split("-")
        if len(pieces) == 2:
            home, away = _number(pieces[0]), _number(pieces[1])
    if home is None or away is None:
        return None
    return {"home_score": home, "away_score": away}


def canonicalize(rows: Sequence[Any]) -> tuple[list[dict[str, Any]], dict[str, int]]:
    """Canonicalize the latest pre-kickoff snapshot for every fixture/stage."""
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
        item = {"row": raw, "fixture_id": fixture_id, "stage": stage, "predicted_at": predicted, "kickoff": kickoff, "leads": leads, "fingerprint": fingerprint}
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
    cumulative = 0
    for target in targets:
        choices = []
        running = 0
        for index in range(len(cohorts) + 1):
            if index:
                running += len(cohorts[index - 1])
            choices.append((abs(running - target), index, running))
        # Prefer the earlier boundary on an exact distance tie.
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
    # Asian inputs must be in quarter-goal increments.
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
    return {"full_win": odds - 1.0, "half_win": (odds - 1.0) / 2.0, "push": 0.0, "half_loss": -0.5, "full_loss": -1.0}[grade]


def _t5_pick(fixture: dict[str, Any], market: str) -> dict[str, Any] | None:
    stage = fixture["stages"].get("T-5")
    return stage["leads"].get(market) if stage else None


def consistency_pass(fixture: dict[str, Any], market: str, mode: str) -> bool:
    t5 = _t5_pick(fixture, market)
    if t5 is None:
        return False
    if mode == "any":
        return True
    required = ("T-30",) if mode == "t30_same_market_direction" else ("首預", "T-30")
    for stage_name in required:
        stage = fixture["stages"].get(stage_name)
        lead = stage["leads"].get(market) if stage else None
        if lead is None or lead["direction"] != t5["direction"]:
            return False
    return True


def rule_pass(fixture: dict[str, Any], rule: dict[str, Any]) -> bool:
    pick = _t5_pick(fixture, rule["market"])
    return bool(pick is not None and pick["probability"] + EPS >= rule["prob_min"] and pick["ev"] + EPS >= rule["ev_min"] and pick["odds"] + EPS >= rule["odds_min"] and consistency_pass(fixture, rule["market"], rule["consistency"]))


def observations(fixtures: Iterable[dict[str, Any]], rule: dict[str, Any] | None = None, market: str | None = None, cross_market: bool = False) -> list[dict[str, Any]]:
    output = []
    for fixture in fixtures:
        picks: list[dict[str, Any]] = []
        if rule is not None:
            if rule_pass(fixture, rule):
                pick = _t5_pick(fixture, rule["market"])
                if pick:
                    picks = [pick]
        elif market is not None:
            pick = _t5_pick(fixture, market)
            if pick:
                picks = [pick]
        elif cross_market:
            available = [pick for code in MARKETS if (pick := _t5_pick(fixture, code)) is not None]
            if available:
                picks = [max(available, key=lambda x: (x["ev"], -MARKETS.index(x["market"])))]
        for pick in picks:
            grade = settle(pick, fixture["result"])
            output.append({"fixture_id": fixture["fixture_id"], "pick": pick, "grade": grade, "return": unit_return(grade, pick["odds"])})
    return output


def wilson(hits: int, n: int, z: float = 1.959963984540054) -> list[float | None]:
    if not n:
        return [None, None]
    p = hits / n
    denominator = 1 + z * z / n
    center = (p + z * z / (2 * n)) / denominator
    margin = z * math.sqrt((p * (1 - p) + z * z / (4 * n)) / n) / denominator
    return [center - margin, center + margin]


def percentile(values: Sequence[float], q: float) -> float | None:
    if not values:
        return None
    ordered = sorted(values)
    position = (len(ordered) - 1) * q
    low, high = math.floor(position), math.ceil(position)
    return ordered[low] if low == high else ordered[low] * (high - position) + ordered[high] * (position - low)


def summarize(rows: Sequence[dict[str, Any]], fixture_denominator: int, seed: str, bootstrap_samples: int) -> dict[str, Any]:
    grades = Counter(row["grade"] for row in rows)
    decided_rows = [row for row in rows if row["grade"] != "push"]
    hits = sum(row["grade"] in HITS for row in decided_rows)
    interval = wilson(hits, len(decided_rows))
    unique_fixtures = len({row["fixture_id"] for row in rows})
    returns = [row["return"] for row in rows]
    clusters: dict[str, list[float]] = defaultdict(list)
    for row in rows:
        clusters[row["fixture_id"]].append(row["return"])
    bootstrap = []
    if clusters and bootstrap_samples:
        ids = sorted(clusters)
        rng = random.Random(seed)
        for _ in range(bootstrap_samples):
            sampled = [rng.choice(ids) for __ in ids]
            sample_returns = [value for fixture_id in sampled for value in clusters[fixture_id]]
            bootstrap.append(sum(sample_returns) / len(sample_returns))
    probabilities = [min(max(row["pick"]["probability"], 1e-15), 1 - 1e-15) for row in decided_rows]
    targets = [1.0 if row["grade"] in HITS else 0.0 for row in decided_rows]
    brier = sum((p - y) ** 2 for p, y in zip(probabilities, targets)) / len(targets) if targets else None
    log_loss = -sum(y * math.log(p) + (1 - y) * math.log(1 - p) for p, y in zip(probabilities, targets)) / len(targets) if targets else None
    hit_rate = hits / len(decided_rows) if decided_rows else None
    mean_probability = sum(probabilities) / len(probabilities) if probabilities else None
    return {
        "n": len(rows),
        "unique_fixtures": unique_fixtures,
        "coverage": unique_fixtures / fixture_denominator if fixture_denominator else None,
        "grade_counts": {grade: grades[grade] for grade in GRADES},
        "decided_n_excluding_push": len(decided_rows),
        "hits": hits,
        "hit_rate_excluding_push": hit_rate,
        "wilson_hit_rate_95": interval,
        "unit_pnl": sum(returns),
        "roi": sum(returns) / len(returns) if returns else None,
        "fixture_cluster_bootstrap_roi_95": [percentile(bootstrap, 0.025), percentile(bootstrap, 0.975)],
        "brier_excluding_push": brier,
        "log_loss_excluding_push": log_loss,
        "mean_probability_excluding_push": mean_probability,
        "calibration_gap_abs_excluding_push": abs(mean_probability - hit_rate) if mean_probability is not None and hit_rate is not None else None,
    }


def candidate_grid(market: str, prob_mins: Sequence[float] = DEFAULT_PROB_MINS, ev_mins: Sequence[float] = DEFAULT_EV_MINS, odds_mins: Sequence[float] = DEFAULT_ODDS_MINS) -> list[dict[str, Any]]:
    return [{"market": market, "prob_min": p, "ev_min": ev, "odds_min": odds, "consistency": consistency} for p in prob_mins for ev in ev_mins for odds in odds_mins for consistency in CONSISTENCIES]


def _rule_id(rule: dict[str, Any]) -> str:
    return f"{rule['market']}|p>={rule['prob_min']:.4f}|ev>={rule['ev_min']:.4f}|odds>={rule['odds_min']:.4f}|{rule['consistency']}"


def lock_rule(market: str, discovery: Sequence[dict[str, Any]], selection: Sequence[dict[str, Any]], min_discovery: int, min_selection: int) -> tuple[dict[str, Any] | None, dict[str, Any]]:
    """Filter on discovery+selection counts, rank using selection outcomes only."""
    eligible = []
    for rule in candidate_grid(market):
        d_rows = observations(discovery, rule=rule)
        s_rows = observations(selection, rule=rule)
        if len(d_rows) < min_discovery or len(s_rows) < min_selection:
            continue
        s_metrics = summarize(s_rows, len(selection), _rule_id(rule) + "|selection-rank", 0)
        interval_low = s_metrics["wilson_hit_rate_95"][0]
        rank_key = (
            s_metrics["roi"] if s_metrics["roi"] is not None else -math.inf,
            interval_low if interval_low is not None else -math.inf,
            -s_metrics["calibration_gap_abs_excluding_push"] if s_metrics["calibration_gap_abs_excluding_push"] is not None else -math.inf,
            s_metrics["n"],
            _rule_id(rule),
        )
        eligible.append((rank_key, rule, len(d_rows), s_metrics))
    if not eligible:
        return None, {"candidate_count": len(candidate_grid(market)), "eligible_count": 0, "minimum_discovery_n": min_discovery, "minimum_selection_n": min_selection, "ranking": "selection-only ROI, Wilson lower bound, inverse calibration gap, n; deterministic rule id tie-break"}
    _, locked, discovery_n, selection_metrics = max(eligible, key=lambda item: item[0])
    return locked, {
        "candidate_count": len(candidate_grid(market)),
        "eligible_count": len(eligible),
        "minimum_discovery_n": min_discovery,
        "minimum_selection_n": min_selection,
        "ranking": "selection-only ROI, Wilson lower bound, inverse calibration gap, n; deterministic rule id tie-break",
        "locked_rule_id": _rule_id(locked),
        "locked_discovery_n": discovery_n,
        "locked_selection_metrics_used_for_ranking": selection_metrics,
    }


def _strategy_report(name: str, parts: dict[str, list[dict[str, Any]]], get_rows: Any, bootstrap_samples: int) -> dict[str, Any]:
    return {part: summarize(get_rows(fixtures), len(fixtures), f"crown-v3|{name}|{part}", bootstrap_samples) for part, fixtures in parts.items()}


def build_report(payload: dict[str, Any], input_state: dict[str, Any], min_discovery: int = 30, min_selection: int = 10, bootstrap_samples: int = 5000) -> dict[str, Any]:
    fixtures, diagnostics = canonicalize(payload.get("rows") or [])
    parts, split_metadata = split_kickoff_cohorts(fixtures)
    locked_rules: dict[str, dict[str, Any] | None] = {}
    searches = {}
    for market in MARKETS:
        locked_rules[market], searches[market] = lock_rule(market, parts["discovery"], parts["selection"], min_discovery, min_selection)

    strategies = {
        "v2_baseline_t5_cross_market_max_ev_always": _strategy_report("v2", parts, lambda values: observations(values, cross_market=True), bootstrap_samples),
        "unfiltered_hdc_t5": _strategy_report("unfiltered-hdc", parts, lambda values: observations(values, market="HDC"), bootstrap_samples),
        "unfiltered_hil_t5": _strategy_report("unfiltered-hil", parts, lambda values: observations(values, market="HIL"), bootstrap_samples),
    }
    for market in MARKETS:
        rule = locked_rules[market]
        strategies[f"v3_{market.lower()}"] = _strategy_report(f"v3-{market}", parts, lambda values, selected=rule: observations(values, rule=selected) if selected else [], bootstrap_samples)
    strategies["v3_portfolio"] = {
        part: summarize(
            [row for market in MARKETS if locked_rules[market] for row in observations(values, rule=locked_rules[market])],
            len(values), f"crown-v3|portfolio|{part}", bootstrap_samples,
        )
        for part, values in parts.items()
    }
    public_rules = {market: ({**rule, "rule_id": _rule_id(rule)} if rule else None) for market, rule in locked_rules.items()}
    return {
        "schema_version": "crown-v3-strict-backtest-v1",
        "reproducibility": {
            "input_file_state": input_state,
            "deterministic_ties": True,
            "bootstrap_seed_scheme": "Random(crown-v3|strategy|split), fixture-cluster resampling",
            "bootstrap_samples": bootstrap_samples,
            "report_has_no_wall_clock_field": True,
        },
        "scope": {
            "kind": "decision/calibration V3",
            "not_upstream_retraining": True,
            "upstream_model_weights": "persisted production probabilities only; no refit or change to upstream 70/30 weights",
            "decision_stage": "T-5",
            "v2_baseline_definition": "always select maximum EV across the independent HDC/HIL T-5 leads",
            "calibration_definition": "pushes excluded; half-win is a hit and half-loss is a miss, matching hit-rate denominator",
            "roi_definition": "one-unit decimal-odds return; half win/loss and push settled exactly",
        },
        "diagnostics": diagnostics,
        "split": split_metadata,
        "parameter_search": searches,
        "locked_rules_selected_without_holdout": public_rules,
        "comparisons": strategies,
        "primary_comparison_split": "holdout",
    }


def _summary(report: dict[str, Any]) -> str:
    lines = ["Crown V3 strict holdout summary", f"input_sha256={report['reproducibility']['input_file_state']['sha256']}"]
    for market, rule in report["locked_rules_selected_without_holdout"].items():
        lines.append(f"locked_{market}={rule['rule_id'] if rule else 'NONE'}")
    for name, periods in report["comparisons"].items():
        metrics = periods["holdout"]
        roi = "NA" if metrics["roi"] is None else f"{metrics['roi'] * 100:.2f}%"
        hit = "NA" if metrics["hit_rate_excluding_push"] is None else f"{metrics['hit_rate_excluding_push'] * 100:.2f}%"
        coverage = "NA" if metrics["coverage"] is None else f"{metrics['coverage'] * 100:.2f}%"
        lines.append(f"{name}: n={metrics['n']} coverage={coverage} hit={hit} roi={roi}")
    return "\n".join(lines)


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--input", required=True, type=Path)
    parser.add_argument("--output", required=True, type=Path)
    parser.add_argument("--min-discovery", type=int, default=30)
    parser.add_argument("--min-selection", type=int, default=10)
    parser.add_argument("--bootstrap-samples", type=int, default=5000)
    args = parser.parse_args(argv)
    if args.min_discovery < 1 or args.min_selection < 1 or args.bootstrap_samples < 0:
        parser.error("sample minimums must be positive and bootstrap samples non-negative")
    before = file_state(args.input)
    with args.input.open("r", encoding="utf-8") as handle:
        payload = json.load(handle)
    if not isinstance(payload, dict):
        raise SystemExit("prediction history root must be an object")
    report = build_report(payload, before, args.min_discovery, args.min_selection, args.bootstrap_samples)
    after = file_state(args.input)
    if before != after:
        raise SystemExit("read-only integrity check failed: input changed during analysis")
    report["reproducibility"]["read_only_input_hash_unchanged"] = True
    args.output.write_text(json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    print(_summary(report))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
