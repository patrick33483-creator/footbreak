from __future__ import annotations

import json

import pytest

from stage_engine_v2.cli import _load_history_rows, _write_dashboard
from stage_engine_v2.operator_results import (
    load_operator_history_rows,
    project_verified_crown_scores,
)


def _stage(code: str, side: str, line: float, odds: float) -> dict:
    return {
        "lead_code": code,
        "lead_market": code,
        "lead_side": side,
        "lead_line": line,
        "lead_odds": odds,
        "lead_label": f"{code} {side} {line}",
        "predicted_at_utc": "2026-08-30T16:55:00+00:00",
    }


def _ledger() -> dict:
    return {"fixtures": {"123": {
        "id": "123",
        "league": "測試聯賽",
        "home": "主隊",
        "away": "客隊",
        "kickoff_utc": "2026-08-30T17:00:00+00:00",
        "kickoff_hkt": "2026-08-31T01:00:00+08:00",
        "stages": {
            "首預": _stage("HIL", "H", 3.25, 1.81),
            "T-5": _stage("HIL", "H", 3.25, 1.90),
        },
    }}}


def _overlay() -> dict:
    return {
        "schema_version": 1,
        "batch_id": "operator-test-v1",
        "score_scope": "90_minutes_including_stoppage_time_excluding_extra_time",
        "verified_at": "2026-08-31T18:15:00+08:00",
        "results": [{
            "match_id": "123",
            "league": "測試聯賽",
            "home": "主隊",
            "away": "客隊",
            "kickoff": "2026-08-31T01:00:00+08:00",
            "home_score": 2,
            "away_score": 1,
            "provider_event_id": "provider-123",
            "provider_home": "Home",
            "provider_away": "Away",
            "provider_start": "2026-08-30T17:00:00Z",
            "orientation": "direct",
        }],
        "excluded": [],
    }


def test_operator_overlay_survives_repeated_dashboard_builds(tmp_path) -> None:
    crown_data = tmp_path / "crown.json"
    crown_data.write_text("{}", encoding="utf-8")
    operator_path = tmp_path / "operator.json"
    operator_path.write_text(json.dumps(_overlay()), encoding="utf-8")
    dashboard = tmp_path / "dashboard.json"
    ledger = _ledger()

    for _ in range(2):
        history = _load_history_rows(
            crown_data,
            ledger=ledger,
            operator_results_path=operator_path,
        )
        _write_dashboard(
            ledger,
            dashboard,
            now_utc=__import__("datetime").datetime.fromisoformat(
                "2026-08-31T18:30:00+00:00"
            ),
            history_rows=history,
            condition_sent_log_path=tmp_path / "condition-sent.jsonl",
        )
        payload = json.loads(dashboard.read_text(encoding="utf-8"))
        condition = next(
            row for row in payload["segmented_conditions"]["public_conditions"]
            if row["id"] == "S-HIL-T5-OVER-185"
        )
        assert condition["prospective"]["pending"] == 0
        assert condition["prospective"]["half_loss"] == 1
        assert condition["observations"][0]["result_status"] == "已核實"
        assert condition["observations"][0]["score"] == "2-1"


def test_operator_overlay_rejects_identity_mismatch(tmp_path) -> None:
    overlay = _overlay()
    overlay["results"][0]["home"] = "錯誤主隊"
    path = tmp_path / "operator.json"
    path.write_text(json.dumps(overlay), encoding="utf-8")

    with pytest.raises(ValueError, match="home mismatch"):
        load_operator_history_rows(path, _ledger())


def test_operator_overlay_rejects_verified_score_conflict(tmp_path) -> None:
    crown_data = tmp_path / "crown.json"
    crown_data.write_text(json.dumps({"prediction_history": {"rows": [{
        "match_id": "123",
        "stage": "T-5",
        "result_status": "已核對",
        "score": "9-9",
    }]}}), encoding="utf-8")
    operator_path = tmp_path / "operator.json"
    operator_path.write_text(json.dumps(_overlay()), encoding="utf-8")

    with pytest.raises(ValueError, match="conflicts with verified history"):
        _load_history_rows(
            crown_data,
            ledger=_ledger(),
            operator_results_path=operator_path,
        )


def test_verified_crown_score_grades_v2_stage_even_when_legacy_stage_is_missing(
    tmp_path,
) -> None:
    crown_data = tmp_path / "crown.json"
    crown_data.write_text(json.dumps({"prediction_history": {"rows": [{
        "match_id": "123",
        "stage": "T-30",
        "league": "測試聯賽",
        "home": "主隊",
        "away": "客隊",
        "kickoff": "2026-08-31T01:00:00+08:00",
        "result_status": "已核對",
        "score": "2-1",
        "market_grades": [],
    }]}}), encoding="utf-8")

    history = _load_history_rows(
        crown_data,
        ledger=_ledger(),
        operator_results_path=tmp_path / "missing-operator.json",
    )
    projected_t5 = [
        row for row in history
        if row.get("match_id") == "123"
        and row.get("stage") == "T-5"
        and row.get("result_source") == "crown_verified_history_bridge"
    ]
    assert len(projected_t5) == 1
    assert projected_t5[0]["score"] == "2-1"
    assert projected_t5[0]["market_grades"][0]["settlement"] == "Half Lost"


def test_verified_score_bridge_outranks_legacy_stage_with_different_line(
    tmp_path,
) -> None:
    crown_data = tmp_path / "crown.json"
    crown_data.write_text(json.dumps({"prediction_history": {"rows": [{
        "match_id": "123",
        "stage": "T-5",
        "league": "測試聯賽",
        "home": "主隊",
        "away": "客隊",
        "kickoff": "2026-08-31T01:00:00+08:00",
        "predicted_at": "2026-08-30T16:59:00+00:00",
        "result_status": "已核對",
        "score": "2-1",
        "market_grades": [{
            "code": "HIL",
            "side": "L",
            "line": 2.5,
            "grade_status": "GRADED",
            "settlement": "Lost",
        }],
    }]}}), encoding="utf-8")
    dashboard = tmp_path / "dashboard.json"
    ledger = _ledger()
    history = _load_history_rows(
        crown_data,
        ledger=ledger,
        operator_results_path=tmp_path / "missing-operator.json",
    )
    _write_dashboard(
        ledger,
        dashboard,
        now_utc=__import__("datetime").datetime.fromisoformat(
            "2026-08-31T18:30:00+00:00"
        ),
        history_rows=history,
        condition_sent_log_path=tmp_path / "condition-sent.jsonl",
    )
    condition = next(
        row for row in json.loads(dashboard.read_text(encoding="utf-8"))[
            "segmented_conditions"
        ]["public_conditions"]
        if row["id"] == "S-HIL-T5-OVER-185"
    )
    assert condition["prospective"]["settled"] == 1
    assert condition["prospective"]["half_loss"] == 1


def test_verified_crown_score_rejects_fixture_identity_mismatch() -> None:
    rows = [{
        "match_id": "123",
        "stage": "T-30",
        "home": "錯誤主隊",
        "away": "客隊",
        "kickoff": "2026-08-31T01:00:00+08:00",
        "result_status": "已核對",
        "score": "2-1",
    }]
    with pytest.raises(ValueError, match="home mismatch"):
        project_verified_crown_scores(rows, _ledger())
