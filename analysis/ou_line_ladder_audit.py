#!/usr/bin/env python3
"""Read-only OU (goals over/under) three-stage audit that follows every exact quoted line."""

from __future__ import annotations

import argparse
import collections
import json
import math
import sqlite3
import time
from typing import Any

STAGES = ("initial", "T30", "T5")
DECIDED = {"win", "half_win", "half_loss", "loss"}
HITS = {"win", "half_win"}


def number(value: Any) -> float | None:
    try:
        parsed = float(value)
    except (TypeError, ValueError):
        return None
    return parsed if math.isfinite(parsed) else None


def bucket_drift(gap: float) -> str:
    """Bucket the initial-to-T5 odds gap for the selected side.

    Positive gap means the odds shortened (T5 odds lower than initial) --
    i.e. the market grew more confident in that side.
    """
    if gap >= 0.20:
        return "收縮>=0.20"
    if gap >= 0.10:
        return "收縮0.10-0.20"
    if gap >= 0.05:
        return "收縮0.05-0.10"
    if gap > 0:
        return "收縮<0.05"
    return "持平或拉闊"


BUCKET_ORDER = ["收縮>=0.20", "收縮0.10-0.20", "收縮0.05-0.10", "收縮<0.05", "持平或拉闊"]


def line_number(value: Any) -> float | None:
    text = str(value or "").strip()
    if "/" in text:
        parts = [number(part.strip()) for part in text.split("/")]
        if not parts or any(part is None for part in parts):
            return None
        parsed = sum(part for part in parts if part is not None) / len(parts)
    else:
        parsed = number(text)
    if parsed is None:
        return None
    quarter = round(parsed * 4) / 4
    return quarter if abs(parsed - quarter) < 1e-8 else None


def split_line(value: float) -> list[float]:
    if abs(value * 2 - round(value * 2)) < 1e-9:
        return [value]
    return [value - 0.25, value + 0.25]


def settle_ou(line: float, side: str, total_goals: int) -> str:
    outcomes = []
    for half in split_line(line):
        diff = (total_goals - half) if side == "O" else (half - total_goals)
        outcomes.append(1 if diff > 1e-9 else -1 if diff < -1e-9 else 0)
    if len(outcomes) == 1:
        return {1: "win", 0: "push", -1: "loss"}[outcomes[0]]
    if outcomes == [1, 1]:
        return "win"
    if sum(outcomes) == 1:
        return "half_win"
    if sum(outcomes) == 0:
        return "push"
    if sum(outcomes) == -1:
        return "half_loss"
    return "loss"


def unit_return(status: str, odds: float) -> float:
    return {
        "win": odds - 1,
        "half_win": (odds - 1) / 2,
        "push": 0,
        "half_loss": -0.5,
        "loss": -1,
    }[status]


def actual_cover_side(line: float, total_goals: int) -> str:
    """Which side actually covers a fixed OU line, independent of any bet side.

    Used only for stages settled as "D" (平 — over/under odds exactly equal),
    where there is no selected side to grade a win/loss against.
    """
    outcomes = []
    for half in split_line(line):
        diff = total_goals - half
        outcomes.append(1 if diff > 1e-9 else -1 if diff < -1e-9 else 0)
    total = sum(outcomes)
    if len(outcomes) == 1:
        return {1: "O", 0: "push", -1: "U"}[outcomes[0]]
    if total == 0:
        return "push"
    return "O" if total > 0 else "U"


def outcome_probability(rows: list[dict[str, Any]]) -> dict[str, Any]:
    """不看方向是否命中，只看實際賽果貼線情況：大球開出/走盤/小球開出。"""
    settled = [row for row in rows if row.get("actual_result")]
    counts = collections.Counter(row["actual_result"] for row in settled)
    total = len(settled)
    labels = ["大球開出", "走盤", "小球開出"]
    return {
        "observations": len(rows),
        "unique_fixtures": len({row["fixture_id"] for row in rows}),
        "settled": total,
        "pending": len(rows) - total,
        "counts": {label: counts.get(label, 0) for label in labels},
        "probability": {
            label: (counts.get(label, 0) / total if total else None) for label in labels
        },
    }


