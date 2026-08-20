"""Build the recovered Crown static dashboard's data.json without remote calls."""
from __future__ import annotations

import argparse
import hashlib
import json
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
from .state import load_ledger, load_predictions, paths, save_ledger
from analysis.wilson_validation import active_bets


# A full publish writes an immutable, content-addressed sidecar before data.
# If it is interrupted between those writes, the old data.json still points to
# its old sidecar rather than a mutable file from the next generation.
HISTORY_DATA_URL = "history.json"
HISTORY_ARTIFACT_SCHEMA = "crown-history-v1"


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
    active_condition_bets = active_bets(ledger, "crown")
    dashboard_ledger["bets"] = active_condition_bets
    wilson = ledger.get("wilson_validation") if isinstance(ledger.get("wilson_validation"), dict) else {}
    dashboard_ledger["independent_validation"] = {
        "schema_version": wilson.get("schema_version"),
        "validation_started_at": wilson.get("activation_at"),
        "activation_at": wilson.get("activation_at"),
        "cutover_at": wilson.get("cutover_at"),
        "display_name": "Wilson 測試攻略",
        "conditions": wilson.get("conditions") or {},
        "condition_order": wilson.get("condition_order") or [],
        "rollover": {
            "batch_size": 20,
            "conditions": {
                signature: {
                    "condition_number": row.get("condition_number"),
                    "active_evidence": row.get("active_evidence") or {},
                    "last_merged_batch": (row.get("rollover_audit") or [])[-1] if isinstance(row.get("rollover_audit"), list) and row.get("rollover_audit") else None,
                    "pending_progress": row.get("pending_rollover_progress") or {"eligible_decided": 0, "required": 20, "display": "0/20"},
                }
                for signature, row in (wilson.get("conditions") or {}).items()
                if isinstance(row, dict)
            },
        },
        "observations": [
            {
                key: value for key, value in row.items()
                if key in {
                    "match_id", "league", "home", "away", "kickoff", "market", "market_label",
                    "code", "side", "line", "selected_role", "selected_line", "odds", "stage",
                    "created_at", "condition_number", "bet_status", "no_bet_reason",
                    "frozen_condition_signature", "wilson_admission",
                    "evidence_version", "evidence_hash",
                }
            }
            for row in (wilson.get("observations") or [])
            if isinstance(row, dict) and row.get("formal_bet") is False
        ],
        "retired_v1": wilson.get("retired_v1") or {},
        "historical_discovery_archive": wilson.get("retired_v1") or {},
        "audit": wilson.get("audit") or [],
    }
    for key in (
        "shadow_bets", "shadow_stats", "shadow_comparison",
        "handicap_world", "handicap_world_audit", "handicap_world_stats",
        # v2 has a dedicated top-level dashboard contract.  Do not let its
        # research-only rows appear inside the historical v1 ledger projection.
        "crown_v2_challenger",
    ):
        dashboard_ledger.pop(key, None)
    return dashboard_ledger, active_condition_bets


def _wilson_match_projection(row: dict[str, Any], *, bet_status: str) -> dict[str, Any]:
    """Expose raw frozen admission values rather than presentation-rounding them."""
    arithmetic = row.get("wilson_admission") if isinstance(row.get("wilson_admission"), dict) else {}
    display = arithmetic.get("display") if isinstance(arithmetic.get("display"), dict) else {}
    return {
        "condition_number": row.get("condition_number"),
        "market": row.get("market") or row.get("code"),
        "market_label": row.get("market_label"),
        "selected_role": row.get("selected_role"),
        "selected_line": row.get("selected_line", row.get("line")),
        "odds": arithmetic.get("actual_decimal_odds_raw", row.get("odds")),
        "minimum_required_odds": arithmetic.get("minimum_acceptable_odds_raw"),
        "minimum_required_odds_display": display.get("minimum_acceptable_odds"),
        "evidence_version": row.get("evidence_version"),
        "evidence_hash": row.get("evidence_hash"),
        "bet_status": bet_status,
        "no_bet_reason": row.get("no_bet_reason") if bet_status != "BET" else None,
    }


