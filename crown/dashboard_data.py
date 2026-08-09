"""Build the recovered Crown static dashboard's data.json without remote calls."""
from __future__ import annotations

import argparse
import os
from pathlib import Path
from typing import Any

from .common import iso_hkt, parse_time, read_json, write_json_atomic
from .config import Settings, settings
from .ledger import recompute_stats
from .period import is_upcoming_in_current_period
from .state import load_ledger, load_predictions, paths


def build(config: Settings) -> dict[str, Any]:
    ledger, all_matches = load_ledger(config), load_predictions(config)
    matches = [
        row for row in all_matches
        if (kickoff := parse_time(row.get("kickoff_hkt") or row.get("kickoff"))) is not None
        and is_upcoming_in_current_period(kickoff)
    ]
    prediction_history = read_json(
        config.state_dir / "prediction_history.json",
        {"rows": [], "stats": {}},
    )
    if not isinstance(prediction_history, dict):
        prediction_history = {"rows": [], "stats": {}}
    prediction_history.setdefault("rows", [])
    prediction_history.setdefault("stats", {})
    # A newly seeded ledger has no calculated stats yet; emit the complete
    # dashboard contract even before the first remote Crown pass.
    recompute_stats(ledger, config)
    stats = ledger.get("stats") or {}
    return {
        "schema_version": "crown-dashboard-v2", "generated_at": iso_hkt(), "title": "足破 · 皇冠賽事預測終端",
        "summary": {"crown_matches": len(matches), "hkjc_overlaps": sum(bool(row.get("hkjc_match_id")) for row in matches),
                    "predicted": sum(row.get("status") == "REFERENCE_READY" for row in matches),
                    "actionable": sum(bool(row.get("pick")) for row in matches),
                    "simulation_t5_picks": sum(bool(row.get("pick")) and row.get("stage") == "T-5" for row in matches),
                    "signal_data_missing": sum(row.get("status") == "DATA_MISSING" for row in matches)},
        "market_policy": {"model_HDC": "Titan007 Crown company ID=3", "model_HIL": "Titan007 Crown company ID=3",
                          "model_CHL": "HKJC only after strict same-event mapping", "sharp_reference": "PinnAPI Edge"},
        "signal_policy": {"mode": "simulation_only", "execution_enabled": True, "execution_mode": "simulation",
                          "real_betting_enabled": False, "baseline": "PinnAPI full-match exact-line no-vig",
                          "markets": {"HDC": "Titan007 Crown ID=3", "HIL": "Titan007 Crown ID=3",
                                      "CHL": "HKJC after strict team/kickoff match; no bet without sharp baseline"},
                          "stages": ["首預", "T-30", "T-5"], "confidence_floor": config.confidence_floor,
                          "minimum_ev": config.min_edge, "minimum_odds": 1.01},
        "matches": matches, "ledger": ledger,
        "prediction_history": prediction_history,
    }


def write_dashboard_data(config: Settings, out: Path | None = None) -> Path:
    """Atomically write a world-readable static artifact, never Crown state."""
    destination = out or config.web_root / "data.json"
    write_json_atomic(destination, build(config))
    # A root-owned runner replaces the file atomically; retain nginx readability
    # on every pass, not just setup/update.
    os.chmod(destination, 0o644)
    return destination


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--out", type=Path, default=None)
    args = parser.parse_args()
    config = settings()
    out = args.out or config.web_root / "data.json"
    print(f"wrote {write_dashboard_data(config, out)}")


if __name__ == "__main__":
    main()
