"""V2 footbreak predictor：直接讀 legacy footbreak stages[i].lead，唔重排 EV。

Legacy footbreak data.json schema：
    match["stages"] = [
        {"stage": "首預", "ts": "...", "lead": {"market", "label", "odds", "prob", "ev"},
         "conviction": ..., "verdict": ..., "market_predictions": [...], ...},
        ...
    ]

V2 footbreak 邏輯：
1. Fixture 由 legacy data.json matches 抽 (kickoff_hkt / match_id)
2. Stage 由 legacy stages list 匹配 (首預 / T-30 / T-5)
3. Lead 直接讀 stage["lead"]，如果冇就返回 None（唔自己排）
4. Conviction 直接讀 stage["conviction"] 或 match["conviction"]
"""
from __future__ import annotations
from datetime import datetime, timezone
from typing import Any
from stage_engine_v2.fixtures import Fixture


def _as_float(value: Any) -> float | None:
    try:
        if value is None or value == "":
            return None
        return float(value)
    except (TypeError, ValueError):
        return None


def _find_stage(match_card: dict[str, Any], stage_name: str) -> dict[str, Any] | None:
    """由 stages list 揾對應 stage。同名多個就攞最新 ts。"""
    stages = match_card.get("stages")
    if not isinstance(stages, list):
        return None
    matched = [s for s in stages if isinstance(s, dict) and s.get("stage") == stage_name]
    if not matched:
        return None
    matched.sort(key=lambda s: str(s.get("ts") or s.get("source_snapshot_at") or ""))
    return matched[-1]


def build_prediction_fb(
    fx: Fixture,
    stage: str,
    *,
    now_utc: datetime | None = None,
) -> dict[str, Any] | None:
    """組裝 V2 footbreak stage prediction record。

    唔會重新計算或者重排 lead。直接複製 legacy stage[stage].lead + conviction。
    如果 legacy 未 fire 呢個 stage（stages 入面冇對應 stage），返回 None，
    tick 下次再試。
    """
    stage_entry = _find_stage(fx.raw, stage)
    if not stage_entry:
        return None

    lead = stage_entry.get("lead")
    if not isinstance(lead, dict) or not lead:
        return None

    odds = _as_float(lead.get("odds"))
    prob = _as_float(lead.get("prob") or lead.get("probability"))
    ev = _as_float(lead.get("ev"))
    if odds is None or prob is None:
        return None

    # 由 legacy stage entry 讀 conviction；否則 fallback match top-level
    conviction = _as_float(stage_entry.get("conviction"))
    if conviction is None:
        conviction = _as_float(fx.raw.get("conviction"))

    now = now_utc or datetime.now(timezone.utc)

    return {
        "captured_at_utc": now.isoformat(),
        "fixture_id": fx.id,
        "kickoff_utc": fx.kickoff_utc.isoformat(),
        "kickoff_hkt": fx.kickoff_hkt.isoformat(),
        "league": fx.league,
        "home": fx.home,
        "away": fx.away,
        "source": fx.source,
        "conviction": conviction,
        "pick": stage_entry.get("pick") or fx.raw.get("pick"),
        "verdict": stage_entry.get("verdict"),
        "no_bet_reason": stage_entry.get("no_bet_reason") or fx.raw.get("no_bet_reason"),
        "lead_market": str(lead.get("market") or lead.get("market_label") or ""),
        "lead_label": str(lead.get("label") or lead.get("selection") or ""),
        "lead_odds": odds,
        "lead_prob": prob,
        "lead_ev": ev if ev is not None else (prob * odds - 1.0),
        "lead_conviction": conviction,
        # 保留 legacy 原始 ts（reference：呢場 legacy 幾時觸發過 stage）
        "legacy_stage_ts": stage_entry.get("ts"),
        "legacy_stage_snapshot_at": stage_entry.get("source_snapshot_at"),
        # 直接記錄埋 legacy stage 全部 keys 嘅子集，方便日後對照
        "legacy_source_status": stage_entry.get("source_status"),
        "legacy_can_bet": stage_entry.get("can_bet"),
        "legacy_odds_status": stage_entry.get("odds_status"),
    }


__all__ = ["build_prediction_fb"]
