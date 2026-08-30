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
    fx = _fx({
        "stage": "首預",
        "prediction_model": "crown-opening-fixed-v1",
        "market_predictions": [{"market": "x"}],
    })
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
    fx = _fx({"stage": "T-30", "market_predictions": rows})
    pred = build_prediction(fx, "T-30")
    assert pred is not None
    assert pred["lead_market"] == "OU"
    assert pred["lead_label"] == "over 2.5"
    assert abs(pred["lead_ev"] - 0.2) < 1e-9
    assert pred["market_predictions_count"] == 3


def test_uses_exact_stage_instead_of_latest_stage():
    """T-30 must not borrow the newer T-5 prediction."""
    raw = {
        "stages": [
            {
                "stage": "首預",
                "ts": "2026-08-29T05:00:00+08:00",
                "prediction_model": "crown-opening-fixed-v1",
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
            {
                "stage": "T-5",
                "ts": "2026-08-29T11:55:00+08:00",
                "market_predictions": [
                    {"market": "OU", "label": "over 3.5", "probability": 0.8, "odds": 2.0}
                ],
            },
        ]
    }
    fx = _fx(raw)
    pred = build_prediction(fx, "T-30")
    assert pred is not None
    assert pred["lead_market"] == "1X2"
    assert pred["source_stage"] == "T-30"


def test_timed_stage_prefers_exact_native_payload_over_legacy_projection():
    raw = {
        "stages": [{
            "stage": "T-30",
            "market_predictions": [
                {"market": "OU", "label": "legacy under", "probability": 0.51, "odds": 1.9}
            ],
        }]
    }
    native = {
        "match_id": "F1",
        "stage": "T-30",
        "generated_at": "2026-08-29T11:30:02+08:00",
        "status": "OK",
        "forecast_candidates": [{
            "code": "HIL",
            "market": "入球大細",
            "side": "O",
            "line": 2.75,
            "odds": 1.95,
            "prob": 0.60,
        }],
    }
    pred = build_prediction(_fx(raw), "T-30", native_payload=native)
    assert pred is not None
    assert pred["lead_market"] == "入球大細"
    assert pred["lead_code"] == "HIL"
    assert pred["lead_label"] == "O 2.75"
    assert pred["lead_side"] == "O"
    assert pred["lead_line"] == 2.75
    assert pred["lead_odds"] == 1.95
    assert pred["lead_prob"] == 0.60
    assert pred["stage_payload_source"] == "crown_native_stage_queue"
    assert pred["source_predicted_at"] == "2026-08-29T11:30:02+08:00"


def test_native_payload_cannot_cross_stage():
    native_t5 = {
        "match_id": "F1",
        "stage": "T-5",
        "status": "OK",
        "forecast_candidates": [{
            "code": "HIL", "side": "O", "line": 2.5,
            "odds": 1.9, "prob": 0.6,
        }],
    }
    assert build_prediction(_fx({}), "T-30", native_payload=native_t5) is None


def test_opening_rejects_legacy_first_prediction():
    raw = {
        "stages": [{
            "stage": "首預",
            "market_predictions": [
                {"market": "OU", "label": "over 2.5", "probability": 0.6, "odds": 1.9}
            ],
        }]
    }
    assert build_prediction(_fx(raw), "首預") is None


def test_opening_accepts_fixed_model_and_preserves_audit_metadata():
    raw = {
        "stages": [{
            "stage": "首預",
            "prediction_model": "crown-opening-fixed-v1",
            "input_policy": "first_complete_crown_quote_plus_pre_cutoff_results",
            "input_cutoff_at": "2026-08-29T01:00:00+00:00",
            "opening_snapshot_hash": "abc123",
            "opening_model_status": "market_plus_team_history",
            "late_inputs_used": [],
            "market_predictions": [
                {"market": "OU", "label": "over 2.5", "probability": 0.6, "odds": 1.9}
            ],
        }]
    }
    pred = build_prediction(_fx(raw), "首預")
    assert pred is not None
    assert pred["prediction_model"] == "crown-opening-fixed-v1"
    assert pred["opening_snapshot_hash"] == "abc123"
    assert pred["late_inputs_used"] == []


def test_rejects_impossible_probability():
    rows = [{"market": "x", "label": "y", "probability": 1.5, "odds": 2.0}]
    fx = _fx({
        "stage": "首預",
        "prediction_model": "crown-opening-fixed-v1",
        "market_predictions": rows,
    })
    assert build_prediction(fx, "首預") is None


def test_rejects_zero_or_negative_odds():
    rows = [{"market": "x", "label": "y", "probability": 0.5, "odds": 0.9}]
    fx = _fx({
        "stage": "首預",
        "prediction_model": "crown-opening-fixed-v1",
        "market_predictions": rows,
    })
    assert build_prediction(fx, "首預") is None
