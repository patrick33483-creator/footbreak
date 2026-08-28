"""端到端 dry-run：模擬今晚 00:30 場景，兩個 tick 分別 fire 首預／T-30／T-5。"""
import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from stage_engine_v2.cli import _run_tick
from stage_engine_v2.fixtures import HKT
from stage_engine_v2.writer import load_ledger

UTC = timezone.utc


def _crown_payload(kickoff_hkt: str, fx_id: str = "50073785") -> dict:
    return {
        "matches": [
            {
                "match_id": fx_id,
                "native_fixture_id": fx_id,
                "hkjc_match_id": fx_id,
                "league": "德國乙組",
                "home": "布倫瑞克",
                "away": "哈化柏林",
                "kickoff": kickoff_hkt,
                "market_predictions": [
                    {
                        "market": "OU",
                        "label": "over 2.5",
                        "probability": 0.58,
                        "odds": 1.95,
                    },
                    {
                        "market": "1X2",
                        "label": "H",
                        "probability": 0.45,
                        "odds": 2.20,
                    },
                ],
            }
        ]
    }


def test_first_stage_fires_at_startup(tmp_path: Path):
    """T minus 60 分：只 fire 首預，未到 T-30／T-5 窗。"""
    crown = tmp_path / "crown.json"
    crown.write_text(json.dumps(_crown_payload("2026-08-29 00:30")), encoding="utf-8")
    now_hkt = datetime(2026, 8, 28, 23, 30, tzinfo=HKT)  # T-60min
    result = _run_tick(
        crown_data_path=crown,
        ledger_path=tmp_path / "ledger.json",
        dashboard_path=tmp_path / "dashboard.json",
        sent_log_path=tmp_path / "sent.jsonl",
        now_utc=now_hkt.astimezone(UTC),
        window_hours=48,
        dry_run=False,
    )
    assert result["fired_count"] == 1
    assert result["fired"][0]["stage"] == "首預"


def test_t30_fires_at_30min_mark(tmp_path: Path):
    """T-30 剛好：fire T-30；首預 亦一齊被 fire（因為之前無跑過）。"""
    crown = tmp_path / "crown.json"
    crown.write_text(json.dumps(_crown_payload("2026-08-29 00:30")), encoding="utf-8")
    now_hkt = datetime(2026, 8, 29, 0, 0, tzinfo=HKT)  # T-30min 剛剛
    result = _run_tick(
        crown_data_path=crown,
        ledger_path=tmp_path / "ledger.json",
        dashboard_path=tmp_path / "dashboard.json",
        sent_log_path=tmp_path / "sent.jsonl",
        now_utc=now_hkt.astimezone(UTC),
        window_hours=48,
        dry_run=False,
    )
    stages_fired = {row["stage"] for row in result["fired"]}
    assert stages_fired == {"首預", "T-30"}


def test_t5_fires_in_final_window(tmp_path: Path):
    """T-5 剛好：fire T-5；首預／T-30 一齊補跑（因為窗口內）。"""
    crown = tmp_path / "crown.json"
    crown.write_text(json.dumps(_crown_payload("2026-08-29 00:30")), encoding="utf-8")
    now_hkt = datetime(2026, 8, 29, 0, 25, tzinfo=HKT)  # T-5min
    result = _run_tick(
        crown_data_path=crown,
        ledger_path=tmp_path / "ledger.json",
        dashboard_path=tmp_path / "dashboard.json",
        sent_log_path=tmp_path / "sent.jsonl",
        now_utc=now_hkt.astimezone(UTC),
        window_hours=48,
        dry_run=False,
    )
    stages_fired = {row["stage"] for row in result["fired"]}
    # T-30 已經 miss 咗（超過 25 min catchup），只 fire 首預 + T-5
    assert "首預" in stages_fired
    assert "T-5" in stages_fired