def metrics(rows: list[dict[str, Any]]) -> dict[str, Any]:
    settled = [row for row in rows if row.get("settlement")]
    decided = [row for row in settled if row["settlement"] in DECIDED]
    hits = sum(row["settlement"] in HITS for row in decided)
    profit = sum(float(row.get("unit_return") or 0) for row in settled)
    return {
        "observations": len(rows),
        "unique_fixtures": len({row["fixture_id"] for row in rows}),
        "settled": len(settled),
        "pending": len(rows) - len(settled),
        "pushes": sum(row.get("settlement") == "push" for row in rows),
        "decided": len(decided),
        "hits": hits,
        "hit_rate": hits / len(decided) if decided else None,
        "unit_profit": round(profit, 6),
        "roi": profit / len(settled) if settled else None,
    }


def run(db: sqlite3.Connection, threshold: float) -> dict[str, Any]:
    db.row_factory = sqlite3.Row
    db.execute("PRAGMA query_only=ON")
    columns = {row["name"] for row in db.execute("PRAGMA table_info(matches)")}
    home_expr = "m.home_team" if "home_team" in columns else "NULL"
    away_expr = "m.away_team" if "away_team" in columns else "NULL"
    rows = db.execute(
        f"""
        SELECT q.match_id,q.stage,q.provider,q.line_key,q.selection,
               q.decimal_odds,q.captured_at,q.origin,m.kickoff_utc,
               {home_expr} AS home_team,{away_expr} AS away_team,
               COALESCE(rr.home_score,r.home_score) AS home_score,
               COALESCE(rr.away_score,r.away_score) AS away_score
        FROM research_timeline_snapshots q
        JOIN matches m ON m.id=q.match_id
        LEFT JOIN research_results rr ON rr.match_id=m.id
        LEFT JOIN results r ON r.match_id=m.id
        WHERE q.stage IN ('initial','T30','T5')
          AND q.provider IN ('hkjc','pinnacle')
          AND q.market='OU'
        ORDER BY q.match_id,q.provider,q.stage,q.line_key,q.selection,q.captured_at
        """
    )
    side_map = {"H": "O", "O": "O", "L": "U", "U": "U"}

    quotes: dict[tuple[str, str, float, str, str], dict[str, Any]] = {}
    metadata: dict[str, dict[str, Any]] = {}
    for row in rows:
        kickoff = number(row["kickoff_utc"])
        captured = number(row["captured_at"])
        if kickoff is None or captured is None or captured >= kickoff:
            continue
        stage = str(row["stage"])
        origin = str(row["origin"] or "")
        if (stage == "initial" and origin != "external_opening") or (
            stage != "initial" and origin != "live_observation"
        ):
            continue
        line = line_number(row["line_key"])
        side = side_map.get(str(row["selection"] or "").upper())
        odds = number(row["decimal_odds"])
        if line is None or side is None or odds is None or odds <= 1:
            continue
        match_id = str(row["match_id"])
        provider = str(row["provider"]).lower()
        key = (match_id, provider, line, stage, side)
        candidate = {"odds": odds, "captured_at": captured}
        if key not in quotes or captured > quotes[key]["captured_at"]:
            quotes[key] = candidate
        metadata[match_id] = {
            "kickoff": int(kickoff),
            "home_team": row["home_team"],
            "away_team": row["away_team"],
            "home_score": row["home_score"],
            "away_score": row["away_score"],
        }

    paired: dict[tuple[str, str, float, str], dict[str, dict[str, Any]]] = (
        collections.defaultdict(dict)
    )
    for (match_id, provider, line, stage, side), quote in quotes.items():
        paired[(match_id, provider, line, stage)][side] = quote

    stage_rows: dict[tuple[str, str, float], dict[str, dict[str, Any]]] = (
        collections.defaultdict(dict)
    )
    for (match_id, provider, line, stage), sides in paired.items():
        if "O" not in sides or "U" not in sides:
            continue
        if abs(sides["O"]["odds"] - sides["U"]["odds"]) < 1e-12:
            stage_rows[(match_id, provider, line)][stage] = {
                "side": "D",
                "line": line,
                "odds": sides["O"]["odds"],
                "other_odds": sides["U"]["odds"],
            }
            continue
        selected = "O" if sides["O"]["odds"] < sides["U"]["odds"] else "U"
        opposite = "U" if selected == "O" else "O"
        stage_rows[(match_id, provider, line)][stage] = {
            "side": selected,
            "line": line,
            "odds": sides[selected]["odds"],
            "other_odds": sides[opposite]["odds"],
        }

    complete = []
    eligible = []
    for (match_id, provider, line), stages in stage_rows.items():
        if not set(STAGES).issubset(stages):
            continue
        meta = metadata[match_id]
        observation = {
            "fixture_id": match_id,
            "kickoff": meta["kickoff"],
            "home_team": meta["home_team"],
            "away_team": meta["away_team"],
            "provider": provider,
            "line": line,
            "direction_path": "→".join(stages[stage]["side"] for stage in STAGES),
            **{stage: stages[stage] for stage in STAGES},
        }
        has_score = meta["home_score"] is not None and meta["away_score"] is not None
        total_goals = (
            int(meta["home_score"]) + int(meta["away_score"]) if has_score else None
        )
        if has_score:
            cover_neutral = actual_cover_side(line, total_goals)
            observation["actual_result"] = {
                "O": "大球開出",
                "U": "小球開出",
                "push": "走盤",
            }[cover_neutral]
        else:
            observation["actual_result"] = None
        if stages["T5"]["side"] == "D":
            observation["settlement"] = None
            observation["unit_return"] = None
            if has_score:
                cover = actual_cover_side(line, total_goals)
                observation["t5_level_result"] = {
                    "O": "大球開出",
                    "U": "小球開出",
                    "push": "走盤",
                }[cover]
            else:
                observation["t5_level_result"] = None
        else:
            observation["t5_level_result"] = None
            if has_score:
                status = settle_ou(line, stages["T5"]["side"], total_goals)
                observation["settlement"] = status
                observation["unit_return"] = unit_return(status, stages["T5"]["odds"])
            else:
                observation["settlement"] = None
                observation["unit_return"] = None
        complete.append(observation)
        if all(stages[stage]["odds"] > threshold for stage in STAGES):
            eligible.append(observation)

    stage_presence: dict[tuple[str, str], set[str]] = collections.defaultdict(set)
    for (match_id, provider, _line), stages in stage_rows.items():
        stage_presence[(match_id, provider)].update(stages.keys())
    any_ou_fixtures: dict[str, set[str]] = collections.defaultdict(set)
    for match_id, provider, _line, _stage in paired:
        any_ou_fixtures[provider].add(match_id)
    all_three_any_line_fixtures: dict[str, set[str]] = collections.defaultdict(set)
    for (match_id, provider), stages_seen in stage_presence.items():
        if set(STAGES).issubset(stages_seen):
            all_three_any_line_fixtures[provider].add(match_id)
    data_availability = {
        provider: {
            "raw_any_ou_quote_fixtures": len(any_ou_fixtures.get(provider, set())),
            "all_three_stages_any_line_fixtures": len(
                all_three_any_line_fixtures.get(provider, set())
            ),
            "same_line_all_three_stages_fixtures": len(
                {row["fixture_id"] for row in complete if row["provider"] == provider}
            ),
            "eligible_after_threshold_fixtures": len(
                {row["fixture_id"] for row in eligible if row["provider"] == provider}
            ),
        }
        for provider in ("hkjc", "pinnacle")
    }

    now_ms = time.time() * 1000
    stage_pattern_breakdown: dict[str, dict[str, dict[str, int]]] = {}
    for provider in ("hkjc", "pinnacle"):
        fixtures_with_quotes = any_ou_fixtures.get(provider, set())
        pattern_counts: dict[str, int] = collections.Counter()
        pattern_past_counts: dict[str, int] = collections.Counter()
        pattern_future_counts: dict[str, int] = collections.Counter()
        pattern_examples: dict[str, list[dict[str, Any]]] = collections.defaultdict(list)
        for match_id in fixtures_with_quotes:
            present = stage_presence.get((match_id, provider), set())
            missing = [stage for stage in STAGES if stage not in present]
            pattern = "齊三個時點" if not missing else "缺:" + "+".join(missing)
            pattern_counts[pattern] += 1
            meta = metadata.get(match_id)
            if meta is not None:
                if meta["kickoff"] < now_ms:
                    pattern_past_counts[pattern] += 1
                else:
                    pattern_future_counts[pattern] += 1
            if len(pattern_examples[pattern]) < 5 and meta is not None:
                pattern_examples[pattern].append(
                    {
                        "fixture_id": match_id,
                        "home_team": meta["home_team"],
                        "away_team": meta["away_team"],
                        "kickoff": meta["kickoff"],
                        "stages_present": sorted(present),
                    }
                )
        stage_pattern_breakdown[provider] = {
            "counts": dict(pattern_counts),
            "past_counts": dict(pattern_past_counts),
            "future_counts": dict(pattern_future_counts),
            "examples": {k: v for k, v in pattern_examples.items()},
        }

    conditions = []
    grouped: dict[tuple[str, str, float], list[dict[str, Any]]] = collections.defaultdict(list)
    for row in eligible:
        grouped[(row["provider"], row["direction_path"], row["line"])].append(row)
    for (provider, path, line), group in sorted(grouped.items()):
        conditions.append(
            {
                "provider": provider,
                "direction_path": path,
                "line": line,
                **metrics(group),
            }
        )

    line_path_probability = [
        {
            "provider": provider,
            "line": line,
            "direction_path": path,
            **outcome_probability(group),
        }
        for (provider, path, line), group in sorted(grouped.items())
    ]

    provider_summary = {
        provider: metrics([row for row in eligible if row["provider"] == provider])
        for provider in ("hkjc", "pinnacle")
    }

    path_groups: dict[tuple[str, str], list[dict[str, Any]]] = collections.defaultdict(list)
    for row in eligible:
        path_groups[(row["provider"], row["direction_path"])].append(row)
    path_probability = [
        {
            "provider": provider,
            "direction_path": path,
            **outcome_probability(group),
        }
        for (provider, path), group in sorted(path_groups.items())
    ]

    drift_groups: dict[tuple[str, str, str], list[dict[str, Any]]] = collections.defaultdict(list)
    outcome_drift_groups: dict[tuple[str, str, str], list[float]] = collections.defaultdict(list)
    for row in eligible:
        path = row["direction_path"]
        if path not in ("O→O→O", "U→U→U"):
            continue
        gap = round(row["initial"]["odds"] - row["T5"]["odds"], 4)
        bucket = bucket_drift(gap)
        drift_groups[(row["provider"], path, bucket)].append(row)
        if row.get("actual_result") is not None:
            outcome_drift_groups[(row["provider"], path, row["actual_result"])].append(gap)

    odds_drift_breakdown = [
        {
            "provider": provider,
            "direction_path": path,
            "bucket": bucket,
            "avg_gap": round(sum(r["initial"]["odds"] - r["T5"]["odds"] for r in group) / len(group), 4),
            "avg_initial_odds": round(sum(r["initial"]["odds"] for r in group) / len(group), 4),
            "avg_t5_odds": round(sum(r["T5"]["odds"] for r in group) / len(group), 4),
            **outcome_probability(group),
        }
        for (provider, path, bucket), group in sorted(
            drift_groups.items(),
            key=lambda kv: (kv[0][0], kv[0][1], BUCKET_ORDER.index(kv[0][2])),
        )
    ]
    drift_by_outcome = [
        {
            "provider": provider,
            "direction_path": path,
            "actual_result": outcome,
            "observations": len(gaps),
            "avg_gap": round(sum(gaps) / len(gaps), 4),
            "min_gap": round(min(gaps), 4),
            "max_gap": round(max(gaps), 4),
        }
        for (provider, path, outcome), gaps in sorted(outcome_drift_groups.items())
    ]

    margin_groups: dict[tuple[str, str], list[float]] = collections.defaultdict(list)
    odds_level_groups: dict[tuple[str, str], list[float]] = collections.defaultdict(list)
    for row in eligible:
        for stage in STAGES:
            odds = row[stage]["odds"]
            other = row[stage]["other_odds"]
            odds_level_groups[(row["provider"], stage)].append(odds)
            if odds and other and odds > 1 and other > 1:
                margin_groups[(row["provider"], stage)].append(1 / odds + 1 / other - 1)

    margin_summary = [
        {
            "provider": provider,
            "stage": stage,
            "observations": len(margins),
            "avg_margin_pct": round(sum(margins) / len(margins) * 100, 3),
        }
        for (provider, stage), margins in sorted(
            margin_groups.items(), key=lambda kv: (kv[0][0], STAGES.index(kv[0][1]))
        )
    ]
    odds_level_summary = [
        {
            "provider": provider,
            "stage": stage,
            "observations": len(vals),
            "avg_selected_odds": round(sum(vals) / len(vals), 4),
        }
        for (provider, stage), vals in sorted(
            odds_level_groups.items(), key=lambda kv: (kv[0][0], STAGES.index(kv[0][1]))
        )
    ]

    level_rows = [row for row in eligible if row["direction_path"].endswith("D")]
    level_groups: dict[tuple[str, str, float], collections.Counter] = collections.defaultdict(
        collections.Counter
    )
    for row in level_rows:
        key = (row["provider"], row["direction_path"], row["line"])
        label = row.get("t5_level_result") or "待賽果"
        level_groups[key][label] += 1
    level_breakdown = [
        {
            "provider": provider,
            "direction_path": path,
            "line": line,
            "observations": sum(counts.values()),
            "counts": dict(counts),
        }
        for (provider, path, line), counts in sorted(level_groups.items())
    ]
    level_summary = {
        "total": len(level_rows),
        "counts": dict(
            collections.Counter(
                row.get("t5_level_result") or "待賽果" for row in level_rows
            )
        ),
    }
    examples = sorted(
        eligible,
        key=lambda row: (row.get("settlement") is not None, row["kickoff"]),
        reverse=True,
    )[:10]
    level_involved_rows = sorted(
        (row for row in eligible if "D" in row["direction_path"]),
        key=lambda row: (row["provider"], row["direction_path"], row["line"], row["kickoff"]),
    )
    level_involved_detail = [
        {
            "provider": row["provider"],
            "direction_path": row["direction_path"],
            "line": row["line"],
            "fixture_id": row["fixture_id"],
            "home_team": row["home_team"],
            "away_team": row["away_team"],
            "kickoff": row["kickoff"],
            "initial_side": row["initial"]["side"],
            "initial_odds": row["initial"]["odds"],
            "t30_side": row["T30"]["side"],
            "t30_odds": row["T30"]["odds"],
            "t5_side": row["T5"]["side"],
            "t5_odds": row["T5"]["odds"],
            "actual_result": row.get("actual_result"),
        }
        for row in level_involved_rows
    ]
    recent_complete_rows = sorted(
        complete,
        key=lambda row: row["kickoff"],
        reverse=True,
    )[:100]
    return {
        "report": "ou_line_ladder_audit",
        "definition": {
            "identity": "fixture + provider + exact OU (大小球) line",
            "path": "initial side → T30 side → T5 side, each O/U/D (D = over odds == under odds exactly, 平)",
            "price_rule": f"selected/shared-D odds at all three stages strictly >{threshold:.2f}",
            "main_line_rule": "no stage-level main-line collapse; every exact line is followed independently",
            "level_rule": "when T5 side is D there is no bet side to grade; settlement is reported as 大球開出/小球開出/走盤 (actual cover side vs the fixed line) in level_breakdown, and excluded from hit-rate/ROI",
        },
        "summary": {
            "complete_line_paths": len(complete),
            "complete_unique_fixtures": len({row["fixture_id"] for row in complete}),
            "eligible_line_paths": len(eligible),
            "eligible_unique_fixtures": len({row["fixture_id"] for row in eligible}),
        },
        "provider_summary": provider_summary,
        "data_availability": data_availability,
        "stage_pattern_breakdown": stage_pattern_breakdown,
        "path_probability": path_probability,
        "line_path_probability": line_path_probability,
        "conditions": conditions,
        "examples": examples,
        "recent_complete_rows": recent_complete_rows,
        "level_breakdown": level_breakdown,
        "level_summary": level_summary,
        "level_involved_detail": level_involved_detail,
        "odds_drift_breakdown": odds_drift_breakdown,
        "drift_by_outcome": drift_by_outcome,
        "margin_summary": margin_summary,
        "odds_level_summary": odds_level_summary,
    }


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--db",
        default="file:/opt/odds-radar/data/data.db?mode=ro",
    )
    parser.add_argument("--threshold", type=float, default=1.70)
    args = parser.parse_args()
    db = sqlite3.connect(args.db, uri=True)
    try:
        report = run(db, args.threshold)
    finally:
        db.close()
    print(json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True))


if __name__ == "__main__":
    main()
