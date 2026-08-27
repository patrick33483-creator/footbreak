#!/usr/bin/env python3
"""Read-only audit of the user-confirmed Crown condition #2 semantics.

The intended rule is:
* first look HIL over, selected line 2.75 through 3.0, odds >= 1.70;
* T-30 HIL selection remains over;
* T-5 HIL selection remains over.

Later-stage line and odds are recorded but do not qualify or disqualify a
fixture. Settlement remains tied to the first-look selected line.
"""
from __future__ import annotations

import argparse
import json
from collections import Counter
from pathlib import Path
from typing import Any

from analysis.granular_conditions import _canonical_rows
from analysis.recover_crown_condition2_history import (
    CONDITION_NUMBER,
    SIGNATURE,
    _condition,
    _read,
    _rows,
)


STAGES = ("首預", "T-30", "T-5")


def _qualify(
    canonical: dict[tuple[str, str, str], dict[str, Any]],
    fixture: str,
) -> tuple[bool, str, dict[str, Any]]:
    stages = {
        stage: canonical.get((fixture, "HIL", stage))
        for stage in STAGES
    }
    first = stages["首預"]
    if not isinstance(first, dict):
        return False, "missing_first_look_hil", stages
    if first.get("side") != "H":
        return False, "first_look_not_over", stages
    line = first.get("selected_line")
    if not isinstance(line, (int, float)) or not 2.75 <= float(line) <= 3.0:
        return False, "first_look_line_outside_2.75_3.0", stages
    odds = first.get("odds")
    if not isinstance(odds, (int, float)) or float(odds) < 1.70:
        return False, "first_look_odds_below_1.70", stages
    for stage in ("T-30", "T-5"):
        item = stages[stage]
        if not isinstance(item, dict):
            return False, f"missing_{stage}_hil", stages
        if item.get("side") != "H":
            return False, f"{stage}_not_over", stages
    return True, "qualified", stages


def audit(ledger: dict[str, Any], history: dict[str, Any]) -> dict[str, Any]:
    _namespace, frozen = _condition(ledger)
    history_rows = _rows(history)
    admission = _canonical_rows(history_rows, settled_only=False)
    settled = _canonical_rows(history_rows, settled_only=True)
    recovery = frozen.get("historical_recovery_rows")
    if not isinstance(recovery, list):
        raise ValueError("condition #2 historical recovery rows missing")

    recovered_by_fixture = {
        str(row.get("match_id") or ""): row
        for row in recovery if isinstance(row, dict) and row.get("match_id")
    }
    rows: list[dict[str, Any]] = []
    reasons: Counter[str] = Counter()
    result_counts: Counter[str] = Counter()
    for fixture, recovery_row in recovered_by_fixture.items():
        qualified, reason, stages = _qualify(admission, fixture)
        reasons[reason] += 1
        result = str(recovery_row.get("result") or "PENDING")
        if qualified:
            result_counts[result] += 1
        first_grade = settled.get((fixture, "HIL", "首預"))
        rows.append({
            "match_id": fixture,
            "kickoff": recovery_row.get("kickoff"),
            "league": recovery_row.get("league"),
            "home": recovery_row.get("home"),
            "away": recovery_row.get("away"),
            "qualified": qualified,
            "reason": reason,
            "stored_result": result,
            "first_look_stored_line": recovery_row.get("line"),
            "first_look_stored_odds": recovery_row.get("odds"),
            "first_look_settled_hit": (
                first_grade.get("hit") if isinstance(first_grade, dict) else None
            ),
            "stages": {
                stage: (
                    {
                        "side": item.get("side"),
                        "line": item.get("selected_line"),
                        "odds": item.get("odds"),
                    }
                    if isinstance(item, dict) else None
                )
                for stage, item in stages.items()
            },
        })

    qualified_rows = [row for row in rows if row["qualified"]]
    decided = sum(
        result_counts[result]
        for result in ("Won", "Half Won", "Lost", "Half Lost")
    )
    hits = result_counts["Won"] + result_counts["Half Won"]
    all_first_candidates = {
        fixture
        for fixture, market, stage in admission
        if market == "HIL" and stage == "首預"
        and _qualify(admission, fixture)[1] not in {
            "missing_first_look_hil", "first_look_not_over",
            "first_look_line_outside_2.75_3.0",
            "first_look_odds_below_1.70",
        }
    }
    all_three_stage = {
        fixture for fixture in all_first_candidates
        if _qualify(admission, fixture)[0]
    }
    return {
        "mode": "read_only_three_stage_audit",
        "condition_number": CONDITION_NUMBER,
        "current_signature": SIGNATURE,
        "intended_rule": {
            "first_look": "HIL over; line 2.75-3.0; odds >=1.70",
            "T-30": "HIL over; line and odds unrestricted",
            "T-5": "HIL over; line and odds unrestricted",
            "settlement_line": "first-look selected line",
        },
        "current_recovery_rows": len(rows),
        "qualified_recovery_rows": len(qualified_rows),
        "disqualified_recovery_rows": len(rows) - len(qualified_rows),
        "qualified_results": dict(sorted(result_counts.items())),
        "qualified_hits": hits,
        "qualified_decided": decided,
        "qualified_pushes": result_counts["Refunded"],
        "qualified_pending": result_counts["PENDING"],
        "reasons": dict(sorted(reasons.items())),
        "all_history_first_candidates_with_later_stages": len(all_first_candidates),
        "all_history_qualified_three_stage": len(all_three_stage),
        "rows": rows,
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--ledger", type=Path, required=True)
    parser.add_argument("--history", type=Path, required=True)
    args = parser.parse_args()
    print(json.dumps(
        audit(_read(args.ledger), _read(args.history)),
        ensure_ascii=False, indent=2, sort_keys=True,
    ))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
