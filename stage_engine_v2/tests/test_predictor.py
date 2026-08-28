from datetime import datetime, timezone

from stage_engine_v2.fixtures import Fixture, HKT
from stage_engine_v2.predictor import build_prediction


def _fx(raw: dict, fx_id: str = "F1") -> Fixture:
    kickoff_hkt = datetime(2026, 8, 29, 12, 0, tzinfo=HKT)
    return Fixture(
        id=fx_id, league="L", home="H", away="A",
        kickoff_utc=kickoff_hkt.astimezone(timezone.utc),
        kickoff_hkt=kickoff_hkt,
        source="hkjc",
        raw=raw,
    )


def test_none_when_no_market_predictions():
    fx = _fx({})
    assert build_prediction(fx, "首預") is None


def test_none_when_no_valid_row():
    fx = _fx({"market_predictions": [{"market": "x"}]})
    assert build_prediction(fx, "首預") is None


def test_picks_highest_ev():
    rows = [
        # EV = 0.5 * 2.0 - 1 = 0
        {"market": "1X2", "label": "H", "probability": 0.5, "odds": 2.0},
        # EV = 0.6 * 2.0 - 1 = 0.2 ← winner
        {"market": "OU", "label": "over 2.5", "probability": 0.6, "odds": 2.0},
        # EV = 0.3 * 2.5 - 1 = -0.25
        {"market": "BTTS", "label": "yes", "probability": 0.3, "odds": 2.5},
    ]
    fx = _fx({"market_predictions": rows})
    pred = build_prediction(fx, "T-30")
    assert pred is not None
    assert pred["lead_market"] == "OU"
    assert pred["lead_label"] == "over 2.5"
    assert abs(pred["lead_ev"] - 0.2) < 1e-9
    assert pred["market_predictions_count"] == 3


def test_falls_back_to_latest_stage():
    """market_predictions 藏喺 stages list 入面（真實 crown card 格式）。"""
    raw = {
        "stages": [
            {
                "stage": "首預",
                "ts": "2026-08-29T05:00:00+08:00",
                "market_predictions": [
                    {"market": "OU", "label": "under 2.5", "probability": 0.55, "odds": 1.9}
                ],
            },
            {
                "stage": "T-30",
                "ts": "2026-08-29T11:30:00+08:00",  # 較新
                "market_predictions": [
                    {"market": "1X2", "label": "H", "probability": 0.6, "odds": 2.0}
                ],
            },
        ]
    }
    fx = _fx(raw)
    pred = build_prediction(fx, "T-30")
    assert pred is not None
    assert pred["lead_market"] == "1X2"  # 攞最新 stage 嘅


def test_rejects_impossible_probability():
    rows = [{"market": "x", "label": "y", "probability": 1.5, "odds": 2.0}]
    fx = _fx({"market_predictions": rows})
    assert build_prediction(fx, "首預") is None


def test_rejects_zero_or_negative_odds():
    rows = [{"market": "x", "label": "y", "probability": 0.5, "odds": 0.9}]
    fx = _fx({"market_predictions": rows})
    assert build_prediction(fx, "首預") is None