def test_full_lifecycle_three_ticks(tmp_path: Path):
    """三個 tick：T-60（首預）、T-30（T-30）、T-5（T-5）。每 stage 剛好 fire 一次。"""
    crown = tmp_path / "crown.json"
    ledger = tmp_path / "ledger.json"
    dashboard = tmp_path / "dashboard.json"
    sent = tmp_path / "sent.jsonl"
    crown.write_text(json.dumps(_crown_payload("2026-08-29 00:30")), encoding="utf-8")

    ticks = [
        (datetime(2026, 8, 28, 23, 30, tzinfo=HKT), {"首預"}),
        (datetime(2026, 8, 29, 0, 0, tzinfo=HKT), {"T-30"}),
        (datetime(2026, 8, 29, 0, 25, tzinfo=HKT), {"T-5"}),
    ]
    for now_hkt, expected in ticks:
        result = _run_tick(
            crown_data_path=crown,
            ledger_path=ledger,
            dashboard_path=dashboard,
            sent_log_path=sent,
            now_utc=now_hkt.astimezone(UTC),
            window_hours=48,
            dry_run=False,
        )
        stages_fired = {row["stage"] for row in result["fired"]}
        assert stages_fired == expected, f"at {now_hkt}: expected {expected} got {stages_fired}"

    # 最終 ledger 三個 stage 都應該有
    final = load_ledger(ledger)
    fixture_slot = final["fixtures"]["50073785"]
    assert set(fixture_slot["stages"].keys()) == {"首預", "T-30", "T-5"}
    for stage_row in fixture_slot["stages"].values():
        assert stage_row["lead_market"] == "OU"  # 揀最高 EV


def test_no_fire_after_kickoff(tmp_path: Path):
    """開賽後：即使 stage 未 fire 過，都唔會再 fire。"""
    crown = tmp_path / "crown.json"
    crown.write_text(json.dumps(_crown_payload("2026-08-29 00:30")), encoding="utf-8")
    now_hkt = datetime(2026, 8, 29, 0, 45, tzinfo=HKT)  # 開賽後 15 min
    result = _run_tick(
        crown_data_path=crown,
        ledger_path=tmp_path / "ledger.json",
        dashboard_path=tmp_path / "dashboard.json",
        sent_log_path=tmp_path / "sent.jsonl",
        now_utc=now_hkt.astimezone(UTC),
        window_hours=48,
        dry_run=False,
    )
    # fixtures_upcoming 應該係 0（已經過咗）
    assert result["fixtures_upcoming"] == 0
    assert result["fired_count"] == 0


def test_dry_run_does_not_write_ledger(tmp_path: Path):
    crown = tmp_path / "crown.json"
    ledger = tmp_path / "ledger.json"
    crown.write_text(json.dumps(_crown_payload("2026-08-29 00:30")), encoding="utf-8")
    now_hkt = datetime(2026, 8, 28, 23, 30, tzinfo=HKT)
    _run_tick(
        crown_data_path=crown,
        ledger_path=ledger,
        dashboard_path=tmp_path / "dashboard.json",
        sent_log_path=tmp_path / "sent.jsonl",
        now_utc=now_hkt.astimezone(UTC),
        window_hours=48,
        dry_run=True,
    )
    assert not ledger.exists()


def test_dashboard_written(tmp_path: Path):
    crown = tmp_path / "crown.json"
    dashboard = tmp_path / "dashboard.json"
    crown.write_text(json.dumps(_crown_payload("2026-08-29 00:30")), encoding="utf-8")
    now_hkt = datetime(2026, 8, 28, 23, 30, tzinfo=HKT)
    _run_tick(
        crown_data_path=crown,
        ledger_path=tmp_path / "ledger.json",
        dashboard_path=dashboard,
        sent_log_path=tmp_path / "sent.jsonl",
        now_utc=now_hkt.astimezone(UTC),
        window_hours=48,
        dry_run=False,
    )
    assert dashboard.exists()
    payload = json.loads(dashboard.read_text(encoding="utf-8"))
    assert payload["schema_version"] == "stage-engine-v2"
    assert payload["fixtures_count"] == 1