def _attach_wilson_matches(matches: list[dict[str, Any]], ledger: dict[str, Any]) -> None:
    """Attach every formal/rejected match to its current card, never by UI index."""
    by_fixture: dict[str, list[dict[str, Any]]] = {}
    for row in active_bets(ledger, "crown"):
        match_id = str(row.get("match_id") or "")
        if match_id:
            by_fixture.setdefault(match_id, []).append(_wilson_match_projection(row, bet_status="BET"))
    wilson = ledger.get("wilson_validation") if isinstance(ledger.get("wilson_validation"), dict) else {}
    for row in wilson.get("observations") or []:
        if not isinstance(row, dict) or row.get("formal_bet") is not False:
            continue
        match_id = str(row.get("match_id") or "")
        if match_id:
            by_fixture.setdefault(match_id, []).append(_wilson_match_projection(row, bet_status="NO_BET_LOW_ODDS"))
    for values in by_fixture.values():
        values.sort(key=lambda item: (
            int(item.get("condition_number") or 10**9),
            str(item.get("market") or ""), str(item.get("selected_role") or ""),
        ))
    for match in matches:
        if isinstance(match, dict):
            match["wilson_matches"] = by_fixture.get(str(match.get("match_id") or ""), [])


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
    _attach_wilson_matches(projected_matches, current_ledger)
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
            else {"stats": {}}
        ),
        "history_data_url": payload.get("history_data_url") or HISTORY_DATA_URL,
        "history_data_version": payload.get("history_data_version"),
        "v2_challenger": (
            current_ledger.get("crown_v2_challenger")
            if isinstance(current_ledger.get("crown_v2_challenger"), dict)
            else None
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


def _history_version(prediction_history: dict[str, Any]) -> str:
    """Return a stable content marker for the separately published history."""
    encoded = json.dumps(
        prediction_history,
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()[:20]


def _history_data_url(version: str) -> str:
    return f"history-{version}.json"


def _build_payloads(config: Settings) -> tuple[dict[str, Any], dict[str, Any]]:
    """Build the lightweight dashboard and its complete history sidecar.

    History calculation remains exactly the same as before.  Only publication
    changes: the boot payload retains the aggregate contract while the row
    collection is moved to a separately fetched, versioned artifact.
    """
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
    # The browser's granular cards are discovery views, but the Wilson
    # threshold must use the separately persisted active evidence version.
    # Keep the raw rows untouched and replace only this dashboard projection.
    from analysis.wilson_validation import project_granular_ranking_evidence
    raw_ranking = (
        prediction_history["stats"].get("granular_conditions", {}).get("ranking") or []
    )
    projected_ranking = project_granular_ranking_evidence(
        ledger, "crown", raw_ranking, now=iso_hkt(),
    )
    # The initial full-cohort merge is a ledger migration, not a browser-only
    # presentation calculation. Persist it atomically with no provider work;
    # later dashboard reruns remain idempotent.
    save_ledger(config, ledger)
    if isinstance(prediction_history["stats"].get("granular_conditions"), dict):
        prediction_history["stats"]["granular_conditions"]["ranking"] = projected_ranking
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
    ranking = projected_ranking
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
    _attach_wilson_matches(matches, ledger)
    stats = ledger.get("stats") or {}
    # Project a condition-portfolio-only ledger for the browser.  Retired
    # portfolio keys may survive in old state until an explicit reset, but
    # cannot become dashboard data again.
    dashboard_ledger, active_condition_bets = _public_ledger(ledger)
    challenger_v2 = ledger.get("crown_v2_challenger")
    generated_at = iso_hkt()
    history_version = _history_version(prediction_history)
    history_data_url = _history_data_url(history_version)
    dashboard = {
        "schema_version": "crown-dashboard-v2", "generated_at": generated_at, "title": "足破 · 皇冠賽事預測終端",
        "summary": _summary(matches, active_condition_bets),
        "market_policy": {"model_HDC": "Titan007 Crown company ID=3", "model_HIL": "Titan007 Crown company ID=3",
                          "model_CHL": "HKJC角球大細 vs PinnAPI CHL exact line; never Crown odds",
                          "sharp_reference": "PinnAPI Edge"},
        "signal_policy": {"mode": "simulation_only", "execution_enabled": True, "execution_mode": "wilson_test",
                          "real_betting_enabled": False,
                          "strategy": "wilson-test-strategy-v1",
                          "entry_rule": "newly persisted native pre-kickoff T-5 only; frozen unique decided fixture-markets >=50; Wilson 95% lower bound >= break-even +3pp",
                          "fixed_stake": 500, "fixture_stake_cap": 1500, "fixture_market_cap": 3, "starting_bankroll": 50000,
                          "markets": {"HDC": "selected valid Crown/PinnAPI-backed snapshot", "HIL": "selected valid Crown/PinnAPI-backed snapshot",
                                      "CHL": "selected valid HKJC/PinnAPI-backed snapshot"},
                          "stages": ["首預", "T-30", "T-5"], "decision_stage": "T-5", "minimum_odds": 1.01},
        "matches": matches, "ledger": dashboard_ledger,
        "v2_challenger": challenger_v2 if isinstance(challenger_v2, dict) else None,
        # Keep all history aggregates in the fast boot contract.  Rows are
        # deliberately absent; the History view asks for the sidecar on
        # demand, avoiding a download and browser walk of unbounded history.
        "prediction_history": {"stats": prediction_history["stats"]},
        "history_data_url": history_data_url,
        "history_data_version": history_version,
        "stage_completeness": stage_completeness(matches, ledger),
    }
    history_artifact = {
        "schema_version": HISTORY_ARTIFACT_SCHEMA,
        "generated_at": generated_at,
        "history_data_version": history_version,
        "prediction_history": prediction_history,
    }
    return dashboard, history_artifact


def build(config: Settings) -> dict[str, Any]:
    """Build the lightweight boot payload without embedding history rows."""
    dashboard, _history_artifact = _build_payloads(config)
    return dashboard


def write_dashboard_data(config: Settings, out: Path | None = None) -> Path:
    """Atomically publish a lightweight dashboard and complete history sidecar."""
    destination = out or config.web_root / "data.json"
    dashboard, history_artifact = _build_payloads(config)
    history_destination = destination.with_name(str(dashboard["history_data_url"]))
    # Publish the sidecar first.  The browser verifies the version marker
    # before using it, so it never renders a mixed-generation history during
    # the short two-file replacement window.
    write_json_atomic(history_destination, history_artifact)
    os.chmod(history_destination, 0o644)
    write_json_atomic(destination, dashboard)
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
