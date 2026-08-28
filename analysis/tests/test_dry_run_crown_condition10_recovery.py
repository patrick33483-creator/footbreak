from __future__ import annotations

import copy
import json
from pathlib import Path

import pytest

from analysis.dry_run_crown_condition10_recovery import plan


ROOT = Path(__file__).resolve().parents[2]
OVERRIDES = json.loads(
    (ROOT / "audit/crown-condition-10-result-overrides-20260828.json").read_text()
)


def _audit() -> dict:
    definition = {
        "system": "crown", "market": "HIL", "path": "首預→T-30",
        "stage": "T-30", "odds_tier": "≥1.70", "direction": "A→A",
        "role": "大", "line_bucket": "2.75–3.0", "movement": "不變",
        "odds_trajectory": "",
    }
    override_fixtures = {row["fixture"] for row in OVERRIDES["results"]}
    rows = []
    settled_grades = ["Won"] * 56 + ["Lost"] * 43 + ["Refunded"] * 9
    for index in range(114):
        fixture = (
            list(sorted(override_fixtures))[index]
            if index < len(override_fixtures)
            else f"fixture-{index:03d}"
        )
        grade = "PENDING" if fixture in override_fixtures else settled_grades.pop()
        rows.append({
            "fixture": fixture,
            "kickoff": f"2026-08-{21 + index // 100:02d}T00:00:00+08:00",
            "t30_stage_at": f"2026-08-21T00:{index:02d}:00+08:00",
            "league": "test", "home": "home", "away": "away",
            "grade": grade, "enrolled": False,
            "t30": {"selected_line": 3.0, "odds": 1.8},
        })
    return {
        "read_only": True,
        "condition": {
            "condition_number": 10,
            "signature": "f956f75e552c8de37b0f2656",
            "definition": definition,
            "active_evidence": {
                "version": 2, "cumulative_hits": 96, "cumulative_decided": 152,
            },
        },
        "summary": {
            "post_activation_missing_enrolment": 114,
            "post_activation_enrolled": 0,
        },
        "missing_enrolments": rows,
    }


def test_plan_is_non_writing_complete_and_idempotent() -> None:
    audit = _audit()
    before = copy.deepcopy(audit)
    result = plan(audit, OVERRIDES)
    assert audit == before
    assert result["production_touched"] is False
    assert result["writes"] == result["deletes"] == 0
    assert result["candidate_count"] == 114
    assert result["first_pass"] == {"added": 114, "skipped": 0}
    assert result["idempotent_second_pass"] == {"added": 0, "skipped": 114}
    assert result["grade_counts"] == {
        "Lost": 47, "PENDING": 1, "Refunded": 9, "Won": 57,
    }
    assert result["rollover"]["sealed_batch_count"] == 5
    assert result["rollover"]["pending_rollover_progress"]["eligible_decided"] == 4


def test_plan_rejects_identity_drift() -> None:
    audit = _audit()
    audit["condition"]["definition"]["direction"] = "A→B"
    with pytest.raises(ValueError, match="immutable axes changed"):
        plan(audit, OVERRIDES)


def test_plan_rejects_duplicate_fixture() -> None:
    audit = _audit()
    audit["missing_enrolments"][1]["fixture"] = audit["missing_enrolments"][0]["fixture"]
    with pytest.raises(ValueError, match="missing or duplicated"):
        plan(audit, OVERRIDES)
