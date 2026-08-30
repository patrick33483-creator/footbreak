#!/usr/bin/env python3
"""Read-only AH three-stage audit that follows every exact quoted line."""

from __future__ import annotations

import argparse
import collections
import json
import math
import sqlite3
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


def settle_ah(line: float, side: str, home_score: int, away_score: int) -> str:
    outcomes = []
    for half in split_line(line):
        adjusted = home_score + half - away_score
        diff = adjusted if side == "H" else -adjusted
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
          AND q.market='AH'
        ORDER BY q.match_id,q.provider,q.stage,q.line_key,q.selection,q.captured_at
        """
    )

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
        side = str(row["selection"] or "").upper()
        odds = number(row["decimal_odds"])
        if line is None or side not in {"H", "A"} or odds is None or odds <= 1:
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
        if "H" not in sides or "A" not in sides:
            continue
        if abs(sides["H"]["odds"] - sides["A"]["odds"]) < 1e-12:
            continue
        selected = "H" if sides["H"]["odds"] < sides["A"]["odds"] else "A"
        opposite = "A" if selected == "H" else "H"
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
        if meta["home_score"] is not None and meta["away_score"] is not None:
            status = settle_ah(
                line,
                stages["T5"]["side"],
                int(meta["home_score"]),
                int(meta["away_score"]),
            )
            observation["settlement"] = status
            observation["unit_return"] = unit_return(status, stages["T5"]["odds"])
        else:
            observation["settlement"] = None
            observation["unit_return"] = None
        complete.append(observation)
        if all(stages[stage]["odds"] > threshold for stage in STAGES):
            eligible.append(observation)

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

    provider_summary = {
        provider: metrics([row for row in eligible if row["provider"] == provider])
        for provider in ("hkjc", "pinnacle")
    }
    examples = sorted(
        eligible,
        key=lambda row: (row.get("settlement") is not None, row["kickoff"]),
        reverse=True,
    )[:10]
    recent_complete_rows = sorted(
        complete,
        key=lambda row: row["kickoff"],
        reverse=True,
    )[:100]
    return {
        "report": "ah_line_ladder_audit",
        "definition": {
            "identity": "fixture + provider + exact AH line",
            "path": "initial lower-odds side → T30 lower-odds side → T5 lower-odds side",
            "price_rule": f"selected side odds at all three stages strictly >{threshold:.2f}",
            "main_line_rule": "no stage-level main-line collapse; every exact line is followed independently",
        },
        "summary": {
            "complete_line_paths": len(complete),
            "complete_unique_fixtures": len({row["fixture_id"] for row in complete}),
            "eligible_line_paths": len(eligible),
            "eligible_unique_fixtures": len({row["fixture_id"] for row in eligible}),
        },
        "provider_summary": provider_summary,
        "conditions": conditions,
        "examples": examples,
        "recent_complete_rows": recent_complete_rows,
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
