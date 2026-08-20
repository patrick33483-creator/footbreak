"""Separate Crown state, intentionally outside Footbreak's system/ state."""
from __future__ import annotations

import fcntl
import time
from contextlib import contextmanager
from datetime import datetime
from pathlib import Path
from typing import Any

from .common import HKT, parse_time, read_json, write_json_atomic
from .config import Settings
from .period import in_current_period


@contextmanager
def state_lock(config: Settings, *, timeout_seconds: float | None = None):
    """Serialize only Crown state commits, never slow provider reads.

    Deadline-bound callers may request a finite wait.  Returning ``False``
    means a concurrent short commit did not release the lock in time; callers
    must leave their work retryable rather than letting a tick wait through
    the service limit.
    """
    config.state_dir.mkdir(parents=True, exist_ok=True)
    lock_path = config.state_dir / ".state.lock"
    with lock_path.open("a+", encoding="utf-8") as handle:
        acquired = False
        if timeout_seconds is None:
            fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
            acquired = True
        else:
            deadline = time.monotonic() + max(0.0, timeout_seconds)
            while True:
                try:
                    fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                    acquired = True
                    break
                except BlockingIOError:
                    if time.monotonic() >= deadline:
                        yield False
                        return
                    time.sleep(min(0.02, max(0.0, deadline - time.monotonic())))
        try:
            yield acquired
        finally:
            if acquired:
                fcntl.flock(handle.fileno(), fcntl.LOCK_UN)


@contextmanager
def settlement_lock(config: Settings):
    """Serialize settlement passes without holding the short state commit lock.

    A result lookup can take minutes.  It must not prevent the time-critical
    T-5 worker from persisting an independently prepared pre-kickoff stage.
    The final settlement merge still uses ``state_lock``.
    """
    config.state_dir.mkdir(parents=True, exist_ok=True)
    lock_path = config.state_dir / ".settlement.lock"
    with lock_path.open("a+", encoding="utf-8") as handle:
        try:
            fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            yield False
            return
        try:
            yield True
        finally:
            fcntl.flock(handle.fileno(), fcntl.LOCK_UN)


@contextmanager
def notification_lock(config: Settings):
    """Serialize Telegram delivery without blocking prediction state commits.

    Delivery is best-effort and retryable.  If another mode is already
    delivering, the caller skips this pass instead of waiting inside a
    deadline-bound tick.
    """
    config.state_dir.mkdir(parents=True, exist_ok=True)
    lock_path = config.state_dir / ".notification.lock"
    with lock_path.open("a+", encoding="utf-8") as handle:
        try:
            fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            yield False
            return
        try:
            yield True
        finally:
            fcntl.flock(handle.fileno(), fcntl.LOCK_UN)


def paths(config: Settings) -> dict[str, Path]:
    return {
        "ledger": config.state_dir / "ledger.json",
        "predictions": config.state_dir / "predictions.json",
        # This is a deliberately narrow, local-only handoff for Footbreak's
        # T-5 cross-book execution adapter.  It is not a dashboard payload and
        # contains no provider response, credential, or unselected board data.
        "footbreak_execution_evidence": (
            config.state_dir / "footbreak-execution-evidence.json"
        ),
        "notify": config.state_dir / "notify_state.json",
        "health": config.state_dir / "health.json",
        "live": config.state_dir / "pinnapi_live.json",
    }


def default_ledger(bankroll: float) -> dict[str, Any]:
    return {
        "bankroll": bankroll, "bets": [], "watch": {}, "log": [], "stats": {},
    }


def load_ledger(config: Settings) -> dict[str, Any]:
    data = read_json(paths(config)["ledger"], default_ledger(config.bankroll))
    data.setdefault("bankroll", config.bankroll)
    data.setdefault("bets", [])
    data.setdefault("watch", {})
    data.setdefault("log", [])
    data.setdefault("stats", {})
    return data


def save_ledger(config: Settings, data: dict[str, Any]) -> None:
    write_json_atomic(paths(config)["ledger"], data)


def load_predictions(config: Settings) -> list[dict[str, Any]]:
    value = read_json(paths(config)["predictions"], [])
    return value if isinstance(value, list) else []


def save_predictions(config: Settings, data: list[dict[str, Any]]) -> None:
    write_json_atomic(paths(config)["predictions"], data)
    # Publish only after the native card persistence is durable.  The
    # counterpart will either observe the complete former sidecar or this
    # complete replacement; it never reads a copied/partially-written file.
    write_json_atomic(
        paths(config)["footbreak_execution_evidence"],
        _footbreak_execution_evidence(data),
    )


