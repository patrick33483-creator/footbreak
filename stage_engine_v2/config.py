"""v2 門檻 config。可以由環境變數 STAGE_V2_THRESHOLDS_JSON 覆蓋。"""
from __future__ import annotations

import json
import os
from dataclasses import dataclass, field
from typing import Any


@dataclass(frozen=True)
class StageThresholds:
    """單一 stage 嘅推播門檻。全部係「至少」邏輯。
    
    crown data.json 用 conviction 分數（0-100）作主判準；
    EV / prob 係 secondary sanity check。
    """
    min_conviction: float
    min_ev: float = -0.15  # 預設寬鬆：crown no-vig prob 本來就若幹負 EV
    min_prob: float = 0.35  # 下限：防垃圾高 odds lead
    min_odds: float = 1.20
    max_odds: float = 15.0


DEFAULT_THRESHOLDS: dict[str, StageThresholds] = {
    "首預": StageThresholds(min_conviction=60.0),
    "T-30": StageThresholds(min_conviction=65.0),
    "T-5":  StageThresholds(min_conviction=70.0),
}


def load_thresholds() -> dict[str, StageThresholds]:
    """由環境變數 STAGE_V2_THRESHOLDS_JSON 讀 override；缺 key 用 default。"""
    raw = os.environ.get("STAGE_V2_THRESHOLDS_JSON")
    if not raw:
        return dict(DEFAULT_THRESHOLDS)
    try:
        override = json.loads(raw)
    except (json.JSONDecodeError, TypeError):
        return dict(DEFAULT_THRESHOLDS)
    merged: dict[str, StageThresholds] = {}
    for stage, default in DEFAULT_THRESHOLDS.items():
        entry = override.get(stage) or {}
        merged[stage] = StageThresholds(
            min_conviction=float(entry.get("min_conviction", default.min_conviction)),
            min_ev=float(entry.get("min_ev", default.min_ev)),
            min_prob=float(entry.get("min_prob", default.min_prob)),
            min_odds=float(entry.get("min_odds", default.min_odds)),
            max_odds=float(entry.get("max_odds", default.max_odds)),
        )
    return merged
