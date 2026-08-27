"""Shared admission adapter for the Wilson simulation portfolio."""
from __future__ import annotations

import copy

import math
from datetime import datetime, timezone
from typing import Any, Callable, Iterable

from .granular_conditions import MARKETS, _role
from .wilson_validation import (
    CONDITION_AUDIT_LIMIT, DECISION_STAGE, FIXED_STAKE, FIXTURE_MARKET_CAP, FIXTURE_STAKE_CAP,
    _canonical_hash, _fixture_market_hash, apply_active_evidence, commit_bet,
    ensure_namespace, matching_admissions,
    record_match_observation, formal_registry_candidates, match_formal_registry,
)


def _finite(value: Any) -> float | None:
    try:
        value = float(value)
    except (TypeError, ValueError):
        return None
    return value if math.isfinite(value) else None


def _parse(value: Any, parse_time: Callable[[Any], datetime | None]) -> datetime | None:
    result = parse_time(value)
    if result is not None:
        return result if result.tzinfo is not None else result.replace(tzinfo=timezone.utc)
    numeric = _finite(value)
    if numeric is None or numeric <= 0:
        return None
    if numeric >= 10_000_000_000:
        numeric /= 1000
    try:
        return datetime.fromtimestamp(numeric, tz=timezone.utc)
    except (OSError, OverflowError, ValueError):
        return None


def _selected(
    stage: dict[str, Any], market: str, parse_time: Callable[[Any], datetime | None],
    *, fixture_kickoff: Any = None,
) -> tuple[dict[str, Any] | None, str | None]:
    rows = [row for row in stage.get("market_predictions") or []
            if isinstance(row, dict) and str(row.get("code") or "").upper() == market]
    if len(rows) != 1:
        return None, "selected_market_missing_or_ambiguous"
    row = rows[0]
    odds, line = _finite(row.get("odds")), _finite(row.get("line", row.get("condition")))
    side = str(row.get("side") or "").upper()
    if odds is None or odds <= 1:
        return None, "selected_odds_invalid_or_missing"
    if line is None or side not in ({"H", "A"} if market == "HDC" else {"H", "L"}):
        return None, "selected_line_or_side_invalid"
    source = str(row.get("quote_source") or row.get("source") or "").strip().lower()
    if not source or source in {"none", "fallback", "model_only", "model-only", "unavailable"}:
        return None, "selected_source_observation_invalid_or_missing"
    # Older persisted native snapshots store kickoff once on the fixture rather
    # than duplicating it on every stage.  That remains exact fixture
    # provenance; it is not a guessed clock value.
    kickoff = _parse(
        stage.get("kickoff") or stage.get("kickoff_hkt") or fixture_kickoff,
        parse_time,
    )
    observed = _parse(row.get("observed_at"), parse_time)
    if kickoff is None or observed is None or observed >= kickoff:
        return None, "selected_quote_not_provably_pre_kickoff"
    return row, None


def _native_stage(
    watch: dict[str, Any], stage: dict[str, Any],
    parse_time: Callable[[Any], datetime | None], decision_stage: str,
) -> bool:
    rows = [row for row in watch.get("stages") or []
            if isinstance(row, dict) and row.get("stage") == decision_stage]
    if len(rows) != 1 or rows[0] is not stage:
        return False
    if (
        stage.get("post_hoc_backfill") or stage.get("exclude_from_simulation")
        or stage.get("status") == "DATA_MISSING"
    ):
        return False
    saved = _parse(stage.get("ts") or stage.get("source_snapshot_at"), parse_time)
    kickoff = _parse(stage.get("kickoff") or stage.get("kickoff_hkt") or watch.get("kickoff") or watch.get("kickoff_hkt"), parse_time)
    return saved is not None and kickoff is not None and saved < kickoff


def _native_t5(
    watch: dict[str, Any], stage: dict[str, Any],
    parse_time: Callable[[Any], datetime | None],
) -> bool:
    """Compatibility wrapper for the unchanged native T-5 contract."""
    return _native_stage(watch, stage, parse_time, DECISION_STAGE)


