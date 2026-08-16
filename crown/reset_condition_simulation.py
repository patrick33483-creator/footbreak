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
from analysis.independent_validation import ensure_namespace

CONFIRMATION = "RESET_CROWN_CONDITION_SIMULATION_50000"


def reset(confirmation: str) -> dict[str, object]:
    if confirmation != CONFIRMATION:
        raise ValueError("exact_confirmation_required")
    config = settings()
    with state_lock(config):
        ledger = load_ledger(config)
        namespace = ensure_namespace(ledger, "crown")
        save_ledger(config, ledger)
    write_dashboard_data(config)
    return {"ok": True, "bankroll": STARTING_BANKROLL, "cleared_main_bets": 0,
            "legacy_keys_removed": False, "migration_only": True,
            "validation_started_at": namespace["validation_started_at"]}


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--confirm", required=True)
    args = parser.parse_args()
    print(json.dumps(reset(args.confirm), sort_keys=True))

if __name__ == "__main__":
    main()
