"""Footbreak Wilson 測試攻略 simulation adapter (v1 is retired)."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Iterable

from analysis.granular_conditions import MARKET_LABELS, mine
from analysis.wilson_portfolio import _native_t5 as _wilson_native_t5
from analysis.wilson_portfolio import _selected as _wilson_selected
from analysis.wilson_portfolio import _audit_selection as _wilson_audit_selection
from analysis.wilson_portfolio import evaluate_stage as _evaluate_stage
from analysis.wilson_validation import (
    DECISION_STAGE, DISPLAY_NAME, FIXED_STAKE, FIXTURE_MARKET_CAP,
    FIXTURE_STAKE_CAP, STARTING_BANKROLL, STRATEGY, portfolio_name,
    project_granular_ranking_evidence,
)

SYSTEM = "footbreak"
PORTFOLIO = portfolio_name(SYSTEM)
HKT = timezone(timedelta(hours=8))
AUDIT_LIMIT = 1600
LOG_LIMIT = 100


def iso_hkt() -> str:
    return datetime.now(HKT).isoformat(timespec="seconds")


def parse_time(value: Any) -> datetime | None:
    try:
        answer = datetime.fromisoformat(str(value or "").replace("Z", "+00:00"))
    except (TypeError, ValueError):
        return None
    return answer.replace(tzinfo=HKT) if answer.tzinfo is None else answer


def historical_rows_from_accuracy(history_path: Path) -> list[dict[str, Any]]:
    # Kept for offline migration/tests only. Production uses its persisted
    # frozen ranking artifact and never mines inside the T-5 admission path.
    import json
    try:
        payload = json.loads(history_path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return []
    return [
        {"match_id": match.get("match_id"), "kickoff": match.get("kickoff"),
         "predicted_at": stage.get("predicted_at"), "stage": stage.get("stage"),
         "market_grades": stage.get("market_grades") or []}
        for match in payload.get("matches") or [] if isinstance(match, dict)
        for stage in match.get("stages") or [] if isinstance(stage, dict)
    ]


def _native_t5(stage: dict[str, Any], kickoff: Any) -> bool:
    """Compatibility helper for isolated probability research, never admission."""
    watch = {"stages": [stage], "kickoff": kickoff}
    return _wilson_native_t5(watch, stage, parse_time)


def _valid_selected(stage: dict[str, Any], market: str) -> tuple[dict[str, Any] | None, str | None]:
    """Compatibility helper with the same strict provenance gates as Wilson."""
    return _wilson_selected(stage, market, parse_time)


def _audit_selection(market: str, row: dict[str, Any]) -> dict[str, Any]:
    """Return the public selection fields consumed by research-only code."""
    role, selected_line, label = _wilson_audit_selection(market, row)
    return {
        "selected_side": row.get("side"), "selected_line": selected_line,
        "selected_role": role, "selected_label": f"{MARKET_LABELS.get(market, market)} · {label}",
    }


def _live_rows(watch: dict[str, Any]) -> list[dict[str, Any]]:
    """Project persisted rows for the research matcher; no new history is mined."""
    return [{
        "match_id": watch.get("match_id"), "kickoff": watch.get("kickoff") or watch.get("kickoff_hkt"),
        "stage": stage.get("stage"), "predicted_at": stage.get("ts") or stage.get("source_snapshot_at"),
        "market_predictions": stage.get("market_predictions") or [],
    } for stage in watch.get("stages") or [] if isinstance(stage, dict)]


def evaluate_new_t5(
    ledger: dict[str, Any], watch: dict[str, Any], history_path: Path | None = None, *,
    history_rows: Iterable[dict[str, Any]] | None = None,
    ranking: Iterable[dict[str, Any]] | None = None,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    if ranking is None and history_rows is not None:
        ranking = mine(list(history_rows), system=SYSTEM).get("ranking")
    now = iso_hkt()
    if ranking is not None:
        # The persisted granular card is the discovery source for Wilson
        # admission.  Decorate it from immutable active evidence before the
        # exact matcher runs; this prevents the old discovery total from
        # silently winning after the initial completed holdout was merged.
        ranking = project_granular_ranking_evidence(
            ledger, SYSTEM, ranking, now=now,
        )
    return _evaluate_stage(
        ledger, watch, system=SYSTEM, market_labels=MARKET_LABELS,
        parse_time=parse_time, now=now, ranking=ranking,
        decision_stage=DECISION_STAGE,
    )


def evaluate_stage(
    ledger: dict[str, Any], watch: dict[str, Any], decision_stage: str,
    history_path: Path | None = None, *,
    history_rows: Iterable[dict[str, Any]] | None = None,
    ranking: Iterable[dict[str, Any]] | None = None,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """Evaluate an exact committed native stage; T-5 remains compatible."""
    if decision_stage == DECISION_STAGE:
        return evaluate_new_t5(
            ledger, watch, history_path, history_rows=history_rows, ranking=ranking,
        )
    if ranking is None and history_rows is not None:
        ranking = mine(list(history_rows), system=SYSTEM).get("ranking")
    now = iso_hkt()
    if ranking is not None:
        ranking = project_granular_ranking_evidence(
            ledger, SYSTEM, ranking, now=now,
        )
    return _evaluate_stage(
        ledger, watch, system=SYSTEM, market_labels=MARKET_LABELS,
        parse_time=parse_time, now=now, ranking=ranking,
        decision_stage=decision_stage,
    )
