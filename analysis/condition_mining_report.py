#!/usr/bin/env python3
"""Leakage-conscious aggregate condition mining for settled market predictions.

The script reads the generated Footbreak and Crown dashboard JSON files and
emits aggregate statistics only.  It never writes source state and never emits
fixture, team, provider, or prediction identifiers.
"""
from __future__ import annotations

import argparse
import json
import math
from collections import defaultdict
from datetime import datetime
from pathlib import Path
from typing import Any, Callable, Iterable


STAGES = ("首預", "T-30", "T-5")
STAGE_RANK = {stage: rank for rank, stage in enumerate(STAGES)}
MARKETS = ("HDC", "HIL", "CHL")
ODDS_THRESHOLD = 1.70
HOLDOUT_SHARE = 0.30
MIN_TOTAL_DECIDED = 30
MIN_TRAIN_DECIDED = 20
MIN_HOLDOUT_DECIDED = 10
ROBUST_TOTAL_DECIDED = 60
ROBUST_TRAIN_DECIDED = 40
ROBUST_HOLDOUT_DECIDED = 20


def _number(value: Any) -> float | None:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if math.isfinite(number) else None


def _valid_odds(value: Any) -> float | None:
    odds = _number(value)
    return odds if odds is not None and odds > 1.0 else None


def _timestamp(value: Any) -> datetime | None:
    text = str(value or "").strip()
    if not text:
        return None
    try:
        return datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        return None


def _history_rows(payload: Any) -> list[dict[str, Any]]:
    if not isinstance(payload, dict):
        return []
    history = payload.get("prediction_history", payload)
    if not isinstance(history, dict):
        return []
    rows = history.get("rows") or []
    return [row for row in rows if isinstance(row, dict)]


def _line(grade: dict[str, Any]) -> float | None:
    return _number(grade.get("line", grade.get("condition")))


def _selected_line(grade: dict[str, Any], market: str) -> float | None:
    line = _line(grade)
    if line is None:
        return None
    if market == "HDC" and str(grade.get("side") or "").upper() == "A":
        return -line
    return line


def _direction(grade: dict[str, Any], market: str, row: dict[str, Any]) -> str | None:
    side = str(grade.get("side") or grade.get("selection") or "").upper()
    if market == "HDC":
        if side not in {"H", "A"}:
            return None
        team = grade.get("selected_team") or grade.get("selection_team")
        if not team:
            team = row.get("home") if side == "H" else row.get("away")
        normalized = "".join(str(team or "").split()).casefold()
        return f"team:{normalized}" if normalized else f"side:{side}"
    if market in {"HIL", "CHL"}:
        return {"H": "over", "L": "under"}.get(side)
    return None


def _role(grade: dict[str, Any], market: str) -> str | None:
    side = str(grade.get("side") or grade.get("selection") or "").upper()
    line = _line(grade)
    if market != "HDC":
        return {
            ("HIL", "H"): "入球大",
            ("HIL", "L"): "入球細",
            ("CHL", "H"): "角球大",
            ("CHL", "L"): "角球細",
        }.get((market, side))
    if line is None or side not in {"H", "A"}:
        return None
    if abs(line) < 1e-9:
        return "平手盤（主）" if side == "H" else "平手盤（客）"
    if side == "H":
        return "主讓" if line < 0 else "主受讓"
    return "客讓" if line > 0 else "客受讓"


def _line_bucket(grade: dict[str, Any], market: str) -> str | None:
    value = _selected_line(grade, market)
    if value is None:
        return None
    magnitude = abs(value)
    if market == "HDC":
        if magnitude < 1e-9:
            return "平手"
        if magnitude <= 0.5:
            return "淺盤≤0.5"
        if magnitude <= 1.0:
            return "中盤0.75–1.0"
        return "深盤>1.0"
    if market == "HIL":
        if value <= 2.5:
            return "≤2.5"
        if value <= 3.0:
            return "2.75–3.0"
        return ">3.0"
    if value <= 9.5:
        return "≤9.5"
    if value <= 10.5:
        return "10–10.5"
    return "≥10.75"


