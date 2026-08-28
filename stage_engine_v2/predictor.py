"""Reuse 舊 crown data.json 已算好嘅預測。

v2 唔重做預測模型。舊系統 predict 得到，只係唔 fire 時點。
v2 只做「攞最新 market_predictions → 揀 lead → 寫入 v2 ledger」。

同 stage 唔同時間讀取 crown data.json 都會攞到當時 authoritative snapshot；
v2 記錄嘅 lead 反映觸發時刻嘅市場觀察。
"""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

from .fixtures import Fixture


def _extract_market_predictions(match_card: dict[str, Any]) -> list[dict[str, Any]]:
    """從 crown match card 攞最新 stage 嘅 market_predictions。

    優先順序：直接 top-level market_predictions（若 dashboard 已 flatten）
    否則從 stages list 取最新 (ts 最大者)。
    """
    top = match_card.get("market_predictions")
    if isinstance(top, list) and top:
        return [row for row in top if isinstance(row, dict)]

    stages = match_card.get("stages")
    if not isinstance(stages, list):
        return []
    dated = []
    for stage in stages:
        if not isinstance(stage, dict):
            continue
        rows = stage.get("market_predictions")
        if not isinstance(rows, list) or not rows:
            continue
        ts = str(stage.get("ts") or stage.get("source_snapshot_at") or "")
        dated.append((ts, rows))
    if not dated:
        return []
    dated.sort(key=lambda item: item[0])
    return [row for row in dated[-1][1] if isinstance(row, dict)]


def _as_float(value: Any) -> float | None:
    try:
        if value is None or value == "":
            return None
        return float(value)
    except (TypeError, ValueError):
        return None


def _pick_lead(rows: list[dict[str, Any]]) -> dict[str, Any] | None:
    """喺 market_predictions 揀 lead——最高 EV 且 conviction ≥ 門檻嘅一項。

    無明確 conviction 就用 probability * odds - 1 = EV 排序。
    """
    scored: list[tuple[float, dict[str, Any]]] = []
    for row in rows:
        prob = _as_float(row.get("probability") or row.get("prob"))
        odds = _as_float(row.get("odds") or row.get("decimal_odds"))
        if prob is None or odds is None or odds <= 1.0 or not (0 < prob <= 1):
            continue
        ev = prob * odds - 1.0
        scored.append((ev, row))
    if not scored:
        return None
    scored.sort(key=lambda item: item[0], reverse=True)
    ev, best = scored[0]
    prob = _as_float(best.get("probability") or best.get("prob")) or 0.0
    odds = _as_float(best.get("odds") or best.get("decimal_odds")) or 0.0
    conviction = _as_float(best.get("conviction"))
    return {
        "lead_market": str(best.get("market") or best.get("market_label") or ""),
        "lead_label": str(best.get("label") or best.get("selection") or ""),
        "lead_odds": odds,
        "lead_prob": prob,
        "lead_ev": ev,
        "lead_conviction": conviction,
        "market_predictions_count": len(rows),
    }


def build_prediction(
    fx: Fixture,
    stage: str,
    *,
    now_utc: datetime | None = None,
) -> dict[str, Any] | None:
    """組裝 v2 stage prediction record。

    Returns None 表示暫未有可用嘅預測（例如 crown 未算好），
    tick 會下次再試——因為 v2 ledger append-only，唔會鎖死。
    """
    rows = _extract_market_predictions(fx.raw)
    lead = _pick_lead(rows) if rows else None
    if lead is None:
        return None
    now = now_utc or datetime.now(timezone.utc)
    # 追加 crown 頂層 conviction / pick / forecast（如果有）
    return {
        "captured_at_utc": now.isoformat(),
        "fixture_id": fx.id,
        "kickoff_utc": fx.kickoff_utc.isoformat(),
        "kickoff_hkt": fx.kickoff_hkt.isoformat(),
        "league": fx.league,
        "home": fx.home,
        "away": fx.away,
        "source": fx.source,
        "conviction": _as_float(fx.raw.get("conviction")),
        "pick": fx.raw.get("pick"),
        "forecast": fx.raw.get("forecast"),
        "crown_quote_status": fx.raw.get("crown_quote_status"),
        **lead,
    }


__all__ = ["build_prediction"]
