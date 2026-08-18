"""Build the recovered Crown static dashboard's data.json without remote calls."""
from __future__ import annotations

import argparse
import os
from datetime import datetime
from pathlib import Path
from typing import Any

from .common import HKT, iso_hkt, parse_time, read_json, write_json_atomic
from .config import Settings, settings
from .ledger import condition_bets, recompute_stats
from .period import in_current_period
from .prediction_history import normalize_history, project_watch_rows
from .ledger import PREDICTION_ERA
from .state import load_ledger, load_predictions, paths


def _dashboard_matches(config: Settings) -> list[dict[str, Any]]:
    """Return the persisted current-period Crown cards without remote work."""
    return [
        row for row in load_predictions(config)
        if (kickoff := parse_time(row.get("kickoff_hkt") or row.get("kickoff"))) is not None
        and in_current_period(kickoff)
        and bool((row.get("book_odds") or {}).get("crown"))
    ]


def _public_ledger(ledger: dict[str, Any]) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    """Return the browser-safe active portfolio projection of a Crown ledger."""
    dashboard_ledger = dict(ledger)
    active_condition_bets = condition_bets(ledger)
    dashboard_ledger["bets"] = active_condition_bets
    for key in (
        "shadow_bets", "shadow_stats", "shadow_comparison",
        "handicap_world", "handicap_world_audit", "handicap_world_stats",
    ):
        dashboard_ledger.pop(key, None)
    return dashboard_ledger, active_condition_bets


def _summary(matches: list[dict[str, Any]], active_condition_bets: list[dict[str, Any]]) -> dict[str, int]:
    return {
        "crown_matches": len(matches),
        "hkjc_overlaps": sum(bool(row.get("hkjc_match_id")) for row in matches),
        "predicted": sum(
            row.get("status") in {"PREDICTION_READY", "REFERENCE_READY", "SIMULATION_READY"}
            for row in matches
        ),
        "actionable": len(active_condition_bets),
        "simulation_t5_picks": sum(
            str(bet.get("stage") or "") == "T-5" for bet in active_condition_bets
        ),
        "signal_data_missing": sum(row.get("status") == "DATA_MISSING" for row in matches),
    }


def write_tick_dashboard_projection(
    config: Settings,
    ledger: dict[str, Any] | None = None,
    out: Path | None = None,
) -> Path:
    """Publish committed tick cards without history grading, mining, or remote reads.

    A normal dashboard rebuild derives history statistics and condition matches,
    which is intentionally deferred from the deadline-bound tick.  This small
    projection replaces only the current card/ledger view from already-persisted
    local state, so a native T-5 cannot remain invisible until a later sweep.
    """
    destination = out or config.web_root / "data.json"
    previous = read_json(destination, {})
    payload = previous if (
        isinstance(previous, dict)
        and previous.get("schema_version") == "crown-dashboard-v2"
    ) else {}
    current_ledger = ledger if isinstance(ledger, dict) else load_ledger(config)
    matches = _dashboard_matches(config)
    old_matches = {
        str(row.get("match_id") or ""): row
        for row in (payload.get("matches") or [])
        if isinstance(row, dict) and row.get("match_id")
    }
    # Preserve dashboard-only fields such as the last condition-ranking match,
    # then replace every persisted stage/card field with the committed state.
    projected_matches = [
        (dict(old_matches.get(str(row.get("match_id") or ""), {})) | row)
        for row in matches
    ]
    dashboard_ledger, active_condition_bets = _public_ledger(current_ledger)
    payload.update({
        "schema_version": "crown-dashboard-v2",
        "generated_at": iso_hkt(),
        "title": payload.get("title") or "足破 · 皇冠賽事預測終端",
        "summary": _summary(projected_matches, active_condition_bets),
        "matches": projected_matches,
        "ledger": dashboard_ledger,
        # The fast archive already writes raw history rows.  Do not parse,
        # normalize, grade, calculate statistics, or mine conditions here.
        "prediction_history": (
            payload.get("prediction_history")
            if isinstance(payload.get("prediction_history"), dict)
            else {"rows": [], "stats": {}}
        ),
        "stage_completeness": stage_completeness(projected_matches, current_ledger),
    })
    write_json_atomic(destination, payload)
    os.chmod(destination, 0o644)
    return destination


