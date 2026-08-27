#!/usr/bin/env python3
"""Read-only historical replay for one frozen Crown Wilson condition."""
from __future__ import annotations

import argparse
import json
import sqlite3
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from analysis.granular_conditions import _descriptor, _paths, canonical_panels
from crown.common import HKT, parse_time
from crown.config import settings
from crown.state import paths, state_lock


def _read_object(path: Path) -> dict[str, Any]:
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise ValueError(f"{path} must contain a JSON object")
    return value


def _time(value: Any) -> datetime | None:
    parsed = parse_time(str(value or ""))
    if parsed is None:
        return None
    if parsed.tzinfo is None:
        return parsed.replace(tzinfo=HKT)
    return parsed.astimezone(HKT)


def _condition(ledger: dict[str, Any], number: int) -> tuple[str, dict[str, Any]]:
    namespace = ledger.get("wilson_validation")
    if not isinstance(namespace, dict):
        raise ValueError("missing Wilson validation namespace")
    conditions = namespace.get("conditions")
    if not isinstance(conditions, dict):
        raise ValueError("missing Wilson conditions")
    matches = [
        (str(signature), row)
        for signature, row in conditions.items()
        if isinstance(row, dict) and row.get("condition_number") == number
    ]
    if len(matches) != 1:
        raise ValueError("condition number is not unique")
    return matches[0]


def _history_row_index(rows: list[dict[str, Any]]) -> dict[tuple[str, str], dict[str, Any]]:
    grouped: dict[tuple[str, str], list[dict[str, Any]]] = defaultdict(list)
    for row in rows:
        if not isinstance(row, dict):
            continue
        fixture = str(row.get("match_id") or row.get("history_key") or "").strip()
        stage = str(row.get("stage") or "").strip()
        if fixture and stage:
            grouped[(fixture, stage)].append(row)
    return {
        key: max(
            values,
            key=lambda row: str(
                row.get("predicted_at") or row.get("ts") or row.get("observed_at") or ""
            ),
        )
        for key, values in grouped.items()
    }


def _score(row: dict[str, Any]) -> str | None:
    if row.get("score") not in (None, ""):
        return str(row["score"])
    home, away = row.get("home_score"), row.get("away_score")
    if home is not None and away is not None:
        return f"{home}-{away}"
    return None


def _hdc_grade(row: dict[str, Any]) -> dict[str, Any] | None:
    values = [
        item for item in row.get("market_grades") or []
        if isinstance(item, dict) and str(item.get("code") or "").upper() == "HDC"
    ]
    return values[0] if len(values) == 1 else None


def _formal_rows(
    ledger: dict[str, Any], signature: str, condition_number: int
) -> dict[str, list[dict[str, Any]]]:
    output: dict[str, list[dict[str, Any]]] = defaultdict(list)
    rows: list[dict[str, Any]] = []
    rows.extend(row for row in ledger.get("bets") or [] if isinstance(row, dict))
    namespace = ledger.get("wilson_validation")
    if isinstance(namespace, dict):
        rows.extend(
            row for row in namespace.get("observations") or [] if isinstance(row, dict)
        )
    for row in rows:
        if (
            row.get("portfolio") not in {
                "crown_wilson_test", "crown_wilson_observations"
            }
            or (
                str(row.get("frozen_condition_signature") or "") != signature
                and row.get("condition_number") != condition_number
            )
        ):
            continue
        fixture = str(row.get("match_id") or "").strip()
        if fixture:
            output[fixture].append(row)
    return output


def _latest_learning_results(path: Path) -> dict[str, dict[str, Any]]:
    if not path.is_file():
        return {}
    connection = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
    connection.row_factory = sqlite3.Row
    try:
        rows = connection.execute("""
            SELECT r.*
              FROM results r
              JOIN (
                    SELECT system, fixture_id, MAX(result_attempt) AS result_attempt
                      FROM results
                     WHERE system = 'crown'
                     GROUP BY system, fixture_id
                   ) latest
                ON latest.system=r.system
               AND latest.fixture_id=r.fixture_id
               AND latest.result_attempt=r.result_attempt
             WHERE r.system = 'crown'
        """)
        return {str(row["fixture_id"]): dict(row) for row in rows}
    finally:
        connection.close()


