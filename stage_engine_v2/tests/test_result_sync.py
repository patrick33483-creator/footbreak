from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone

import pytest

from crown.common import SETTLE_AFTER_SECONDS
from stage_engine_v2.cli import _write_dashboard
from stage_engine_v2.result_sync import (
    load_automatic_history_rows,
    merge_automatic_history_rows,
    sync_results,
)


def _stage() -> dict:
    return {
        "lead_code": "HIL",
        "lead_market": "HIL",
        "lead_side": "H",
        "lead_line": 2.5,
        "lead_odds": 1.9,
        "predicted_at_utc": "2026-09-03T00:55:00+00:00",
    }


def _ledger(kickoff: datetime) -> dict:
    return {"fixtures": {"123": {
        "id": "123",
        "league": "測試聯賽",
        "home": "主隊",
        "away": "客隊",
        "kickoff_utc": kickoff.isoformat(),
        "kickoff_hkt": kickoff.astimezone(
            timezone(timedelta(hours=8))
        ).isoformat(),
        "stages": {"T-5": _stage()},
    }}}


class _Client:
    def __init__(self, rows: list[dict]) -> None:
        self.rows = rows
        self.calls: list[tuple[set[str], float]] = []

    def results(self, dates: set[str], max_seconds: float) -> list[dict]:
        self.calls.append((dates, max_seconds))
        return self.rows


def _provider_row(kickoff: datetime, *, reversed_order: bool = False) -> dict:
    return {
        "id": "provider-123",
        "league": "測試聯賽",
        "home": "客隊" if reversed_order else "主隊",
        "away": "主隊" if reversed_order else "客隊",
        "kickoff": kickoff,
        "home_score": 1 if reversed_order else 2,
        "away_score": 2 if reversed_order else 1,
    }


def test_sync_waits_until_settlement_delay(tmp_path) -> None:
    kickoff = datetime(2026, 9, 3, 1, 0, tzinfo=timezone.utc)
    client = _Client([_provider_row(kickoff)])
    result = sync_results(
        _ledger(kickoff),
        path=tmp_path / "automatic.json",
        now_utc=kickoff + timedelta(seconds=SETTLE_AFTER_SECONDS - 1),
        client=client,
    )
    assert result["due"] == 0
    assert client.calls == []


@pytest.mark.parametrize("reversed_order", [False, True])
def test_sync_identity_locks_and_normalizes_orientation(
    tmp_path, reversed_order: bool
) -> None:
    kickoff = datetime(2026, 9, 3, 1, 0, tzinfo=timezone.utc)
    path = tmp_path / "automatic.json"
    client = _Client([_provider_row(kickoff, reversed_order=reversed_order)])
    result = sync_results(
        _ledger(kickoff),
        path=path,
        now_utc=kickoff + timedelta(seconds=SETTLE_AFTER_SECONDS + 1),
        client=client,
    )
    assert result["settled_now"] == 1
    saved = json.loads(path.read_text(encoding="utf-8"))["results"]["123"]
    assert (saved["home_score"], saved["away_score"]) == (2, 1)
    assert saved["orientation"] == ("reversed" if reversed_order else "direct")

    rows = load_automatic_history_rows(path, _ledger(kickoff))
    assert len(rows) == 1
    assert rows[0]["score"] == "2-1"
    assert rows[0]["market_grades"][0]["settlement"] == "Won"

    # A completed match is cached by match_id and never fetched/counts twice.
    second = sync_results(
        _ledger(kickoff),
        path=path,
        now_utc=kickoff + timedelta(seconds=SETTLE_AFTER_SECONDS + 120),
        client=client,
    )
    assert second["due"] == 0
    assert len(client.calls) == 1


def test_automatic_result_rejects_verified_score_conflict() -> None:
    existing = [{
        "match_id": "123",
        "stage": "T-5",
        "result_status": "已核對",
        "score": "9-9",
    }]
    automatic = [{
        "match_id": "123",
        "stage": "T-5",
        "result_status": "已核對",
        "score": "2-1",
    }]
    with pytest.raises(ValueError, match="conflicts with Crown history"):
        merge_automatic_history_rows(existing, automatic)


def test_settlement_dashboard_can_skip_notification_path(
    tmp_path, monkeypatch
) -> None:
    def _unexpected(*args, **kwargs):
        raise AssertionError("settlement must not enter Telegram delivery")

    monkeypatch.setattr("stage_engine_v2.cli.send_condition_alerts", _unexpected)
    dashboard = tmp_path / "dashboard.json"
    result = _write_dashboard(
        {"fixtures": {}},
        dashboard,
        now_utc=datetime(2026, 9, 3, tzinfo=timezone.utc),
        history_rows=[],
        send_alerts=False,
    )
    assert result == []
    assert dashboard.exists()
