"""Crown prediction pass: Crown/Titan quotes versus PinnAPI reference, fail closed."""
from __future__ import annotations

import math
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime
from typing import Any

from .common import HKT, iso_hkt
from .config import Settings
from .hkjc import event_from_match, fetch_matches, flatten_odds
from .ledger import completed_stages, recompute_stats, stage_for, sync_prediction
from .lines import parse_hkjc_total
from .matching import MATCHING_VERSION, Event, BridgeMatch, bridge_titan_to_pinnapi
from .pinnapi import PinnapiClient
from .period import in_current_period
from .state import load_ledger, load_predictions, merge_predictions, save_ledger
from .titan import TitanClient


def _event_from_titan(row: dict[str, Any]) -> Event:
    return Event(str(row["id"]), str(row["league"]), str(row["home"]), str(row["away"]), row["kickoff"], {"raw": row})


def _event_from_pinnapi(row: dict[str, Any]) -> Event:
    return Event(str(row["id"]), str(row["league"]), str(row["home"]), str(row["away"]),
                 datetime.fromtimestamp(float(row["kickoff"]), HKT), {"raw": row})


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


def _prediction(titan: dict[str, Any], bridge: BridgeMatch, h_match: dict[str, Any] | None,
                stage: str, config: Settings, titan_client: TitanClient, pinnapi_client: PinnapiClient,
                crown_snapshot: dict[str, Any] | None = None) -> dict[str, Any]:
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
        "candidates": [], "pick": None, "lead_view": None, "status": "DATA_MISSING",
        "verdict": "無法完整預測", "no_bet_reason": None, "book_odds": {"crown": [], "hkjc_chl": _hkjc_chl(h_match)},
        "outcome": None, "forecast": None, "probability": None, "likely_score": None,
        "prediction_source": None,
    }
    # Crown is the board master.  Fetch and preserve its quote before any
    # HKJC/PinnAPI bridge decision so Crown-only fixtures can still be shown,
    # while edge calculation remains fail-closed without PinnAPI.
    crown = list((crown_snapshot or {}).get("prices") or []) if crown_snapshot is not None else titan_client.crown_prices(event.id)
    base["book_odds"]["crown"] = crown
    base["source_snapshot_at"] = iso_hkt()
    if not crown:
        base["no_bet_reason"] = "皇冠公司盤口目前不可用；不顯示為皇冠有效賽事。"
        return base
    if not bridge.event:
        base["no_bet_reason"] = (
            "PinnAPI 無安全唯一同場對應；禁止計算 edge。"
            f" 映射診斷：{bridge.reason or 'unknown'}。"
        )
        return base
    try:
        pinnapi = pinnapi_client.lines(bridge.event.id)
    except Exception as exc:
        # Deliberately do not include provider responses, URLs, or credentials.
        base["no_bet_reason"] = f"PinnAPI lines unavailable ({type(exc).__name__}); fail closed。"
        return base
    prices = pinnapi["prices"]
    base.update(_wdl_prediction(prices))
    now = time.time()
    candidates, reasons = _candidates(crown, prices, config, now, bool(pinnapi["timestamp_inferred"]))
    corner_candidates: list[dict[str, Any]] = []
    corner_reasons: list[str] = []
    if h_match:
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
    base["candidates"] = candidates
    base["lead_view"] = candidates[0] if candidates else None
    if not candidates:
        base["no_bet_reason"] = "；".join(reasons + corner_reasons or ["Crown/PinnAPI 無可比較完整雙邊盤"])
        return base
    lead = candidates[0]
    base["conviction"] = lead["conviction"]
    base["status"] = "REFERENCE_READY"
    base["verdict"] = "傾向" if stage != "T-5" else "觀望"
    if stage == "T-5" and lead["conviction"] >= config.confidence_floor and lead["ev"] >= config.min_edge:
        stake = min(config.bankroll * 0.04, config.bankroll * lead["kelly_used"])
        if stake > 0:
            base["pick"] = lead | {"stake": round(stake, 2)}
            base["verdict"] = "模擬注"
            base["status"] = "SIMULATION_READY"
            base["no_bet_reason"] = None
            return base
    if stage == "T-5":
        base["no_bet_reason"] = f"未過 T-5 門檻（信念 {lead['conviction']}/{config.confidence_floor}，EV {lead['ev']:.2%}/{config.min_edge:.2%}）"
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
        return settle_due(config)
    titan_client, pinnapi_client = TitanClient(config), PinnapiClient(config)
    titan_rows, pinnapi_rows, hkjc_rows = titan_client.fixtures(), pinnapi_client.fixtures(), fetch_matches()
    h_events = [(event_from_match(row), row) for row in hkjc_rows]
    h_events = [(event, row) for event, row in h_events if event]
    p_events = [_event_from_pinnapi(row) for row in pinnapi_rows]
    ledger, predictions, emitted = load_ledger(config), [], []
    current_predictions = {
        str(row.get("match_id")): row
        for row in load_predictions(config)
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
        # Keep the complete Crown/Titan fixture board visible.  Unmatched
        # events remain DATA_MISSING and return before any edge calculation;
        # only the strict HKJC -> PinnAPI bridge can unlock pricing or a bet.
        h_row = next((row for candidate, row in h_events if bridge.hkjc.event and candidate.id == bridge.hkjc.event.id), None)
        prediction = _prediction(
            titan,
            bridge,
            h_row,
            stage,
            config,
            titan_client,
            pinnapi_client,
            crown_snapshot=crown_snapshot,
        )
        emitted += sync_prediction(ledger, prediction, config)
        # The dashboard card keeps all completed stages while the top-level
        # fields remain the latest stage snapshot.  This also survives a later
        # empty tick through merge_predictions().
        prediction["stages"] = list(ledger["watch"].get(event.id, {}).get("stages") or [])
        predictions.append(prediction)
    recompute_stats(ledger, config)
    ledger["log"].append({"ts": iso_hkt(), "kind": mode, "n_changes": len(emitted),
                          "changes": emitted or ["今次無模擬注動作"], "simulation_only": True})
    ledger["log"] = ledger["log"][-100:]
    save_ledger(config, ledger)
    retained = merge_predictions(config, predictions)
    return {"ok": True, "mode": mode, "predictions": len(predictions), "retained_predictions": len(retained),
            "simulations_created": len(emitted), "mapping": mapping,
            "pinnapi_fixtures": len(pinnapi_rows), "titan_fixtures": len(titan_rows), "hkjc_fixtures": len(h_events)}
