"""決定「呢個 lead 值唔值得發 Telegram」。純 gate，唔改 lead selection。"""
from __future__ import annotations

from typing import Any

from stage_engine_v2.config import StageThresholds, load_thresholds


def decide_publish(
    prediction: dict[str, Any],
    stage: str,
    thresholds: dict[str, StageThresholds] | None = None,
) -> tuple[bool, str]:
    """
    返回 (should_publish, reason)。
    reason 一律填，方便 telemetry／debug。
    """
    if not prediction:
        return False, "no_prediction"

    thresholds = thresholds or load_thresholds()
    thr = thresholds.get(stage)
    if not thr:
        return False, f"no_threshold_for_stage:{stage}"

    conviction = prediction.get("conviction")
    if conviction is None:
        return False, "missing_conviction"
    if conviction < thr.min_conviction:
        return False, f"below_min_conviction:{conviction:.1f}<{thr.min_conviction:.1f}"

    # Secondary sanity checks（寬鬆門檻）
    ev = prediction.get("lead_ev")
    prob = prediction.get("lead_prob")
    odds = prediction.get("lead_odds")

    if ev is not None and ev < thr.min_ev:
        return False, f"below_min_ev:{ev:.3f}<{thr.min_ev:.3f}"
    if prob is not None and prob < thr.min_prob:
        return False, f"below_min_prob:{prob:.3f}<{thr.min_prob:.3f}"
    if odds is not None and odds < thr.min_odds:
        return False, f"below_min_odds:{odds:.2f}<{thr.min_odds:.2f}"
    if odds is not None and odds > thr.max_odds:
        return False, f"above_max_odds:{odds:.2f}>{thr.max_odds:.2f}"

    return True, "ok"
