"""Crown prediction pass with independent forecasting and strict PinnAPI betting gates."""
from __future__ import annotations

import math
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime
from typing import Any

from .common import HKT, iso_hkt, parse_time
from .config import Settings
from .hkjc import event_from_match, fetch_matches, flatten_odds
from .ledger import (
    completed_stages,
    market_entry_thresholds,
    recompute_stats,
    stage_for,
    sync_prediction,
)
from .lines import parse_hkjc_total
from .matching import MATCHING_VERSION, Event, BridgeMatch, bridge_titan_to_pinnapi
from .pinnapi import PinnapiClient
from .period import in_current_period
from .state import load_ledger, load_predictions, merge_predictions, save_ledger, state_lock
from .titan import TitanClient


def _event_from_titan(row: dict[str, Any]) -> Event:
    return Event(str(row["id"]), str(row["league"]), str(row["home"]), str(row["away"]), row["kickoff"], {"raw": row})


def _event_from_pinnapi(row: dict[str, Any]) -> Event:
    return Event(str(row["id"]), str(row["league"]), str(row["home"]), str(row["away"]),
                 datetime.fromtimestamp(float(row["kickoff"]), HKT), {"raw": row})


def _tick_rows_from_predictions(
    predictions: list[dict[str, Any]],
    ledger: dict[str, Any],
    now: datetime,
) -> list[dict[str, Any]]:
    """Select only locally known Crown cards whose timed stage is due."""
    rows: list[dict[str, Any]] = []
    seen: set[str] = set()
    for card in predictions:
        match_id = str(card.get("match_id") or "")
        kickoff = parse_time(card.get("kickoff_hkt") or card.get("kickoff"))
        if not match_id or match_id in seen or kickoff is None:
            continue
        minutes = (kickoff - now).total_seconds() / 60
        done = completed_stages(
            (ledger.get("watch") or {}).get(match_id, {}),
            MATCHING_VERSION,
        )
        if not stage_for(minutes, False, done):
            continue
        rows.append({
            "id": match_id,
            "league": card.get("league") or "",
            "home": card.get("home") or "",
            "away": card.get("away") or "",
            "kickoff": kickoff,
        })
        seen.add(match_id)
    rows.sort(key=lambda row: row["kickoff"])
    return rows


def _line_key(market: str, line: float | None) -> tuple[str, int | None]:
    return market, None if line is None else round(line * 4)


def _fresh(line: dict[str, Any], config: Settings, now: float) -> tuple[bool, str | None]:
    age = now - float(line.get("source_at") or 0)
    if age < -30 or age > config.source_max_age_seconds:
        return False, f"source_stale_{age:.0f}s"
    return True, None


def _pairs(prices: list[dict[str, Any]], market: str, line: float) -> dict[str, float] | None:
    wanted = [price for price in prices if _line_key(price["market"], price.get("line")) == _line_key(market, line)]
    keys = ("H", "A") if market == "HDC" else ("H", "L")
    result = {key: next((float(price["odds"]) for price in wanted if price["selection"] == key), None) for key in keys}
    return result if all(value and value > 1 for value in result.values()) else None


def _display_quarter_line(line: float, *, signed: bool = True) -> str:
    quarters = round(float(line) * 4)
    if quarters % 2 == 0:
        values = [quarters / 4]
    elif quarters > 0:
        values = [(quarters - 1) / 4, (quarters + 1) / 4]
    else:
        values = [(quarters + 1) / 4, (quarters - 1) / 4]

    def part(value: float) -> str:
        text = f"{value:g}"
        return f"+{text}" if signed and value > 0 else text

    return "/".join(part(value) for value in values)