def _footbreak_execution_evidence(data: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Project persisted Crown selected quotes into a bounded local handoff.

    Keep duplicates intact: the Footbreak consumer must reject ambiguous
    fixture identities rather than silently selecting a preferred card.  The
    full Crown prediction card remains authoritative; this artifact is only a
    local, exact-quote view for the reciprocal execution venue.
    """
    cards: list[dict[str, Any]] = []
    quote_keys = (
        "code", "line", "condition", "side", "odds", "odds_status", "reason",
        "source", "provider", "observed_at", "observed_board_at",
    )
    for card in data:
        if not isinstance(card, dict):
            continue
        hkjc_match_id = str(card.get("hkjc_match_id") or "").strip()
        kickoff = card.get("kickoff_hkt") or card.get("kickoff")
        if not hkjc_match_id or kickoff is None:
            continue
        # Cross-book execution is permitted only from Crown's immutable native
        # T-5 snapshot, never from a later dashboard quote refresh.  The
        # commit path attaches the just-persisted stage snapshots before
        # `save_predictions()` is called.
        t5_rows = [
            row for row in (card.get("stages") or [])
            if isinstance(row, dict) and row.get("stage") == "T-5"
        ]
        journal = (
            t5_rows[0].get("selected_odds_journal")
            if len(t5_rows) == 1 else None
        )
        rows = [
            {key: row.get(key) for key in quote_keys if key in row}
            for row in journal if isinstance(row, dict)
        ] if isinstance(journal, list) else None
        projected = {
            "match_id": card.get("match_id") or card.get("titan_match_id"),
            "hkjc_match_id": hkjc_match_id,
            "kickoff_hkt": kickoff,
        }
        if rows is not None:
            # The consumer's historical name remains for compatibility, but
            # its values are exclusively the immutable Crown T-5 selection.
            projected["current_selected_odds_journal"] = rows
        cards.append(projected)
        # The active-board prediction writer already retains only the current
        # period.  A hard cap still bounds a damaged/unexpected local payload;
        # omitted evidence fails closed at the reader.
        if len(cards) >= 400:
            break
    return cards


def _prediction_time(prediction: dict[str, Any]) -> datetime | None:
    return parse_time(prediction.get("kickoff_hkt") or prediction.get("kickoff"))


def _prediction_is_useful(prediction: dict[str, Any], now: datetime) -> bool:
    """Keep every card in the active 12:00-to-11:59 board period."""
    kickoff = _prediction_time(prediction)
    return kickoff is not None and in_current_period(kickoff, now)


def merge_predictions(
    config: Settings,
    updates: list[dict[str, Any]],
    now: datetime | None = None,
) -> list[dict[str, Any]]:
    """Merge new stage snapshots by match ID instead of replacing the card.

    Updates are idempotent: the same match ID replaces its prior current card,
    and an empty update list simply keeps useful cards already on disk.  Invalid
    or safely stale records are pruned only after the retention window.
    """
    now = (now or datetime.now(HKT)).astimezone(HKT)
    merged: dict[str, dict[str, Any]] = {}
    for row in load_predictions(config):
        match_id = str(row.get("match_id") or "")
        if match_id and _prediction_is_useful(row, now):
            merged[match_id] = row
    for row in updates:
        match_id = str(row.get("match_id") or "")
        if match_id and _prediction_is_useful(row, now):
            previous = merged.get(match_id)
            if row.get("_quote_refresh_only") and previous is not None:
                quote_fields = {
                    "league", "home", "away", "kickoff_hkt", "mins_to_ko",
                    "generated_at", "source_snapshot_at",
                    "crown_quote_attempted_at", "crown_quote_refreshed_at",
                    "crown_quote_stale_markets", "book_odds",
                    "current_selected_odds_journal", "current_odds_status",
                    "current_odds_reason",
                }
                refreshed = dict(previous)
                refreshed.update({
                    key: value for key, value in row.items()
                    if key in quote_fields
                })
                merged[match_id] = refreshed
                continue
            # A stage update replaces the current card, but retains any
            # dashboard-only fields absent from a thinner later snapshot.
            merged[match_id] = (previous or {}) | {
                key: value for key, value in row.items()
                if key != "_quote_refresh_only"
            }
    output = list(merged.values())
    output.sort(key=lambda row: (_prediction_time(row) or now, str(row.get("match_id") or "")))
    save_predictions(config, output)
    return output
