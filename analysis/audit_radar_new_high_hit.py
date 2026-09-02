#!/usr/bin/env python3
"""Read-only exploratory scan for new Odds Radar OU conditions."""

from __future__ import annotations

import collections
import json
import math
import sqlite3
from typing import Any, Callable


DB_URI = "file:/opt/odds-radar/data/data.db?mode=ro"
STAGES = ("initial", "T30", "T5")
SIDES = ("O", "U")
DECIDED = {"win", "half_win", "half_loss", "loss"}
HITS = {"win", "half_win"}


def number(value: Any) -> float | None:
    try:
        result = float(value)
    except (TypeError, ValueError):
        return None
    return result if math.isfinite(result) else None


def line_number(value: Any) -> float | None:
    text = str(value or "").strip().replace("O", "").replace("U", "")
    if "/" in text:
        parts = [number(part) for part in text.split("/", 1)]
        if all(part is not None for part in parts):
            return sum(parts) / 2
    return number(text)


def settle(total: int, line: float, side: str) -> str:
    split = [line] if abs(line * 2 - round(line * 2)) < 1e-9 else [line - 0.25, line + 0.25]
    outcomes = []
    for half in split:
        diff = (total - half) if side == "O" else (half - total)
        outcomes.append(1 if diff > 1e-9 else -1 if diff < -1e-9 else 0)
    if outcomes == [1] or outcomes == [1, 1]:
        return "win"
    if outcomes == [-1] or outcomes == [-1, -1]:
        return "loss"
    total_outcome = sum(outcomes)
    if total_outcome == 1:
        return "half_win"
    if total_outcome == -1:
        return "half_loss"
    return "push"


def unit_return(status: str, odds: float) -> float:
    return {
        "win": odds - 1,
        "half_win": (odds - 1) / 2,
        "push": 0,
        "half_loss": -0.5,
        "loss": -1,
    }[status]


def wilson(hits: int, decided: int, z: float = 1.959963984540054) -> list[float | None]:
    if decided <= 0:
        return [None, None]
    p = hits / decided
    denominator = 1 + z * z / decided
    centre = (p + z * z / (2 * decided)) / denominator
    margin = z * math.sqrt((p * (1 - p) + z * z / (4 * decided)) / decided) / denominator
    return [round(max(0, centre - margin), 6), round(min(1, centre + margin), 6)]


def drift_bucket(gap: float) -> str:
    if gap >= 0.20:
        return "short_020_plus"
    if gap >= 0.10:
        return "short_010_020"
    if gap >= 0.05:
        return "short_005_010"
    if gap > 0:
        return "short_000_005"
    return "flat_or_wider"


def line_band(line: float) -> str:
    if line <= 2.25:
        return "line_le_2.25"
    if line <= 2.50:
        return "line_2.25_2.50"
    if line <= 2.75:
        return "line_2.50_2.75"
    return "line_gt_2.75"


def odds_band(odds: float) -> str:
    if odds <= 1.80:
        return "odds_le_1.80"
    if odds <= 1.90:
        return "odds_1.80_1.90"
    if odds <= 2.05:
        return "odds_1.90_2.05"
    return "odds_gt_2.05"


def columns(db: sqlite3.Connection, table: str) -> set[str]:
    return {str(row[1]) for row in db.execute(f"PRAGMA table_info({table})")}


