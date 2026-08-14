"""Explicit, manually invoked reset for the condition-driven Crown simulation."""
from __future__ import annotations

import argparse
import json
from pathlib import Path

from .condition_portfolio import STARTING_BANKROLL
from .config import settings
from .common import iso_hkt
from .dashboard_data import write_dashboard_data
from .state import load_ledger, save_ledger, state_lock

CONFIRMATION = "RESET_CROWN_CONDITION_SIMULATION_50000"


def reset(confirmation: str) -> dict[str, object]:
    if confirmation != CONFIRMATION:
        raise ValueError("exact_confirmation_required")
    config = settings()
    with state_lock(config):
        ledger = load_ledger(config)
        before = len(ledger.get("bets") or [])
        ledger["bankroll"] = STARTING_BANKROLL
        ledger["bets"] = []
        ledger["stats"] = {}
        # Delete retired portfolio state rather than preserving it for display
        # or settlement.  Readers remain tolerant of absent legacy keys.
        ledger.pop("shadow_bets", None)
        ledger.pop("shadow_stats", None)
        ledger.pop("shadow_comparison", None)
        ledger.pop("handicap_world", None)
        ledger.pop("handicap_world_audit", None)
        ledger.pop("handicap_world_stats", None)
        ledger.pop("condition_simulation_audit", None)
        ledger.setdefault("log", []).append({
            "ts": iso_hkt(),
            "action": "condition_simulation_reset",
            "cleared_bets": before,
        })
        save_ledger(config, ledger)
    write_dashboard_data(config)
    return {"ok": True, "bankroll": STARTING_BANKROLL, "cleared_main_bets": before, "legacy_keys_removed": True}


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--confirm", required=True)
    args = parser.parse_args()
    print(json.dumps(reset(args.confirm), sort_keys=True))

if __name__ == "__main__":
    main()
