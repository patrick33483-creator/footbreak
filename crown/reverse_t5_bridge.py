"""Post-commit Crown T-5 to HKJC public-board bridge.

This module is intentionally a sidecar.  It is started only after the native
Crown tick has durably committed and its normal bounded notification attempt
has completed.  It never participates in the native stage transaction.
"""
from __future__ import annotations

import multiprocessing
import os
import signal
from pathlib import Path
from typing import Any, Iterable

from .common import iso_hkt, parse_time, write_json_atomic
from .config import Settings
from .hkjc import event_from_match, fetch_matches, flatten_odds
from .matching import Event, same_event_for_hkjc
from .state import load_ledger, save_ledger, state_lock

_WORKER_TIMEOUT_SECONDS = 3.0
_REMOTE_TIMEOUT_SECONDS = 1.0
_HEALTH_FILE = "reverse-t5-bridge-health.json"
_MARKETS = ("HDC", "HIL", "CHL")


def _record_health(
    config: Settings, status: str, *, detail: str | None = None,
    fixtures: int | None = None, decisions: int | None = None,
) -> None:
    """Persist sidecar-only health; native stage state is never touched here."""
    try:
        write_json_atomic(Path(config.state_dir) / _HEALTH_FILE, {
            "at": iso_hkt(), "status": status, "detail": detail,
            "fixtures": fixtures, "decisions": decisions,
            "provider": "hkjc_public_board",
        })
    except BaseException:
        pass


def _number(value: Any) -> float | None:
    try:
        value = float(value)
    except (TypeError, ValueError):
        return None
    return value if value == value and value not in {float("inf"), float("-inf")} else None


def _board_quotes(match: dict[str, Any], *, observed_at: str) -> list[dict[str, Any]]:
    """Normalize all public-board rows without guessing side or Asian line."""
    match_id = str(match.get("id") or match.get("frontEndId") or "").strip()
    quotes: list[dict[str, Any]] = []
    for market in _MARKETS:
        for line in flatten_odds(match).get(market) or []:
            asian_line = _number(line.get("condition"))
            if asian_line is None:
                continue
            for side, odds in (line.get("odds") or {}).items():
                side = str(side or "").upper()
                if side not in ({"H", "A"} if market == "HDC" else {"H", "L"}):
                    continue
                quotes.append({
                    "code": market, "side": side, "line": asian_line, "odds": odds,
                    "source": "hkjc_public_board", "observed_at": observed_at,
                    "fixture_identity": {"hkjc_match_id": match_id},
                })
    return quotes


def _target(watch: dict[str, Any]) -> Event | None:
    kickoff = parse_time(watch.get("kickoff_hkt") or watch.get("kickoff"))
    fixture = str(watch.get("match_id") or "").strip()
    if not fixture or kickoff is None:
        return None
    return Event(
        fixture, str(watch.get("league") or ""), str(watch.get("home") or ""),
        str(watch.get("away") or ""), kickoff, None,
    )


def _t5_targets(ledger: dict[str, Any], fixture_ids: set[str]) -> Iterable[tuple[str, dict[str, Any]]]:
    for fixture in sorted(fixture_ids):
        watch = (ledger.get("watch") or {}).get(fixture)
        if not isinstance(watch, dict) or _target(watch) is None:
            continue
        yield fixture, watch