def load_rows(db: sqlite3.Connection) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    match_cols = columns(db, "matches")
    snap_cols = columns(db, "research_timeline_snapshots")
    league_expr = "m.league" if "league" in match_cols else "''"
    home_expr = "m.home_team" if "home_team" in match_cols else "''"
    away_expr = "m.away_team" if "away_team" in match_cols else "''"
    source_updated = "s.source_updated_at" if "source_updated_at" in snap_cols else "NULL"
    sql = f"""
    SELECT s.match_id,s.provider,s.stage,s.line_key,s.selection,s.decimal_odds,
           s.is_main,s.captured_at,{source_updated} AS source_updated_at,s.origin,
           m.kickoff_utc,{league_expr} AS league,{home_expr} AS home_team,
           {away_expr} AS away_team,
           COALESCE(rr.home_score,r.home_score) AS home_score,
           COALESCE(rr.away_score,r.away_score) AS away_score
    FROM research_timeline_snapshots s
    JOIN matches m ON m.id=s.match_id
    LEFT JOIN research_results rr ON rr.match_id=m.id
    LEFT JOIN results r ON r.match_id=m.id
    WHERE s.market='OU'
      AND s.provider IN ('hkjc','pinnacle')
      AND s.stage IN ('initial','T30','T5')
      AND s.selection IN ('O','U')
    ORDER BY s.match_id,s.provider,s.stage,s.captured_at
    """
    quotes: dict[tuple[str, str, str, float, str], dict[str, Any]] = {}
    meta: dict[str, dict[str, Any]] = {}
    rejected_post_kickoff = 0
    rejected_origin = 0
    for row in db.execute(sql):
        kickoff = number(row["kickoff_utc"])
        captured = number(row["captured_at"])
        line = line_number(row["line_key"])
        odds = number(row["decimal_odds"])
        if None in (kickoff, captured, line, odds) or odds <= 1:
            continue
        if captured >= kickoff:
            rejected_post_kickoff += 1
            continue
        stage = str(row["stage"])
        origin = str(row["origin"] or "")
        if (stage == "initial" and origin != "external_opening") or (
            stage != "initial" and origin != "live_observation"
        ):
            rejected_origin += 1
            continue
        match_id = str(row["match_id"])
        provider = str(row["provider"]).lower()
        side = str(row["selection"])
        key = (match_id, provider, stage, float(line), side)
        candidate = {
            "odds": float(odds),
            "is_main": int(row["is_main"] or 0),
            "captured_at": int(captured),
            "effective_at": int(number(row["source_updated_at"]) or captured),
        }
        if key not in quotes or candidate["captured_at"] > quotes[key]["captured_at"]:
            quotes[key] = candidate
        meta[match_id] = {
            "kickoff": int(kickoff),
            "league": str(row["league"] or "Unknown"),
            "home_team": str(row["home_team"] or ""),
            "away_team": str(row["away_team"] or ""),
            "home_score": row["home_score"],
            "away_score": row["away_score"],
        }

    groups: dict[tuple[str, str, float], dict[str, dict[str, dict[str, Any]]]] = (
        collections.defaultdict(lambda: collections.defaultdict(dict))
    )
    for (match_id, provider, stage, line, side), quote in quotes.items():
        groups[(match_id, provider, line)][stage][side] = quote

    observations: list[dict[str, Any]] = []
    for (match_id, provider, line), stages in groups.items():
        if not all(stage in stages and all(side in stages[stage] for side in SIDES) for stage in STAGES):
            continue
        decisions = []
        valid = True
        for stage in STAGES:
            over, under = stages[stage]["O"]["odds"], stages[stage]["U"]["odds"]
            if abs(over - under) < 1e-12:
                valid = False
                break
            side = "O" if over < under else "U"
            selected_odds = stages[stage][side]["odds"]
            if selected_odds <= 1.70:
                valid = False
                break
            decisions.append((side, selected_odds))
        if not valid:
            continue
        info = meta[match_id]
        if info["home_score"] is None or info["away_score"] is None:
            continue
        path = "→".join(side for side, _ in decisions)
        final_side = decisions[-1][0]
        initial_final_odds = stages["initial"][final_side]["odds"]
        t5_final_odds = stages["T5"][final_side]["odds"]
        gap = round(initial_final_odds - t5_final_odds, 4)
        main_score = sum(
            stages[stage]["O"]["is_main"] + stages[stage]["U"]["is_main"]
            for stage in STAGES
        )
        base = {
            "fixture_id": match_id,
            "kickoff": info["kickoff"],
            "league": info["league"],
            "fixture": f'{info["home_team"]} vs {info["away_team"]}',
            "provider": provider,
            "line": line,
            "path": path,
            "final_side": final_side,
            "gap": gap,
            "drift_bucket": drift_bucket(gap),
            "line_band": line_band(line),
            "main_score": main_score,
            "score": f'{int(info["home_score"])}-{int(info["away_score"])}',
        }
        total = int(info["home_score"]) + int(info["away_score"])
        for bet_side in SIDES:
            odds = stages["T5"][bet_side]["odds"]
            status = settle(total, line, bet_side)
            observations.append(
                {
                    **base,
                    "bet_side": bet_side,
                    "mode": "direct" if bet_side == final_side else "reverse",
                    "t5_odds": odds,
                    "odds_band": odds_band(odds),
                    "settlement": status,
                    "unit_return": unit_return(status, odds),
                }
            )

    # A strict main-line view: one line per fixture/provider, using the strongest
    # main flags, then nearest-to-2.5 as a deterministic tie breaker.
    by_fixture: dict[tuple[str, str, str], list[dict[str, Any]]] = collections.defaultdict(list)
    for row in observations:
        by_fixture[(row["fixture_id"], row["provider"], row["bet_side"])].append(row)
    strict = []
    for rows in by_fixture.values():
        strict.append(
            sorted(rows, key=lambda row: (-row["main_score"], abs(row["line"] - 2.5), row["line"]))[0]
        )
    return strict, {
        "raw_snapshot_rows": db.execute(
            "SELECT COUNT(*) FROM research_timeline_snapshots WHERE market='OU'"
        ).fetchone()[0],
        "settled_strict_bet_rows": len(strict),
        "settled_strict_fixture_provider_pairs": len(strict) // 2,
        "kickoff_min": min((row["kickoff"] for row in strict), default=None),
        "kickoff_max": max((row["kickoff"] for row in strict), default=None),
        "rejected_post_kickoff_quotes": rejected_post_kickoff,
        "rejected_wrong_origin_quotes": rejected_origin,
    }


