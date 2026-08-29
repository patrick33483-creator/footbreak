#!/usr/bin/env python3
"""Report-only Footbreak model direction-path ledger for HKJC V2.

Reads immutable learning snapshots in SQLite query-only mode.  A fixture-market
enters the ledger only after all three saved stages exist.  Price never blocks
accumulation; T-5 odds >=1.70 is an additional reporting view only.
"""
from __future__ import annotations

import argparse
import collections
import datetime as dt
import fcntl
import json
import os
import sqlite3
from pathlib import Path
from typing import Any

from analysis.direction_path_conditions import (
    ODDS_THRESHOLD,
    atomic_json,
    build_report,
    load_json,
    observation_key,
)
from analysis.three_stage_historical_backtest import (
    line_number,
    matching_prediction,
    number,
    side_key,
    target_parts,
    unit_return,
)

DEFAULT_DB = "file:/var/lib/footbreak/learning/predictions.sqlite?mode=ro"
DEFAULT_STATE = "/var/lib/footbreak/footbreak-direction-path-conditions/ledger.json"
DEFAULT_PUBLIC = "/var/www/stage_engine_v2_fb/direction-path-conditions.json"
STAGES = ("首預", "T-30", "T-5")
PUBLIC_STAGE_KEYS = {"首預": "initial", "T-30": "T30", "T-5": "T5"}
MARKETS = ("HDC", "HIL", "CHL")
SETTLEMENTS = {
    1.0: "win",
    0.75: "half_win",
    0.5: "push",
    0.25: "half_loss",
    0.0: "loss",
}


def utc_now() -> str:
    return dt.datetime.now(dt.timezone.utc).replace(microsecond=0).isoformat()


def _prediction_for_market(
    payload: dict[str, Any],
    market: str,
    grade: dict[str, Any] | None,
) -> dict[str, Any] | None:
    if grade is not None:
        matched = matching_prediction(payload, market, grade["target_key"])
        if matched is not None:
            return matched
    candidates = [
        item
        for item in payload.get("market_predictions") or []
        if isinstance(item, dict) and str(item.get("code") or "").upper() == market
    ]
    return candidates[0] if len(candidates) == 1 else None


def extract_footbreak(db: sqlite3.Connection) -> tuple[list[dict[str, Any]], dict[str, int]]:
    query = """
    WITH ranked_snapshots AS (
      SELECT s.*,
             ROW_NUMBER() OVER (
               PARTITION BY system,fixture_id,stage
               ORDER BY generated_at DESC,snapshot_id DESC
             ) AS snapshot_rank
      FROM prediction_snapshots s
      WHERE system='footbreak' AND stage IN ('首預','T-30','T-5')
        AND pre_kickoff=1
        AND NOT EXISTS (
          SELECT 1 FROM stage_snapshot_reconciliations r
          WHERE r.system=s.system AND r.fixture_id=s.fixture_id
            AND r.stage=s.stage AND r.canonical_snapshot_id != s.snapshot_id
        )
    ), ranked_grades AS (
      SELECT g.*,
             ROW_NUMBER() OVER (
               PARTITION BY snapshot_id,market,target
               ORDER BY grade_attempt DESC,grade_id DESC
             ) AS grade_rank
      FROM grades g WHERE market IN ('HDC','HIL','CHL')
    )
    SELECT s.snapshot_id,s.fixture_id,s.stage,s.generated_at,s.kickoff,
           s.payload_json,g.market,g.target,g.state,g.metrics_json
    FROM ranked_snapshots s
    LEFT JOIN ranked_grades g ON g.snapshot_id=s.snapshot_id AND g.grade_rank=1
    WHERE s.snapshot_rank=1
    ORDER BY s.kickoff,s.fixture_id,s.stage,g.market,g.target
    """
    snapshots: dict[int, dict[str, Any]] = {}
    diagnostics: collections.Counter[str] = collections.Counter()
    for row in db.execute(query):
        try:
            generated = dt.datetime.fromisoformat(str(row["generated_at"]))
            kickoff = dt.datetime.fromisoformat(str(row["kickoff"]))
            payload = json.loads(row["payload_json"])
            if generated >= kickoff or not isinstance(payload, dict):
                raise ValueError("invalid snapshot")
        except (TypeError, ValueError, json.JSONDecodeError):
            diagnostics["invalid_snapshot"] += 1
            continue
        snapshot = snapshots.setdefault(
            int(row["snapshot_id"]),
            {
                "fixture_id": str(row["fixture_id"]),
                "stage": str(row["stage"]),
                "kickoff": int(kickoff.timestamp()),
                "payload": payload,
                "grades": {},
            },
        )
        if row["market"] is None:
            continue
        try:
            metrics = json.loads(row["metrics_json"])
            if not isinstance(metrics, dict):
                raise ValueError("invalid metrics")
        except (TypeError, ValueError, json.JSONDecodeError):
            diagnostics["invalid_grade"] += 1
            continue
        market = str(row["market"]).upper()
        target = number(metrics.get("target"))
        snapshot["grades"].setdefault(market, []).append(
            {
                "target_key": str(row["target"]),
                "state": str(row["state"]),
                "target": target,
            }
        )

    stage_rows: dict[tuple[str, str], dict[str, dict[str, Any]]] = collections.defaultdict(dict)
    metadata: dict[str, dict[str, Any]] = {}
    for snapshot in snapshots.values():
        payload = snapshot["payload"]
        fixture = snapshot["fixture_id"]
        metadata.setdefault(
            fixture,
            {
                "kickoff": snapshot["kickoff"],
                "home_team": payload.get("home") or payload.get("home_team"),
                "away_team": payload.get("away") or payload.get("away_team"),
            },
        )
        for market in MARKETS:
            grades = snapshot["grades"].get(market, [])
            graded = [grade for grade in grades if grade["state"] == "GRADED"]
            grade = graded[0] if len(graded) == 1 else None
            prediction = _prediction_for_market(payload, market, grade)
            if prediction is None:
                diagnostics["no_unique_prediction"] += 1
                continue
            odds = number(prediction.get("odds"))
            line = line_number(prediction.get("line", prediction.get("condition")))
            side = side_key(prediction.get("side"), market)
            if odds is None or odds <= 1 or line is None or side is None:
                diagnostics["invalid_prediction"] += 1
                continue
            stage_rows[(fixture, market)][snapshot["stage"]] = {
                "side": side,
                "line": line,
                "odds": odds,
                "target": grade.get("target") if grade else None,
            }

    observations: list[dict[str, Any]] = []
    for (fixture, market), stages in stage_rows.items():
        if not set(STAGES).issubset(stages):
            diagnostics["incomplete_three_stage_path"] += 1
            continue
        t5 = stages["T-5"]
        target = t5.get("target")
        settlement = None
        if target is not None:
            settlement = SETTLEMENTS.get(round(float(target), 2))
            if settlement is None:
                diagnostics["unknown_settlement_target"] += 1
        meta = metadata[fixture]
        observations.append(
            {
                "fixture_id": fixture,
                "kickoff": meta["kickoff"],
                "provider": "footbreak",
                "market": market,
                "direction_path": "→".join(stages[stage]["side"] for stage in STAGES),
                "initial": stages["首預"],
                "T30": stages["T-30"],
                "T5": t5,
                "settlement": settlement,
                "unit_return": unit_return(settlement, t5["odds"]) if settlement else None,
                "home_team": meta["home_team"],
                "away_team": meta["away_team"],
            }
        )
    return observations, dict(sorted(diagnostics.items()))


