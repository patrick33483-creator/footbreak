from datetime import datetime, timezone
from pathlib import Path

import pytest

from stage_engine_v2.fixtures import Fixture, HKT
from stage_engine_v2.writer import (
    already_fired, load_ledger, record_stage, save_ledger,
)


def _fx(fx_id: str = "F1") -> Fixture:
    kickoff_hkt = datetime(2026, 8, 29, 12, 0, tzinfo=HKT)
    return Fixture(
        id=fx_id, league="L", home="H", away="A",
        kickoff_utc=kickoff_hkt.astimezone(timezone.utc),
        kickoff_hkt=kickoff_hkt,
        source="hkjc",
    )


def test_load_ledger_missing_returns_empty(tmp_path: Path):
    ledger = load_ledger(tmp_path / "nope.json")
    assert ledger["schema_version"] == 1
    assert ledger["fixtures"] == {}


def test_save_and_load_roundtrip(tmp_path: Path):
    path = tmp_path / "ledger.json"
    ledger = load_ledger(path)
    fx = _fx()
    record_stage(ledger, fx, "首預", {"stage": "首預", "lead_odds": 2.0})
    save_ledger(ledger, path)
    reloaded = load_ledger(path)
    assert "F1" in reloaded["fixtures"]
    assert "首預" in reloaded["fixtures"]["F1"]["stages"]


def test_record_stage_append_only(tmp_path: Path):
    ledger = load_ledger(tmp_path / "ledger.json")
    fx = _fx()
    record_stage(ledger, fx, "首預", {"lead_odds": 2.0, "predicted_at_utc": "first"})
    # 嘗試覆蓋，應該 no-op
    record_stage(ledger, fx, "首預", {"lead_odds": 9.9, "predicted_at_utc": "second"})
    row = ledger["fixtures"]["F1"]["stages"]["首預"]
    assert row["lead_odds"] == 2.0


def test_already_fired_reports_stages(tmp_path: Path):
    ledger = load_ledger(tmp_path / "ledger.json")
    fx = _fx()
    record_stage(ledger, fx, "首預", {"lead_odds": 2.0})
    record_stage(ledger, fx, "T-30", {"lead_odds": 2.1})
    assert already_fired(ledger, "F1") == {"首預", "T-30"}
    assert already_fired(ledger, "missing") == set()


def test_atomic_write_leaves_no_tmp(tmp_path: Path):
    path = tmp_path / "ledger.json"
    ledger = load_ledger(path)
    record_stage(ledger, _fx(), "首預", {})
    save_ledger(ledger, path)
    residues = [p.name for p in tmp_path.iterdir() if p.name.startswith(".ledger-")]
    assert residues == []


def test_multiple_fixtures_separate_slots(tmp_path: Path):
    ledger = load_ledger(tmp_path / "ledger.json")
    record_stage(ledger, _fx("A"), "首預", {})
    record_stage(ledger, _fx("B"), "首預", {})
    assert set(ledger["fixtures"].keys()) == {"A", "B"}
