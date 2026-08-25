"""Build the recovered Crown static dashboard's data.json without remote calls."""
from __future__ import annotations

import argparse
import contextlib
import copy
import fcntl
import hashlib
import json
import os
from datetime import datetime
from pathlib import Path
from typing import Any

from .common import HKT, iso_hkt, parse_time, read_json, write_json_atomic
from .config import Settings, settings
from .ledger import STAGES, condition_bets, recompute_stats
from .period import in_current_period
from .prediction_history import normalize_history, project_watch_rows
from .ledger import PREDICTION_ERA
from .state import load_ledger, load_predictions, paths
from analysis.wilson_validation import (
    active_bets, project_dashboard_research_matches, project_frozen_ranking_evidence,
)


# A full publish writes an immutable, content-addressed sidecar before data.
# If it is interrupted between those writes, the old data.json still points to
# its old sidecar rather than a mutable file from the next generation.
HISTORY_DATA_URL = "history.json"
HISTORY_ARTIFACT_SCHEMA = "crown-history-v1"


@contextlib.contextmanager
def _dashboard_publish_lock(destination: Path):
    """Serialize only final public-snapshot replacement across Crown writers.

    Native stages commit to the durable ledger before either publisher runs.
    A full sweep may have begun deriving history from an older read while a
    deadline-bound tick commits and publishes T-5.  Without a common final
    publication lock, that older full writer can atomically replace the newer
    public snapshot after the tick.  The lock deliberately protects neither
    provider work nor history derivation; it protects the small final
    authoritative rebase and atomic replacement only.
    """
    lock_path = destination.with_name(f".{destination.name}.publish.lock")
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    with lock_path.open("a+", encoding="utf-8") as handle:
        fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(handle.fileno(), fcntl.LOCK_UN)


def _dashboard_watch_card(watch: dict[str, Any]) -> dict[str, Any] | None:
    """Recover one current-period card from a committed first-look watch.

    A watch is eligible only after ``sync_prediction`` has persisted an
    identity-complete first-look snapshot.  This deliberately excludes empty
    scheduler shells and malformed/ambiguous stage state while allowing the
    public board to recover when the replaceable ``predictions.json`` cache is
    stale or incomplete.
    """
    if not isinstance(watch, dict):
        return None
    rows = watch.get("stages")
    first_looks = [
        row for row in rows if isinstance(row, dict) and row.get("stage") == "首預"
    ] if isinstance(rows, list) else []
    if len(first_looks) != 1:
        return None
    first_look_seed = first_looks[0]
    top_match_id = str(watch.get("match_id") or "").strip()
    stage_match_id = str(first_look_seed.get("match_id") or "").strip()
    if top_match_id and stage_match_id and top_match_id != stage_match_id:
        return None
    match_id = top_match_id or stage_match_id
    top_kickoff_raw = watch.get("kickoff_hkt") or watch.get("kickoff")
    stage_kickoff_raw = (
        first_look_seed.get("kickoff_hkt") or first_look_seed.get("kickoff")
    )
    top_kickoff = parse_time(top_kickoff_raw)
    stage_kickoff = parse_time(stage_kickoff_raw)
    if top_kickoff is not None and stage_kickoff is not None and top_kickoff != stage_kickoff:
        return None
    kickoff_raw = top_kickoff_raw or stage_kickoff_raw
    kickoff = top_kickoff or stage_kickoff
    if not match_id or kickoff is None or not in_current_period(kickoff):
        return None
    identity = {
        "match_id": match_id,
        "league": watch.get("league") or first_look_seed.get("league"),
        "home": watch.get("home") or first_look_seed.get("home"),
        "away": watch.get("away") or first_look_seed.get("away"),
        "kickoff_hkt": kickoff_raw,
    }
    if any(not str(identity.get(key) or "").strip() for key in ("league", "home", "away")):
        return None
    # Legacy watches can predate top-level identity denormalisation.  Validate
    # against a local identity-complete copy without mutating durable state.
    identity_watch = copy.deepcopy(watch)
    identity_watch["match_id"] = match_id
    identity_watch["kickoff_hkt"] = kickoff_raw
    snapshots = _native_watch_stages(identity, identity_watch)
    first_look = snapshots.get("首預")
    if first_look is None or not str(first_look.get("status") or "").strip():
        return None

    latest_stage = max(snapshots, key=STAGES.__getitem__)
    card = copy.deepcopy(snapshots[latest_stage])
    for key, value in watch.items():
        if key != "stages" and value is not None:
            card[key] = copy.deepcopy(value)
    card.update(identity)
    card["stage"] = latest_stage
    card["status"] = snapshots[latest_stage].get("status")
    card["stages"] = [
        snapshots[stage] for stage in STAGES if stage in snapshots
    ]
    if not isinstance(card.get("book_odds"), dict):
        card["book_odds"] = {"crown": []}
    card.setdefault("current_odds_status", "missing")
    card.setdefault(
        "current_odds_reason",
        "durable_first_look_projection_without_current_quote",
    )
    return card