def _native_match_rows(
    watch: dict[str, Any], parse_time: Callable[[Any], datetime | None], *,
    through_stage: str = DECISION_STAGE,
) -> list[dict[str, Any]]:
    """Return only immutable, pre-kickoff native market evidence by stage.

    The formal matcher needs the actual 首預→T-30→T-5 sequence, not a mutable
    current card or a later quote refresh.  Missing source/time evidence is
    omitted so a fine-grained frozen price trajectory fails closed.
    """
    kickoff = watch.get("kickoff") or watch.get("kickoff_hkt")
    fixture = str(watch.get("match_id") or "")
    kickoff_at = _parse(kickoff, parse_time)
    if not fixture or kickoff_at is None:
        return []
    result: list[dict[str, Any]] = []
    previous_saved: datetime | None = None
    stages = ("首預", "T-30", "T-5")
    if through_stage not in stages:
        return []
    for stage_name in stages[:stages.index(through_stage) + 1]:
        snapshots = [
            row for row in watch.get("stages") or []
            if isinstance(row, dict) and row.get("stage") == stage_name
        ]
        if len(snapshots) != 1:
            continue
        stage = snapshots[0]
        saved = _parse(stage.get("ts") or stage.get("source_snapshot_at"), parse_time)
        if (
            saved is None or saved >= kickoff_at
            or stage.get("status") == "DATA_MISSING"
            or stage.get("post_hoc_backfill")
            or stage.get("exclude_from_simulation")
        ):
            continue
        # A multi-stage trajectory is causal only when native commits are
        # strictly ordered in stage order. Reject the whole panel rather than
        # silently constructing a shorter path around corrupt chronology.
        if previous_saved is not None and saved <= previous_saved:
            return []
        selections = []
        for market in MARKETS:
            selected, _reason = _selected(
                stage, market, parse_time, fixture_kickoff=kickoff,
            )
            observed = (
                _parse(selected.get("observed_at"), parse_time)
                if selected is not None else None
            )
            if selected is not None and observed is not None and observed <= saved:
                selections.append(dict(selected))
        if not selections:
            continue
        previous_saved = saved
        result.append({
            "match_id": fixture, "stage": stage_name, "kickoff": kickoff,
            "predicted_at": str(stage.get("ts") or stage.get("source_snapshot_at")),
            "market_predictions": selections,
        })
    return result


def _audit_selection(market: str, row: dict[str, Any]) -> tuple[str | None, float | None, str]:
    side = str(row.get("side") or "").upper()
    line = _finite(row.get("line", row.get("condition")))
    selected_line = -line if market == "HDC" and side == "A" and line is not None else line
    role = _role(market, side, line)
    return role, selected_line, f"{role or '—'} {selected_line:g}" if selected_line is not None else (role or "—")


def _exact_match_binding(
    admission: dict[str, Any], *, system: str, fixture: str, market: str,
) -> dict[str, Any]:
    """Bounded diagnostic proof for a successful structural match."""
    decision_stage = str(
        (admission.get("definition") or {}).get("stage") or DECISION_STAGE
    )
    binding = {
        "schema_version": 1,
        "condition_signature": admission.get("signature"),
        "evidence_version": admission.get("evidence_version"),
        "evidence_hash": admission.get("evidence_hash"),
        "native_stage_at": admission.get("stage_at"),
        "definition_hash": _canonical_hash(admission.get("definition")),
        "fixture_market_hash": _fixture_market_hash(system, fixture, market),
    }
    if decision_stage != DECISION_STAGE:
        binding["schema_version"] = 2
        binding["decision_stage"] = decision_stage
    return binding


