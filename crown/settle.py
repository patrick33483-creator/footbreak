"""Safe Crown simulation settlement: PinnAPI live cache first, then identified fallback only."""
from __future__ import annotations

from datetime import datetime
from typing import Any

from .common import HKT, SETTLE_AFTER_SECONDS, iso_hkt, parse_time, read_json, write_json_atomic
from .config import Settings
from .hkjc import fetch_official_results
from .ledger import recompute_stats
from .lines import pnl, settle_handicap, settle_total
from .matching import Event, match_event
from .pinnapi import PinnapiClient
from .state import load_ledger, paths, save_ledger
from .titan import TitanClient


def _target(bet: dict[str, Any]) -> Event | None:
    kickoff = parse_time(bet.get("kickoff"))
    if not kickoff or not bet.get("home") or not bet.get("away"):
        return None
    return Event(str(bet.get("match_id")), str(bet.get("league") or ""), str(bet["home"]), str(bet["away"]), kickoff)


def _refresh_live(config: Settings, due: list[dict[str, Any]]) -> dict[str, Any]:
    cache = read_json(paths(config)["live"], {})
    ids = {str(bet.get("pinnapi_event_id")) for bet in due if bet.get("pinnapi_event_id")}
    if not ids:
        return cache
    snapshot = PinnapiClient(config).live_scores()
    for event_id in ids:
        record = cache.get(event_id, {})
        score = snapshot.get(event_id)
        if score:
            cache[event_id] = record | score | {"seen_live": True, "no_longer_live": False}
        elif record.get("seen_live"):
            cache[event_id] = record | {"no_longer_live": True, "ended_candidate_at": iso_hkt()}
    write_json_atomic(paths(config)["live"], cache)
    return cache


def _settle(bet: dict[str, Any], score: dict[str, Any], source: str) -> bool:
    try:
        condition = float(bet["condition"])
        if bet["code"] == "CHL":
            corners = int(score["corners_total"])
            result = settle_total(condition, bet["side"], corners, 0)
            result_score = {"corners_total": corners}
        else:
            home, away = int(score["home_score"]), int(score["away_score"])
            result = (settle_handicap(condition, bet["side"], home, away)
                      if bet["code"] == "HDC" else settle_total(condition, bet["side"], home, away))
            result_score = {"goals": f"{home}-{away}", "goals_total": home + away}
    except (KeyError, TypeError, ValueError):
        return False
    bet.update({"status": "SETTLED", "result": result, "pnl": pnl(result, float(bet["stake"]), float(bet["odds"])),
                "score": result_score, "settled_at": iso_hkt(),
                "settlement_source": source})
    bet.setdefault("history", []).append({"ts": iso_hkt(), "stage": "結算", "action": "模擬結算",
                                           "result": result, "source": source})
    return True


def settle_due(config: Settings) -> dict[str, Any]:
    ledger = load_ledger(config)
    now = datetime.now(HKT)
    due = [bet for bet in ledger["bets"] if bet.get("status") == "PENDING" and (parse_time(bet.get("kickoff")) and
           (now - parse_time(bet["kickoff"])).total_seconds() >= SETTLE_AFTER_SECONDS)]
    if not due:
        return {"ok": True, "settled": 0, "pending": sum(b.get("status") == "PENDING" for b in ledger["bets"])}
    cache: dict[str, Any] = {}
    standard_due = [bet for bet in due if bet.get("code") != "CHL"]
    try:
        cache = _refresh_live(config, standard_due)
    except Exception:
        # A live-score failure cannot cause fallback settlement for a known-live event.
        cache = read_json(paths(config)["live"], {})
    # CHL is never settled from PinnAPI live goal scores or Titan results.  It
    # always waits for HKJC's confirmed exact-ID full-match corners total.
    corner_due = [bet for bet in due if bet.get("code") == "CHL"]
    fallback = [
        bet for bet in due
        if bet.get("code") != "CHL"
        and not (cache.get(str(bet.get("pinnapi_event_id"))) or {}).get("seen_live")
    ]
    titan_results: list[dict[str, Any]] = []
    hkjc_results: dict[str, dict[str, Any]] = {}
    official_due = fallback + corner_due
    if fallback:
        try:
            titan_results = TitanClient(config).results()
        except Exception:
            titan_results = []
    if official_due:
        dates = {parse_time(bet.get("kickoff")).strftime("%Y-%m-%d") for bet in official_due if parse_time(bet.get("kickoff"))}
        ids = {str(bet.get("hkjc_match_id")) for bet in official_due if bet.get("hkjc_match_id")}
        try:
            hkjc_results = fetch_official_results(ids, dates)
        except Exception:
            hkjc_results = {}
    settled = 0
    for bet in due:
        if bet.get("code") == "CHL":
            official = hkjc_results.get(str(bet.get("hkjc_match_id") or ""))
            if official and official.get("corners_total") is not None and _settle(
                bet, official, "hkjc_official_exact_id_corners"
            ):
                settled += 1
            continue
        event_id = str(bet.get("pinnapi_event_id") or "")
        live = cache.get(event_id, {})
        if live.get("seen_live"):
            if live.get("no_longer_live") and _settle(bet, live, "pinnapi_live_observed_then_absent"):
                settled += 1
            continue
        target = _target(bet)
        titan_id = str(bet.get("titan_match_id") or "")
        official = hkjc_results.get(str(bet.get("hkjc_match_id") or ""))
        if official and _settle(bet, official, "hkjc_official_exact_id"):
            settled += 1
            continue
        titan = next((row for row in titan_results if str(row.get("id")) == titan_id and row.get("home_score") is not None), None)
        # Stored Titan ID is fast-path only; identity is still checked before it can settle.
        if titan and target:
            candidate = Event(str(titan["id"]), str(titan["league"]), str(titan["home"]), str(titan["away"]), titan["kickoff"])
            if match_event(target, [candidate]).event and _settle(bet, titan, "titan_verified_identity"):
                settled += 1
                continue
    recompute_stats(ledger, config)
    save_ledger(config, ledger)
    return {"ok": True, "settled": settled, "pending": sum(b.get("status") == "PENDING" for b in ledger["bets"])}
