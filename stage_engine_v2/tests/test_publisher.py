"""publisher 門檻邏輯測試（conviction-based）。"""
from stage_engine_v2.publisher import decide_publish
from stage_engine_v2.config import StageThresholds


def _pred(**overrides):
    """預設一個 conviction 70、EV 微負但仍過寬鬆門檻嘅 pred。"""
    base = {
        "conviction": 70.0,
        "lead_ev": -0.05,
        "lead_prob": 0.50,
        "lead_odds": 1.90,
    }
    base.update(overrides)
    return base


def test_ok_when_conviction_above_threshold():
    ok, reason = decide_publish(_pred(), "T-5")
    assert ok, reason


def test_reject_when_conviction_below():
    ok, reason = decide_publish(_pred(conviction=55.0), "T-5")
    assert not ok
    assert "below_min_conviction" in reason


def test_missing_conviction_rejected():
    p = _pred()
    del p["conviction"]
    ok, reason = decide_publish(p, "首預")
    assert not ok
    assert reason == "missing_conviction"


def test_首預_relaxed_vs_T5_strict():
    """同一個 conviction=62 lead，首預過（≥60），T-5 唔過（要 70）。"""
    p = _pred(conviction=62.0)
    ok_首預, _ = decide_publish(p, "首預")
    ok_T5, r_T5 = decide_publish(p, "T-5")
    assert ok_首預
    assert not ok_T5
    assert "below_min_conviction" in r_T5


def test_ev_secondary_only():
    """EV 極負超過 -0.15 就算 conviction 高都 reject（sanity check）。"""
    ok, reason = decide_publish(_pred(lead_ev=-0.30), "T-5")
    assert not ok
    assert "below_min_ev" in reason


def test_prob_secondary_only():
    ok, reason = decide_publish(_pred(lead_prob=0.20), "T-5")
    assert not ok
    assert "below_min_prob" in reason


def test_odds_too_low():
    ok, reason = decide_publish(_pred(lead_odds=1.05), "T-5")
    assert not ok
    assert "below_min_odds" in reason


def test_odds_too_high():
    ok, reason = decide_publish(_pred(lead_odds=20.0), "T-5")
    assert not ok
    assert "above_max_odds" in reason


def test_missing_secondary_fields_ok():
    """secondary 缺失都要 pass——只 conviction 係硬要求。"""
    ok, _ = decide_publish({"conviction": 75.0}, "T-5")
    assert ok


def test_no_prediction_rejected():
    ok, reason = decide_publish({}, "首預")
    assert not ok
    assert reason == "no_prediction"


def test_unknown_stage_rejected():
    ok, reason = decide_publish(_pred(), "T-15")
    assert not ok
    assert "no_threshold_for_stage" in reason


def test_env_override(monkeypatch):
    """由環境變數 override 門檻。"""
    monkeypatch.setenv("STAGE_V2_THRESHOLDS_JSON", '{"T-5": {"min_conviction": 90.0}}')
    ok, reason = decide_publish(_pred(conviction=70.0), "T-5")
    assert not ok
    assert "below_min_conviction" in reason
    assert "90.0" in reason
