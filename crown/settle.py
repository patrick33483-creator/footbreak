"""Safe Crown simulation settlement: PinnAPI live cache first, then identified fallback only."""
from __future__ import annotations

import copy
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
from .hkjc import fetch_official_settlement_bundle
from .ledger import condition_bets, recompute_stats
from .challenger_v2 import NAMESPACE as V2_NAMESPACE, research_bets
from .lines import pnl, settle_handicap, settle_total
from .matching import Event, canonical_league_key, canonical_team_key, match_event
from .pinnapi import PinnapiClient
from .state import load_ledger, paths, save_ledger, settlement_lock, state_lock
from .titan import TitanClient


LIVE_CACHE_STALE_SECONDS = 30 * 60
LIVE_CACHE_FAILURES_BEFORE_FALLBACK = 2


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
            cache[event_id] = record | score | {
                "seen_live": True,
                "no_longer_live": False,
                "last_live_seen_at": iso_hkt(),
                "live_refresh_failures": 0,
                "last_live_refresh_failure_at": None,
            }
        elif record.get("seen_live"):
            cache[event_id] = record | {"no_longer_live": True, "ended_candidate_at": iso_hkt()}
    write_json_atomic(paths(config)["live"], cache)
    return cache


def _live_blocks_fallback(live: dict[str, Any], now: datetime) -> bool:
    """Keep fresh live observations authoritative, never indefinitely so."""
    if not live.get("seen_live") or live.get("no_longer_live"):
        return False
    observed_at = parse_time(live.get("last_live_seen_at"))
    if observed_at and (now - observed_at).total_seconds() >= LIVE_CACHE_STALE_SECONDS:
        return False
    try:
        failures = int(live.get("live_refresh_failures") or 0)
    except (TypeError, ValueError):
        failures = 0
    return failures < LIVE_CACHE_FAILURES_BEFORE_FALLBACK


def _record_live_refresh_failure(
    config: Settings,
    cache: dict[str, Any],
    due: list[dict[str, Any]],
) -> dict[str, Any]:
    """Persist a bounded failure condition for legacy and current live caches."""
    failed_at = iso_hkt()
    for event_id in {str(bet.get("pinnapi_event_id") or "") for bet in due} - {""}:
        record = cache.get(event_id)
        if not isinstance(record, dict) or not record.get("seen_live"):
            continue
        try:
            failures = int(record.get("live_refresh_failures") or 0)
        except (TypeError, ValueError):
            failures = 0
        cache[event_id] = record | {
            "live_refresh_failures": failures + 1,
            "last_live_refresh_failure_at": failed_at,
        }
    write_json_atomic(paths(config)["live"], cache)
    return cache


def _pending_diagnostic(bet: dict[str, Any], reason: str) -> None:
    bet["last_settlement_attempt_at"] = iso_hkt()
    bet["settlement_pending_reason"] = reason


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
    bet.pop("settlement_pending_reason", None)
    action = "模擬結算"
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
    bet.pop("settlement_pending_reason", None)
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