def replay(
    condition_number: int, since: datetime | None, learning_db: Path
) -> dict[str, Any]:
    config = settings()
    ledger_path = paths(config)["ledger"]
    history_path = config.state_dir / "prediction_history.json"
    with state_lock(config) as acquired:
        if not acquired:
            raise RuntimeError("could not acquire Crown state lock")
        ledger = _read_object(ledger_path)
        history = _read_object(history_path)

    signature, condition = _condition(ledger, condition_number)
    definition = condition.get("definition")
    active = condition.get("active_evidence")
    if not isinstance(definition, dict) or not isinstance(active, dict):
        raise ValueError("condition lacks frozen definition or active evidence")
    if definition.get("system") != "crown":
        raise ValueError("requested condition is not a Crown condition")
    expected_key = tuple(str(value) for value in definition.get("miner_key") or [])
    if len(expected_key) != 9 or any(not value for value in expected_key):
        raise ValueError("condition matcher key is not the expected level-2 shape")
    boundary = since or _time(active.get("activation_boundary_at"))
    if boundary is None:
        raise ValueError("missing activation boundary")

    rows = [row for row in history.get("rows") or [] if isinstance(row, dict)]
    history_index = _history_row_index(rows)
    formal = _formal_rows(ledger, signature, condition_number)
    learning_results = _latest_learning_results(learning_db)
    minimum_odds = float(active["minimum_acceptable_odds_raw"])
    candidates: list[dict[str, Any]] = []
    excluded_before_boundary = 0

    for panel in canonical_panels(rows, settled_only=False):
        if panel.get("market") != "HDC":
            continue
        matching_paths = [
            path for path in _paths(panel, "T-5")
            if path[-1].get("stage") == "T-5"
            and _descriptor("crown", path, 2)[0] == expected_key
        ]
        if len(matching_paths) != 1:
            continue
        path = matching_paths[0]
        fixture = str(panel.get("fixture") or "")
        terminal_row = history_index.get((fixture, "T-5"), {})
        stage_at = _time(
            terminal_row.get("predicted_at")
            or terminal_row.get("ts")
            or terminal_row.get("observed_at")
        )
        if stage_at is None or stage_at < boundary:
            excluded_before_boundary += 1
            continue
        kickoff = _time(terminal_row.get("kickoff") or terminal_row.get("kickoff_hkt"))
        grade = _hdc_grade(terminal_row)
        score = _score(terminal_row)
        learning_result = learning_results.get(fixture)
        if score is None and learning_result is not None:
            home_score = learning_result.get("home_score")
            away_score = learning_result.get("away_score")
            if home_score is not None and away_score is not None:
                score = f"{home_score}-{away_score}"
        result_known = bool(
            score
            or (
                isinstance(grade, dict)
                and str(grade.get("grade_status") or "").upper() == "GRADED"
            )
        )
        recorded = formal.get(fixture, [])
        terminal = path[-1]
        passes_wilson_price = float(terminal["odds"]) >= minimum_odds
        expected_record_type = "formal_bet" if passes_wilson_price else "observation"
        matching_records = [
            row for row in recorded
            if (
                expected_record_type == "formal_bet"
                and row.get("portfolio") == "crown_wilson_test"
            ) or (
                expected_record_type == "observation"
                and row.get("portfolio") == "crown_wilson_observations"
            )
        ]
        candidates.append({
            "match_id": fixture,
            "league": terminal_row.get("league"),
            "home": terminal_row.get("home"),
            "away": terminal_row.get("away"),
            "kickoff_hkt": kickoff.isoformat() if kickoff else None,
            "t5_recorded_at": stage_at.isoformat(),
            "stage_path": [item.get("stage") for item in path],
            "role_path": [item.get("role") for item in path],
            "selected_line_path": [item.get("selected_line") for item in path],
            "t5_odds": terminal.get("odds"),
            "passes_wilson_price": passes_wilson_price,
            "expected_record_type": expected_record_type,
            "formal_row_count": len(recorded),
            "formal_row_ids": [
                row.get("bet_id") or row.get("observation_id") for row in recorded
            ],
            "formal_statuses": [row.get("status") for row in recorded],
            "matching_record_count": len(matching_records),
            "missing_expected_record": not matching_records,
            "result_known": result_known,
            "result_source": (
                "prediction_history"
                if _score(terminal_row) is not None or grade is not None
                else "learning_db"
                if learning_result is not None and score is not None
                else None
            ),
            "result_status": terminal_row.get("result_status"),
            "score": score,
            "hdc_grade": (
                {
                    "grade_status": grade.get("grade_status"),
                    "hit": grade.get("hit"),
                    "result": grade.get("result"),
                }
                if isinstance(grade, dict) else None
            ),
        })

    candidates.sort(key=lambda row: (row.get("kickoff_hkt") or "", row["match_id"]))
    missing = [row for row in candidates if row["missing_expected_record"]]
    unknown = [row for row in candidates if not row["result_known"]]
    recorded_unknown = [
        row for row in candidates
        if not row["missing_expected_record"] and not row["result_known"]
    ]
    price_pass = [row for row in candidates if row["passes_wilson_price"]]
    low_price = [row for row in candidates if not row["passes_wilson_price"]]
    return {
        "schema": "crown_condition_read_only_replay_v1",
        "read_only": True,
        "provider_calls": 0,
        "writes": 0,
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "condition_number": condition_number,
        "condition_signature": signature,
        "activation_boundary_hkt": boundary.isoformat(),
        "definition": definition,
        "minimum_acceptable_odds_raw": minimum_odds,
        "history_source_rows": len(rows),
        "learning_result_rows": len(learning_results),
        "excluded_matching_before_activation": excluded_before_boundary,
        "summary": {
            "matching_fixture_count": len(candidates),
            "wilson_price_pass_fixture_count": len(price_pass),
            "low_price_observation_fixture_count": len(low_price),
            "recorded_expected_fixture_count": len(candidates) - len(missing),
            "missing_expected_record_fixture_count": len(missing),
            "unknown_result_fixture_count": len(unknown),
            "recorded_unknown_result_fixture_count": len(recorded_unknown),
        },
        "matching_fixtures": candidates,
        "missing_formal_fixtures": missing,
        "unknown_result_fixtures": unknown,
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--condition-number", type=int, default=4)
    parser.add_argument("--since")
    parser.add_argument(
        "--learning-db",
        type=Path,
        default=Path("/var/lib/footbreak/learning/predictions.sqlite"),
    )
    args = parser.parse_args()
    since = _time(args.since) if args.since else None
    if args.since and since is None:
        raise SystemExit("--since must be a valid ISO-8601 timestamp")
    print(json.dumps(
        replay(args.condition_number, since, args.learning_db),
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