def metrics(rows: list[dict[str, Any]]) -> dict[str, Any]:
    decided = [row for row in rows if row["settlement"] in DECIDED]
    hits = sum(row["settlement"] in HITS for row in decided)
    pnl = sum(row["unit_return"] for row in rows)
    return {
        "bets": len(rows),
        "unique_fixtures": len({row["fixture_id"] for row in rows}),
        "decided": len(decided),
        "hits": hits,
        "hit_rate": round(hits / len(decided), 6) if decided else None,
        "wilson_95": wilson(hits, len(decided)),
        "mean_odds": round(sum(row["t5_odds"] for row in rows) / len(rows), 4) if rows else None,
        "unit_pnl": round(pnl, 4),
        "roi": round(pnl / len(rows), 6) if rows else None,
        "settlements": dict(collections.Counter(row["settlement"] for row in rows)),
    }


def split_metrics(rows: list[dict[str, Any]]) -> dict[str, Any]:
    ordered = sorted(rows, key=lambda row: (row["kickoff"], row["fixture_id"]))
    cut = max(1, int(len(ordered) * 0.70))
    if cut >= len(ordered):
        cut = len(ordered) - 1
    train, holdout = ordered[:cut], ordered[cut:]
    return {"all": metrics(ordered), "train": metrics(train), "holdout": metrics(holdout)}


