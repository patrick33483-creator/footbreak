import json
from pathlib import Path

from stage_engine_v2.telegram import format_message, send_stage


def _pred() -> dict:
    return {
        "stage": "T-5",
        "fixture_id": "F1",
        "league": "德甲",
        "home": "拜仁",
        "away": "多蒙特",
        "kickoff_hkt": "2026-08-29T21:30:00+08:00",
        "lead_market": "OU",
        "lead_label": "over 2.5",
        "lead_odds": 1.85,
        "lead_prob": 0.62,
        "lead_ev": 0.147,
    }


def test_format_contains_key_fields():
    text = format_message(_pred())
    assert "T-5" in text
    assert "德甲" in text
    assert "拜仁" in text
    assert "over 2.5" in text
    assert "1.85" in text


def test_shadow_writes_log_and_no_send(tmp_path: Path, monkeypatch):
    monkeypatch.delenv("STAGE_V2_TELEGRAM_ENABLED", raising=False)
    log = tmp_path / "sent.jsonl"
    result = send_stage(_pred(), sent_log_path=log)
    assert result["shadow"] is True
    assert result["sent"] is False
    lines = log.read_text(encoding="utf-8").strip().splitlines()
    assert len(lines) == 1
    row = json.loads(lines[0])
    assert row["key"] == "F1:T-5"
    assert row["mode"] == "shadow"


def test_dedupe_skips_second_call(tmp_path: Path):
    log = tmp_path / "sent.jsonl"
    send_stage(_pred(), sent_log_path=log)
    result2 = send_stage(_pred(), sent_log_path=log)
    assert result2["skipped"] is True
    assert result2["reason"] == "duplicate"


def test_enabled_without_credentials_still_shadow(tmp_path: Path, monkeypatch):
    monkeypatch.setenv("STAGE_V2_TELEGRAM_ENABLED", "1")
    monkeypatch.delenv("STAGE_V2_TELEGRAM_BOT_TOKEN", raising=False)
    monkeypatch.delenv("STAGE_V2_TELEGRAM_CHAT_ID", raising=False)
    log = tmp_path / "sent.jsonl"
    result = send_stage(_pred(), sent_log_path=log)
    assert result["shadow"] is True
    assert result["reason"] == "missing_credentials"