def _verified_titan_detail_score(
    bet: dict[str, Any],
    client: TitanClient,
) -> dict[str, Any] | None:
    """Recover a completed exact-ID result omitted from Titan's result index."""
    target = _target(bet)
    titan_id = str(bet.get("titan_match_id") or bet.get("match_id") or "")
    if target is None or not titan_id:
        return None
    try:
        detail = client.result_detail(titan_id)
    except (KeyError, TypeError, ValueError, OSError):
        return None
    kickoff = parse_time((detail or {}).get("kickoff"))
    if (
        not detail
        or str(detail.get("id") or "") != titan_id
        or kickoff is None
        or detail.get("home_score") is None
        or detail.get("away_score") is None
    ):
        return None
    candidate = Event(
        titan_id,
        str(detail.get("league") or ""),
        str(detail.get("home") or ""),
        str(detail.get("away") or ""),
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
    if not matched.event:
        return None
    try:
        home_score = int(detail["home_score"])
        away_score = int(detail["away_score"])
    except (KeyError, TypeError, ValueError):
        return None
    if matched.reversed:
        home_score, away_score = away_score, home_score
    return {"home_score": home_score, "away_score": away_score}


def _commit_settlement(
    config: Settings,
    before: dict[str, dict[str, Any]],
    staged: dict[str, Any],
) -> None:
    """Merge only settlement-owned changes into the newest ledger atomically.

    The slow result lookup runs without ``state_lock`` so a T-5 can commit.
    Reload under that lock and apply only updates to bets that are still
    pending; this avoids restoring an old watch/prediction snapshot over a
    concurrent tick and makes a second settlement pass idempotent.
    """
    current = load_ledger(config)
    current_by_id = {
        str(bet.get("bet_id") or ""): bet
        for bet in current.get("bets") or []
        if isinstance(bet, dict)
    }
    current_v2 = current.get(V2_NAMESPACE)
    current_v2_by_id = {
        str(bet.get("research_id") or ""): bet
        for bet in research_bets(current)
    } if isinstance(current_v2, dict) else {}
    owned = (
        "status", "result", "pnl", "score", "settled_at",
        "settlement_source", "void_reason", "last_settlement_attempt_at",
        "settlement_pending_reason", "history",
    )
    for staged_bet in condition_bets(staged):
        bet_id = str(staged_bet.get("bet_id") or "")
        original = before.get(bet_id)
        current_bet = current_by_id.get(bet_id)
        if not original or not current_bet or current_bet.get("status") != "PENDING":
            continue
        if all(staged_bet.get(key) == original.get(key) for key in owned):
            continue
        for key in owned:
            if key in staged_bet:
                current_bet[key] = staged_bet[key]
            else:
                current_bet.pop(key, None)
    # v2 research rows use their own namespace and own dedupe IDs.  Apply the
    # same verified result only to the matching shadow row; v1 rows and stats
    # remain untouched by this branch.
    for staged_bet in research_bets(staged):
        bet_id = str(staged_bet.get("research_id") or "")
        original = before.get(bet_id)
        current_bet = current_v2_by_id.get(bet_id)
        if not original or not current_bet or current_bet.get("status") != "PENDING":
            continue
        if all(staged_bet.get(key) == original.get(key) for key in owned):
            continue
        for key in owned:
            if key in staged_bet:
                current_bet[key] = staged_bet[key]
            else:
                current_bet.pop(key, None)
    recompute_stats(current, config)
    save_ledger(config, current)


def _settle_due_locked(config: Settings) -> dict[str, Any]:
    """Perform one settlement pass; caller holds settlement_lock only."""
    ledger = load_ledger(config)
    official_bets = condition_bets(ledger)
    v2_bets = research_bets(ledger)
    before = {
        str(bet.get("bet_id") or ""): copy.deepcopy(bet)
        for bet in official_bets
        if bet.get("bet_id")
    }
    before.update({
        str(bet.get("research_id") or ""): copy.deepcopy(bet)
        for bet in v2_bets if bet.get("research_id")
    })
    now = datetime.now(HKT)
    official_due = [
        bet for bet in official_bets
        if bet.get("status") == "PENDING"
        and parse_time(bet.get("kickoff"))
        and (now - parse_time(bet["kickoff"])).total_seconds() >= SETTLE_AFTER_SECONDS
    ]
    v2_due = [
        bet for bet in v2_bets
        if bet.get("status") == "PENDING"
        and parse_time(bet.get("kickoff"))
        and (now - parse_time(bet["kickoff"])).total_seconds() >= SETTLE_AFTER_SECONDS
    ]
    due = official_due + v2_due
    if not due:
        # Retain the historical cheap statistics refresh, but do it as a
        # fresh short commit so a concurrently written T-5 is never lost.
        with state_lock(config):
            current = load_ledger(config)
            recompute_stats(current, config)
            save_ledger(config, current)
        return {"ok": True, "settled": 0, "voided": 0,
                "pending": sum(b.get("status") == "PENDING" for b in official_bets + v2_bets)}
    cache: dict[str, Any] = {}
    standard_due = [bet for bet in due if bet.get("code") != "CHL"]
    try:
        cache = _refresh_live(config, standard_due)
    except Exception:
        # Preserve a bounded failure count.  Strict exact-ID official/Titan
        # fallback remains unavailable until a live observation is stale or
        # repeated live refresh failures make the cache non-authoritative.
        cache = read_json(paths(config)["live"], {})
        cache = _record_live_refresh_failure(config, cache, standard_due)
    # CHL is never settled from PinnAPI live goal scores or Titan results.  It
    # always waits for HKJC's confirmed exact-ID full-match corners total.
    corner_due = [bet for bet in due if bet.get("code") == "CHL"]
    fallback = [
        bet for bet in due
        if bet.get("code") != "CHL"
        and not _live_blocks_fallback(
            cache.get(str(bet.get("pinnapi_event_id")) or "", {}),
            now,
        )
    ]
    hkjc_results: dict[str, dict[str, Any]] = {}
    hkjc_statuses: dict[str, dict[str, Any]] = {}
    result_lookup_due = fallback + corner_due
    if result_lookup_due:
        dates = {parse_time(bet.get("kickoff")).strftime("%Y-%m-%d") for bet in result_lookup_due if parse_time(bet.get("kickoff"))}
        ids = {str(bet.get("hkjc_match_id")) for bet in result_lookup_due if bet.get("hkjc_match_id")}
        try:
            hkjc_results, hkjc_statuses = fetch_official_settlement_bundle(
                ids,
                dates,
                max_seconds=60.0,
            )
        except Exception:
            hkjc_results, hkjc_statuses = {}, {}
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
        return not _live_blocks_fallback(live, now) and official is None

    titan_client = TitanClient(config)
    # Exact detail pages can require two provider reads each. Keep a small
    # per-pass fanout cap; unresolved bets remain pending for the next pass.
    titan_client.limit_result_detail_requests(3)
    titan_results: list[dict[str, Any]] = []
    if any(needs_titan_result(bet) for bet in due):
        try:
            titan_results = titan_client.results()
        except Exception:
            titan_results = []
    titan_by_id = {str(row.get("id") or ""): row for row in titan_results}
    counters = {"settled": 0, "voided": 0}

    def count(outcome: str, bet: dict[str, Any]) -> None:
        counters[outcome] += 1

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
        if _live_blocks_fallback(live, now):
            _pending_diagnostic(bet, "pinnapi_live_cache_fresh")
            continue
        if live.get("seen_live") and live.get("no_longer_live"):
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
        if not titan:
            detail_score = _verified_titan_detail_score(bet, titan_client)
            if detail_score and _settle(
                bet,
                detail_score,
                "titan007_detail_exact_id_identity",
            ):
                count("settled", bet)
                continue
        _pending_diagnostic(
            bet,
            "pinnapi_live_cache_stale_fallback_unresolved"
            if live.get("seen_live")
            else "verified_result_unavailable",
        )
    # Do not save the stale ledger loaded before provider reads.  A tick can
    # have created T-5 stages/bets while this pass was waiting on a result
    # source, so merge settlement-owned fields into the latest ledger only.
    with state_lock(config):
        _commit_settlement(config, before, ledger)
    return {"ok": True, "settled": counters["settled"], "voided": counters["voided"],
            "pending": sum(b.get("status") == "PENDING" for b in official_bets + v2_bets)}


def settle_due(config: Settings) -> dict[str, Any]:
    """Settle due rows without blocking time-critical prediction commits."""
    with settlement_lock(config) as acquired:
        if not acquired:
            return {"ok": False, "reason": "settlement_busy"}
        return _settle_due_locked(config)