def _crown_market_forecasts(
    crown_prices: list[dict[str, Any]],
    config: Settings,
    now: float,
    require_fresh: bool = True,
) -> tuple[list[dict[str, Any]], list[str]]:
    """Build forecast-only HDC/HIL views from Crown's complete current market.

    This is deliberately independent of PinnAPI. It supplies a direction for
    the prediction-learning ledger and may support a confidence-only T-5
    simulation when the current Crown quote is complete and fresh.
    """
    output: list[dict[str, Any]] = []
    reasons: list[str] = []
    for market, sides in (("HDC", ("H", "A")), ("HIL", ("H", "L"))):
        lines = sorted({
            float(row["line"])
            for row in crown_prices
            if row.get("market") == market and row.get("line") is not None
        })
        complete: list[tuple[float, dict[str, dict[str, Any]], dict[str, float]]] = []
        for line in lines:
            rows = {
                side: next((
                    row for row in crown_prices
                    if _line_key(str(row.get("market")), row.get("line")) == _line_key(market, line)
                    and row.get("selection") == side
                ), None)
                for side in sides
            }
            if not all(rows.values()):
                reasons.append(f"crown_incomplete_{market}_{line:g}")
                continue
            if require_fresh:
                stale = [
                    reason
                    for row in rows.values()
                    if not _fresh(row, config, now)[0]
                    for reason in [_fresh(row, config, now)[1]]
                ]
                if stale:
                    reasons.extend(f"crown_{reason}" for reason in stale if reason)
                    continue
            try:
                implied = {side: 1 / float(rows[side]["odds"]) for side in sides}
            except (TypeError, ValueError, ZeroDivisionError):
                reasons.append(f"crown_invalid_odds_{market}_{line:g}")
                continue
            if any(float(rows[side]["odds"]) <= 1 for side in sides):
                reasons.append(f"crown_invalid_odds_{market}_{line:g}")
                continue
            denominator = sum(implied.values())
            probabilities = {side: implied[side] / denominator for side in sides}
            complete.append((line, rows, probabilities))
        if not complete:
            reasons.append(f"no_complete_current_crown_{market}")
            continue
        # The most balanced complete line is the market's central/main line.
        line, rows, probabilities = min(
            complete,
            key=lambda item: (abs(item[2][sides[0]] - 0.5), abs(item[0]), item[0]),
        )
        side = max(sides, key=lambda item: probabilities[item])
        side_label = (
            ("主" if side == "H" else "客")
            if market == "HDC"
            else ("大" if side == "H" else "細")
        )
        market_label = "讓球" if market == "HDC" else "入球大細"
        probability = probabilities[side]
        selected_line = -line if market == "HDC" and side == "A" else line
        display_line = _display_quarter_line(selected_line, signed=market == "HDC")
        output.append({
            "market": market_label,
            "code": market,
            "condition": f"{line:g}",
            "line": line,
            "side": side,
            "label": f"皇冠{market_label} {side_label} {display_line}",
            "odds": round(float(rows[side]["odds"]), 3),
            "prob": round(probability, 5),
            "conviction": round(probability * 100, 1),
            "provider": "Crown",
            "source": "titan007-crown-id-3",
            "bookmaker": "Crown",
            "reference": "crown_full_market_no_vig",
            "forecast_only": True,
        })
    return output, sorted(set(reasons))


def _apply_confidence_only_pick(
    base: dict[str, Any],
    forecasts: list[dict[str, Any]],
    stage: str,
    config: Settings,
) -> bool:
    """Fail closed when no independent same-line market reference exists."""
    if stage == "T-5":
        base["no_bet_reason"] = (
            "PinnAPI 無安全同場基準，confidence-only 入倉已停用；"
            "保留預測及學習紀錄，但不建立模擬注。"
        )
    return False


def _candidates(crown_prices: list[dict[str, Any]], pinnapi_prices: list[dict[str, Any]], config: Settings,
                now: float, inferred_timestamp: bool) -> tuple[list[dict[str, Any]], list[str]]:
    reasons: list[str] = []
    if inferred_timestamp and not config.allow_inferred_pinnapi_timestamp:
        return [], ["pinnapi_source_timestamp_missing"]
    candidates = []
    for crown in crown_prices:
        market, line, side = crown["market"], float(crown["line"]), crown["selection"]
        good, reason = _fresh(crown, config, now)
        if not good:
            reasons.append(f"crown_{reason}")
            continue
        reference = _pairs(pinnapi_prices, market, line)
        if not reference:
            reasons.append(f"no_exact_pinnapi_{market}_{line:g}")
            continue
        match_keys = ("H", "A") if market == "HDC" else ("H", "L")
        implied = {key: 1 / float(reference[key]) for key in match_keys}
        den = sum(implied.values())
        probability = implied[side] / den
        odds = float(crown["odds"])
        ev = probability * odds - 1
        kelly = max(0.0, (probability * odds - 1) / (odds - 1))
        # Keep 50 as genuinely neutral instead of flattening every negative
        # edge to the same score.  Positive-edge thresholds are unchanged.
        conviction = max(0.0, min(100.0, 50.0 + ev * 500.0))
        candidates.append({
            "market": market, "code": market, "condition": f"{line:g}", "line": line, "side": side,
            "label": f"{market} {side} {line:g}", "odds": round(odds, 3), "prob": round(probability, 5),
            "ev": round(ev, 5), "kelly_raw": round(kelly, 5), "kelly_used": round(kelly / 3, 5),
            "conviction": round(conviction, 1), "reference": "pinnapi_exact_full_match",
        })
    return sorted(candidates, key=lambda row: (row["ev"], row["conviction"]), reverse=True), sorted(set(reasons))


def _hkjc_chl(match: dict[str, Any] | None) -> list[dict[str, Any]]:
    """Return HKJC CHL rows after the strict bridge, never as Crown prices."""
    if not match:
        return []
    return [{
        "market": "CHL",
        "provider": "HKJC",
        "source": "hkjc_chl",
        "bookmaker": "HKJC",
        **row,
    } for row in flatten_odds(match).get("CHL", [])]