def evaluate_stage(
    ledger: dict[str, Any], watch: dict[str, Any], *, system: str,
    market_labels: dict[str, str], parse_time: Callable[[Any], datetime | None],
    now: str, ranking: Iterable[dict[str, Any]] | None, decision_stage: str,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """Evaluate one already-persisted native stage against its own identities."""
    from .legacy_batch_runtime import load_production_legacy_batch_authority
    authority_context = load_production_legacy_batch_authority(ledger)
    ns = ensure_namespace(ledger, system, now=now)
    def rejected(reason: str) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
        row = {"market": "*", "status": "SKIPPED", "reason": reason}
        ns["audit"] = (ns.get("audit") or []) + [{"ts": now, "match_id": str(watch.get("match_id") or ""), **row}]
        ns["audit"] = ns["audit"][-CONDITION_AUDIT_LIMIT:]
        return [], [row]

    fixture = str(watch.get("match_id") or "")
    current = next((row for row in watch.get("stages") or []
                    if isinstance(row, dict) and row.get("stage") == decision_stage), None)
    if (
        decision_stage not in {"首預", "T-30", "T-5"}
        or not fixture or current is None
        or not _native_stage(watch, current, parse_time, decision_stage)
    ):
        return rejected(
            "not_first_native_pre_kickoff_t5"
            if decision_stage == DECISION_STAGE
            else f"not_first_native_pre_kickoff_{decision_stage}"
        )
    if not all(str(watch.get(field) or "").strip() for field in ("league", "home", "away")):
        return rejected("missing_fixture_context_for_public_condition_bet")
    stage_at = str(current.get("ts") or current.get("source_snapshot_at") or now)
    # Formal admission is projected from the immutable condition registry,
    # not the mutable/re-ranked granular research cards.  ``ranking`` remains
    # an input for the one-time freeze performed by the caller, but can never
    # decide whether an already frozen condition exists.
    formal_candidates = formal_registry_candidates(
        ledger, system, now=now, authority_context=authority_context,
    )
    if not formal_candidates:
        # Keep the historic reason stable for callers which supplied an
        # initial discovery snapshot that yielded no valid formal condition.
        return rejected(
            "no_frozen_historical_condition"
            if ranking is not None else "formal_condition_registry_unavailable"
        )
    # Match all persisted native stages.  A frozen multi-stage tier trajectory
    # must be proven by its own pre-kickoff source timestamp at every stage;
    # only the Wilson execution decision reads the terminal selected price.
    current_rows = _native_match_rows(
        watch, parse_time, through_stage=decision_stage,
    )
    matched = match_formal_registry(
        current_rows, formal_candidates, system=system, decision_stage=decision_stage,
    ).get(fixture, [])
    # A replay of an already committed T-5 must report its durable idempotency
    # outcome before consulting a later evidence-version boundary. It neither
    # creates a second bet nor re-evaluates old quote evidence.
    existing = [row for row in ledger.get("bets") or []
                if isinstance(row, dict) and row.get("portfolio") == f"{system}_wilson_test"
                and str(row.get("match_id") or "") == fixture]
    existing_observations = [
        row for row in ((ns.get("observations") or []))
        if isinstance(row, dict) and str(row.get("match_id") or "") == fixture
        and str(row.get("stage") or "") == decision_stage
    ]
    proposed: list[tuple[str, dict[str, Any], dict[str, Any], str | None, float, str]] = []
    audit: list[dict[str, Any]] = []
    for market in MARKETS:
        selected, reason = _selected(
            current, market, parse_time,
            fixture_kickoff=watch.get("kickoff") or watch.get("kickoff_hkt"),
        )
        if selected is None:
            audit.append({"market": market, "status": "SKIPPED", "reason": reason})
            continue
        selected = copy.deepcopy(selected)
        if (
            current.get("formal_admission_snapshot_id")
            and current.get("formal_admission_snapshot_hash")
        ):
            selected["native_snapshot_binding"] = {
                "schema_version": 1,
                "system": system,
                "snapshot_id": current["formal_admission_snapshot_id"],
                "snapshot_hash": current["formal_admission_snapshot_hash"],
            }
        elif current.get("native_snapshot_id") and current.get("native_snapshot_hash"):
            selected["native_snapshot_binding"] = {
                "schema_version": 1,
                "system": system,
                "snapshot_id": current["native_snapshot_id"],
                "snapshot_hash": current["native_snapshot_hash"],
            }
        if (
            decision_stage == DECISION_STAGE
            and any(str(row.get("code") or "") == market for row in existing)
        ) or any(
            str(row.get("code") or row.get("market") or "") == market
            for row in existing_observations
        ):
            audit.append({
                "market": market, "status": "SKIPPED",
                "reason": "idempotent_existing_market",
            })
            continue
        admissions, reason = matching_admissions(system, market, selected, matched, stage_at=stage_at)
        if not admissions:
            audit.append({"market": market, "status": "SKIPPED", "reason": reason})
            continue
        role, selected_line, label = _audit_selection(market, selected)
        if selected_line is None:
            audit.append({"market": market, "status": "SKIPPED", "reason": "selected_line_or_side_invalid"})
            continue
        accepted = []
        for admission in admissions:
            adjusted, boundary_reason = apply_active_evidence(
                ledger, system, admission, stage_at=stage_at, now=now,
                authority_context=authority_context,
            )
            frozen = ns["conditions"].get(str(admission["signature"])) or {}
            if adjusted is None:
                audit.append({
                    "market": market, "status": "SKIPPED",
                    "reason": boundary_reason or "active_evidence_unavailable",
                    "condition_number": frozen.get("condition_number"),
                    "frozen_condition_signature": admission["signature"],
                })
                continue
            if decision_stage == DECISION_STAGE and adjusted["arithmetic"].get("passes"):
                accepted.append(adjusted)
                continue
            observation = record_match_observation(
                ledger, system, watch, market, selected, adjusted, now=now,
                market_label=market_labels[market], selected_role=role,
                selected_line=selected_line, decision_stage=decision_stage,
                authority_context=authority_context,
            )
            audit.append({
                "market": market, "status": "MATCHED_NO_BET",
                "reason": (
                    "wilson_gate_not_passed"
                    if decision_stage == DECISION_STAGE
                    else "early_stage_formal_observation"
                ),
                "decision_stage": decision_stage,
                "condition_number": frozen.get("condition_number"),
                "frozen_condition_signature": adjusted["signature"],
                "wilson_admission": adjusted["arithmetic"],
                "evidence_version": adjusted.get("evidence_version"),
                "exact_match_binding": _exact_match_binding(
                    adjusted, system=system, fixture=fixture, market=market,
                ),
                "observation_id": observation.get("observation_id") if observation else None,
            })
        if not accepted:
            continue
        admission = accepted[0]
        proposed.append((market, selected, admission, role, selected_line, label))
    # One candidate per market was selected above. Fixture caps are defensive
    # only; all three supported markets may be admitted for HK$1,500.
    created: list[dict[str, Any]] = []
    for market, selected, admission, role, selected_line, label in proposed:
        if any(str(row.get("code") or "") == market for row in existing):
            audit.append({"market": market, "status": "SKIPPED", "reason": "idempotent_existing_market"})
            continue
        if len(existing) >= FIXTURE_MARKET_CAP or sum(float(row.get("stake") or 0) for row in existing) + FIXED_STAKE > FIXTURE_STAKE_CAP:
            audit.append({"market": market, "status": "SKIPPED", "reason": "fixture_cap_reached"})
            continue
        bet = commit_bet(ledger, system, watch, market, selected, admission, now=now,
                         market_label=market_labels[market], selected_label=f"{market_labels[market]} · {label}",
                         selected_role=role, selected_line=selected_line,
                         authority_context=authority_context)
        if bet is None:
            audit.append({"market": market, "status": "SKIPPED", "reason": "idempotent_existing_market"})
            continue
        existing.append(bet)
        created.append(bet)
        audit.append({"market": market, "status": "CREATED", "reason": "wilson_candidate_frozen",
                      "bet_id": bet["bet_id"], "frozen_condition_signature": bet["frozen_condition_signature"],
                      "condition_number": bet.get("condition_number"),
                      "wilson_admission": bet["wilson_admission"],
                      "exact_match_binding": _exact_match_binding(
                          admission, system=system, fixture=fixture, market=market,
                      )})
        # The caller owns one atomic ledger append after all markets pass.
    ns["audit"] = (ns.get("audit") or []) + [{"ts": now, "match_id": fixture, **row} for row in audit]
    ns["audit"] = ns["audit"][-CONDITION_AUDIT_LIMIT:]
    return created, audit


def evaluate(
    ledger: dict[str, Any], watch: dict[str, Any], *, system: str,
    market_labels: dict[str, str], parse_time: Callable[[Any], datetime | None],
    now: str, ranking: Iterable[dict[str, Any]] | None,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """Compatibility wrapper preserving the existing T-5 evaluator."""
    return evaluate_stage(
        ledger, watch, system=system, market_labels=market_labels,
        parse_time=parse_time, now=now, ranking=ranking,
        decision_stage=DECISION_STAGE,
    )
