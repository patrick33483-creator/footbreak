"""Safe Crown simulation settlement: PinnAPI live cache first, then identified fallback only."""
from __future__ import annotations

from datetime import datetime
from typing import Any

from .common import (
    HKT,
    SETTLE_AFTER_SECONDS,
    is_non_result_terminal_status,
    iso_hkt,
    parse_time,
    read_json,
    write_json_atomic,
)
from .config import Settings
from .hkjc import fetch_official_match_statuses, fetch_official_results
from .ledger import recompute_stats
from .lines import pnl, settle_handicap, settle_total
from .matching import Event, canonical_league_key, canonical_team_key, match_event
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
    action = "影子結算" if bet.get("portfolio") == "shadow" else "模擬結算"
    bet.setdefault("history", []).append({"ts": iso_hkt(), "stage": "結算", "action": action,
                                           "result": result, "source": source})
    return True


def _void(bet: dict[str, Any], status: str, source: str) -> None:
    bet.update({
        "status": "VOIDED",
        "result": "Refunded",
        "pnl": 0.0,
        "void_reason": f"fixture_not_played:{status}",
        "settled_at": iso_hkt(),
        "settlement_source": source,
    })
    bet.setdefault("history", []).append({
        "ts": iso_hkt(),
        "stage": "結算",
        "action": "賽事不計",
        "result": "Refunded",
        "source": source,
        "terminal_status": status,
    })


def _verified_titan_terminal_status(
    bet: dict[str, Any],
    titan_by_id: dict[str, dict[str, Any]],
) -> str | None:
    target = _target(bet)
    titan_id = str(bet.get("titan_match_id") or bet.get("match_id") or "")
    titan = titan_by_id.get(titan_id)
    kickoff = parse_time((titan or {}).get("kickoff"))
    if (
        target is None
        or not titan
        or not kickoff
        or not is_non_result_terminal_status(titan.get("status"))
    ):
        return None
    candidate = Event(
        titan_id,
        str(titan.get("league") or ""),
        str(titan.get("home") or ""),
        str(titan.get("away") or ""),
        kickoff,
    )
    matched = match_event(
        target,
        [candidate],
        team_key=canonical_team_key,
        league_key=canonical_league_key,
        allow_reversed=True,
        require_qualifiers=True,
    )
    return str(titan.get("status") or "") if matched.event else None


def _verified_titan_corner_score(
    bet: dict[str, Any],
    official: dict[str, Any],
    titan_by_id: dict[str, dict[str, Any]],
    client: TitanClient,
) -> dict[str, Any] | None:
    """Use Titan corners only after exact ID, identity and official-score checks."""
    target = _target(bet)
    titan_id = str(bet.get("titan_match_id") or bet.get("match_id") or "")
    titan = titan_by_id.get(titan_id)
    if target is None or not titan or titan.get("home_score") is None:
        return None
    candidate = Event(
        str(titan["id"]),
        str(titan.get("league") or ""),
        str(titan.get("home") or ""),
        str(titan.get("away") or ""),
        titan["kickoff"],
    )
    matched = match_event(
        target,
        [candidate],
        team_key=canonical_team_key,
        league_key=canonical_league_key,
        allow_reversed=True,
        require_qualifiers=True,
    )
    if not matched.event:
        return None
    titan_home, titan_away = titan.get("home_score"), titan.get("away_score")
    if matched.reversed:
        titan_home, titan_away = titan_away, titan_home
    try:
        official_score = (int(official["home_score"]), int(official["away_score"]))
        titan_score = (int(titan_home), int(titan_away))
    except (KeyError, TypeError, ValueError):
        return None
    if titan_score != official_score:
        return None
    try:
        detail = client.result_detail(titan_id)
        corners = int(detail["corners_total"]) if detail else None
    except (KeyError, TypeError, ValueError, OSError):
        return None
    if corners is None or corners < 0:
        return None
    return {**official, "corners_total": corners}


