#!/usr/bin/env python3
"""Persistent, report-only three-stage market-direction condition ledger.

The worker reads Odds Radar in SQLite query-only mode.  It never places bets,
sends notifications, or changes Crown/Footbreak predictions.  Historical seed
rows are kept separate from prospective rows; every complete three-stage path
is accumulated regardless of price, while >=1.70 is an additional view only.
"""

from __future__ import annotations

import argparse
import collections
import datetime as dt
import fcntl
import json
import math
import os
import sqlite3
import tempfile
from pathlib import Path
from typing import Any

from analysis.three_stage_historical_backtest import (
    STAGES,
    line_number,
    number,
    settle,
    side_key,
    unit_return,
)


DEFAULT_RADAR_URI = "file:/opt/odds-radar/data/data.db?mode=ro"
DEFAULT_STATE = "/var/lib/footbreak/direction-path-conditions/ledger.json"
DEFAULT_PUBLIC = "/var/www/stage_engine_v2/direction-path-conditions.json"
DEFAULT_SEED = str(
    Path(__file__).with_name("seeds") / "direction_path_conditions_20260829.json"
)
ODDS_THRESHOLD = 1.70
VERSION_SIZE = 20
DECIDED = {"win", "half_win", "half_loss", "loss"}
HITS = {"win", "half_win"}

CATALOG = {
    ("pinnacle", "OU", "O→O→O"): {
        "id": "PATH-OU-01",
        "status": "candidate",
        "note": "主要候選：全段市場均偏向大。",
    },
    ("pinnacle", "AH", "A→H→H"): {
        "id": "PATH-AH-01",
        "status": "candidate",
        "note": "主要候選：初盤偏客，其後兩段偏主。",
    },
    ("pinnacle", "AH", "H→A→A"): {
        "id": "PATH-AH-W01",
        "status": "watch",
        "note": "觀察：近期較好，但早段樣本偏弱。",
    },
    ("hkjc", "AH", "H→H→H"): {
        "id": "PATH-AH-W02",
        "status": "watch",
        "note": "觀察：馬會三段持續偏主。",
    },
    ("hkjc", "AH", "A→A→A"): {
        "id": "PATH-AH-C01",
        "status": "control",
        "note": "退化監察：早段好、留出樣本轉差。",
    },
    ("pinnacle", "OU", "U→O→O"): {
        "id": "PATH-OU-C01",
        "status": "control",
        "note": "負對照：樣本足夠但暫未見正回報。",
    },
}


def utc_now() -> str:
    return dt.datetime.now(dt.timezone.utc).replace(microsecond=0).isoformat()


