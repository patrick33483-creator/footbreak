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


class _DetailClient(_Client):
    def __init__(self, rows: list[dict], details: dict[str, dict | None]) -> None:
        super().__init__(rows)
        self.details = details
        self.detail_calls: list[tuple[str, float]] = []

    def result_header(self, match_id: str, max_seconds: float) -> dict | None:
        self.detail_calls.append((match_id, max_seconds))
        return self.details.get(match_id)


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


def test_sync_compiles_provider_candidates_once(tmp_path, monkeypatch) -> None:
    kickoff = datetime(2026, 9, 3, 1, 0, tzinfo=timezone.utc)
    ledger = _ledger(kickoff)
    second = dict(ledger["fixtures"]["123"])
    second["id"] = "9002"
    second["home"] = "另一主隊"
    second["away"] = "另一客隊"
    ledger["fixtures"]["9002"] = second

    import stage_engine_v2.result_sync as result_sync

    calls = 0
    original = result_sync._candidate

    def counted(row):
        nonlocal calls
        calls += 1
        return original(row)

    monkeypatch.setattr(result_sync, "_candidate", counted)
    provider_rows = [_provider_row(kickoff)]
    sync_results(
        ledger,
        path=tmp_path / "automatic.json",
        now_utc=kickoff + timedelta(seconds=SETTLE_AFTER_SECONDS + 1),
        client=_Client(provider_rows),
    )
    assert calls == len(provider_rows)


def test_sync_prioritises_newest_bounded_batch(tmp_path) -> None:
    now = datetime(2026, 9, 3, 12, 0, tzinfo=timezone.utc)
    ledger = {"fixtures": {}}
    provider_rows = []
    for number in range(3):
        kickoff = now - timedelta(hours=number + 2)
        match_id = str(100 + number)
        slot = dict(_ledger(kickoff)["fixtures"]["123"])
        slot["id"] = match_id
        slot["home"] = f"主隊 {number}"
        slot["away"] = f"客隊 {number}"
        ledger["fixtures"][match_id] = slot
        row = _provider_row(kickoff)
        row.update({
            "id": f"provider-{match_id}",
            "home": slot["home"],
            "away": slot["away"],
        })
        provider_rows.append(row)
    path = tmp_path / "automatic.json"
    result = sync_results(
        ledger,
        path=path,
        now_utc=now,
        max_fixtures=2,
        client=_Client(provider_rows),
    )
    assert result["eligible_due"] == 3
    assert result["due"] == 2
    assert set(json.loads(path.read_text())["results"]) == {"100", "101"}


def test_sync_falls_back_to_exact_result_detail_when_bulk_page_is_empty(
    tmp_path,
) -> None:
    kickoff = datetime(2026, 9, 3, 1, 0, tzinfo=timezone.utc)
    detail = _provider_row(kickoff)
    detail["id"] = "123"
    client = _DetailClient([], {"123": detail})
    path = tmp_path / "automatic.json"

    result = sync_results(
        _ledger(kickoff),
        path=path,
        now_utc=kickoff + timedelta(seconds=SETTLE_AFTER_SECONDS + 1),
        client=client,
    )

    assert result["bulk_fetched"] == 0
    assert result["detail_fetched"] == 1
    assert result["settled_now"] == 1
    assert client.detail_calls and client.detail_calls[0][0] == "123"
    saved = json.loads(path.read_text(encoding="utf-8"))["results"]["123"]
    assert (saved["home_score"], saved["away_score"]) == (2, 1)
