"""Crown Wilson 測試攻略 simulation adapter (v1 is retired)."""
from __future__ import annotations

from typing import Any, Iterable

from analysis.granular_conditions import MARKET_LABELS, mine
from analysis.wilson_portfolio import evaluate
from analysis.wilson_validation import (
    DECISION_STAGE, DISPLAY_NAME, FIXED_STAKE, FIXTURE_MARKET_CAP,
    FIXTURE_STAKE_CAP, STARTING_BANKROLL, STRATEGY, portfolio_name,
    project_granular_ranking_evidence,
)
from .common import iso_hkt, parse_time
from .config import Settings

SYSTEM = "crown"
PORTFOLIO = portfolio_name(SYSTEM)
AUDIT_LIMIT = 1600


def evaluate_new_t5(
    ledger: dict[str, Any], watch: dict[str, Any], config: Settings, *,
    history_rows: Iterable[dict[str, Any]] | None = None,
    ranking: Iterable[dict[str, Any]] | None = None,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    del config
    if ranking is None and history_rows is not None:
        ranking = mine(list(history_rows), system=SYSTEM).get("ranking")
    now = iso_hkt()
    if ranking is not None:
        ranking = project_granular_ranking_evidence(
            ledger, SYSTEM, ranking, now=now,
        )
    return evaluate(ledger, watch, system=SYSTEM, market_labels=MARKET_LABELS,
                    parse_time=parse_time, now=now, ranking=ranking)
