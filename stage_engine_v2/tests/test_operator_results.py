from __future__ import annotations

import json

import pytest

from stage_engine_v2.cli import _load_history_rows, _write_dashboard
from stage_engine_v2.operator_results import load_operator_history_rows


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
            "首預": _stage("HIL", "H", 2.25, 1.81),
            "T-5": _stage("HIL", "H", 2.25, 1.90),
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
            "home_score": 1,
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
        assert condition["observations"][0]["score"] == "1-1"


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