def _hkjc_chl_candidates(
    hkjc_lines: list[dict[str, Any]],
    pinnapi_corner_prices: list[dict[str, Any]],
    config: Settings,
    now: float,
    inferred_timestamp: bool,
) -> tuple[list[dict[str, Any]], list[str]]:
    """Compare HKJC full-match CHL with PinnAPI's exact same corner line.

    Titan's Crown feed has no verified corner quote.  This intentionally builds
    an independent HKJC-priced candidate rather than placing CHL in the Crown
    quote list or assigning it Crown bookmaker provenance.
    """
    if inferred_timestamp and not config.allow_inferred_pinnapi_timestamp:
        return [], ["pinnapi_corner_source_timestamp_missing"]
    candidates: list[dict[str, Any]] = []
    reasons: list[str] = []
    for hkjc in hkjc_lines:
        line = parse_hkjc_total(hkjc.get("condition"))
        if line is None:
            reasons.append(f"invalid_hkjc_chl_line_{hkjc.get('condition')}")
            continue
        # The HKJC query is a current response snapshot; it exposes no quote
        # timestamp, so the response-observed time is retained explicitly.
        quote = dict(hkjc, source_at=float(hkjc.get("source_at") or now))
        good, reason = _fresh(quote, config, now)
        if not good:
            reasons.append(f"hkjc_{reason}")
            continue
        reference_rows = [
            row for row in pinnapi_corner_prices
            if _line_key(str(row.get("market")), row.get("line")) == _line_key("CHL", line)
        ]
        if not reference_rows:
            reasons.append(f"no_exact_pinnapi_CHL_{line:g}")
            continue
        if any(not _fresh(row, config, now)[0] for row in reference_rows):
            reasons.append(f"pinnapi_corner_source_stale_CHL_{line:g}")
            continue
        reference = _pairs(pinnapi_corner_prices, "CHL", line)
        if not reference:
            reasons.append(f"no_complete_pinnapi_CHL_{line:g}")
            continue
        implied = {key: 1 / float(reference[key]) for key in ("H", "L")}
        denominator = sum(implied.values())
        for side in ("H", "L"):
            try:
                odds = float((hkjc.get("odds") or {}).get(side))
            except (TypeError, ValueError):
                continue
            if odds <= 1:
                continue
            probability = implied[side] / denominator
            ev = probability * odds - 1
            kelly = max(0.0, (probability * odds - 1) / (odds - 1))
            conviction = max(0.0, min(100.0, 50.0 + ev * 500.0))
            candidates.append({
                "market": "HKJC角球大細",
                "code": "CHL",
                # Persist the canonical quarter line for Asian settlement;
                # ``label`` retains HKJC's original split-line presentation.
                "condition": f"{line:g}",
                "line": line,
                "side": side,
                "label": f"HKJC角球大細 {'大' if side == 'H' else '細'} {hkjc.get('condition')}",
                "odds": round(odds, 3),
                "prob": round(probability, 5),
                "ev": round(ev, 5),
                "kelly_raw": round(kelly, 5),
                "kelly_used": round(kelly / 3, 5),
                "conviction": round(conviction, 1),
                "provider": "HKJC",
                "source": "hkjc_chl",
                "bookmaker": "HKJC",
                "reference": "pinnapi_corner_exact_full_match",
                "reference_provider": "PinnAPI",
            })
    return sorted(candidates, key=lambda row: (row["ev"], row["conviction"]), reverse=True), sorted(set(reasons))


def _wdl_prediction(prices: list[dict[str, Any]]) -> dict[str, Any]:
    """Return a complete no-vig 1X2 view, or no prediction at all."""
    odds = {
        selection: next((
            float(price["odds"]) for price in prices
            if price.get("market") == "1X2" and price.get("selection") == selection
        ), None)
        for selection in ("H", "D", "A")
    }
    if not all(value and value > 1 for value in odds.values()):
        return {
            "outcome": None, "forecast": None, "probability": None,
            "likely_score": None, "prediction_source": None,
        }
    implied = {selection: 1 / float(value) for selection, value in odds.items()}
    total = sum(implied.values())
    probabilities = {selection: implied[selection] / total for selection in ("H", "D", "A")}
    pick = max(probabilities, key=probabilities.get)
    labels = {"H": "主勝", "D": "和局", "A": "客勝"}
    return {
        "outcome": {
            "home": round(probabilities["H"], 6),
            "draw": round(probabilities["D"], 6),
            "away": round(probabilities["A"], 6),
        },
        "forecast": labels[pick],
        "probability": round(probabilities[pick], 6),
        "likely_score": None,
        "prediction_source": "pinnapi_1x2_no_vig",
    }