def _dashboard_matches(
    config: Settings,
    ledger: dict[str, Any] | None = None,
) -> list[dict[str, Any]]:
    """Return every committed current-period Crown card without remote work.

    A Crown quote is stage evidence, not fixture-discovery eligibility.  Cards
    whose current quote is unavailable must stay on the work board so their
    immutable first-look/T-30/T-5 state remains visible and the browser can
    render its existing "odds unavailable" treatment.  ``predictions.json`` is
    the rich cache; an identity-complete durable watch with a committed first
    look is the safe recovery source when that cache omits a fixture.
    """
    cards = [
        row for row in load_predictions(config)
        if (kickoff := parse_time(row.get("kickoff_hkt") or row.get("kickoff"))) is not None
        and in_current_period(kickoff)
    ]
    known = {
        str(row.get("match_id") or "").strip()
        for row in cards if isinstance(row, dict)
    }
    watches = ledger.get("watch") if isinstance(ledger, dict) else None
    if not isinstance(watches, dict):
        return cards
    for watch in watches.values():
        recovered = _dashboard_watch_card(watch)
        if recovered is None or recovered["match_id"] in known:
            continue
        cards.append(recovered)
        known.add(recovered["match_id"])
    return cards


def _native_watch_stages(
    card: dict[str, Any],
    watch: dict[str, Any],
) -> dict[str, dict[str, Any]]:
    """Return only verifiable native snapshots for one identity-matched card.

    The watch ledger is authoritative for native stage completion, but it is
    accepted only when the supplied card carries the same immutable identity
    and kickoff.  Timed stages additionally need their persisted pre-kickoff
    timestamp and status.  Bad state is ignored rather than repaired or
    replayed into the public board.
    """
    match_id = str(card.get("match_id") or "").strip()
    if not match_id or str(watch.get("match_id") or "").strip() != match_id:
        return {}
    card_kickoff = parse_time(card.get("kickoff_hkt") or card.get("kickoff"))
    watch_kickoff = parse_time(watch.get("kickoff_hkt") or watch.get("kickoff"))
    if card_kickoff is None or watch_kickoff is None or card_kickoff != watch_kickoff:
        return {}
    rows = watch.get("stages")
    if not isinstance(rows, list):
        return {}
    snapshots: dict[str, dict[str, Any]] = {}
    for row in rows:
        if not isinstance(row, dict):
            continue
        stage = str(row.get("stage") or "")
        if stage not in STAGES:
            continue
        # A native timed snapshot must carry the immutable observation facts
        # established by ``sync_prediction``.  In particular, never publish a
        # stage recorded after kickoff as if it had been a pre-kickoff decision.
        if stage in {"T-30", "T-5"}:
            observed_at = parse_time(row.get("ts"))
            if (
                observed_at is None
                or observed_at >= card_kickoff
                or not str(row.get("status") or "").strip()
            ):
                continue
        # One identity can have only one native stage.  Ambiguous authoritative
        # state fails closed instead of choosing a row or duplicating a card.
        if stage in snapshots:
            return {}
        snapshots[stage] = copy.deepcopy(row)
    return snapshots