def stage_completeness(
    matches: list[dict[str, Any]],
    ledger: dict[str, Any],
    *,
    now: datetime | None = None,
) -> dict[str, Any]:
    """Summarise stage coverage once per Crown fixture.

    T-30 and T-5 become overdue only after their write windows have closed.
    This prevents a fixture that has not reached a stage deadline from
    depressing the completeness rate.  DATA_MISSING attempts remain incomplete
    because the scheduler deliberately keeps those stages eligible for retry.
    """
    checked_at = now or datetime.now(HKT)
    if checked_at.tzinfo is None:
        checked_at = checked_at.replace(tzinfo=HKT)
    else:
        checked_at = checked_at.astimezone(HKT)

    watches = ledger.get("watch") if isinstance(ledger.get("watch"), dict) else {}
    unique_matches: dict[str, dict[str, Any]] = {}
    for index, match in enumerate(matches):
        if not isinstance(match, dict):
            continue
        match_id = str(match.get("match_id") or "").strip()
        key = match_id or "|".join(
            str(match.get(field) or "").strip()
            for field in ("kickoff_hkt", "home", "away", "league")
        )
        if not key:
            key = f"unidentified:{index}"
        unique_matches.setdefault(key, match)

    stages = {
        stage: {
            "recorded": 0,
            "due": 0,
            "missing_due": 0,
            "not_due": 0,
            # A recorded stage and its price evidence are separate facts.
            # This lets operations distinguish scheduler starvation from an
            # explicit, auditable provider quote failure.
            "odds_available": 0,
            "odds_missing": 0,
            "odds_unobserved": 0,
            "completeness": None,
        }
        for stage in ("首預", "T-30", "T-5")
    }
    incomplete_fixtures: set[str] = set()
    for fixture_key, match in unique_matches.items():
        match_id = str(match.get("match_id") or "").strip()
        watch = watches.get(match_id) if match_id else None
        watch = watch if isinstance(watch, dict) else {}
        stage_rows = {
            str(row.get("stage")): row
            for row in (watch.get("stages") or [])
            if (
                isinstance(row, dict)
                and row.get("stage") in stages
                and row.get("status") != "DATA_MISSING"
            )
        }
        kickoff = parse_time(match.get("kickoff_hkt") or match.get("kickoff"))
        minutes_to_kickoff = (
            (kickoff - checked_at).total_seconds() / 60
            if kickoff is not None else None
        )
        due = {
            "首預": True,
            # The T-30 write window is 40 to 20 minutes before kickoff.
            "T-30": minutes_to_kickoff is None or minutes_to_kickoff < 20,
            # The T-5 write window remains open until kickoff.
            "T-5": minutes_to_kickoff is None or minutes_to_kickoff <= 0,
        }
        for stage, metric in stages.items():
            if stage in stage_rows:
                metric["recorded"] += 1
                metric["due"] += 1
                snapshot = stage_rows[stage]
                if snapshot.get("odds_status") == "available":
                    metric["odds_available"] += 1
                elif snapshot.get("odds_status") == "missing":
                    metric["odds_missing"] += 1
                else:
                    metric["odds_unobserved"] += 1
            elif due[stage]:
                metric["due"] += 1
                metric["missing_due"] += 1
                incomplete_fixtures.add(fixture_key)
            else:
                metric["not_due"] += 1

    for metric in stages.values():
        metric["completeness"] = (
            metric["recorded"] / metric["due"]
            if metric["due"] else None
        )
    return {
        "sample_basis": "unique_crown_fixtures_in_current_period",
        "checked_at": checked_at.isoformat(),
        "fixtures_total": len(unique_matches),
        "fixtures_with_overdue_stage": len(incomplete_fixtures),
        "healthy": not incomplete_fixtures,
        "stages": stages,
    }