def _fixture_baseline_prediction(
    forecasts: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    """Return a low-confidence, always-available WDL learning prediction.

    This fallback exists so every Crown fixture produces a scoreable prediction
    record even when one or more quote/reference providers are incomplete.  It
    is deliberately excluded from EV, Kelly and simulated-bet candidates.
    Where Crown has an HDC direction, that direction nudges the league-neutral
    prior; otherwise a conservative home-advantage prior is used.
    """
    handicap = next(
        (row for row in (forecasts or []) if row.get("code") == "HDC"),
        None,
    )
    side = str((handicap or {}).get("side") or "")
    if side == "H":
        probabilities = {"home": 0.44, "draw": 0.29, "away": 0.27}
        forecast, likely_score = "主勝", "1-0"
        source = "crown_hdc_direction_low_confidence_v1"
    elif side == "A":
        probabilities = {"home": 0.28, "draw": 0.29, "away": 0.43}
        forecast, likely_score = "客勝", "0-1"
        source = "crown_hdc_direction_low_confidence_v1"
    else:
        probabilities = {"home": 0.40, "draw": 0.30, "away": 0.30}
        forecast, likely_score = "主勝", "1-0"
        source = "fixture_prior_low_confidence_v1"
    return {
        "probabilities": probabilities,
        "forecast": forecast,
        "probability": max(probabilities.values()),
        "likely_score": likely_score,
        "prediction_source": source,
        "baseline_low_confidence": True,
    }


def _prediction(titan: dict[str, Any], bridge: BridgeMatch, h_match: dict[str, Any] | None,
                stage: str, config: Settings, titan_client: TitanClient, pinnapi_client: PinnapiClient,
                crown_snapshot: dict[str, Any] | None = None,
                previous_crown_prices: list[dict[str, Any]] | None = None,
                entry_policies: dict[str, dict[str, Any]] | None = None) -> dict[str, Any]:
    event = _event_from_titan(titan)
    minutes = round((event.kickoff - datetime.now(HKT)).total_seconds() / 60, 1)
    base = {
        "schema_version": "crown-prediction-v2", "matching_version": MATCHING_VERSION,
        "generated_at": iso_hkt(), "match_id": event.id,
        "league": event.league, "home": event.home, "away": event.away, "kickoff_hkt": iso_hkt(event.kickoff),
        "mins_to_ko": minutes, "stage": stage, "titan_match_id": event.id,
        "pinnapi_event_id": bridge.event.id if bridge.event else None,
        "hkjc_match_id": str((h_match or {}).get("id") or (h_match or {}).get("frontEndId") or "") or None,
        "market_sources": {
            "HDC": "titan007-crown-id-3",
            "HIL": "titan007-crown-id-3",
            "CHL": "HKJC CHL odds vs PinnAPI CHL exact full-match reference; not Crown odds",
        },
        "mapping": {
            "path": bridge.path, "reason": bridge.reason,
            "titan_to_hkjc_score": round(bridge.hkjc.score, 3),
            "titan_to_hkjc_reason": bridge.hkjc.reason,
            "hkjc_to_pinnapi_score": round(bridge.pinnapi.score, 3),
            "hkjc_to_pinnapi_reason": bridge.pinnapi.reason,
            "orientation": "reversed_identity_only" if bridge.reversed else "direct_only",
        },
        "execution": {"enabled": True, "mode": "simulation", "real_betting_enabled": False,
                      "reason": "Only T-5 can create an idempotent simulated bet; no order client exists."},
        "candidates": [], "forecast_candidates": [], "pick": None, "lead_view": None, "status": "DATA_MISSING",
        "verdict": "無法完整預測", "no_bet_reason": None, "book_odds": {"crown": [], "hkjc_chl": _hkjc_chl(h_match)},
        "outcome": None, "forecast": None, "probability": None, "likely_score": None,
        "prediction_source": None, "sharp_reference_available": False,
        "edge_reference_status": "not_checked", "edge_reference_note": None,
    }
    # Crown is the board master.  Fetch and preserve its quote before any
    # HKJC/PinnAPI bridge decision so Crown-only fixtures can still be shown,
    # while edge calculation remains fail-closed without PinnAPI.
    crown = list((crown_snapshot or {}).get("prices") or []) if crown_snapshot is not None else titan_client.crown_prices(event.id)
    used_cached_crown = False
    if not crown and previous_crown_prices:
        # A current empty/error response must not erase an earlier valid
        # pre-match market view.  Reuse it for forecasting and learning only;
        # stale/current-source uncertainty can never unlock edge or a bet.
        crown = list(previous_crown_prices)
        used_cached_crown = True
    base["book_odds"]["crown"] = crown
    base["source_snapshot_at"] = iso_hkt()
    if not crown:
        base.update(_fixture_baseline_prediction())
        base["status"] = "PREDICTION_READY"
        base["verdict"] = "已預測"
        base["conviction"] = round(float(base["probability"]) * 100, 1)
        base["edge_reference_status"] = "unavailable"
        base["edge_reference_note"] = "皇冠即時盤及 PinnAPI EV 參考暫不可用；只保留低信念純預測。"
        base["no_bet_reason"] = None
        return base
    now = time.time()
    forecasts, forecast_reasons = _crown_market_forecasts(
        crown, config, now, require_fresh=not used_cached_crown
    )
    base["forecast_candidates"] = forecasts
    base["crown_quote_cached_forecast_only"] = used_cached_crown
    if used_cached_crown:
        source_times = [
            float(row.get("source_at") or 0)
            for row in crown
            if float(row.get("source_at") or 0) > 0
        ]
        base["crown_cached_source_at"] = min(source_times) if source_times else None
    if forecasts:
        base["status"] = "PREDICTION_READY"
        base["verdict"] = "已預測"
        base["conviction"] = max(float(row["conviction"]) for row in forecasts)
        base["prediction_source"] = "crown_full_market_no_vig"
    base.update(_fixture_baseline_prediction(forecasts))
    base["status"] = "PREDICTION_READY"
    base["verdict"] = "已預測"
    base["conviction"] = max(
        float(base.get("conviction") or 0),
        round(float(base["probability"]) * 100, 1),
    )
    if used_cached_crown:
        base["no_bet_reason"] = (
            "皇冠即時盤目前不可用；已沿用最後一次有效皇冠盤作純預測及學習，"
            "禁止計算 edge 及投注。"
        )
        return base
    if not bridge.event:
        # PinnAPI is an optional EV reference, never a prerequisite for Crown
        # forecasting. Keep mapping diagnostics out of the main prediction /
        # no-bet verdict so the UI cannot mislabel a valid Crown forecast as
        # "unable to predict".
        base["edge_reference_status"] = "unavailable"
        base["edge_reference_note"] = (
            "PinnAPI 暫無安全唯一同場參考；不計算 EV。"
            f" 映射診斷：{bridge.reason or 'unknown'}。"
        )
        if forecasts:
            base["no_bet_reason"] = None
        else:
            base["edge_reference_note"] = (
                "皇冠盤未形成完整雙邊市場，已保留低信念賽果 baseline；"
                "PinnAPI EV 參考暫不可用。"
            )
            base["no_bet_reason"] = None
        _apply_confidence_only_pick(base, forecasts, stage, config)
        return base
    try:
        pinnapi = pinnapi_client.lines(bridge.event.id)
    except Exception as exc:
        # Deliberately do not include provider responses, URLs, or credentials.
        base["edge_reference_status"] = "unavailable"
        base["edge_reference_note"] = (
            f"PinnAPI 參考暫時不可用 ({type(exc).__name__})；不計算 EV。"
        )
        if forecasts:
            base["no_bet_reason"] = None
        else:
            base["edge_reference_note"] = (
                "皇冠盤未形成完整雙邊市場，已保留低信念賽果 baseline；"
                f"PinnAPI 參考暫時不可用 ({type(exc).__name__})。"
            )
            base["no_bet_reason"] = None
        _apply_confidence_only_pick(base, forecasts, stage, config)
        return base
    prices = pinnapi["prices"]
    base.update(_wdl_prediction(prices))
    base["sharp_reference_available"] = True
    base["edge_reference_status"] = "available"
    base["edge_reference_note"] = None
    candidates, reasons = _candidates(crown, prices, config, now, bool(pinnapi["timestamp_inferred"]))
    corner_candidates: list[dict[str, Any]] = []
    corner_reasons: list[str] = []
    if base["book_odds"]["hkjc_chl"]:
        try:
            corners = pinnapi_client.corner_lines(bridge.event.id)
            base["pinnapi_corner_event_id"] = corners.get("corner_event_id")
            base["pinnapi_corner_source_at"] = corners.get("source_at")
            base["pinnapi_corner_timestamp_inferred"] = corners.get("timestamp_inferred")
            corner_candidates, corner_reasons = _hkjc_chl_candidates(
                base["book_odds"]["hkjc_chl"],
                list(corners.get("prices") or []),
                config,
                now,
                bool(corners.get("timestamp_inferred")),
            )
        except Exception as exc:
            # CHL is a separate HKJC candidate.  A special-market outage must
            # fail it closed without changing Crown HDC/HIL decisions.
            corner_reasons = [f"pinnapi_corner_lines_unavailable_{type(exc).__name__}"]
    if corner_reasons:
        # Keep CHL's independently fail-closed state visible even where a
        # normal Crown HDC/HIL candidate remains available.
        base["corner_no_bet_reason"] = "；".join(corner_reasons)
    base["pinnapi_source_at"] = pinnapi["source_at"]
    base["pinnapi_timestamp_inferred"] = pinnapi["timestamp_inferred"]
    base["pinnapi_timestamp_basis"] = pinnapi.get("timestamp_basis")
    candidates = sorted(candidates + corner_candidates,
                        key=lambda row: (row["ev"], row["conviction"]), reverse=True)
    policies = entry_policies or {
        code: {
            "code": code,
            "n_settled": 0,
            "min_samples": 30,
            "min_edge": config.min_edge,
            "confidence_floor": config.confidence_floor,
            "reason": "insufficient_market_sample",
        }
        for code in ("HDC", "HIL", "CHL")
    }
    for candidate in candidates:
        candidate["entry_policy"] = policies.get(
            str(candidate.get("code") or ""),
            {
                "min_edge": config.min_edge,
                "confidence_floor": config.confidence_floor,
                "reason": "configured_default",
            },
        )
    base["entry_policies"] = policies
    base["candidates"] = candidates
    if candidates:
        # Exact PinnAPI same-line views supersede Crown-only forecasts for
        # learning quality.  They still do not create a bet before T-5.
        base["forecast_candidates"] = candidates
    base["lead_view"] = candidates[0] if candidates else None
    if not candidates:
        prefix = "已保留皇冠全盤預測；" if forecasts else ""
        base["no_bet_reason"] = prefix + "；".join(
            reasons + corner_reasons or ["Crown/PinnAPI 無可比較完整雙邊盤"]
        )
        _apply_confidence_only_pick(base, forecasts, stage, config)
        return base
    eligible = [
        candidate for candidate in candidates
        if float(candidate["conviction"]) >= float(candidate["entry_policy"]["confidence_floor"])
        and float(candidate["ev"]) >= float(candidate["entry_policy"]["min_edge"])
    ]
    lead = eligible[0] if eligible else candidates[0]
    base["conviction"] = lead["conviction"]
    base["lead_view"] = lead
    base["status"] = "REFERENCE_READY"
    base["verdict"] = "傾向" if stage != "T-5" else "觀望"
    if stage == "T-5" and eligible:
        stake = min(config.bankroll * 0.04, config.bankroll * lead["kelly_used"])
        if stake > 0:
            base["pick"] = lead | {"stake": round(stake, 2)}
            base["verdict"] = "模擬注"
            base["status"] = "SIMULATION_READY"
            base["no_bet_reason"] = None
            return base
    if stage == "T-5":
        policy = lead["entry_policy"]
        base["no_bet_reason"] = (
            f"未過 {lead['code']} 動態門檻（信念 {lead['conviction']}/"
            f"{policy['confidence_floor']:g}，EV {lead['ev']:.2%}/"
            f"{policy['min_edge']:.2%}；樣本 {policy.get('n_settled', 0)}）"
        )
    else:
        base["no_bet_reason"] = f"{stage} 僅記錄資訊，不建立模擬注。"
    return base


def _refresh_crown_quote(
    previous: dict[str, Any],
    titan: dict[str, Any],
    titan_client: TitanClient,
    crown_snapshot: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Refresh board identity and Crown prices without replaying a prediction stage.

    The 30-minute board pass is intentionally separate from 首預/T-30/T-5.
    Existing stage decisions, candidates and simulated picks remain historical
    snapshots.  A successfully fetched market replaces its old quote, including
    a confirmed empty market.  A failed market fetch retains its prior quote and
    is explicitly marked stale, preventing transient source failures from
    deleting otherwise valid Crown fixtures from the board.
    """
    event = _event_from_titan(titan)
    refreshed = dict(previous)
    # Internal merge instruction: a concurrent sweep may finish after a newer
    # T-30/T-5 tick.  It may refresh quote fields, never roll the card's stage
    # or decision backwards.
    refreshed["_quote_refresh_only"] = True
    refreshed.update({
        "league": event.league,
        "home": event.home,
        "away": event.away,
        "kickoff_hkt": iso_hkt(event.kickoff),
        "mins_to_ko": round((event.kickoff - datetime.now(HKT)).total_seconds() / 60, 1),
        "generated_at": iso_hkt(),
        "source_snapshot_at": iso_hkt(),
        "crown_quote_attempted_at": iso_hkt(),
    })
    book_odds = dict(refreshed.get("book_odds") or {})
    snapshot = crown_snapshot or titan_client.crown_price_snapshot(event.id)
    incoming = list(snapshot.get("prices") or [])
    prior = list(book_odds.get("crown") or [])
    merged_prices: list[dict[str, Any]] = []
    stale_markets: list[str] = []
    for market, status_key in (("HDC", "asian_ok"), ("HIL", "total_ok")):
        if snapshot.get(status_key):
            merged_prices.extend(row for row in incoming if row.get("market") == market)
        else:
            merged_prices.extend(row for row in prior if row.get("market") == market)
            stale_markets.append(market)
    book_odds["crown"] = merged_prices
    refreshed["book_odds"] = book_odds
    refreshed["crown_quote_stale_markets"] = stale_markets
    if not stale_markets:
        refreshed["crown_quote_refreshed_at"] = iso_hkt()
    if not book_odds["crown"]:
        refreshed["no_bet_reason"] = "皇冠公司盤口目前不可用；不顯示為皇冠有效賽事。"
    return refreshed


def run(mode: str, config: Settings) -> dict[str, Any]:
    """Run a remote pass only when the explicit validation gate and PinnAPI key exist."""
    if mode not in {"tick", "sweep", "settle"}:
        raise ValueError("mode must be tick, sweep, or settle")
    if not config.enabled:
        return {"ok": False, "reason": "CROWN_ENABLED=0; no network call was made"}
    if not config.pinnapi_configured:
        return {"ok": False, "reason": "PinnAPI credentials are not configured; no network call was made"}
    if mode == "settle":
        from .settle import settle_due
        with state_lock(config):
            return settle_due(config)
    ledger = load_ledger(config)
    entry_policies = {
        code: market_entry_thresholds(ledger, code, config)
        for code in ("HDC", "HIL", "CHL")
    }
    existing_predictions = load_predictions(config)
    if mode == "tick":
        titan_rows = _tick_rows_from_predictions(
            existing_predictions, ledger, datetime.now(HKT)
        )
        if not titan_rows:
            return {
                "ok": True, "mode": mode, "fast_noop": True,
                "predictions": 0, "retained_predictions": len(existing_predictions),
                "simulations_created": 0,
            }
    titan_client, pinnapi_client = TitanClient(config), PinnapiClient(config)
    if mode == "sweep":
        titan_rows = titan_client.fixtures()
    pinnapi_rows, hkjc_rows = pinnapi_client.fixtures(), fetch_matches()
    h_events = [(event_from_match(row), row) for row in hkjc_rows]
    h_events = [(event, row) for event, row in h_events if event]
    p_events = [_event_from_pinnapi(row) for row in pinnapi_rows]
    predictions = []
    stage_predictions: list[dict[str, Any]] = []
    pending_predictions: list[
        tuple[
            dict[str, Any], BridgeMatch, dict[str, Any] | None, str,
            dict[str, Any] | None, list[dict[str, Any]],
        ]
    ] = []
    current_predictions = {
        str(row.get("match_id")): row
        for row in existing_predictions
        if row.get("match_id")
    }
    refresh_quotes: dict[str, dict[str, Any]] = {}
    if mode == "sweep":
        refresh_rows = []
        now = datetime.now(HKT)
        for titan in titan_rows:
            event = _event_from_titan(titan)
            if not in_current_period(event.kickoff) or event.kickoff <= now:
                continue
            refresh_rows.append(titan)
        if refresh_rows:
            # Titan's two quote pages per fixture are independent network
            # reads.  A small bounded pool prevents a 100+ match refresh from
            # blocking the two-minute T-30/T-5 worker for most of the window.
            with ThreadPoolExecutor(max_workers=min(6, len(refresh_rows))) as pool:
                futures = {
                    pool.submit(titan_client.crown_price_snapshot, str(row["id"])): str(row["id"])
                    for row in refresh_rows
                }
                for future in as_completed(futures):
                    match_id = futures[future]
                    try:
                        refresh_quotes[match_id] = future.result()
                    except Exception:
                        refresh_quotes[match_id] = {
                            "prices": [], "asian_ok": False, "total_ok": False,
                        }
    mapping = {
        "titan_due": 0, "titan_to_hkjc_mapped": 0, "hkjc_to_pinnapi_mapped": 0,
        "direct_same_script_mapped": 0, "unmapped_titan_to_hkjc": 0, "unmapped_hkjc_to_pinnapi": 0,
        "reversed_identity_mapped": 0,
        "reasons": {},
    }
    # Provider order is not a scheduling guarantee.  Nearest kickoff first
    # prevents a large T-30 batch from starving T-5.
    titan_rows.sort(key=lambda row: row["kickoff"])
    for titan in titan_rows:
        event = _event_from_titan(titan)
        if not in_current_period(event.kickoff):
            continue
        watch = ledger["watch"].get(event.id, {})
        done = completed_stages(watch, MATCHING_VERSION)
        minutes = (event.kickoff - datetime.now(HKT)).total_seconds() / 60
        # Started fixtures remain visible until the 12:00 board rollover, but
        # no pass spends provider calls rebuilding prices after kickoff.
        if minutes <= 0:
            continue
        previous = current_predictions.get(event.id)
        if mode == "sweep" and previous is not None and "首預" in done:
            predictions.append(
                _refresh_crown_quote(
                    previous,
                    titan,
                    titan_client,
                    refresh_quotes.get(event.id),
                )
            )
            continue
        crown_snapshot = refresh_quotes.get(event.id) if mode == "sweep" else None
        if (
            crown_snapshot is not None
            and crown_snapshot.get("asian_ok")
            and crown_snapshot.get("total_ok")
            and not crown_snapshot.get("prices")
        ):
            # The fixture exists on Titan's complete schedule but Crown has
            # neither supported market. Keep it off the Crown board and avoid
            # unnecessary HKJC/PinnAPI matching until the next 30-minute sweep.
            continue
        stage = stage_for(minutes, mode == "sweep", done)
        if not stage:
            continue
        mapping["titan_due"] += 1
        bridge = bridge_titan_to_pinnapi(event, [item[0] for item in h_events], p_events)
        if bridge.hkjc.event:
            mapping["titan_to_hkjc_mapped"] += 1
            if bridge.reversed:
                mapping["reversed_identity_mapped"] += 1
        elif bridge.path != "direct_same_script":
            mapping["unmapped_titan_to_hkjc"] += 1
        if bridge.event:
            if bridge.path == "hkjc_bilingual_bridge":
                mapping["hkjc_to_pinnapi_mapped"] += 1
            elif bridge.path == "direct_same_script":
                mapping["direct_same_script_mapped"] += 1
        elif bridge.hkjc.event:
            mapping["unmapped_hkjc_to_pinnapi"] += 1
        if bridge.reason:
            mapping["reasons"][bridge.reason] = mapping["reasons"].get(bridge.reason, 0) + 1
        # Keep the complete Crown/Titan fixture board visible.  Crown's own
        # complete market can always create a forecast-learning snapshot;
        # only the strict HKJC -> PinnAPI bridge can unlock edge or a bet.
        h_row = next((row for candidate, row in h_events if bridge.hkjc.event and candidate.id == bridge.hkjc.event.id), None)
        previous_crown_prices = list(
            ((previous or {}).get("book_odds") or {}).get("crown") or []
        )
        pending_predictions.append((
            titan, bridge, h_row, stage, crown_snapshot, previous_crown_prices
        ))

    # A same-kickoff batch can contain dozens of fixtures.  Each prediction
    # performs independent Crown and PinnAPI reads, so serial execution could
    # consume the entire ten-minute T-5 window.  Keep concurrency bounded, but
    # finish T-5 rows before T-30/first-look rows and commit only after every
    # result is complete.
    pending_predictions.sort(
        key=lambda job: (
            0 if job[3] == "T-5" else 1 if job[3] == "T-30" else 2,
            job[0]["kickoff"],
        )
    )
    if pending_predictions:
        with ThreadPoolExecutor(max_workers=min(10, len(pending_predictions))) as pool:
            futures = {
                pool.submit(
                    _prediction,
                    titan,
                    bridge,
                    h_row,
                    stage,
                    config,
                    titan_client,
                    pinnapi_client,
                    crown_snapshot,
                    previous_crown_prices,
                    entry_policies,
                ): str(titan["id"])
                for (
                    titan, bridge, h_row, stage, crown_snapshot,
                    previous_crown_prices,
                ) in pending_predictions
            }
            completed: dict[str, dict[str, Any]] = {}
            for future in as_completed(futures):
                completed[futures[future]] = future.result()
        for titan, _bridge, _h_row, _stage, _snapshot, _previous in pending_predictions:
            prediction = completed[str(titan["id"])]
            stage_predictions.append(prediction)
            # The dashboard card keeps all completed stages while the top-level
            # fields remain the latest stage snapshot.  This also survives a
            # later empty tick through merge_predictions().
            prediction["stages"] = list(
                ledger["watch"].get(str(titan["id"]), {}).get("stages") or []
            )
            predictions.append(prediction)
    with state_lock(config):
        # Reload the latest state because another provider pass may have
        # committed while this one was fetching quotes.
        ledger = load_ledger(config)
        emitted: list[str] = []
        for prediction in stage_predictions:
            kickoff = datetime.fromisoformat(str(prediction["kickoff_hkt"]))
            if kickoff.tzinfo is None:
                kickoff = kickoff.replace(tzinfo=HKT)
            if kickoff <= datetime.now(HKT):
                # A quote request admitted just before kickoff may return after
                # the match starts.  It must never become a T-5 bet.
                continue
            emitted += sync_prediction(ledger, prediction, config)
            prediction["stages"] = list(
                ledger["watch"].get(str(prediction["match_id"]), {}).get("stages") or []
            )
        recompute_stats(ledger, config)
        ledger["log"].append({"ts": iso_hkt(), "kind": mode, "n_changes": len(emitted),
                              "changes": emitted or ["今次無模擬注動作"], "simulation_only": True})
        ledger["log"] = ledger["log"][-100:]
        save_ledger(config, ledger)
        retained = merge_predictions(config, predictions)
    return {"ok": True, "mode": mode, "predictions": len(predictions), "retained_predictions": len(retained),
            "simulations_created": len(emitted), "mapping": mapping,
            "pinnapi_fixtures": len(pinnapi_rows), "titan_fixtures": len(titan_rows), "hkjc_fixtures": len(h_events)}