def _project_authoritative_watch_stages(
    cards: list[dict[str, Any]],
    ledger: dict[str, Any],
) -> list[dict[str, Any]]:
    """Overlay durable native stages onto stale cards without changing state.

    ``predictions.json`` supplies rich cards while ``ledger.watch`` supplies
    committed stage snapshots and can recover a missing first-look card before
    this function runs.  This narrow, pure projection closes the interval where
    their atomic writes were interrupted or a fast-noop tick retained an older
    card.  It never creates a stage, history row, bet, or provider request.
    """
    watches = ledger.get("watch") if isinstance(ledger, dict) else None
    if not isinstance(watches, dict):
        return [copy.deepcopy(card) for card in cards if isinstance(card, dict)]
    projected: list[dict[str, Any]] = []
    for card in cards:
        if not isinstance(card, dict):
            continue
        output = copy.deepcopy(card)
        match_id = str(output.get("match_id") or "").strip()
        watch = watches.get(match_id)
        snapshots = _native_watch_stages(output, watch) if isinstance(watch, dict) else {}
        if not snapshots:
            projected.append(output)
            continue

        # Preserve non-stage/dashboard fields and the existing order, but make
        # every native identity unique.  The authoritative copy replaces a
        # stale same-stage card row; missing stages are appended in native
        # order.  No source document is mutated.
        stages = output.get("stages")
        merged_stages: list[dict[str, Any]] = []
        native_positions: dict[str, int] = {}
        for row in stages if isinstance(stages, list) else []:
            if not isinstance(row, dict):
                continue
            stage = str(row.get("stage") or "")
            if stage in STAGES:
                if stage in native_positions:
                    continue
                native_positions[stage] = len(merged_stages)
            merged_stages.append(copy.deepcopy(row))
        for stage in STAGES:
            snapshot = snapshots.get(stage)
            if snapshot is None:
                continue
            position = native_positions.get(stage)
            if position is None:
                native_positions[stage] = len(merged_stages)
                merged_stages.append(snapshot)
            else:
                merged_stages[position] = snapshot
        merged_stages.sort(
            key=lambda row: (
                0 if str(row.get("stage") or "") in STAGES else 1,
                STAGES.get(str(row.get("stage") or ""), len(STAGES) + 1),
            )
        )
        output["stages"] = merged_stages
        available = [
            str(row.get("stage") or "")
            for row in merged_stages
            if str(row.get("stage") or "") in STAGES
        ]
        latest = max(available, key=STAGES.__getitem__)
        if latest in snapshots and "status" in snapshots[latest]:
            output["stage"] = latest
            output["status"] = snapshots[latest]["status"]
        projected.append(output)
    return projected


def _public_ledger(ledger: dict[str, Any]) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    """Return the browser-safe active portfolio projection of a Crown ledger."""
    # Never shallow-copy the authoritative ledger into the browser payload.
    # In particular, ``watch`` contains the durable per-fixture stage journal
    # and can grow to tens of megabytes.  The dashboard reads only this
    # explicit, bounded projection.
    raw_log = ledger.get("log") if isinstance(ledger.get("log"), list) else []
    dashboard_ledger = {
        "bankroll": ledger.get("bankroll"),
        "stats": ledger.get("stats") if isinstance(ledger.get("stats"), dict) else {},
        "log": raw_log[-200:],
    }
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
        # No-bet condition observations are evidence-only: outcome status is
        # visible, but they never enter the isolated simulation PnL ledger.
        "observations": [
            {
                key: value for key, value in row.items()
                if key in {
                    "match_id", "league", "home", "away", "kickoff", "market", "market_label",
                    "code", "side", "line", "selected_role", "selected_line", "odds", "stage",
                    "created_at", "condition_number", "bet_status", "no_bet_reason",
                    "frozen_condition_signature", "wilson_admission",
                    "evidence_version", "evidence_hash",
                    # Evidence-only settlement is public/auditable while
                    # remaining entirely outside bets, stake, and PnL.
                    "status", "result", "settled_at", "settlement_source", "void_reason",
                }
            }
            for row in (wilson.get("observations") or [])
            if isinstance(row, dict) and row.get("formal_bet") is False
        ],
        "retired_v1": wilson.get("retired_v1") or {},
        "historical_discovery_archive": wilson.get("retired_v1") or {},
        "audit": wilson.get("audit") or [],
    }
    cross = ledger.get("crown_hkjc_execution_test")
    if isinstance(cross, dict):
        from analysis.bilateral_decision import public_decision
        visible = {
            "bet_id", "portfolio", "strategy", "league", "home", "away", "kickoff",
            "market", "market_label", "side", "line", "selected_role", "selected_line",
            "crown_signal_odds", "crown_signal_observed_at", "hkjc_execution_odds",
            "hkjc_execution_observed_at", "stake", "status", "condition_number",
            "wilson_admission", "result", "pnl", "settled_at", "score",
        }
        dashboard_ledger["hkjc_execution_test"] = {
            "display_name": "皇冠×馬會執行測試倉（模擬）",
            "bets": [{key: value for key, value in row.items() if key in visible}
                     for row in (cross.get("bets") or []) if isinstance(row, dict)],
            "stats": cross.get("stats") or {},
            "decisions": [public_decision(row) for row in (cross.get("decisions") or [])
                          if isinstance(row, dict)],
        }
    return dashboard_ledger, active_condition_bets