def settle_due(config: Settings) -> dict[str, Any]:
    ledger = load_ledger(config)
    now = datetime.now(HKT)
    official_bets = ledger.setdefault("bets", [])
    shadow_bets = ledger.setdefault("shadow_bets", [])
    official_due = [
        bet for bet in official_bets
        if bet.get("status") == "PENDING"
        and parse_time(bet.get("kickoff"))
        and (now - parse_time(bet["kickoff"])).total_seconds() >= SETTLE_AFTER_SECONDS
    ]
    shadow_due = [
        bet for bet in shadow_bets
        if bet.get("status") == "PENDING"
        and parse_time(bet.get("kickoff"))
        and (now - parse_time(bet["kickoff"])).total_seconds() >= SETTLE_AFTER_SECONDS
    ]
    due = official_due + shadow_due
    if not due:
        recompute_stats(ledger, config)
        return {
            "ok": True,
            "settled": 0,
            "voided": 0,
            "pending": sum(b.get("status") == "PENDING" for b in official_bets),
            "shadow_settled": 0,
            "shadow_voided": 0,
            "shadow_pending": sum(b.get("status") == "PENDING" for b in shadow_bets),
        }
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
    hkjc_results: dict[str, dict[str, Any]] = {}
    hkjc_statuses: dict[str, dict[str, Any]] = {}
    result_lookup_due = fallback + corner_due
    if result_lookup_due:
        dates = {parse_time(bet.get("kickoff")).strftime("%Y-%m-%d") for bet in result_lookup_due if parse_time(bet.get("kickoff"))}
        ids = {str(bet.get("hkjc_match_id")) for bet in result_lookup_due if bet.get("hkjc_match_id")}
        try:
            hkjc_results = fetch_official_results(ids, dates)
        except Exception:
            hkjc_results = {}
        try:
            hkjc_statuses = fetch_official_match_statuses(ids, dates)
        except Exception:
            hkjc_statuses = {}
    def needs_titan_result(bet: dict[str, Any]) -> bool:
        hkjc_id = str(bet.get("hkjc_match_id") or "")
        hkjc_state = hkjc_statuses.get(hkjc_id) or {}
        if is_non_result_terminal_status(
            hkjc_state.get("status"),
            refund_pools=hkjc_state.get("refund_pools"),
            payout_refund_pools=hkjc_state.get("payout_refund_pools"),
        ):
            return False
        official = hkjc_results.get(hkjc_id)
        if bet.get("code") == "CHL":
            return not (official and official.get("corners_total") is not None)
        live = cache.get(str(bet.get("pinnapi_event_id") or ""), {})
        return not live.get("seen_live") and official is None

    titan_client = TitanClient(config)
    titan_results: list[dict[str, Any]] = []
    if any(needs_titan_result(bet) for bet in due):
        try:
            titan_results = titan_client.results()
        except Exception:
            titan_results = []
    titan_by_id = {str(row.get("id") or ""): row for row in titan_results}
    counters = {
        "settled": 0,
        "voided": 0,
        "shadow_settled": 0,
        "shadow_voided": 0,
    }

    def count(outcome: str, bet: dict[str, Any]) -> None:
        prefix = "shadow_" if bet.get("portfolio") == "shadow" else ""
        counters[f"{prefix}{outcome}"] += 1

    for bet in due:
        hkjc_state = hkjc_statuses.get(str(bet.get("hkjc_match_id") or "")) or {}
        if is_non_result_terminal_status(
            hkjc_state.get("status"),
            refund_pools=hkjc_state.get("refund_pools"),
            payout_refund_pools=hkjc_state.get("payout_refund_pools"),
        ):
            _void(
                bet,
                str(hkjc_state.get("status") or "REFUNDED"),
                "hkjc_official_exact_id_terminal_status",
            )
            count("voided", bet)
            continue
        titan_status = _verified_titan_terminal_status(bet, titan_by_id)
        if titan_status:
            _void(bet, titan_status, "titan_exact_id_terminal_status")
            count("voided", bet)
            continue
        if bet.get("code") == "CHL":
            official = hkjc_results.get(str(bet.get("hkjc_match_id") or ""))
            if official and official.get("corners_total") is not None and _settle(
                bet, official, "hkjc_official_exact_id_corners"
            ):
                count("settled", bet)
                continue
            if official:
                verified = _verified_titan_corner_score(
                    bet, official, titan_by_id, titan_client
                )
                if verified and _settle(
                    bet,
                    verified,
                    "hkjc_official_score+titan007_detail_exact_id_identity",
                ):
                    count("settled", bet)
            continue
        event_id = str(bet.get("pinnapi_event_id") or "")
        live = cache.get(event_id, {})
        if live.get("seen_live"):
            if live.get("no_longer_live") and _settle(bet, live, "pinnapi_live_observed_then_absent"):
                count("settled", bet)
            continue
        target = _target(bet)
        titan_id = str(bet.get("titan_match_id") or "")
        official = hkjc_results.get(str(bet.get("hkjc_match_id") or ""))
        if official and _settle(bet, official, "hkjc_official_exact_id"):
            count("settled", bet)
            continue
        titan = next((row for row in titan_results if str(row.get("id")) == titan_id and row.get("home_score") is not None), None)
        # Stored Titan ID is fast-path only; identity is still checked before it can settle.
        if titan and target:
            candidate = Event(str(titan["id"]), str(titan["league"]), str(titan["home"]), str(titan["away"]), titan["kickoff"])
            if match_event(target, [candidate]).event and _settle(bet, titan, "titan_verified_identity"):
                count("settled", bet)
                continue
    recompute_stats(ledger, config)
    save_ledger(config, ledger)
    return {
        "ok": True,
        "settled": counters["settled"],
        "voided": counters["voided"],
        "pending": sum(b.get("status") == "PENDING" for b in official_bets),
        "shadow_settled": counters["shadow_settled"],
        "shadow_voided": counters["shadow_voided"],
        "shadow_pending": sum(b.get("status") == "PENDING" for b in shadow_bets),
    }