def collect_and_evaluate(config: Settings, fixture_ids: Iterable[str]) -> dict[str, int]:
    """Fetch once, strictly match, then commit only isolated bridge outcomes.

    Provider work occurs before the state lock.  Every candidate can fail
    closed into an idempotent research-only record; only an existing complete
    Crown/Footbreak-formal three-stage condition and one exact, fresh board
    quote can reach the pre-existing bilateral decision/outbox contract.
    """
    fixture_set = {str(value) for value in fixture_ids if str(value)}
    if not fixture_set:
        return {"fixtures": 0, "decisions": 0}
    previous_timeout = os.environ.get("FOOTBREAK_REMOTE_TIMEOUT_SECONDS")
    os.environ["FOOTBREAK_REMOTE_TIMEOUT_SECONDS"] = str(_REMOTE_TIMEOUT_SECONDS)
    try:
        matches = fetch_matches()
    finally:
        if previous_timeout is None:
            os.environ.pop("FOOTBREAK_REMOTE_TIMEOUT_SECONDS", None)
        else:
            os.environ["FOOTBREAK_REMOTE_TIMEOUT_SECONDS"] = previous_timeout
    events = [event_from_match(row) for row in matches if isinstance(row, dict)]
    events = [event for event in events if event is not None]
    observed_at = iso_hkt()
    decisions = 0
    processed = 0
    with state_lock(config, timeout_seconds=0.50) as acquired:
        if not acquired:
            return {"fixtures": 0, "decisions": 0}
        ledger = load_ledger(config)
        from . import hkjc_execution_test as reciprocal

        for fixture, watch in _t5_targets(ledger, fixture_set):
            processed += 1
            target = _target(watch)
            assert target is not None
            matched = same_event_for_hkjc(target, events)
            ns = reciprocal.ensure_namespace(ledger)
            if matched.event is None or matched.reversed:
                reciprocal.record_research_observation(
                    ns, fixture=fixture, market="*",
                    reason=f"hkjc_fixture_strict_identity_{matched.reason or 'unavailable'}",
                    hkjc_match_id=str(watch.get("hkjc_match_id") or "") or None,
                    captured_at=observed_at,
                )
                continue
            hkjc_id = str(matched.event.id)
            # A prior durable identity is immutable; a later board match may
            # confirm it but may never silently replace it.
            existing = str(watch.get("hkjc_match_id") or "").strip()
            if existing and existing != hkjc_id:
                reciprocal.record_research_observation(
                    ns, fixture=fixture, market="*",
                    reason="hkjc_fixture_identity_conflicts_with_durable_mapping",
                    hkjc_match_id=existing, captured_at=observed_at,
                )
                continue
            board = next(
                (row for row in matches if str(row.get("id") or row.get("frontEndId") or "") == hkjc_id),
                None,
            )
            if not isinstance(board, dict):
                reciprocal.record_research_observation(
                    ns, fixture=fixture, market="*", reason="hkjc_strict_match_raw_board_missing",
                    hkjc_match_id=hkjc_id, captured_at=observed_at,
                )
                continue
            # Preserve Crown's committed watch byte-for-byte.  The strict
            # public-board identity is carried into the isolated bilateral
            # record only; normal identity reconciliation owns any durable
            # card enrichment.
            evaluation_watch = {**watch, "hkjc_match_id": hkjc_id}
            created, _audit = reciprocal.evaluate_new_t5(
                ledger, evaluation_watch, ranking=[],
                counterpart_quotes=_board_quotes(board, observed_at=observed_at),
                counterpart_captured_at=observed_at,
                require_complete_history=True,
                record_native_observation=False,
            )
            decisions += len(created)
        if processed:
            save_ledger(config, ledger)
    return {"fixtures": processed, "decisions": decisions}


def _worker(config: Settings, fixture_ids: tuple[str, ...]) -> None:
    def timed_out(_signum, _frame) -> None:
        raise TimeoutError("reverse T-5 bridge timed out")

    timer_set = False
    try:
        if hasattr(signal, "SIGALRM"):
            signal.signal(signal.SIGALRM, timed_out)
            signal.setitimer(signal.ITIMER_REAL, _WORKER_TIMEOUT_SECONDS)
            timer_set = True
        result = collect_and_evaluate(config, fixture_ids)
        _record_health(config, "COMPLETED", **result)
    except BaseException as exc:
        _record_health(config, "FAILED", detail=type(exc).__name__)
    finally:
        if timer_set:
            signal.setitimer(signal.ITIMER_REAL, 0)


def schedule_reverse_t5_bridge(config: Settings, fresh_stages: Iterable[dict[str, Any]]) -> bool:
    """Start one best-effort child only for newly durable Crown T-5 snapshots."""
    if os.getenv("CROWN_REVERSE_T5_BRIDGE_ENABLED", "1").lower() in {"0", "false", "no", "off"}:
        return False
    fixtures = tuple(sorted({
        str(row.get("match_id") or "")
        for row in fresh_stages
        if isinstance(row, dict) and str(row.get("stage") or "") == "T-5"
        and str(row.get("match_id") or "")
    }))
    if not fixtures or os.name != "posix":
        return False
    try:
        _record_health(config, "LAUNCHED", fixtures=len(fixtures))
        context = multiprocessing.get_context("fork")
        child = context.Process(
            target=_worker, args=(config, fixtures), name="crown-reverse-t5-hkjc",
        )
        child.daemon = False
        child.start()
    except BaseException as exc:
        _record_health(config, "LAUNCH_FAILED", detail=type(exc).__name__, fixtures=len(fixtures))
        return False
    return True
