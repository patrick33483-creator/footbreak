import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

from stage_engine_v2.telegram import (
    T5_CONDITION_ALERT_IDS,
    format_condition_message,
    send_condition_alert,
    send_condition_alerts,
)


def _condition(condition_id: str = "S-HIL-T5-OVER-185") -> dict:
    return {
        "id": condition_id,
        "tier": "S",
        "market": "HIL",
        "title": "T-5 預測大；賠率 > 1.85",
        "path_label": "T-5：大",
        "historical": {
            "sample": 94, "hit_rate": 0.6667, "roi": 0.2284,
        },
    }


NOW = datetime(2026, 8, 29, 13, 25, tzinfo=timezone.utc)


def _observation(match_id: str = "M1") -> dict:
    return {
        "condition_id": "S-HIL-T5-OVER-185",
        "match_id": match_id,
        "league": "德甲",
        "home": "拜仁",
        "away": "多蒙特",
        "kickoff": "2026-08-29T21:30:00+08:00",
        "decision_stage": "T-5",
        "selected_side": "H",
        "selected_line": 2.5,
        "odds": 1.92,
        "directions": {"T-5": "大"},
    }


def test_t5_condition_alert_ids_exactly_three_and_excludes_opening_condition():
    assert T5_CONDITION_ALERT_IDS == {
        "S-HIL-T5-OVER-185",
        "A-HIL-OPEN-T5-OVER-180",
        "A-HDC-HHH-SAME-LINE",
    }
    assert "A-HDC-OPEN-AWAY-MINUS-050" not in T5_CONDITION_ALERT_IDS


def test_format_condition_message_contains_key_fields():
    text = format_condition_message(_condition(), _observation())
    assert "S" in text
    assert "T-5" in text
    assert "德甲" in text
    assert "拜仁" in text
    assert "多蒙特" in text
    assert "大" in text
    assert "1.92" in text
    assert "94" in text
    assert "66.7" in text


def test_shadow_writes_log_and_no_send(tmp_path: Path, monkeypatch):
    monkeypatch.delenv("STAGE_V2_CONDITION_ALERT_ENABLED", raising=False)
    log = tmp_path / "condition_sent.jsonl"
    result = send_condition_alert(
        _condition(), _observation(), sent_log_path=log, now_utc=NOW
    )
    assert result["shadow"] is True
    assert result["sent"] is False
    lines = log.read_text(encoding="utf-8").strip().splitlines()
    assert len(lines) == 1
    row = json.loads(lines[0])
    assert row["key"] == "S-HIL-T5-OVER-185:M1"
    assert row["mode"] == "shadow"


def test_dedupe_skips_second_call(tmp_path: Path):
    log = tmp_path / "condition_sent.jsonl"
    send_condition_alert(
        _condition(), _observation(), sent_log_path=log, now_utc=NOW
    )
    result2 = send_condition_alert(
        _condition(), _observation(), sent_log_path=log, now_utc=NOW
    )
    assert result2["skipped"] is True
    assert result2["reason"] == "duplicate"


def test_enabled_without_credentials_still_shadow(tmp_path: Path, monkeypatch):
    monkeypatch.setenv("STAGE_V2_CONDITION_ALERT_ENABLED", "1")
    monkeypatch.delenv("TELEGRAM_BOT_TOKEN", raising=False)
    monkeypatch.delenv("TELEGRAM_CHAT_ID", raising=False)
    log = tmp_path / "condition_sent.jsonl"
    result = send_condition_alert(
        _condition(), _observation(), sent_log_path=log, now_utc=NOW
    )
    assert result["shadow"] is True
    assert result["reason"] == "missing_credentials"


def test_identity_key_falls_back_to_match_details_when_no_match_id(tmp_path: Path):
    log = tmp_path / "condition_sent.jsonl"
    obs = _observation(match_id="")
    result = send_condition_alert(
        _condition(), obs, sent_log_path=log, now_utc=NOW
    )
    assert result["key"] == "S-HIL-T5-OVER-185:拜仁|多蒙特|2026-08-29T21:30:00+08:00"


def test_send_condition_alerts_only_covers_t5_conditions(tmp_path: Path):
    log = tmp_path / "condition_sent.jsonl"
    payload = {
        "public_conditions": [
            {**_condition("S-HIL-T5-OVER-185"), "observations": [_observation("M1")]},
            {**_condition("A-HIL-OPEN-T5-OVER-180"), "observations": [_observation("M2")]},
            {**_condition("A-HDC-HHH-SAME-LINE"), "observations": [_observation("M3")]},
            # Opening-stage condition, must be excluded.
            {**_condition("A-HDC-OPEN-AWAY-MINUS-050"), "observations": [_observation("M4")]},
        ]
    }
    results = send_condition_alerts(payload, sent_log_path=log, now_utc=NOW)
    keys = {r["key"] for r in results}
    assert keys == {
        "S-HIL-T5-OVER-185:M1",
        "A-HIL-OPEN-T5-OVER-180:M2",
        "A-HDC-HHH-SAME-LINE:M3",
    }


def test_send_condition_alerts_is_idempotent_across_calls(tmp_path: Path):
    log = tmp_path / "condition_sent.jsonl"
    payload = {
        "public_conditions": [
            {**_condition("S-HIL-T5-OVER-185"), "observations": [_observation("M1")]},
        ]
    }
    first = send_condition_alerts(payload, sent_log_path=log, now_utc=NOW)
    second = send_condition_alerts(payload, sent_log_path=log, now_utc=NOW)
    assert first[0]["skipped"] is False
    assert second[0]["skipped"] is True
    assert second[0]["reason"] == "duplicate"


def test_condition_alert_never_sends_after_kickoff(tmp_path: Path, monkeypatch):
    monkeypatch.setenv("STAGE_V2_CONDITION_ALERT_ENABLED", "1")
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "token")
    monkeypatch.setenv("TELEGRAM_CHAT_ID", "chat")
    monkeypatch.setattr(
        "stage_engine_v2.telegram._post_telegram",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            AssertionError("expired alert must not call Telegram")
        ),
    )
    log = tmp_path / "condition_sent.jsonl"
    result = send_condition_alert(
        _condition(),
        _observation(),
        sent_log_path=log,
        now_utc=NOW + timedelta(minutes=6),
    )
    assert result["sent"] is False
    assert result["skipped"] is True
    assert result["reason"] == "outside_t5_window"
    assert not log.exists()


def test_condition_alert_rejects_too_early_observation(tmp_path: Path):
    log = tmp_path / "condition_sent.jsonl"
    result = send_condition_alert(
        _condition(),
        _observation(),
        sent_log_path=log,
        now_utc=NOW - timedelta(minutes=6),
    )
    assert result["skipped"] is True
    assert result["reason"] == "outside_t5_window"
    assert not log.exists()