def _probability_bucket(grade: dict[str, Any]) -> str | None:
    value = _number(grade.get("probability"))
    if value is None:
        return None
    if value > 1.0:
        value /= 100.0
    if not 0.0 <= value <= 1.0:
        return None
    if value < 0.50:
        return "<50%"
    if value < 0.60:
        return "50–59.9%"
    return "≥60%"


def _odds_tier(odds: float) -> str:
    return "≥1.70" if odds >= ODDS_THRESHOLD else "<1.70"


def _settlement_return(grade: dict[str, Any], odds: float) -> float:
    settlement = str(grade.get("settlement") or "").strip().casefold()
    if settlement in {"won", "win"}:
        return odds - 1.0
    if settlement in {"half won", "half_won", "half-win"}:
        return (odds - 1.0) / 2.0
    if settlement in {"refunded", "refund", "push", "void"}:
        return 0.0
    if settlement in {"half lost", "half_lost", "half-loss"}:
        return -0.5
    if settlement in {"lost", "loss"}:
        return -1.0
    hit = grade.get("hit")
    if hit is True:
        return odds - 1.0
    if hit is False:
        return -1.0
    return 0.0


def _deduplicated_panels(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    candidates: dict[tuple[str, str, str], list[tuple[dict[str, Any], dict[str, Any]]]] = defaultdict(list)
    for row in rows:
        fixture = str(row.get("match_id") or "")
        stage = str(row.get("stage") or "")
        if not fixture or stage not in STAGE_RANK:
            continue
        for grade in row.get("market_grades") or []:
            if not isinstance(grade, dict):
                continue
            market = str(grade.get("code") or "")
            if market in MARKETS:
                candidates[(fixture, market, stage)].append((row, grade))

    selected: dict[tuple[str, str, str], dict[str, Any]] = {}
    for key, values in candidates.items():
        row, grade = max(
            values,
            key=lambda pair: (
                str(pair[0].get("predicted_at") or pair[0].get("ts") or ""),
                json.dumps(pair, ensure_ascii=False, sort_keys=True, default=str),
            ),
        )
        market = key[1]
        odds = _valid_odds(grade.get("odds"))
        direction = _direction(grade, market, row)
        selected[key] = {
            "fixture": key[0],
            "market": market,
            "stage": key[2],
            "kickoff": _timestamp(row.get("kickoff") or row.get("kickoff_hkt")),
            "predicted_at": str(row.get("predicted_at") or row.get("ts") or ""),
            "direction": direction,
            "line": _selected_line(grade, market),
            "role": _role(grade, market),
            "line_bucket": _line_bucket(grade, market),
            "probability_bucket": _probability_bucket(grade),
            "odds": odds,
            "odds_tier": _odds_tier(odds) if odds is not None else None,
            "graded": grade.get("grade_status") == "GRADED",
            "hit": grade.get("hit"),
            "settlement": grade.get("settlement"),
            "return": _settlement_return(grade, odds) if odds is not None else None,
        }

    panels: dict[tuple[str, str], dict[str, Any]] = {}
    for (fixture, market, stage), item in selected.items():
        panel = panels.setdefault(
            (fixture, market),
            {"fixture": fixture, "market": market, "stages": {}, "kickoff": item["kickoff"]},
        )
        panel["stages"][stage] = item
        if panel["kickoff"] is None and item["kickoff"] is not None:
            panel["kickoff"] = item["kickoff"]
    return list(panels.values())


def _relative_path(values: list[str | None]) -> str | None:
    if any(value is None for value in values):
        return None
    labels: dict[str, str] = {}
    alphabet = iter(("A", "B", "C"))
    output = []
    for value in values:
        assert value is not None
        if value not in labels:
            labels[value] = next(alphabet)
        output.append(labels[value])
    return "→".join(output)


def _numeric_path(values: list[float | None], tolerance: float = 1e-9) -> str | None:
    if any(value is None for value in values):
        return None
    clean = [float(value) for value in values if value is not None]
    if max(clean) - min(clean) <= tolerance:
        return "不變"
    if len(clean) == 3 and abs(clean[0] - clean[2]) <= tolerance:
        return "先變後返原位"
    deltas = [right - left for left, right in zip(clean, clean[1:])]
    if all(delta > tolerance for delta in deltas):
        return "持續上升"
    if all(delta < -tolerance for delta in deltas):
        return "持續下降"
    return "反覆變動"


def _odds_path(values: list[float | None]) -> str | None:
    path = _numeric_path(values, tolerance=0.005)
    return {
        "不變": "賠率不變",
        "先變後返原位": "賠率先變後返原位",
        "持續上升": "賠率持續上升",
        "持續下降": "賠率持續下降",
        "反覆變動": "賠率反覆",
    }.get(path) if path else None


def _terminal_sample(panel: dict[str, Any], stages: tuple[str, ...]) -> dict[str, Any] | None:
    items = [panel["stages"].get(stage) for stage in stages]
    if any(item is None for item in items):
        return None
    terminal = items[-1]
    assert terminal is not None
    if not terminal["graded"] or terminal["odds"] is None:
        return None
    if terminal["hit"] not in {True, False, None}:
        return None
    return {
        "fixture": panel["fixture"],
        "market": panel["market"],
        "kickoff": panel["kickoff"],
        "stages": items,
        "terminal": terminal,
    }


def _candidate_definitions(sample: dict[str, Any]) -> list[tuple[str, str]]:
    stages = sample["stages"]
    terminal = sample["terminal"]
    stage_names = tuple(item["stage"] for item in stages)
    prefix = "→".join(stage_names)
    role = terminal["role"]
    line_bucket = terminal["line_bucket"]
    probability = terminal["probability_bucket"]
    definitions: list[tuple[str, str]] = [("all", f"{prefix} 全部")]
    if role:
        definitions.append(("role", f"{prefix}｜{role}"))
    if line_bucket:
        definitions.append(("line_bucket", f"{prefix}｜{line_bucket}"))
    if probability:
        definitions.append(("probability", f"{prefix}｜模型概率 {probability}"))

    if len(stages) >= 2:
        direction_path = _relative_path([item["direction"] for item in stages])
        line_path = _numeric_path([item["line"] for item in stages])
        odds_path = _odds_path([item["odds"] for item in stages])
        tier_path = "→".join(str(item["odds_tier"]) for item in stages) if all(
            item["odds_tier"] for item in stages
        ) else None
        if direction_path:
            definitions.append(("direction_path", f"{prefix}｜方向 {direction_path}"))
            if role:
                definitions.append(("direction_role", f"{prefix}｜方向 {direction_path}｜{role}"))
        if line_path:
            definitions.append(("line_path", f"{prefix}｜盤口 {line_path}"))
        if odds_path:
            definitions.append(("odds_path", f"{prefix}｜{odds_path}"))
        if tier_path:
            definitions.append(("tier_path", f"{prefix}｜賠率層 {tier_path}"))
        if direction_path and line_path:
            definitions.append(
                ("direction_line", f"{prefix}｜方向 {direction_path}｜盤口 {line_path}")
            )
        if direction_path and odds_path:
            definitions.append(
                ("direction_odds", f"{prefix}｜方向 {direction_path}｜{odds_path}")
            )
        if direction_path and line_path and role:
            definitions.append(
                (
                    "direction_line_role",
                    f"{prefix}｜方向 {direction_path}｜盤口 {line_path}｜{role}",
                )
            )
    return definitions


def _all_samples(panels: list[dict[str, Any]]) -> list[dict[str, Any]]:
    output = []
    for panel in panels:
        for stages in (
            ("首預",), ("T-30",), ("T-5",),
            ("首預", "T-30"), ("T-30", "T-5"), ("首預", "T-5"),
            ("首預", "T-30", "T-5"),
        ):
            sample = _terminal_sample(panel, stages)
            if sample is not None:
                output.append(sample)
    return output


def _wilson(hits: int, decided: int) -> list[float] | None:
    if decided <= 0:
        return None
    z = 1.959963984540054
    p = hits / decided
    denominator = 1.0 + z * z / decided
    center = (p + z * z / (2.0 * decided)) / denominator
    half = z / denominator * math.sqrt(p * (1.0 - p) / decided + z * z / (4.0 * decided * decided))
    return [round(max(0.0, center - half), 6), round(min(1.0, center + half), 6)]


def _metrics(samples: Iterable[dict[str, Any]]) -> dict[str, Any]:
    samples = list(samples)
    decided = [sample for sample in samples if sample["terminal"]["hit"] is not None]
    hits = sum(sample["terminal"]["hit"] is True for sample in decided)
    returns = [sample["terminal"]["return"] for sample in samples if sample["terminal"]["return"] is not None]
    return {
        "settled": len(samples),
        "decided": len(decided),
        "hits": hits,
        "pushes": len(samples) - len(decided),
        "accuracy": round(hits / len(decided), 6) if decided else None,
        "wilson95": _wilson(hits, len(decided)),
        "roi": round(sum(returns) / len(returns), 6) if returns else None,
        "average_odds": round(
            sum(sample["terminal"]["odds"] for sample in samples) / len(samples), 4
        ) if samples else None,
    }


def _split_fixture_ids(samples: list[dict[str, Any]]) -> tuple[set[str], set[str], str | None]:
    fixture_times: dict[str, datetime] = {}
    for sample in samples:
        kickoff = sample["kickoff"]
        if kickoff is None:
            continue
        old = fixture_times.get(sample["fixture"])
        if old is None or kickoff < old:
            fixture_times[sample["fixture"]] = kickoff
    ordered = sorted(fixture_times, key=lambda fixture: (fixture_times[fixture], fixture))
    if len(ordered) < 2:
        return set(ordered), set(), None
    cut = max(1, min(len(ordered) - 1, int(len(ordered) * (1.0 - HOLDOUT_SHARE))))
    return set(ordered[:cut]), set(ordered[cut:]), fixture_times[ordered[cut]].isoformat()


def _binomial_tail(hits: int, decided: int, baseline: float) -> float | None:
    if decided <= 0 or not 0.0 <= baseline <= 1.0:
        return None
    total = 0.0
    for value in range(hits, decided + 1):
        total += math.comb(decided, value) * baseline ** value * (1.0 - baseline) ** (decided - value)
    return min(1.0, total)


def _benjamini_hochberg(items: list[dict[str, Any]]) -> None:
    eligible = [(index, item["p_value"]) for index, item in enumerate(items) if item["p_value"] is not None]
    eligible.sort(key=lambda pair: pair[1])
    running = 1.0
    adjusted: dict[int, float] = {}
    count = len(eligible)
    for rank in range(count, 0, -1):
        index, value = eligible[rank - 1]
        running = min(running, value * count / rank)
        adjusted[index] = min(1.0, running)
    for index, item in enumerate(items):
        item["q_value"] = round(adjusted[index], 6) if index in adjusted else None


def mine(system: str, payload: dict[str, Any]) -> dict[str, Any]:
    rows = _history_rows(payload)
    panels = _deduplicated_panels(rows)
    samples = _all_samples(panels)
    train_ids, holdout_ids, cutoff = _split_fixture_ids(samples)

    candidate_samples: dict[tuple[str, str, str, str], list[dict[str, Any]]] = defaultdict(list)
    baseline_samples: dict[tuple[str, str, str], list[dict[str, Any]]] = defaultdict(list)
    for sample in samples:
        terminal = sample["terminal"]
        stage = terminal["stage"]
        tier = terminal["odds_tier"]
        market = sample["market"]
        baseline_samples[(market, stage, tier)].append(sample)
        for family, label in _candidate_definitions(sample):
            candidate_samples[(market, stage, tier, family + "|" + label)].append(sample)

    candidates = []
    for (market, stage, tier, encoded), cohort in candidate_samples.items():
        family, label = encoded.split("|", 1)
        total = _metrics(cohort)
        if total["decided"] < MIN_TOTAL_DECIDED:
            continue
        train = _metrics(sample for sample in cohort if sample["fixture"] in train_ids)
        holdout = _metrics(sample for sample in cohort if sample["fixture"] in holdout_ids)
        baseline_holdout = _metrics(
            sample for sample in baseline_samples[(market, stage, tier)]
            if sample["fixture"] in holdout_ids
        )
        baseline_accuracy = baseline_holdout["accuracy"]
        lift = (
            round(holdout["accuracy"] - baseline_accuracy, 6)
            if holdout["accuracy"] is not None and baseline_accuracy is not None
            else None
        )
        robust = bool(
            total["decided"] >= ROBUST_TOTAL_DECIDED
            and train["decided"] >= ROBUST_TRAIN_DECIDED
            and holdout["decided"] >= ROBUST_HOLDOUT_DECIDED
            and lift is not None
            and lift >= 0
        )
        sample_qualified = bool(
            train["decided"] >= MIN_TRAIN_DECIDED
            and holdout["decided"] >= MIN_HOLDOUT_DECIDED
        )
        p_value = (
            _binomial_tail(holdout["hits"], holdout["decided"], baseline_accuracy)
            if sample_qualified and baseline_accuracy is not None
            else None
        )
        candidates.append({
            "market": market,
            "decision_stage": stage,
            "odds_tier": tier,
            "family": family,
            "condition": label,
            "total": total,
            "train": train,
            "holdout": holdout,
            "holdout_baseline": baseline_holdout,
            "holdout_lift": lift,
            "sample_qualified": sample_qualified,
            "robust": robust,
            "p_value": round(p_value, 6) if p_value is not None else None,
        })
    _benjamini_hochberg(candidates)

    def rank(item: dict[str, Any]) -> tuple[Any, ...]:
        interval = item["holdout"]["wilson95"] or [-1.0, -1.0]
        return (
            item["robust"],
            item["sample_qualified"],
            interval[0],
            item["holdout"]["accuracy"] or -1.0,
            item["total"]["decided"],
            -len(item["condition"]),
        )

    ranked = sorted(candidates, key=rank, reverse=True)
    return {
        "system": system,
        "source_rows": len(rows),
        "fixture_market_panels": len(panels),
        "settled_priced_stage_sequences": len(samples),
        "holdout_cutoff": cutoff,
        "candidate_count_after_total_support": len(candidates),
        "sample_qualified_candidates": sum(item["sample_qualified"] for item in candidates),
        "robust_candidates": sum(item["robust"] for item in candidates),
        "fdr_qualified_candidates": sum(
            item["q_value"] is not None and item["q_value"] <= 0.05 for item in candidates
        ),
        # Keep a bounded but broad aggregate result so uncommon trajectory
        # families (for example A→B→A) are not crowded out by simpler parent
        # conditions.  No fixture-level rows are emitted.
        "top_conditions": ranked,
    }


def build_report(footbreak: dict[str, Any], crown: dict[str, Any]) -> dict[str, Any]:
    return {
        "report": "settled_condition_mining_v1",
        "read_only": True,
        "aggregate_only": True,
        "method": {
            "unit": "one fixture-market panel; one terminal stage grade per candidate",
            "stages": list(STAGES),
            "sequence_lengths": [1, 2, 3],
            "supports_A_B_A": True,
            "valid_odds": "finite decimal odds > 1.0",
            "odds_tiers": [">=1.70", "<1.70"],
            "pushes": "reported but excluded from hit-rate denominator",
            "holdout": "latest 30% of fixtures by kickoff; no random shuffle",
            "ranking": "robust/sample-qualified first, then holdout Wilson lower bound",
            "multiple_testing": "one-sided binomial screen versus same market-stage-tier holdout baseline; Benjamini-Hochberg q-values",
            "minimum_support": {
                "display_total_decided": MIN_TOTAL_DECIDED,
                "sample_qualified_train_decided": MIN_TRAIN_DECIDED,
                "sample_qualified_holdout_decided": MIN_HOLDOUT_DECIDED,
                "robust_total_decided": ROBUST_TOTAL_DECIDED,
                "robust_train_decided": ROBUST_TRAIN_DECIDED,
                "robust_holdout_decided": ROBUST_HOLDOUT_DECIDED,
            },
        },
        "systems": {
            "footbreak": mine("footbreak", footbreak),
            "crown": mine("crown", crown),
        },
    }


def _load(path: Path) -> dict[str, Any]:
    with path.open(encoding="utf-8") as handle:
        payload = json.load(handle)
    if not isinstance(payload, dict):
        raise ValueError(f"{path} must contain a JSON object")
    return payload


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--footbreak", type=Path, required=True)
    parser.add_argument("--crown", type=Path, required=True)
    parser.add_argument("--out", type=Path)
    args = parser.parse_args()
    report = build_report(_load(args.footbreak), _load(args.crown))
    rendered = json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True)
    if args.out:
        args.out.write_text(rendered + "\n", encoding="utf-8")
    else:
        print(rendered)


if __name__ == "__main__":
    main()
