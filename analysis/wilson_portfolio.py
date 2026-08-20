"""Shared admission adapter for the Wilson simulation portfolio."""
from __future__ import annotations

import math
from datetime import datetime, timezone
from typing import Any, Callable, Iterable

from .granular_conditions import MARKETS, _role, match_upcoming
from .wilson_validation import (
    DECISION_STAGE, FIXED_STAKE, FIXTURE_MARKET_CAP, FIXTURE_STAKE_CAP,
    apply_active_evidence, commit_bet, ensure_namespace, matching_admissions,
    record_match_observation,
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


def _native_t5(watch: dict[str, Any], stage: dict[str, Any], parse_time: Callable[[Any], datetime | None]) -> bool:
    rows = [row for row in watch.get("stages") or []
            if isinstance(row, dict) and row.get("stage") == DECISION_STAGE]
    if len(rows) != 1 or rows[0] is not stage:
        return False
    if stage.get("post_hoc_backfill") or stage.get("exclude_from_simulation"):
        return False
    saved = _parse(stage.get("ts") or stage.get("source_snapshot_at"), parse_time)
    kickoff = _parse(stage.get("kickoff") or stage.get("kickoff_hkt") or watch.get("kickoff") or watch.get("kickoff_hkt"), parse_time)
    return saved is not None and kickoff is not None and saved < kickoff


def _audit_selection(market: str, row: dict[str, Any]) -> tuple[str | None, float | None, str]:
    side = str(row.get("side") or "").upper()
    line = _finite(row.get("line", row.get("condition")))
    selected_line = -line if market == "HDC" and side == "A" and line is not None else line
    role = _role(market, side, line)
    return role, selected_line, f"{role or '—'} {selected_line:g}" if selected_line is not None else (role or "—")


def evaluate(
    ledger: dict[str, Any], watch: dict[str, Any], *, system: str,
    market_labels: dict[str, str], parse_time: Callable[[Any], datetime | None],
    now: str, ranking: Iterable[dict[str, Any]] | None,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """Admission on one persisted native T-5 only; ranking is treated as frozen input."""
    ns = ensure_namespace(ledger, system, now=now)
    def rejected(reason: str) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
        row = {"market": "*", "status": "SKIPPED", "reason": reason}
        ns["audit"] = (ns.get("audit") or []) + [{"ts": now, "match_id": str(watch.get("match_id") or ""), **row}]
        ns["audit"] = ns["audit"][-1600:]
        return [], [row]

    fixture = str(watch.get("match_id") or "")
    current = next((row for row in watch.get("stages") or []
                    if isinstance(row, dict) and row.get("stage") == DECISION_STAGE), None)
    if not fixture or current is None or not _native_t5(watch, current, parse_time):
        return rejected("not_first_native_pre_kickoff_t5")
    if not all(str(watch.get(field) or "").strip() for field in ("league", "home", "away")):
        return rejected("missing_fixture_context_for_public_condition_bet")
    if ranking is None:
        return rejected("frozen_discovery_snapshot_unavailable")
    stage_at = str(current.get("ts") or current.get("source_snapshot_at") or now)
    # Match only conditions that the frozen discovery snapshot mapped to the
    # exact persisted line/side; no prospective result is ever an input here.
    current_rows = [{"match_id": fixture, "stage": current.get("stage"), "kickoff": watch.get("kickoff") or watch.get("kickoff_hkt"),
                     "predicted_at": stage_at, "market_predictions": current.get("market_predictions") or []}]
    matched = match_upcoming(current_rows, list(ranking), system=system, decision_stage=DECISION_STAGE).get(fixture, [])
    # A replay of an already committed T-5 must report its durable idempotency
    # outcome before consulting a later evidence-version boundary. It neither
    # creates a second bet nor re-evaluates old quote evidence.
    existing = [row for row in ledger.get("bets") or []
                if isinstance(row, dict) and row.get("portfolio") == f"{system}_wilson_test"
                and str(row.get("match_id") or "") == fixture]
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
        if any(str(row.get("code") or "") == market for row in existing):
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
            if adjusted["arithmetic"].get("passes"):
                accepted.append(adjusted)
                continue
            observation = record_match_observation(
                ledger, system, watch, market, selected, adjusted, now=now,
                market_label=market_labels[market], selected_role=role,
                selected_line=selected_line,
            )
            audit.append({
                "market": market, "status": "MATCHED_NO_BET",
                "reason": "wilson_gate_not_passed",
                "condition_number": frozen.get("condition_number"),
                "frozen_condition_signature": adjusted["signature"],
                "wilson_admission": adjusted["arithmetic"],
                "evidence_version": adjusted.get("evidence_version"),
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
                         selected_role=role, selected_line=selected_line)
        if bet is None:
            audit.append({"market": market, "status": "SKIPPED", "reason": "idempotent_existing_market"})
            continue
        existing.append(bet)
        created.append(bet)
        audit.append({"market": market, "status": "CREATED", "reason": "wilson_candidate_frozen",
                      "bet_id": bet["bet_id"], "frozen_condition_signature": bet["frozen_condition_signature"],
                      "condition_number": bet.get("condition_number"),
                      "wilson_admission": bet["wilson_admission"]})
        # The caller owns one atomic ledger append after all markets pass.
    ns["audit"] = (ns.get("audit") or []) + [{"ts": now, "match_id": fixture, **row} for row in audit]
    ns["audit"] = ns["audit"][-1600:]
    return created, audit
