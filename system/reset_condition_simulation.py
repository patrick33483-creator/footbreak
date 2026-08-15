"""Explicit, manually invoked reset for Footbreak's condition simulation only.

This script intentionally has no automatic caller.  It preserves prediction,
accuracy, learning, result, and provider data; it resets only simulation ledger
state after the new code has been deployed.
"""
from __future__ import annotations

import argparse
import fcntl
import json
import os
import tempfile
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from condition_portfolio import LOG_LIMIT, STARTING_BANKROLL

HERE = Path(__file__).resolve().parent
LEDGER = Path(os.environ.get("FOOTBREAK_LEDGER_PATH", HERE / "sim_ledger.json"))
LOCK = Path(os.environ.get("FOOTBREAK_RESET_LOCK_PATH", f"{LEDGER}.lock"))
CONFIRMATION = "RESET_FOOTBREAK_CONDITION_SIMULATION_50000"
POST_DEPLOY_CONFIRMATION = "FOOTBREAK_CONDITION_SIMULATION_DEPLOYED"
HKT = timezone(timedelta(hours=8))


@contextmanager
def ledger_lock():
    """Serialize a manual reset with local ledger writers."""
    LOCK.parent.mkdir(parents=True, exist_ok=True)
    with LOCK.open("a+", encoding="utf-8") as handle:
        fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(handle.fileno(), fcntl.LOCK_UN)


def _read_ledger() -> dict[str, Any]:
    if not LEDGER.exists():
        return {"bankroll": STARTING_BANKROLL, "bets": [], "stats": {}, "log": [], "watch": {}}
    with LEDGER.open(encoding="utf-8") as handle:
        payload = json.load(handle)
    return payload if isinstance(payload, dict) else {"bankroll": STARTING_BANKROLL, "bets": [], "stats": {}, "log": [], "watch": {}}


def _save_ledger(payload: dict[str, Any]) -> None:
    LEDGER.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary = tempfile.mkstemp(prefix=".condition-reset-", dir=LEDGER.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(payload, handle, ensure_ascii=False, indent=1)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, LEDGER)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def reset(confirmation: str, post_deploy_confirmation: str | None = None) -> dict[str, object]:
    """Clear only Footbreak simulation state after exact manual confirmation."""
    if confirmation != CONFIRMATION:
        raise ValueError("exact_confirmation_required")
    if post_deploy_confirmation != POST_DEPLOY_CONFIRMATION:
        raise ValueError("post_deploy_confirmation_required")
    with ledger_lock():
        ledger = _read_ledger()
        cleared_main_bets = len(ledger.get("bets") or [])
        retired_present = any(
            key in ledger for key in ("shadow_bets", "shadow_stats", "shadow_comparison")
        )
        ledger["bankroll"] = STARTING_BANKROLL
        ledger["bets"] = []
        ledger["stats"] = {}
        # These keys were simulation-only; no predictions, historical grades,
        # learning DB, result cache, or provider material is changed.
        for key in (
            "shadow_bets", "shadow_stats", "shadow_comparison",
            "condition_simulation_audit",
        ):
            ledger.pop(key, None)
        ledger["log"] = [{
            "ts": datetime.now(HKT).isoformat(timespec="seconds"),
            "kind": "條件模擬倉重設",
            "cleared_main_bets": cleared_main_bets,
        }][-LOG_LIMIT:]
        _save_ledger(ledger)

    # The writer uses an atomic replacement. It reads existing local data only.
    from gen_app_data import main as regenerate_dashboard
    regenerate_dashboard()
    return {
        "ok": True,
        "bankroll": STARTING_BANKROLL,
        "cleared_main_bets": cleared_main_bets,
        "retired_shadow_state_removed": retired_present,
    }


def main() -> None:
    parser = argparse.ArgumentParser(description="Guarded Footbreak condition simulation reset")
    parser.add_argument("--confirm", required=True)
    parser.add_argument("--post-deploy", required=True)
    arguments = parser.parse_args()
    # Aggregate-only: never print ledger rows, teams, fixtures, paths, or state.
    print(json.dumps(
        reset(arguments.confirm, arguments.post_deploy),
        ensure_ascii=False,
        sort_keys=True,
    ))


if __name__ == "__main__":
    main()