def atomic_json(path: str | Path, payload: Any, mode: int = 0o600) -> None:
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(prefix=f".{target.name}.", dir=target.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(payload, handle, ensure_ascii=False, sort_keys=True)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.chmod(tmp, mode)
        os.replace(tmp, target)
    finally:
        if os.path.exists(tmp):
            os.unlink(tmp)


def load_json(path: str | Path, fallback: Any) -> Any:
    try:
        with open(path, encoding="utf-8") as handle:
            return json.load(handle)
    except FileNotFoundError:
        return fallback


def wilson(hits: int, decided: int, z: float = 1.959963984540054) -> dict[str, float | None]:
    if decided <= 0:
        return {"low": None, "high": None}
    p = hits / decided
    denominator = 1 + z * z / decided
    centre = (p + z * z / (2 * decided)) / denominator
    margin = z * math.sqrt((p * (1 - p) + z * z / (4 * decided)) / decided) / denominator
    return {"low": max(0.0, centre - margin), "high": min(1.0, centre + margin)}


def observation_key(row: dict[str, Any]) -> str:
    return f'{row["provider"]}|{row["market"]}|{row["fixture_id"]}'


def condition_key(row: dict[str, Any]) -> tuple[str, str, str]:
    return (str(row["provider"]), str(row["market"]), str(row["direction_path"]))


def normalize_seed(row: dict[str, Any]) -> dict[str, Any]:
    return {
        "fixture_id": str(row["fixture_id"]),
        "kickoff": int(row["kickoff"]),
        "provider": str(row["provider"]).lower(),
        "market": str(row["market"]),
        "direction_path": str(row["direction_path"]),
        "initial": row["initial"],
        "T30": row["T30"],
        "T5": row["T5"],
        "settlement": row.get("T5_settlement"),
        "unit_return": row.get("T5_unit_return"),
        "home_team": row.get("home_team"),
        "away_team": row.get("away_team"),
        "cohort": "historical",
        "first_seen_at": "2026-08-29T00:00:00+00:00",
        "updated_at": "2026-08-29T00:00:00+00:00",
    }


def table_columns(db: sqlite3.Connection, table: str) -> set[str]:
    return {str(row[1]) for row in db.execute(f"PRAGMA table_info({table})")}


def extract_radar(db: sqlite3.Connection) -> list[dict[str, Any]]:
    match_cols = table_columns(db, "matches")
    home_expr = "m.home_team" if "home_team" in match_cols else "NULL"
    away_expr = "m.away_team" if "away_team" in match_cols else "NULL"
    query = f"""
    SELECT q.match_id,q.stage,q.provider,q.market,q.line_key,q.selection,
           q.decimal_odds,q.is_main,q.captured_at,q.origin,m.kickoff_utc,
           {home_expr} AS home_team,{away_expr} AS away_team,
           COALESCE(rr.home_score,r.home_score) AS home_score,
           COALESCE(rr.away_score,r.away_score) AS away_score,
           rr.corners_total AS official_corners,rr.source AS result_source
    FROM research_timeline_snapshots q
    JOIN matches m ON m.id=q.match_id
    LEFT JOIN research_results rr ON rr.match_id=m.id
    LEFT JOIN results r ON r.match_id=m.id
    WHERE q.stage IN ('initial','T30','T5')
      AND q.provider IN ('hkjc','pinnacle')
      AND q.market IN ('AH','OU','COU')
    ORDER BY q.match_id,q.provider,q.market,q.stage,q.captured_at
    """
    quotes: dict[tuple[str, str, str, str, float, str], dict[str, Any]] = {}
    metadata: dict[str, dict[str, Any]] = {}
    for row in db.execute(query):
        kickoff, captured = number(row["kickoff_utc"]), number(row["captured_at"])
        if kickoff is None or captured is None or captured >= kickoff:
            continue
        stage, origin = str(row["stage"]), str(row["origin"] or "")
        if (stage == "initial" and origin != "external_opening") or (
            stage != "initial" and origin != "live_observation"
        ):
            continue
        market = str(row["market"])
        line = line_number(row["line_key"])
        side = side_key(row["selection"], market)
        odds = number(row["decimal_odds"])
        if line is None or side is None or odds is None or odds <= 1:
            continue
        match_id, provider = str(row["match_id"]), str(row["provider"]).lower()
        key = (match_id, provider, market, stage, line, side)
        candidate = {
            "odds": odds,
            "is_main": int(row["is_main"] or 0),
            "captured_at": captured,
        }
        if key not in quotes or captured > quotes[key]["captured_at"]:
            quotes[key] = candidate
        metadata[match_id] = {
            "kickoff": int(kickoff),
            "home_team": row["home_team"],
            "away_team": row["away_team"],
            "home_score": row["home_score"],
            "away_score": row["away_score"],
            "corners_total": (
                row["official_corners"] if row["result_source"] == "hkjc_official" else None
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
            continue
        candidates[(match_id, provider, market, stage)].append(
            {
                "line": line,
                "sides": sides,
                "main_score": sum(sides[side]["is_main"] for side in required),
            }
        )

    primary: dict[tuple[str, str, str, str], dict[str, Any]] = {}
    for key, groups in candidates.items():
        best_score = max(group["main_score"] for group in groups)
        best = [group for group in groups if group["main_score"] == best_score]
        if best_score > 0 and len(best) == 1:
            primary[key] = best[0]
        elif best_score == 0 and len(groups) == 1:
            primary[key] = groups[0]

    stages_by_identity: dict[tuple[str, str, str], dict[str, dict[str, Any]]] = (
        collections.defaultdict(dict)
    )
    for (match_id, provider, market, stage), group in primary.items():
        sides = ("H", "A") if market == "AH" else ("O", "U")
        left, right = group["sides"][sides[0]], group["sides"][sides[1]]
        if abs(left["odds"] - right["odds"]) < 1e-12:
            continue
        selected = sides[0] if left["odds"] < right["odds"] else sides[1]
        stages_by_identity[(match_id, provider, market)][stage] = {
            "side": selected,
            "line": group["line"],
            "odds": group["sides"][selected]["odds"],
        }

    extracted = []
    for (match_id, provider, market), stage_rows in stages_by_identity.items():
        if not set(STAGES).issubset(stage_rows):
            continue
        meta, t5 = metadata[match_id], stage_rows["T5"]
        status = None
        if meta["home_score"] is not None and meta["away_score"] is not None:
            status = settle(
                market,
                t5["line"],
                t5["side"],
                int(meta["home_score"]),
                int(meta["away_score"]),
                int(meta["corners_total"]) if meta["corners_total"] is not None else None,
            )
        extracted.append(
            {
                "fixture_id": match_id,
                "kickoff": meta["kickoff"],
                "provider": provider,
                "market": market,
                "direction_path": "→".join(stage_rows[stage]["side"] for stage in STAGES),
                "initial": stage_rows["initial"],
                "T30": stage_rows["T30"],
                "T5": t5,
                "settlement": status,
                "unit_return": unit_return(status, t5["odds"]) if status else None,
                "home_team": meta["home_team"],
                "away_team": meta["away_team"],
            }
        )
    return extracted


def metrics(rows: list[dict[str, Any]], threshold: float | None = None) -> dict[str, Any]:
    scoped = []
    for row in rows:
        odds = number(row.get("T5", {}).get("odds"))
        if threshold is None or (odds is not None and odds >= threshold):
            scoped.append(row)
    settled = [row for row in scoped if row.get("settlement") in DECIDED | {"push"}]
    decided = [row for row in scoped if row.get("settlement") in DECIDED]
    hits = sum(row.get("settlement") in HITS for row in decided)
    profit = sum(float(row.get("unit_return") or 0) for row in settled)
    interval = wilson(hits, len(decided))
    return {
        "observations": len(scoped),
        "pending": sum(not row.get("settlement") for row in scoped),
        "settled": len(settled),
        "decided": len(decided),
        "hits": hits,
        "pushes": sum(row.get("settlement") == "push" for row in scoped),
        "hit_rate": hits / len(decided) if decided else None,
        "wilson_95": interval,
        "unit_profit": round(profit, 6),
        "roi": profit / len(settled) if settled else None,
    }


def version_projection(rows: list[dict[str, Any]]) -> dict[str, Any]:
    decided = sorted(
        [row for row in rows if row.get("settlement") in DECIDED],
        key=lambda row: (int(row["kickoff"]), str(row["fixture_id"])),
    )
    completed = []
    for start in range(0, len(decided) - VERSION_SIZE + 1, VERSION_SIZE):
        batch = decided[start : start + VERSION_SIZE]
        batch_metrics = metrics(batch)
        completed.append(
            {
                "version": start // VERSION_SIZE + 1,
                "hits": batch_metrics["hits"],
                "decided": batch_metrics["decided"],
                "hit_rate": batch_metrics["hit_rate"],
                "wilson_95": batch_metrics["wilson_95"],
            }
        )
    return {
        "active_version": len(completed) + 1,
        "progress": len(decided) % VERSION_SIZE,
        "required": VERSION_SIZE,
        "completed": completed,
    }


def build_report(observations: list[dict[str, Any]]) -> dict[str, Any]:
    grouped: dict[tuple[str, str, str], list[dict[str, Any]]] = collections.defaultdict(list)
    for row in observations:
        grouped[condition_key(row)].append(row)
    conditions = []
    for key, rows in grouped.items():
        provider, market, path = key
        spec = CATALOG.get(key, {})
        historical = [row for row in rows if row["cohort"] == "historical"]
        prospective = [row for row in rows if row["cohort"] == "prospective"]
        conditions.append(
            {
                "id": spec.get("id", f"AUTO-{provider}-{market}-{path.replace('→', '')}".upper()),
                "provider": provider,
                "market": market,
                "direction_path": path,
                "status": spec.get("status", "insufficient"),
                "note": spec.get("note", "自動累積，待樣本增加。"),
                "historical": {
                    "all_odds": metrics(historical),
                    "odds_gte_1_70": metrics(historical, ODDS_THRESHOLD),
                },
                "prospective": {
                    "all_odds": metrics(prospective),
                    "odds_gte_1_70": metrics(prospective, ODDS_THRESHOLD),
                    "versions": version_projection(prospective),
                },
                "combined": {
                    "all_odds": metrics(rows),
                    "odds_gte_1_70": metrics(rows, ODDS_THRESHOLD),
                },
            }
        )
    rank = {"candidate": 0, "watch": 1, "control": 2, "insufficient": 3}
    conditions.sort(
        key=lambda item: (
            rank.get(item["status"], 9),
            -item["historical"]["all_odds"]["observations"],
            item["id"],
        )
    )
    return {
        "report": "direction_path_conditions",
        "schema_version": 1,
        "generated_at": utc_now(),
        "mode": "shadow_only_no_bets_no_notifications",
        "definitions": {
            "direction": "每個時點雙邊報價中 decimal odds 較低的一方",
            "path": "初盤方向 → T-30方向 → T-5方向",
            "decision_and_settlement": "完整路徑在 T-5 才成立；用 T-5 方向、線位及賠率結算",
            "accumulation": "完整三階段路徑即入組，賠率不阻止累積",
            "price_view": "另列 T-5 decimal odds ≥1.70 統計，但不影響入組",
            "wilson": "命中為 win/half_win；push 不計 decided；95% Wilson score interval",
            "versioning": "新前瞻每 20 個 decided observations 完成一個版本",
        },
        "summary": {
            "unique_observations": len(observations),
            "historical": sum(row["cohort"] == "historical" for row in observations),
            "prospective": sum(row["cohort"] == "prospective" for row in observations),
            "pending": sum(not row.get("settlement") for row in observations),
            "condition_count": len(conditions),
        },
        "conditions": conditions,
    }


def update(seed_path: str, radar_uri: str, state_path: str, public_path: str) -> dict[str, Any]:
    state = load_json(state_path, {"schema_version": 1, "observations": {}})
    ledger = state.setdefault("observations", {})
    for seed_row in load_json(seed_path, []):
        normalized = normalize_seed(seed_row)
        ledger.setdefault(observation_key(normalized), normalized)

    db = sqlite3.connect(radar_uri, uri=True)
    db.row_factory = sqlite3.Row
    db.execute("PRAGMA query_only=ON")
    try:
        live_rows = extract_radar(db)
    finally:
        db.close()

    now = utc_now()
    for live in live_rows:
        key = observation_key(live)
        old = ledger.get(key)
        if old is None:
            live.update({"cohort": "prospective", "first_seen_at": now, "updated_at": now})
            ledger[key] = live
        else:
            for field in ("home_team", "away_team", "settlement", "unit_return"):
                if live.get(field) is not None:
                    old[field] = live[field]
            old["updated_at"] = now

    state["updated_at"] = now
    observations = list(ledger.values())
    report = build_report(observations)
    atomic_json(state_path, state, 0o600)
    atomic_json(public_path, report, 0o644)
    return report


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--seed", default=DEFAULT_SEED)
    parser.add_argument("--radar-db", default=DEFAULT_RADAR_URI)
    parser.add_argument("--state", default=DEFAULT_STATE)
    parser.add_argument("--public", default=DEFAULT_PUBLIC)
    parser.add_argument("--lock", default=f"{DEFAULT_STATE}.lock")
    args = parser.parse_args()
    Path(args.lock).parent.mkdir(parents=True, exist_ok=True)
    with open(args.lock, "a+", encoding="utf-8") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        report = update(args.seed, args.radar_db, args.state, args.public)
    print(json.dumps(report["summary"], ensure_ascii=False, sort_keys=True))


if __name__ == "__main__":
    main()
