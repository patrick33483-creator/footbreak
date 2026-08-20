"""Crown prediction pass with independent forecasting and strict PinnAPI betting gates."""
from __future__ import annotations

import math
import multiprocessing
import os
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime
from multiprocessing.connection import wait as wait_for_connections
from typing import Any

from .common import HKT, iso_hkt, parse_time
from .config import Settings
from .hkjc import event_from_match, fetch_matches, flatten_odds
from .ledger import (
    PREDICTION_ERA,
    completed_stages,
    market_entry_thresholds,
    recompute_stats,
    stage_for,
    sync_prediction,
)
from .lines import parse_hkjc_total
from .matching import MATCHING_VERSION, Event, Match, BridgeMatch, bridge_titan_to_pinnapi
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
            PREDICTION_ERA,
        )
        stage = stage_for(minutes, False, done)
        if not stage:
            continue
        rows.append({
            "id": match_id,
            "league": card.get("league") or "",
            "home": card.get("home") or "",
            "away": card.get("away") or "",
            "kickoff": kickoff,
            # Local-only scheduling metadata.  It never becomes provider
            # evidence or a persisted stage field.
            "_due_stage": stage,
        })
        seen.add(match_id)
    rows.sort(key=lambda row: row["kickoff"])
    return rows


def _prioritize_tick_rows(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Give all due T-5 work exclusive use of the current tick."""
    if any(row.get("_due_stage") == "T-5" for row in rows):
        return [row for row in rows if row.get("_due_stage") == "T-5"]
    return rows


def _tick_pass_deadline_seconds() -> float:
    """Return a bounded tick budget, leaving time for the service to stop cleanly."""
    try:
        configured = float(os.getenv("CROWN_TICK_PASS_DEADLINE_SECONDS", "40"))
    except ValueError:
        configured = 40.0
    # The systemd service is the final 55-second backstop.  Keep the in-process
    # budget meaningfully below it, including when an operator misconfigures it.
    return min(50.0, max(1.0, configured))


def _tick_workers() -> int:
    try:
        configured = int(os.getenv("CROWN_TICK_MAX_WORKERS", "8"))
    except ValueError:
        configured = 8
    return min(12, max(1, configured))


def _deadline_remaining(deadline: float) -> float:
    """Return the remaining monotonic pass budget without a negative timeout."""
    return max(0.0, deadline - time.monotonic())


_MIN_DEADLINE_CALL_SECONDS = 0.25


def _tick_hkjc_fetch_process(send: Any) -> None:
    """Keep the shared Footbreak HKJC reader out of Crown's deadline process."""
    try:
        send.send(("ok", fetch_matches()))
    except BaseException as exc:
        send.send(("error", type(exc).__name__))
    finally:
        send.close()


def _fetch_tick_hkjc_matches(deadline: float) -> list[dict[str, Any]]:
    """Fail closed if the legacy HKJC feed cannot finish inside this tick.

    The shared reader does not expose a per-request budget.  Isolating only
    this optional bridge feed preserves Footbreak's scheduling code while
    allowing Crown to terminate a stuck request rather than overrun T-30/T-5.
    """
    remaining = _deadline_remaining(deadline)
    if remaining < _MIN_DEADLINE_CALL_SECONDS or os.name != "posix":
        return []
    context = multiprocessing.get_context("fork")
    receiver, sender = context.Pipe(duplex=False)
    process = context.Process(target=_tick_hkjc_fetch_process, args=(sender,))
    process.start()
    sender.close()
    try:
        if not receiver.poll(remaining):
            return []
        status, value = receiver.recv()
        return value if status == "ok" and isinstance(value, list) else []
    except EOFError:
        return []
    finally:
        receiver.close()
        if process.is_alive():
            process.terminate()
        process.join(timeout=0.05)
        if process.is_alive():
            process.kill()
            process.join(timeout=0.05)


def _prediction_process(send: Any, payload: tuple[Any, ...]) -> None:
    """Run one provider-heavy prediction outside the tick process.

    A socket/library call can ignore cancellation in a Python thread.  A
    separate process is deliberately used here so the parent can terminate an
    overdue page without waiting for an executor's worker shutdown.  This runs
    only on the Linux deployment target, where ``fork`` also avoids sharing a
    request connection between fixtures.
    """
    try:
        send.send(("ok", _prediction(*payload)))
    except BaseException as exc:
        send.send(("error", type(exc).__name__))
    finally:
        send.close()


def _run_tick_predictions(
    payloads: list[tuple[Any, ...]],
    deadline: float,
    on_complete: Any,
) -> dict[str, int]:
    """Bound concurrent T-5 work and commit each completed result immediately.

    ``Process.terminate`` is essential: ``Future.cancel`` cannot cancel a
    running ThreadPoolExecutor task, and executor context-manager shutdown
    waits for exactly the stuck worker that caused this incident.
    """
    if not payloads:
        return {"completed": 0, "failed": 0, "deferred": 0}
    if os.name != "posix":
        # Crown production is Linux.  Failing closed elsewhere is safer than
        # silently reintroducing an unkillable thread-based deadline breach.
        return {"completed": 0, "failed": 0, "deferred": len(payloads)}

    context = multiprocessing.get_context("fork")
    queued = iter(payloads)
    active: dict[Any, tuple[Any, Any]] = {}
    completed = failed = submitted = 0
    exhausted = False
    while active or not exhausted:
        while (
            not exhausted
            and len(active) < _tick_workers()
            and time.monotonic() < deadline
        ):
            try:
                payload = next(queued)
            except StopIteration:
                exhausted = True
                break
            receiver, sender = context.Pipe(duplex=False)
            process = context.Process(target=_prediction_process, args=(sender, payload))
            process.start()
            sender.close()
            active[receiver] = (process, payload)
            submitted += 1

        remaining = deadline - time.monotonic()
        if remaining <= 0:
            break
        if not active:
            continue
        ready = wait_for_connections(
            list(active), timeout=min(0.10, remaining)
        )
        for receiver in ready:
            process, _payload = active.pop(receiver)
            try:
                status, value = receiver.recv()
            except EOFError:
                status, value = "error", "worker_exited"
            finally:
                receiver.close()
                process.join(timeout=0.05)
            if status == "ok":
                on_complete(value)
                completed += 1
            else:
                failed += 1

        # A worker can die before writing its pipe.  Do not wait for a timeout
        # before releasing its slot for the next kickoff group.
        for receiver, (process, _payload) in list(active.items()):
            if process.is_alive():
                continue
            if receiver.poll():
                continue
            active.pop(receiver)
            receiver.close()
            process.join(timeout=0.05)
            failed += 1

    for receiver, (process, _payload) in active.items():
        if process.is_alive():
            process.terminate()
        process.join(timeout=0.25)
        if process.is_alive():
            process.kill()
            process.join(timeout=0.25)
        receiver.close()
    # Anything never submitted, plus forcibly terminated workers, remains due
    # because no stage was written.  The next minute will collect fresh,
    # correctly-labelled pre-kickoff evidence.
    return {
        "completed": completed,
        "failed": failed,
        "deferred": len(payloads) - completed - failed,
    }


def _sweep_rows_with_due_existing(
    titan_rows: list[dict[str, Any]],
    predictions: list[dict[str, Any]],
    ledger: dict[str, Any],
    now: datetime,
) -> list[dict[str, Any]]:
    """Recover stale first-look cards omitted from Titan's current fixture list."""
    rows = list(titan_rows)
    seen = {str(row.get("id") or "") for row in rows}
    for card in predictions:
        match_id = str(card.get("match_id") or "")
        kickoff = parse_time(card.get("kickoff_hkt") or card.get("kickoff"))
        if (
            not match_id
            or match_id in seen
            or kickoff is None
            or kickoff <= now
            or not in_current_period(kickoff, now)
        ):
            continue
        done = completed_stages(
            (ledger.get("watch") or {}).get(match_id, {}),
            MATCHING_VERSION,
            PREDICTION_ERA,
        )
        if not stage_for((kickoff - now).total_seconds() / 60, True, done):
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


_CROWN_ID3_SOURCE = "titan007-crown-id-3"
_CROWN_BULK_ID3_SOURCE = "titan007-crown-id-3-bulk-current"
_CACHED_T5_FALLBACK_SOURCE = "cached_t5_exact_pre_kickoff_crown_id_3"
_CACHED_T5_FALLBACK_MAX_AGE_SECONDS = 10 * 60


def _epoch_observed_at(value: Any) -> float | None:
    """Return a finite epoch timestamp without inventing an observation time."""
    try:
        observed = float(value)
    except (TypeError, ValueError):
        parsed = parse_time(str(value or ""))
        return parsed.timestamp() if parsed is not None else None
    if not math.isfinite(observed) or observed <= 0:
        return None
    # The price adapter writes seconds, but old immutable journals can contain
    # milliseconds.  Normalize only an otherwise real timestamp.
    return observed / 1000 if observed >= 10_000_000_000 else observed


def _same_cached_fixture_identity(
    titan: dict[str, Any],
    cached_card: dict[str, Any],
) -> bool:
    """Require the complete, direct fixture identity; never name-match a cache."""
    kickoff = parse_time(titan.get("kickoff"))
    cached_kickoff = parse_time(
        cached_card.get("kickoff_hkt") or cached_card.get("kickoff")
    )
    return bool(
        str(cached_card.get("match_id") or "") == str(titan.get("id") or "")
        and str(cached_card.get("home") or "") == str(titan.get("home") or "")
        and str(cached_card.get("away") or "") == str(titan.get("away") or "")
        and kickoff is not None
        and cached_kickoff is not None
        and cached_kickoff == kickoff
    )


def _cached_t5_crown_snapshot(
    titan: dict[str, Any],
    cached_card: dict[str, Any] | None,
    now: datetime | None = None,
) -> dict[str, Any] | None:
    """Build a narrow T-5 fallback only from saved exact Crown-ID-3 evidence.

    ``book_odds.crown`` by itself is deliberately insufficient: older cards
    have no fixture-bound selected-quote provenance.  A cache is usable only
    when its current journal proves one exact HDC/HIL selected quote, with the
    same real source timestamp and decimal odds, for this exact fixture.  The
    returned prices remain a complete saved board so the ordinary forecast
    code derives its payload rather than fabricating a selected prediction.
    """
    if not isinstance(cached_card, dict) or not _same_cached_fixture_identity(
        titan, cached_card
    ):
        return None
    now = (now or datetime.now(HKT)).astimezone(HKT)
    kickoff = parse_time(titan.get("kickoff"))
    if kickoff is None or now >= kickoff:
        return None
    prices = list(((cached_card.get("book_odds") or {}).get("crown") or []))
    journal = list(cached_card.get("current_selected_odds_journal") or [])
    if not prices or not journal:
        return None
    accepted: list[dict[str, Any]] = []
    for selected in journal:
        if not isinstance(selected, dict):
            continue
        market = str(selected.get("code") or "")
        side = str(selected.get("side") or "")
        allowed_sides = {"HDC": {"H", "A"}, "HIL": {"H", "L"}}
        if (
            market not in allowed_sides
            or side not in allowed_sides[market]
            or selected.get("provider") != "Crown"
            or selected.get("source") not in {
                _CROWN_ID3_SOURCE, _CROWN_BULK_ID3_SOURCE,
            }
        ):
            continue
        try:
            line = float(selected.get("line"))
            odds = float(selected.get("odds"))
        except (TypeError, ValueError):
            continue
        if not (math.isfinite(line) and math.isfinite(odds) and odds > 1):
            continue
        def exact_saved_quote(row: Any) -> bool:
            try:
                return bool(
                    isinstance(row, dict)
                    and row.get("market") == market
                    and row.get("selection") == side
                    and float(row.get("line")) == line
                    and float(row.get("odds")) == odds
                )
            except (TypeError, ValueError):
                return False
        quote = next((row for row in prices if exact_saved_quote(row)), None)
        if quote is None:
            continue
        observed = _epoch_observed_at(quote.get("source_at"))
        journal_observed = _epoch_observed_at(selected.get("observed_at"))
        if (
            observed is None
            or journal_observed is None
            or observed != journal_observed
            or observed >= kickoff.timestamp()
            or observed > now.timestamp()
            or now.timestamp() - observed > _CACHED_T5_FALLBACK_MAX_AGE_SECONDS
            or kickoff.timestamp() - observed > _CACHED_T5_FALLBACK_MAX_AGE_SECONDS
        ):
            continue
        accepted.append({
            "market": market, "line": line, "selection": side, "odds": odds,
            "observed_at": observed,
        })
    if not accepted:
        return None
    # Keep only supported Crown markets.  A selected exact quote is mandatory;
    # unselected opposite sides are retained solely to calculate the normal
    # complete-market forecast and are never used as a substitute selection.
    fallback_prices = [
        dict(row) for row in prices
        if isinstance(row, dict) and row.get("market") in {"HDC", "HIL"}
    ]
    if not fallback_prices:
        return None
    return {
        "prices": fallback_prices,
        "asian_ok": False,
        "total_ok": False,
        "cached_t5_fallback": True,
        "cached_t5_fallback_source": _CACHED_T5_FALLBACK_SOURCE,
        "cached_t5_selected_quotes": accepted,
    }


def _valid_pre_kickoff_bulk_snapshot(
    snapshot: dict[str, Any] | None,
    kickoff: datetime,
) -> bool:
    """Bulk current odds are unusable if any retained observation is in-play."""
    if not snapshot or snapshot.get("quote_source") != _CROWN_BULK_ID3_SOURCE:
        return True
    prices = list(snapshot.get("prices") or [])
    return bool(prices) and all(
        isinstance(row, dict)
        and (observed := _epoch_observed_at(row.get("source_at"))) is not None
        and observed < kickoff.timestamp()
        for row in prices
    )


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
            # Keep the exact observed source time with the selected quote.
            # It is carried into the immutable stage journal by ledger.py.
            "observed_at": rows[side].get("source_at"),
            "prob": round(probability, 5),
            "conviction": round(probability * 100, 1),
            "provider": "Crown",
            "source": "titan007-crown-id-3",
            "bookmaker": "Crown",
            "reference": "crown_full_market_no_vig",
            "forecast_only": True,
        })
    return output, sorted(set(reasons))


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
        # The independent probability is made from both exact reference
        # selections.  Retain the newer source timestamp: this is the point
        # at which the complete probability was knowable, not the Crown
        # quote timestamp used as the bet price.
        reference_rows = [
            next((
                price for price in pinnapi_prices
                if _line_key(price["market"], price.get("line")) == _line_key(market, line)
                and price["selection"] == key
            ), None)
            for key in match_keys
        ]
        try:
            reference_observed_at = max(float(row["source_at"]) for row in reference_rows if row is not None)
            if len(reference_rows) != len(match_keys) or not math.isfinite(reference_observed_at):
                reference_observed_at = None
        except (KeyError, TypeError, ValueError):
            reference_observed_at = None
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
            "provider": "Crown", "source": "titan007-crown-id-3", "bookmaker": "Crown",
            "observed_at": crown.get("source_at"),
            "probability_observed_at": reference_observed_at,
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


