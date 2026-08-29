"""Read the matching immutable Crown snapshot for each V2 stage.

Opening observations are accepted only from the leakage-safe fixed-input
opening model.  T-30 and T-5 keep using Crown's existing prediction snapshots,
but each V2 stage must read its namesake source stage rather than whichever
snapshot happens to be newest.
"""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

from .fixtures import Fixture

OPENING_MODEL_VERSION = "crown-opening-fixed-v1"


def _source_snapshot(
    match_card: dict[str, Any], stage: str,
) -> dict[str, Any] | None:
    """Return the exact Crown snapshot for ``stage``.

    Never substitute a later snapshot for an earlier decision point.  This is
    the key leakage guard for the three-stage ledger.
    """
    stages = match_card.get("stages")
    if isinstance(stages, list):
        candidates: list[tuple[str, dict[str, Any]]] = []
        for row in stages:
            if not isinstance(row, dict) or str(row.get("stage") or "") != stage:
                continue
            predictions = row.get("market_predictions")
            if not isinstance(predictions, list) or not predictions:
                continue
            timestamp = str(row.get("ts") or row.get("source_snapshot_at") or "")
            candidates.append((timestamp, row))
        if candidates:
            candidates.sort(key=lambda item: item[0])
            return candidates[0][1]

    # Compatibility for fixtures whose dashboard card is already flattened.
    # It is safe only when the card explicitly identifies the requested stage.
    if str(match_card.get("stage") or "") != stage:
        return None
    predictions = match_card.get("market_predictions")
    if not isinstance(predictions, list) or not predictions:
        return None
    return match_card


def _extract_market_predictions(
    match_card: dict[str, Any], stage: str,
) -> tuple[list[dict[str, Any]], dict[str, Any]] | None:
    snapshot = _source_snapshot(match_card, stage)
    if snapshot is None:
        return None
    if stage == "首預" and snapshot.get("prediction_model") != OPENING_MODEL_VERSION:
        # A legacy first-look row is not a genuine fixed-input opening model.
        # Leave it pending so it cannot be relabelled or accumulated as one.
        return None
    rows = snapshot.get("market_predictions")
    if not isinstance(rows, list):
        return None
    clean = [row for row in rows if isinstance(row, dict)]
    if not clean:
        return None
    return clean, snapshot


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
    extracted = _extract_market_predictions(fx.raw, stage)
    if extracted is None:
        return None
    rows, snapshot = extracted
    lead = _pick_lead(rows)
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
        "conviction": _as_float(snapshot.get("conviction")),
        "pick": snapshot.get("pick"),
        "forecast": snapshot.get("forecast"),
        "crown_quote_status": snapshot.get("crown_quote_status"),
        "source_stage": stage,
        "source_predicted_at": (
            snapshot.get("ts") or snapshot.get("source_snapshot_at")
        ),
        "prediction_model": snapshot.get("prediction_model"),
        "input_policy": snapshot.get("input_policy"),
        "input_cutoff_at": snapshot.get("input_cutoff_at"),
        "opening_snapshot_hash": snapshot.get("opening_snapshot_hash"),
        "opening_model_status": snapshot.get("opening_model_status"),
        "team_history_as_of": snapshot.get("team_history_as_of"),
        "team_history_sample": snapshot.get("team_history_sample"),
        "late_inputs_used": snapshot.get("late_inputs_used"),
        **lead,
    }


__all__ = ["build_prediction"]