def _build_report(observations: list[dict[str, Any]], diagnostics: dict[str, int]) -> dict[str, Any]:
    report = build_report(observations)
    report["system"] = "footbreak"
    report["definitions"] = {
        "direction": "每個時點採用馬會模型當刻實際揀選方向",
        "path": "初盤方向 → T-30方向 → T-5方向",
        "decision_and_settlement": "完整路徑在 T-5 才成立；用 T-5 選擇、線位及賠率結算",
        "accumulation": "三階段齊備即入組；賠率及 Wilson 絕不阻止累積",
        "price_view": f"另列 T-5 decimal odds ≥{ODDS_THRESHOLD:.2f}，只作篩選比較",
        "wilson": "贏及半贏計命中；輸及半輸計不中；走盤不計 decided",
        "versioning": "每條路徑每 20 個新前瞻 decided observations 完成一個版本",
        "initial_provenance": "畫面稱初盤；資料鍵沿用舊系統「首預」以保持歷史相容",
    }
    report["diagnostics"] = diagnostics
    return report


def update(db_uri: str, state_path: str, public_path: str) -> dict[str, Any]:
    db = sqlite3.connect(db_uri, uri=True)
    db.row_factory = sqlite3.Row
    db.execute("PRAGMA query_only=ON")
    db.execute("BEGIN")
    try:
        live_rows, diagnostics = extract_footbreak(db)
    finally:
        db.close()

    state = load_json(
        state_path,
        {"schema_version": 1, "system": "footbreak", "observations": {}},
    )
    ledger = state.setdefault("observations", {})
    first_run = not bool(state.get("initialized_at"))
    now = utc_now()
    for live in live_rows:
        key = observation_key(live)
        old = ledger.get(key)
        if old is None:
            live.update(
                {
                    "cohort": "historical" if first_run else "prospective",
                    "first_seen_at": now,
                    "updated_at": now,
                }
            )
            ledger[key] = live
        else:
            for field in ("home_team", "away_team", "settlement", "unit_return"):
                if live.get(field) is not None:
                    old[field] = live[field]
            old["updated_at"] = now
    state.setdefault("initialized_at", now)
    state["updated_at"] = now
    report = _build_report(list(ledger.values()), diagnostics)
    atomic_json(state_path, state, 0o600)
    atomic_json(public_path, report, 0o644)
    return report


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--db", default=DEFAULT_DB)
    parser.add_argument("--state", default=DEFAULT_STATE)
    parser.add_argument("--public", default=DEFAULT_PUBLIC)
    parser.add_argument("--lock", default=f"{DEFAULT_STATE}.lock")
    args = parser.parse_args()
    Path(args.lock).parent.mkdir(parents=True, exist_ok=True)
    with open(args.lock, "a+", encoding="utf-8") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        report = update(args.db, args.state, args.public)
    print(json.dumps(report["summary"], ensure_ascii=False, sort_keys=True))


if __name__ == "__main__":
    main()
