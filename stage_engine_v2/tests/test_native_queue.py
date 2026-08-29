import json
from pathlib import Path

from stage_engine_v2.native_queue import load_native_payloads


def _write(path: Path, value: dict) -> None:
    path.write_text(json.dumps(value), encoding="utf-8")


def test_loads_pending_and_completed_exact_timed_payloads(tmp_path: Path):
    for index, state in enumerate(("PENDING", "COMPLETED")):
        stage = "T-30" if index == 0 else "T-5"
        match_id = f"F{index}"
        payload = {
            "match_id": match_id,
            "stage": stage,
            "status": "OK",
            "forecast_candidates": [{"code": "HIL", "odds": 1.9, "prob": 0.6}],
        }
        _write(tmp_path / f"{index}.json", {
            "match_id": match_id,
            "stage": stage,
            "state": state,
            "payload": payload,
        })
    loaded = load_native_payloads(tmp_path)
    assert set(loaded) == {("F0", "T-30"), ("F1", "T-5")}


def test_rejects_unavailable_mismatch_opening_and_corrupt_rows(tmp_path: Path):
    _write(tmp_path / "missing.json", {
        "match_id": "F1",
        "stage": "T-30",
        "payload": {
            "match_id": "F1", "stage": "T-30", "status": "DATA_MISSING",
            "forecast_candidates": [],
        },
    })
    _write(tmp_path / "mismatch.json", {
        "match_id": "F2",
        "stage": "T-30",
        "payload": {
            "match_id": "OTHER", "stage": "T-30", "status": "OK",
            "forecast_candidates": [{"odds": 1.9, "prob": 0.6}],
        },
    })
    _write(tmp_path / "opening.json", {
        "match_id": "F3",
        "stage": "首預",
        "payload": {
            "match_id": "F3", "stage": "首預", "status": "OK",
            "forecast_candidates": [{"odds": 1.9, "prob": 0.6}],
        },
    })
    (tmp_path / "corrupt.json").write_text("{", encoding="utf-8")
    assert load_native_payloads(tmp_path) == {}
