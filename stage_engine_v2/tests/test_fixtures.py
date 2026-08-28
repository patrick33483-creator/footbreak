import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from stage_engine_v2.fixtures import HKT, parse_crown_matches, refresh_fixtures

UTC = timezone.utc


def test_parse_crown_matches_basic():
    payload = {
        "matches": [
            {
                "match_id": "M1",
                "native_fixture_id": "N1",
                "league": "德甲 2",
                "home": "布倫瑞克",
                "away": "哈化柏林",
                "kickoff": "2026-08-29 00:30",
                "hkjc_match_id": "12345",
            }
        ]
    }
    result = parse_crown_matches(payload)
    assert len(result) == 1
    fx = result[0]
    assert fx.id == "N1"  # 優先 native_fixture_id
    assert fx.league == "德甲 2"
    assert fx.source == "hkjc"
    assert fx.kickoff_hkt.hour == 0
    assert fx.kickoff_hkt.minute == 30


def test_missing_kickoff_skipped():
    payload = {"matches": [{"match_id": "M1"}]}
    assert parse_crown_matches(payload) == []


def test_missing_id_skipped():
    payload = {"matches": [{"kickoff": "2026-08-29 00:30"}]}
    assert parse_crown_matches(payload) == []


def test_refresh_filters_past_and_far_future(tmp_path: Path):
    now = datetime(2026, 8, 29, 0, 0, tzinfo=HKT).astimezone(UTC)
    payload = {
        "matches": [
            {  # 剛過咗
                "match_id": "past", "native_fixture_id": "past",
                "kickoff": "2026-08-28 23:00",
            },
            {  # 未來 18 分鐘（應該入選）
                "match_id": "soon", "native_fixture_id": "soon",
                "kickoff": "2026-08-29 00:18",
            },
            {  # 未來 5 日（超出 48h 窗口）
                "match_id": "far", "native_fixture_id": "far",
                "kickoff": "2026-09-03 00:00",
            },
        ]
    }
    path = tmp_path / "crown.json"
    path.write_text(json.dumps(payload), encoding="utf-8")
    result = refresh_fixtures(
        crown_data_path=path, window_hours=48, now_utc=now,
    )
    assert [fx.id for fx in result] == ["soon"]


def test_refresh_missing_file_returns_empty(tmp_path: Path):
    assert refresh_fixtures(crown_data_path=tmp_path / "nope.json") == []


def test_refresh_sorted_by_kickoff(tmp_path: Path):
    now = datetime(2026, 8, 29, 0, 0, tzinfo=HKT).astimezone(UTC)
    payload = {
        "matches": [
            {"native_fixture_id": "b", "kickoff": "2026-08-29 02:00"},
            {"native_fixture_id": "a", "kickoff": "2026-08-29 01:00"},
        ]
    }
    path = tmp_path / "crown.json"
    path.write_text(json.dumps(payload), encoding="utf-8")
    result = refresh_fixtures(crown_data_path=path, now_utc=now)
    assert [fx.id for fx in result] == ["a", "b"]


def test_dedupe_by_id(tmp_path: Path):
    now = datetime(2026, 8, 29, 0, 0, tzinfo=HKT).astimezone(UTC)
    payload = {
        "matches": [
            {"native_fixture_id": "dup", "kickoff": "2026-08-29 01:00", "league": "L1"},
            {"native_fixture_id": "dup", "kickoff": "2026-08-29 01:00", "league": "L2"},
        ]
    }
    path = tmp_path / "crown.json"
    path.write_text(json.dumps(payload), encoding="utf-8")
    result = refresh_fixtures(crown_data_path=path, now_utc=now)
    assert len(result) == 1