def _hkjc_chl_forecasts(
    hkjc_lines: list[dict[str, Any]],
) -> tuple[list[dict[str, Any]], list[str]]:
    """Build a forecast-only CHL view from a complete current HKJC market.

    This is the corner equivalent of the Crown HDC/HIL no-vig forecast.  It is
    valid for prediction history and learning only: without an independent
    exact-line PinnAPI reference it must never carry EV/Kelly or create a bet.
    """
    complete: list[
        tuple[float, dict[str, Any], dict[str, float]]
    ] = []
    reasons: list[str] = []
    for row in hkjc_lines:
        line = parse_hkjc_total(row.get("condition"))
        if line is None:
            reasons.append(f"invalid_hkjc_chl_line_{row.get('condition')}")
            continue
        odds = row.get("odds") or {}
        try:
            prices = {side: float(odds.get(side)) for side in ("H", "L")}
        except (TypeError, ValueError):
            reasons.append(f"hkjc_incomplete_CHL_{line:g}")
            continue
        if any(price <= 1 for price in prices.values()):
            reasons.append(f"hkjc_invalid_odds_CHL_{line:g}")
            continue
        implied = {side: 1 / prices[side] for side in ("H", "L")}
        denominator = sum(implied.values())
        probabilities = {
            side: implied[side] / denominator for side in ("H", "L")
        }
        complete.append((line, row, probabilities))
    if not complete:
        return [], sorted(set(reasons + ["no_complete_current_hkjc_CHL"]))

    line, row, probabilities = min(
        complete,
        key=lambda item: (
            not bool(item[1].get("main")),
            abs(item[2]["H"] - 0.5),
            abs(item[0]),
            item[0],
        ),
    )
    side = max(("H", "L"), key=lambda item: probabilities[item])
    probability = probabilities[side]
    odds = float((row.get("odds") or {})[side])
    return [{
        "market": "HKJC角球大細",
        "code": "CHL",
        "condition": f"{line:g}",
        "line": line,
        "side": side,
        "label": f"角球大細 {'大' if side == 'H' else '細'} {row.get('condition')}",
        "odds": round(odds, 3),
        "prob": round(probability, 5),
        "conviction": round(probability * 100, 1),
        "provider": "HKJC",
        "source": "hkjc_chl",
        "bookmaker": "HKJC",
        "reference": "hkjc_full_market_no_vig",
        "forecast_only": True,
    }], sorted(set(reasons))


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
                "label": f"角球大細 {'大' if side == 'H' else '細'} {hkjc.get('condition')}",
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
                entry_policies: dict[str, dict[str, Any]] | None = None,
                cached_t5_card: dict[str, Any] | None = None) -> dict[str, Any]:
    event = _event_from_titan(titan)
    minutes = round((event.kickoff - datetime.now(HKT)).total_seconds() / 60, 1)
    base = {
        "schema_version": "crown-prediction-v2", "matching_version": MATCHING_VERSION,
        "generated_at": iso_hkt(), "match_id": event.id,
        "league": event.league, "home": event.home, "away": event.away, "kickoff_hkt": iso_hkt(event.kickoff),
        # Local observation time for the first persisted card.  It lets a
        # read-only diagnostic distinguish a missing first look from a fixture
        # that was not yet discovered; it is never sourced from a provider.
        "discovered_at": iso_hkt(),
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
    corner_forecasts, corner_forecast_reasons = _hkjc_chl_forecasts(
        base["book_odds"]["hkjc_chl"]
    )
    base["forecast_candidates"] = corner_forecasts
    if corner_forecast_reasons:
        base["corner_forecast_notes"] = corner_forecast_reasons
    # Crown is the board master.  A direct bulk/page snapshot wins.  Only when
    # it is absent may a verified, exact saved T-5 card prevent a per-fixture
    # page timeout; an unscoped old price list is never enough to skip a call.
    if not _valid_pre_kickoff_bulk_snapshot(crown_snapshot, event.kickoff):
        crown_snapshot = None
    cached_t5_snapshot = (
        _cached_t5_crown_snapshot(titan, cached_t5_card)
        if stage == "T-5" and crown_snapshot is None else None
    )
    quote_snapshot = crown_snapshot or cached_t5_snapshot
    quote_source = str(
        (quote_snapshot or {}).get("quote_source")
        or (quote_snapshot or {}).get("cached_t5_fallback_source")
        or _CROWN_ID3_SOURCE
    )
    cached_t5_fallback = bool(
        (quote_snapshot or {}).get("cached_t5_fallback")
    )
    base["market_sources"]["HDC"] = quote_source
    base["market_sources"]["HIL"] = quote_source
    base["crown_quote_source"] = quote_source
    base["crown_quote_status"] = (
        "cached_t5_fallback"
        if cached_t5_fallback
        else "bulk_current"
        if quote_source == "titan007-crown-id-3-bulk-current"
        else "direct_current"
    )
    # Fetch and preserve its quote before any
    # HKJC/PinnAPI bridge decision so Crown-only fixtures can still be shown,
    # while edge calculation remains fail-closed without PinnAPI.
    crown = (
        list((quote_snapshot or {}).get("prices") or [])
        if quote_snapshot is not None
        else titan_client.crown_prices(event.id)
    )
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
        base["conviction"] = max(
            [round(float(base["probability"]) * 100, 1)]
            + [float(row["conviction"]) for row in corner_forecasts]
        )
        base["edge_reference_status"] = "unavailable"
        base["edge_reference_note"] = "皇冠即時盤及 PinnAPI EV 參考暫不可用；只保留低信念純預測。"
        base["no_bet_reason"] = None
        return base
    now = time.time()
    forecasts, forecast_reasons = _crown_market_forecasts(
        crown,
        config,
        now,
        # A sweep snapshot was fetched during this same bounded board pass.
        # Large fixture batches can take more than the normal live-freshness
        # window to reach _prediction(), but the retained source_at remains
        # the real pre-kickoff observation time.  Do not discard that valid
        # 首預 evidence merely because it waited in the local work queue.
        # Tick-mode direct reads still enforce the normal freshness gate.
        require_fresh=not (used_cached_crown or quote_snapshot is not None),
    )
    if cached_t5_fallback:
        # The cache proves only the saved selected quote(s), not every other
        # line retained on the board.  Refuse a recomputed direction unless it
        # is exactly the market/line/side/odds evidence that passed validation.
        accepted = {
            (
                row["market"], float(row["line"]), row["selection"],
                float(row["odds"]), float(row["observed_at"]),
            )
            for row in (quote_snapshot or {}).get("cached_t5_selected_quotes", [])
            if isinstance(row, dict)
        }
        forecasts = [
            forecast for forecast in forecasts
            if (
                forecast.get("code"),
                float(forecast.get("line")),
                forecast.get("side"),
                float(forecast.get("odds")),
                _epoch_observed_at(forecast.get("observed_at")),
            ) in accepted
        ]
    for forecast in forecasts:
        forecast["source"] = quote_source
        if cached_t5_fallback:
            forecast["quote_status"] = "cached_t5_fallback"
            forecast["quote_fallback_source"] = _CACHED_T5_FALLBACK_SOURCE
    all_forecasts = forecasts + corner_forecasts
    base["forecast_candidates"] = all_forecasts
    base["crown_quote_cached_forecast_only"] = used_cached_crown
    base["crown_cached_t5_fallback"] = cached_t5_fallback
    if used_cached_crown:
        source_times = [
            float(row.get("source_at") or 0)
            for row in crown
            if float(row.get("source_at") or 0) > 0
        ]
        base["crown_cached_source_at"] = min(source_times) if source_times else None
    if all_forecasts:
        base["status"] = "PREDICTION_READY"
        base["verdict"] = "已預測"
        base["conviction"] = max(
            float(row["conviction"]) for row in all_forecasts
        )
        base["prediction_source"] = (
            "crown_and_hkjc_full_market_no_vig"
            if forecasts and corner_forecasts
            else (
                "crown_full_market_no_vig"
                if forecasts
                else "hkjc_full_market_no_vig"
            )
        )
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
    if stage == "T-5" and (
        quote_source == _CROWN_BULK_ID3_SOURCE or cached_t5_fallback
    ):
        # T-5 stage persistence is time-critical.  A valid exact current bulk
        # Crown board is enough for the granular-condition portfolio; optional
        # per-fixture PinnAPI EV/corner calls can otherwise consume the entire
        # same-kickoff batch deadline.  Do not invent EV, Kelly, or a sharp
        # probability: defer those fields and retain only the real Crown
        # complete-market forecast/selected quote evidence.
        base["edge_reference_status"] = "deferred_t5_stage_priority"
        base["edge_reference_note"] = (
            "T-5 已以皇冠精確盤優先落盤；PinnAPI EV 參考延後，未計算 EV 或 Kelly。"
        )
        base["sharp_reference_available"] = False
        base["no_bet_reason"] = None
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
        return base
    prices = pinnapi["prices"]
    base.update(_wdl_prediction(prices))
    base["sharp_reference_available"] = True
    base["edge_reference_status"] = "available"
    base["edge_reference_note"] = None
    candidates, reasons = _candidates(crown, prices, config, now, bool(pinnapi["timestamp_inferred"]))
    for candidate in candidates:
        candidate["source"] = quote_source
        if cached_t5_fallback:
            candidate["quote_status"] = "cached_t5_fallback"
            candidate["quote_fallback_source"] = _CACHED_T5_FALLBACK_SOURCE
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
        # Exact PinnAPI same-line views supersede same-market no-vig forecasts
        # for learning quality.  Keep forecast-only markets that have no exact
        # reference instead of dropping them from prediction history.
        exact_codes = {str(row.get("code") or "") for row in candidates}
        base["forecast_candidates"] = candidates + [
            row for row in all_forecasts
            if str(row.get("code") or "") not in exact_codes
        ]
    base["lead_view"] = candidates[0] if candidates else None
    if not candidates:
        prefix = "已保留皇冠全盤預測；" if forecasts else ""
        base["no_bet_reason"] = prefix + "；".join(
            reasons + corner_reasons or ["Crown/PinnAPI 無可比較完整雙邊盤"]
        )
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
    base["pick"] = None
    if stage == "T-5":
        base["no_bet_reason"] = "此預測只供資訊；條件模擬倉只會以歷史已結算細緻條件判定。"
    else:
        base["no_bet_reason"] = f"{stage} 僅記錄資訊；條件模擬倉只在新 T-5 判定。"
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
    # A refresh is not a stage replay.  Keep a separate, visibly current
    # selected-quote journal for the dashboard rather than mutating the
    # immutable stage's market_predictions/learning payload.
    current_selected = []
    observed_board_at = refreshed["crown_quote_attempted_at"]
    selected_views = (
        refreshed.get("forecast_candidates")
        or refreshed.get("market_predictions")
        or []
    )
    for selected in selected_views:
        if not isinstance(selected, dict):
            continue
        code, side = selected.get("code"), selected.get("side")
        try:
            line = float(selected.get("line", selected.get("condition")))
        except (TypeError, ValueError):
            continue
        quote = next((
            row for row in merged_prices
            if row.get("market") == code
            and row.get("selection") == side
            and _line_key(str(code), row.get("line")) == _line_key(str(code), line)
        ), None)
        odds = quote.get("odds") if quote else None
        try:
            valid = float(odds) > 1
        except (TypeError, ValueError):
            valid = False
        current_selected.append({
            "code": code, "line": selected.get("line", selected.get("condition")),
            "side": side, "odds": odds if valid else None,
            "odds_status": "available" if valid else "missing",
            "reason": None if valid else "current_exact_quote_unavailable",
            "source": "titan007-crown-id-3", "provider": "Crown",
            # Titan may expose a provider price timestamp.  Separately retain
            # the exact board observation time of this refresh; neither value
            # is inferred from an older prediction-stage generation time.
            "observed_at": quote.get("source_at") if quote else observed_board_at,
            "observed_board_at": observed_board_at,
        })
    refreshed["current_selected_odds_journal"] = current_selected
    refreshed["current_odds_status"] = "available" if current_selected and all(
        item["odds_status"] == "available" for item in current_selected
    ) else "missing"
    refreshed["current_odds_reason"] = (
        None if refreshed["current_odds_status"] == "available"
        else "no_current_selected_quote"
    )
    refreshed["current_odds_refreshed_at"] = observed_board_at
    refreshed["current_odds_refresh_source"] = "titan007-crown-id-3"
    refreshed["crown_quote_stale_markets"] = stale_markets
    if not stale_markets:
        refreshed["crown_quote_refreshed_at"] = iso_hkt()
    if not book_odds["crown"]:
        refreshed["no_bet_reason"] = "皇冠公司盤口目前不可用；不顯示為皇冠有效賽事。"
    return refreshed


def _skip_new_confirmed_empty_crown(
    crown_snapshot: dict[str, Any] | None,
    previous: dict[str, Any] | None,
) -> bool:
    """Skip only a brand-new fixture whose two Crown markets are confirmed empty."""
    return bool(
        previous is None
        and crown_snapshot is not None
        and crown_snapshot.get("asian_ok")
        and crown_snapshot.get("total_ok")
        and not crown_snapshot.get("prices")
    )


def refresh_current_quotes(config: Settings) -> dict[str, Any]:
    """Refresh dashboard-only quote fields for known, not-yet-started cards.

    This command deliberately does not enter the matching, forecasting,
    ledger, learning, settlement, bet, or notification paths.  In particular,
    it cannot retrofit a current price into a completed historical stage.
    """
    now = datetime.now(HKT)
    current = load_predictions(config)
    titan = TitanClient(config)
    updates: list[dict[str, Any]] = []
    for card in current:
        kickoff = parse_time(card.get("kickoff_hkt") or card.get("kickoff"))
        match_id = str(card.get("titan_match_id") or card.get("match_id") or "")
        if not match_id or kickoff is None or kickoff <= now:
            continue
        row = {
            "id": match_id, "league": card.get("league") or "",
            "home": card.get("home") or "", "away": card.get("away") or "",
            "kickoff": kickoff,
        }
        try:
            snapshot = titan.crown_price_snapshot(match_id)
        except Exception:
            snapshot = {"prices": [], "asian_ok": False, "total_ok": False}
        updates.append(_refresh_crown_quote(card, row, titan, snapshot))
    with state_lock(config):
        retained = merge_predictions(config, updates, now=now)
    return {
        "ok": True, "mode": "refresh", "predictions": len(updates),
        "retained_predictions": len(retained), "simulations_created": 0,
        "fresh_t5_predictions": [], "safe_quote_refresh_only": True,
    }


def _commit_stage_predictions(
    config: Settings,
    mode: str,
    stage_predictions: list[dict[str, Any]],
) -> tuple[list[str], list[dict[str, str]], int]:
    """Commit a completed batch while holding the state lock only briefly."""
    if not stage_predictions:
        return [], [], len(load_predictions(config))
    with state_lock(config):
        # Reload after every small batch: another mode can have committed
        # independently while this provider process was working.
        ledger = load_ledger(config)
        emitted: list[str] = []
        fresh_condition_predictions: list[dict[str, str]] = []
        committed_predictions: list[dict[str, Any]] = []
        for prediction in stage_predictions:
            kickoff = datetime.fromisoformat(str(prediction["kickoff_hkt"]))
            if kickoff.tzinfo is None:
                kickoff = kickoff.replace(tzinfo=HKT)
            if kickoff <= datetime.now(HKT):
                # A request admitted before kickoff is no longer a valid
                # pre-kickoff observation if it returns after kickoff.
                continue
            stage = str(prediction.get("stage") or "")
            match_id = str(prediction.get("match_id") or "")
            prior_stage = any(
                row.get("stage") == stage
                for row in ((ledger.get("watch") or {}).get(match_id, {}).get("stages") or [])
                if isinstance(row, dict)
            )
            created = sync_prediction(ledger, prediction, config)
            emitted.extend(created)
            prediction["stages"] = list(
                ledger["watch"].get(match_id, {}).get("stages") or []
            )
            committed_predictions.append(prediction)
            if stage in {"T-30", "T-5"} and (
                not prior_stage or (stage == "T-5" and bool(created))
            ) and any(
                row.get("stage") == stage for row in prediction["stages"]
                if isinstance(row, dict)
            ):
                fresh_condition_predictions.append({"match_id": match_id, "stage": stage})
        recompute_stats(ledger, config)
        ledger["log"].append({
            "ts": iso_hkt(), "kind": mode, "n_changes": len(emitted),
            "changes": emitted or ["今次無模擬注動作"], "simulation_only": True,
        })
        ledger["log"] = ledger["log"][-100:]
        save_ledger(config, ledger)
        retained = merge_predictions(config, committed_predictions)
    return emitted, fresh_condition_predictions, len(retained)


def _local_bulk_t5_prediction(
    titan: dict[str, Any],
    config: Settings,
    crown_snapshot: dict[str, Any],
) -> dict[str, Any]:
    """Build a T-5 card from a persisted identity and current bulk Crown odds.

    This deliberately supplies no HKJC/PinnAPI bridge.  A validated current
    ID-3 bulk board makes the existing T-5 branch return before any optional
    reference provider is read.  The empty bridge is diagnostic only; it must
    never trigger fixture discovery or matching for this deadline-bound path.
    """
    no_provider_match = Match(
        None, False, 0.0, "deferred_for_local_bulk_t5"
    )
    prediction = _prediction(
        titan,
        BridgeMatch(
            no_provider_match,
            no_provider_match,
            "local_bulk_t5",
            "optional_providers_deferred_for_t5",
        ),
        None,
        "T-5",
        config,
        None,  # Valid bulk evidence prevents the existing direct-page branch.
        None,  # Valid bulk evidence prevents the existing PinnAPI branch.
        crown_snapshot,
    )
    # The ordinary prediction function retains a low-confidence WDL baseline
    # for incomplete provider passes.  This route has only Crown HDC/HIL
    # evidence, so do not represent that baseline as an independently observed
    # result probability.  Market no-vig forecasts remain tied to the exact
    # current two-sided Crown quotes, and EV/Kelly remain absent.
    prediction.update({
        "outcome": None,
        "forecast": None,
        "probability": None,
        "likely_score": None,
        "prediction_source": None,
    })
    prediction.pop("probabilities", None)
    prediction.pop("baseline_low_confidence", None)
    return prediction


def _run_local_bulk_t5(
    config: Settings,
    rows: list[dict[str, Any]],
    deadline: float,
) -> dict[str, Any]:
    """Persist all eligible locally due T-5 cards without slow discovery.

    Each usable fixture is committed before the next is processed.  Missing,
    malformed, or post-kickoff bulk rows remain due for a later tick; this
    path never falls through to per-fixture pages or optional providers.
    """
    titan_client = TitanClient(config)
    try:
        remaining = _deadline_remaining(deadline)
        snapshots = (
            titan_client.crown_bulk_price_snapshots(max_seconds=remaining)
            if remaining >= _MIN_DEADLINE_CALL_SECONDS else {}
        )
    except OSError:
        snapshots = {}
    if not isinstance(snapshots, dict):
        snapshots = {}

    stage_predictions: list[dict[str, Any]] = []
    retained = len(load_predictions(config))
    unavailable = 0
    for titan in rows:
        kickoff = parse_time(titan.get("kickoff"))
        snapshot = snapshots.get(str(titan.get("id") or ""))
        if (
            kickoff is None
            or not _valid_pre_kickoff_bulk_snapshot(snapshot, kickoff)
            or not snapshot
            or snapshot.get("quote_source") != _CROWN_BULK_ID3_SOURCE
        ):
            unavailable += 1
            continue
        stage_predictions.append(
            _local_bulk_t5_prediction(titan, config, snapshot)
        )

    # The commit helper already rejects a prediction that has crossed kickoff,
    # preserves stage idempotency, and evaluates each T-5 once. One batch
    # avoids repeating ledger read/recompute/write work for same-kickoff
    # fixtures while retaining those per-prediction protections.
    emitted, fresh_condition_predictions, retained = _commit_stage_predictions(
        config, "tick", stage_predictions
    )
    return {
        "ok": True,
        "mode": "tick",
        "fast_t5_bulk": True,
        "predictions": len(stage_predictions),
        "retained_predictions": retained,
        "simulations_created": len(emitted),
        "fresh_condition_predictions": fresh_condition_predictions,
        "fresh_t5_predictions": [
            item["match_id"] for item in fresh_condition_predictions
            if item["stage"] == "T-5"
        ],
        "bulk_unavailable_predictions": unavailable,
        "pinnapi_fixtures": 0,
        "hkjc_fixtures": 0,
        "deferred_predictions": unavailable,
        "failed_predictions": 0,
    }


def run(mode: str, config: Settings) -> dict[str, Any]:
    """Run a remote pass only when the explicit validation gate and PinnAPI key exist."""
    if mode not in {"tick", "sweep", "settle", "refresh"}:
        raise ValueError("mode must be tick, sweep, settle, or refresh")
    if not config.enabled:
        return {"ok": False, "reason": "CROWN_ENABLED=0; no network call was made"}
    if mode == "refresh":
        return refresh_current_quotes(config)
    if mode == "settle":
        if not config.pinnapi_configured:
            return {"ok": False, "reason": "PinnAPI credentials are not configured; no network call was made"}
        from .settle import settle_due
        # settle_due owns a separate long-running settlement lock and takes
        # state_lock only for its final merge.  Never hold the commit lock
        # while result providers are read: a T-5 commit is deadline-bound.
        return settle_due(config)
    ledger = load_ledger(config)
    existing_predictions = load_predictions(config)
    if mode == "tick":
        tick_deadline = time.monotonic() + _tick_pass_deadline_seconds()
        titan_rows = _tick_rows_from_predictions(
            existing_predictions, ledger, datetime.now(HKT)
        )
        if not titan_rows:
            return {
                "ok": True, "mode": mode, "fast_noop": True,
                "predictions": 0, "retained_predictions": len(existing_predictions),
                "simulations_created": 0,
            }
        # A due T-5 owns this tick.  Deferring recoverable T-30/first-look
        # work by one minute is safe; making a T-5 wait behind it is not.
        titan_rows = _prioritize_tick_rows(titan_rows)
        if any(row.get("_due_stage") == "T-5" for row in titan_rows):
            # This return is intentionally before PinnAPI credentials,
            # policy/model reads, fixture discovery, bridge mapping, and
            # per-fixture providers.  A stalled optional path cannot delay a
            # valid persisted T-5 Crown record.
            return _run_local_bulk_t5(config, titan_rows, tick_deadline)
        # This is deliberately measured before any provider work.  The
        # systemd 55-second limit remains the final safeguard for an upstream
        # fixture-list call that cannot be interrupted in-process.
    else:
        tick_deadline = None
    if not config.pinnapi_configured:
        return {"ok": False, "reason": "PinnAPI credentials are not configured; no network call was made"}
    # This can read the local learning store.  Keep it after the local tick
    # due check so a genuine no-op has no expensive work or provider calls.
    entry_policies = {
        code: market_entry_thresholds(ledger, code, config)
        for code in ("HDC", "HIL", "CHL")
    }
    titan_client, pinnapi_client = TitanClient(config), PinnapiClient(config)
    bulk_crown_quotes: dict[str, dict[str, Any]] = {}
    if mode == "tick":
        # A single company-ID-3 bulk read serves every due local card.  Do not
        # turn a transport/parser failure into a fabricated quote; the
        # per-fixture/cache paths below remain guarded fallbacks.
        try:
            remaining = _deadline_remaining(tick_deadline)
            fetched_bulk = (
                titan_client.crown_bulk_price_snapshots(max_seconds=remaining)
                if remaining >= _MIN_DEADLINE_CALL_SECONDS else {}
            )
            bulk_crown_quotes = fetched_bulk if isinstance(fetched_bulk, dict) else {}
        except OSError:
            bulk_crown_quotes = {}
    if mode == "sweep":
        titan_rows = _sweep_rows_with_due_existing(
            titan_client.fixtures(),
            existing_predictions,
            ledger,
            datetime.now(HKT),
        )
    pinnapi_fixture_status = "available"
    try:
        if mode == "tick":
            remaining = _deadline_remaining(tick_deadline)
            pinnapi_rows = (
                pinnapi_client.fixtures(max_seconds=remaining)
                if remaining >= _MIN_DEADLINE_CALL_SECONDS else []
            )
        else:
            pinnapi_rows = pinnapi_client.fixtures()
    except (OSError, ValueError, TypeError):
        # PinnAPI is an optional reference for bridge/EV work.  A transport
        # failure or malformed response must fail closed for that reference,
        # but must not abort Crown/Titan first-look discovery and persistence.
        pinnapi_rows = []
        pinnapi_fixture_status = "unavailable_fail_closed"
    hkjc_rows = (
        _fetch_tick_hkjc_matches(tick_deadline)
        if mode == "tick" else fetch_matches()
    )
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
        done = completed_stages(watch, MATCHING_VERSION, PREDICTION_ERA)
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
        crown_snapshot = (
            refresh_quotes.get(event.id)
            if mode == "sweep" else bulk_crown_quotes.get(event.id)
        )
        if mode == "tick" and not _valid_pre_kickoff_bulk_snapshot(
            crown_snapshot, event.kickoff
        ):
            # A malformed/in-play bulk row is not direct evidence.  Clear it
            # before cache selection so it cannot accidentally suppress the
            # strict saved-cache fallback or masquerade as a fresh quote.
            crown_snapshot = None
        if _skip_new_confirmed_empty_crown(crown_snapshot, previous):
            # The fixture exists on Titan's complete schedule but Crown has
            # neither supported market and has no earlier valid Crown quote.
            # Keep a brand-new empty fixture off the Crown board and avoid
            # unnecessary HKJC/PinnAPI matching until the next sweep.
            #
            # An existing card must continue into _prediction(): its last
            # valid Crown quote is forecast-only, while a current HKJC corner
            # market can still produce a CHL learning forecast.  Returning
            # here used to strand old first-look cards before corner handling.
            continue
        stage = stage_for(minutes, mode == "sweep", done)
        if not stage:
            continue
        if mode == "tick" and stage == "T-5" and crown_snapshot is None:
            # This is intentionally after stage selection: the narrow saved
            # cache can only suppress a per-fixture direct page for a due
            # native T-5, never for ordinary sweeps/earlier stages.
            crown_snapshot = _cached_t5_crown_snapshot(titan, previous)
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
    if mode == "tick":
        payloads = [
            (
                titan, bridge, h_row, stage, config, titan_client,
                pinnapi_client, crown_snapshot, previous_crown_prices,
                entry_policies,
            )
            for (
                titan, bridge, h_row, stage, crown_snapshot,
                previous_crown_prices,
            ) in pending_predictions
        ]
        emitted: list[str] = []
        fresh_condition_predictions: list[dict[str, str]] = []
        retained = len(existing_predictions)

        def commit_completed(prediction: dict[str, Any]) -> None:
            nonlocal retained
            created, fresh, retained = _commit_stage_predictions(
                config, mode, [prediction]
            )
            emitted.extend(created)
            fresh_condition_predictions.extend(fresh)

        runtime = _run_tick_predictions(
            payloads, tick_deadline if tick_deadline is not None else time.monotonic(),
            commit_completed,
        )
        return {
            "ok": True, "mode": mode, "predictions": runtime["completed"],
            "retained_predictions": retained, "simulations_created": len(emitted),
            "mapping": mapping, "pinnapi_fixtures": len(pinnapi_rows),
            "pinnapi_fixture_status": pinnapi_fixture_status,
            "titan_fixtures": len(titan_rows), "hkjc_fixtures": len(h_events),
            "fresh_condition_predictions": fresh_condition_predictions,
            "fresh_t5_predictions": [
                item["match_id"] for item in fresh_condition_predictions
                if item["stage"] == "T-5"
            ],
            "deadline_seconds": _tick_pass_deadline_seconds(),
            "deferred_predictions": runtime["deferred"],
            "failed_predictions": runtime["failed"],
        }
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
        # These are IDs only, collected after a T-5 snapshot has been newly
        # persisted.  The caller passes them to notifications; it never scans
        # old cards/history after a deploy.
        fresh_condition_predictions: list[dict[str, str]] = []
        for prediction in stage_predictions:
            kickoff = datetime.fromisoformat(str(prediction["kickoff_hkt"]))
            if kickoff.tzinfo is None:
                kickoff = kickoff.replace(tzinfo=HKT)
            if kickoff <= datetime.now(HKT):
                # A quote request admitted just before kickoff may return after
                # the match starts.  It must never become a T-5 bet.
                continue
            stage = str(prediction.get("stage") or "")
            match_id = str(prediction.get("match_id") or "")
            prior_stage = any(
                row.get("stage") == stage
                for row in ((ledger.get("watch") or {}).get(match_id, {}).get("stages") or [])
                if isinstance(row, dict)
            )
            emitted += sync_prediction(ledger, prediction, config)
            prediction["stages"] = list(
                ledger["watch"].get(str(prediction["match_id"]), {}).get("stages") or []
            )
            if stage in {"T-30", "T-5"} and (
                not prior_stage or (stage == "T-5" and bool(emitted))
            ) and any(
                row.get("stage") == stage for row in prediction["stages"]
                if isinstance(row, dict)
            ):
                fresh_condition_predictions.append({"match_id": match_id, "stage": stage})
        recompute_stats(ledger, config)
        ledger["log"].append({"ts": iso_hkt(), "kind": mode, "n_changes": len(emitted),
                              "changes": emitted or ["今次無模擬注動作"], "simulation_only": True})
        ledger["log"] = ledger["log"][-100:]
        save_ledger(config, ledger)
        retained = merge_predictions(config, predictions)
    return {"ok": True, "mode": mode, "predictions": len(predictions), "retained_predictions": len(retained),
            "simulations_created": len(emitted), "mapping": mapping,
            "pinnapi_fixtures": len(pinnapi_rows),
            "pinnapi_fixture_status": pinnapi_fixture_status,
            "titan_fixtures": len(titan_rows), "hkjc_fixtures": len(h_events),
            "fresh_condition_predictions": fresh_condition_predictions,
            # Compatibility only; notification dispatch uses the explicit
            # stage list above and never scans historical cards.
            "fresh_t5_predictions": [item["match_id"] for item in fresh_condition_predictions if item["stage"] == "T-5"]}