def build(config: Settings) -> dict[str, Any]:
    ledger = load_ledger(config)
    matches = _dashboard_matches(config)
    prediction_history = read_json(
        config.state_dir / "prediction_history.json",
        {"rows": [], "stats": {}},
    )
    if not isinstance(prediction_history, dict):
        prediction_history = {"rows": [], "stats": {}}
    prediction_history.setdefault("rows", [])
    prediction_history.setdefault("stats", {})
    normalize_history(prediction_history)
    # The private recovery overlay is applied only to this dashboard copy.
    # It never modifies the persisted prediction history or source ledger.
    from analysis.odds_recovery import overlay_rows
    from .prediction_history import calculate_stats
    prediction_history["rows"] = overlay_rows(
        project_watch_rows(prediction_history["rows"], ledger), "crown",
    )
    prediction_history["stats"] = calculate_stats(
        prediction_history["rows"], comparable_era=PREDICTION_ERA,
    )
    # Card-level matches are derived from persisted immutable snapshots, not
    # from a live quote refresh.  T-30 is limited inside ``match_upcoming`` to
    # first/T-30 paths and therefore cannot borrow a future T-5 observation.
    from analysis.granular_conditions import match_upcoming
    current_rows = [
        {
            "match_id": str(match.get("match_id") or ""),
            "stage": stage.get("stage"),
            "kickoff": match.get("kickoff_hkt") or match.get("kickoff"),
            "predicted_at": stage.get("ts") or stage.get("source_snapshot_at"),
            "market_predictions": stage.get("market_predictions") or [],
        }
        for match in matches if isinstance(match, dict)
        for stage in (match.get("stages") or []) if isinstance(stage, dict)
    ]
    ranking = (
        prediction_history["stats"].get("granular_conditions", {}).get("ranking") or []
    )
    by_stage = {
        stage: match_upcoming(current_rows, ranking, system="crown", decision_stage=stage)
        for stage in ("T-30", "T-5")
    }
    for match in matches:
        match["condition_matches"] = (
            by_stage.get(str(match.get("stage") or ""), {}).get(str(match.get("match_id") or ""), [])
        )
    # A newly seeded ledger has no calculated stats yet; emit the complete
    # dashboard contract even before the first remote Crown pass.
    recompute_stats(ledger, config)
    stats = ledger.get("stats") or {}
    # Project a condition-portfolio-only ledger for the browser.  Retired
    # portfolio keys may survive in old state until an explicit reset, but
    # cannot become dashboard data again.
    dashboard_ledger, active_condition_bets = _public_ledger(ledger)
    return {
        "schema_version": "crown-dashboard-v2", "generated_at": iso_hkt(), "title": "足破 · 皇冠賽事預測終端",
        "summary": _summary(matches, active_condition_bets),
        "market_policy": {"model_HDC": "Titan007 Crown company ID=3", "model_HIL": "Titan007 Crown company ID=3",
                          "model_CHL": "HKJC角球大細 vs PinnAPI CHL exact line; never Crown odds",
                          "sharp_reference": "PinnAPI Edge"},
        "signal_policy": {"mode": "simulation_only", "execution_enabled": True, "execution_mode": "independent_validation",
                          "real_betting_enabled": False,
                          "strategy": "independent-validation-v1",
                          "entry_rule": "newly persisted native pre-kickoff T-5 only; frozen historical discovery accuracy >60% and decided >=20",
                          "fixed_stake": 250, "fixture_stake_cap": 500, "fixture_market_cap": 2, "starting_bankroll": 50000,
                          "markets": {"HDC": "selected valid Crown/PinnAPI-backed snapshot", "HIL": "selected valid Crown/PinnAPI-backed snapshot",
                                      "CHL": "selected valid HKJC/PinnAPI-backed snapshot"},
                          "stages": ["首預", "T-30", "T-5"], "decision_stage": "T-5", "minimum_odds": 1.01},
        "matches": matches, "ledger": dashboard_ledger,
        "prediction_history": prediction_history,
        "stage_completeness": stage_completeness(matches, ledger),
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