def _wilson_match_projection(row: dict[str, Any], *, bet_status: str) -> dict[str, Any]:
    """Expose raw frozen admission values rather than presentation-rounding them."""
    arithmetic = row.get("wilson_admission") if isinstance(row.get("wilson_admission"), dict) else {}
    display = arithmetic.get("display") if isinstance(arithmetic.get("display"), dict) else {}
    return {
        # This is the only dashboard match shape that has passed the native
        # exact-side/line/source/frozen-evidence admission and is therefore
        # allowed to use the formal condition number in the UI or Telegram.
        "match_class": "authoritative_admission",
        "authoritative": True,
        "notification_eligible": True,
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


def _rebase_live_dashboard_payload(
    payload: dict[str, Any],
    config: Settings,
    ledger: dict[str, Any],
    *,
    clear_research_matches: bool,
) -> dict[str, Any]:
    """Rebase one public payload onto the latest committed native ledger state.

    This is intentionally local and read-only.  It never invokes a provider,
    creates stages, or changes lifecycle evidence.  Rich prediction cards are
    retained, while an identity-complete durable first-look watch can recover a
    card omitted by the replaceable cache.  ``generated_at`` is set only after
    this latest durable-state read has been incorporated.
    """
    existing = {
        str(row.get("match_id") or ""): row
        for row in (payload.get("matches") or [])
        if isinstance(row, dict) and row.get("match_id")
    }
    seeded = []
    for card in _dashboard_matches(config, ledger):
        old = dict(existing.get(str(card.get("match_id") or ""), {}))
        if clear_research_matches:
            old.pop("condition_matches", None)
            old.pop("research_matches", None)
        seeded.append(old | card)
    matches = _project_authoritative_watch_stages(seeded, ledger)
    _attach_wilson_matches(matches, ledger)
    if clear_research_matches:
        for row in matches:
            if isinstance(row, dict):
                row["condition_matches"] = []
    dashboard_ledger, active_condition_bets = _public_ledger(ledger)
    payload.update({
        "schema_version": "crown-dashboard-v2",
        # This value must describe the authoritative state used for the
        # public replacement, not the beginning of an earlier full build.
        "generated_at": iso_hkt(),
        "title": payload.get("title") or "足破 · 皇冠賽事預測終端",
        "summary": _summary(matches, active_condition_bets),
        "matches": matches,
        "ledger": dashboard_ledger,
        "v2_challenger": (
            ledger.get("crown_v2_challenger")
            if isinstance(ledger.get("crown_v2_challenger"), dict)
            else None
        ),
        "stage_completeness": stage_completeness(matches, ledger),
    })
    return payload


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
    with _dashboard_publish_lock(destination):
        previous = read_json(destination, {})
        payload = previous if (
            isinstance(previous, dict)
            and previous.get("schema_version") == "crown-dashboard-v2"
        ) else {}
        # Do not trust a child-process copy that could predate a just-committed
        # native T-5 while waiting for this publication lock.
        current_ledger = load_ledger(config)
        payload = _rebase_live_dashboard_payload(
            payload, config, current_ledger, clear_research_matches=True,
        )
        # The fast archive already writes raw history rows.  Do not parse,
        # normalize, grade, calculate statistics, or mine conditions here.
        payload["prediction_history"] = (
            payload.get("prediction_history")
            if isinstance(payload.get("prediction_history"), dict)
            else {"stats": {}}
        )
        payload["history_data_url"] = payload.get("history_data_url") or HISTORY_DATA_URL
        payload["history_data_version"] = payload.get("history_data_version")
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
    matches = _project_authoritative_watch_stages(
        _dashboard_matches(config, ledger), ledger,
    )
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
    raw_ranking = (
        prediction_history["stats"].get("granular_conditions", {}).get("ranking") or []
    )
    # A dashboard is a strictly read-only consumer of the authoritative
    # prediction/evidence chain.  In particular, it must never register a
    # formal condition, initialize an evidence version, or write the ledger
    # that a concurrent native T-5 is committing.
    projected_ranking = project_frozen_ranking_evidence(
        ledger, "crown", raw_ranking,
    )
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
        match["condition_matches"] = project_dashboard_research_matches(
            by_stage.get(
                str(match.get("stage") or ""),
                {},
            ).get(str(match.get("match_id") or ""), [])
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
    with _dashboard_publish_lock(destination):
        # A full build can spend seconds normalizing local history.  Rebase its
        # final public state after it wins the lock so it cannot overwrite a
        # later native T-5 projection produced by the tick process.
        latest_ledger = load_ledger(config)
        dashboard = _rebase_live_dashboard_payload(
            dashboard, config, latest_ledger, clear_research_matches=False,
        )
        write_json_atomic(destination, dashboard)
        # A root-owned runner replaces the file atomically; retain nginx
        # readability on every pass, not just setup/update.
        os.chmod(destination, 0o644)
    # Keep a short overlap window for browsers that loaded the previous boot
    # payload, while bounding derived immutable sidecars. The authoritative
    # prediction_history.json is never a cleanup target.
    from system.disk_guard import prune_crown_history_sidecars
    prune_crown_history_sidecars(destination.parent)
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
