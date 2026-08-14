"""Retired compatibility surface for the removed 讓球世界 portfolio.

This module deliberately has no signal, bet, or settlement implementation.
Old callers can import it during a rolling update, but the only former write
entrypoint is a no-op so it cannot recreate the retired namespace.
"""
from __future__ import annotations

from typing import Any


PORTFOLIO = "handicap_world"


def default_state() -> dict[str, Any]:
    """Provide a harmless read-only-shaped value for compatibility callers."""
    return {
        "schema_version": 1,
        "portfolio": PORTFOLIO,
        "retired": True,
        "bets": [],
        "audit": [],
        "stats": {},
    }


def ensure_state(ledger: dict[str, Any]) -> dict[str, Any]:
    """Never restore the retired namespace into a live ledger."""
    del ledger
    return default_state()


def record_new_t5(ledger: dict[str, Any], watch: dict[str, Any]) -> list[str]:
    """Former creation hook retained solely as a guaranteed no-op."""
    del ledger, watch
    return []