def scan(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    families: list[tuple[str, Callable[[dict[str, Any]], tuple[Any, ...]]]] = [
        ("path_drift", lambda r: (r["provider"], r["path"], r["drift_bucket"], r["bet_side"])),
        (
            "path_drift_line",
            lambda r: (r["provider"], r["path"], r["drift_bucket"], r["line_band"], r["bet_side"]),
        ),
        (
            "path_drift_odds",
            lambda r: (r["provider"], r["path"], r["drift_bucket"], r["odds_band"], r["bet_side"]),
        ),
        (
            "path_line",
            lambda r: (r["provider"], r["path"], r["line_band"], r["bet_side"]),
        ),
        (
            "path_odds",
            lambda r: (r["provider"], r["path"], r["odds_band"], r["bet_side"]),
        ),
        (
            "league_path",
            lambda r: (r["provider"], r["league"], r["path"], r["bet_side"]),
        ),
    ]
    results = []
    for family, key_func in families:
        grouped: dict[tuple[Any, ...], list[dict[str, Any]]] = collections.defaultdict(list)
        for row in rows:
            grouped[key_func(row)].append(row)
        for key, group in grouped.items():
            if len(group) < 12:
                continue
            result = {"family": family, "condition": list(key), **split_metrics(group)}
            all_m, train_m, hold_m = result["all"], result["train"], result["holdout"]
            result["screen_pass"] = bool(
                all_m["bets"] >= 20
                and hold_m["bets"] >= 6
                and all_m["hit_rate"] is not None
                and all_m["hit_rate"] >= 0.65
                and all_m["roi"] > 0.05
                and train_m["roi"] > 0
                and hold_m["roi"] >= 0
                and hold_m["hit_rate"] is not None
                and hold_m["hit_rate"] >= 0.55
            )
            result["sample_warning"] = (
                "exploratory_only" if all_m["bets"] < 50 or hold_m["bets"] < 15 else None
            )
            results.append(result)
    return sorted(
        results,
        key=lambda item: (
            not item["screen_pass"],
            -(item["all"]["roi"] or -99),
            -(item["all"]["hit_rate"] or -99),
            -item["all"]["bets"],
        ),
    )


def existing_rule_audit(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    specs = [
        ("pinnacle-uoo-short-005-010", "pinnacle", "U→O→O", "short_005_010", "O"),
        ("pinnacle-ooo-short-010-020", "pinnacle", "O→O→O", "short_010_020", "O"),
        ("pinnacle-ouu-short-010-020-reverse", "pinnacle", "O→U→U", "short_010_020", "O"),
        ("hkjc-ooo-flat-wide-reverse", "hkjc", "O→O→O", "flat_or_wider", "U"),
        ("pinnacle-uuu-flat-wide-reverse", "pinnacle", "U→U→U", "flat_or_wider", "O"),
    ]
    output = []
    for rule_id, provider, path, drift, bet_side in specs:
        matched = [
            row for row in rows
            if row["provider"] == provider and row["path"] == path
            and row["drift_bucket"] == drift and row["bet_side"] == bet_side
        ]
        output.append({"rule_id": rule_id, **split_metrics(matched)})
    return output


def main() -> None:
    db = sqlite3.connect(DB_URI, uri=True)
    db.row_factory = sqlite3.Row
    db.execute("PRAGMA query_only=ON")
    db.execute("BEGIN")
    rows, dataset = load_rows(db)
    scanned = scan(rows)
    result = {
        "mode": "read_only",
        "method": {
            "market": "OU",
            "stages": list(STAGES),
            "same_line": True,
            "selected_price_each_stage": ">1.70",
            "strict_one_line_per_fixture_provider": True,
            "time_split": "first 70% train, last 30% holdout within each condition",
            "screen": "N>=20; holdout N>=6; all hit>=65%; all ROI>5%; train ROI>0; holdout ROI>=0 and hit>=55%",
            "warning": "Every discovered condition remains exploratory until prospectively validated.",
        },
        "dataset": dataset,
        "existing_rules": existing_rule_audit(rows),
        "screen_pass_count": sum(item["screen_pass"] for item in scanned),
        "screen_passes": [item for item in scanned if item["screen_pass"]],
        "top_exploratory": scanned[:30],
    }
    print(json.dumps(result, ensure_ascii=False, indent=2, sort_keys=True))
    db.rollback()
    db.close()


if __name__ == "__main__":
    main()
