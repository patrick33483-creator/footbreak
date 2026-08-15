"""One fixed-stake Footbreak simulation portfolio driven by settled granular conditions.

This module has no prediction, notification, or settlement side effects.  It
only evaluates a newly persisted T-5 snapshot against *historical* GRADED
rows, then returns auditable creations/skips for the caller to persist.
"""
from __future__ import annotations

import math
from datetime import datetime, timedelta, timezone
from typing import Any, Iterable

from analysis.granular_conditions import MARKET_LABELS, MARKETS, _role, match_upcoming, mine

import json
from pathlib import Path

HKT = timezone(timedelta(hours=8))

def iso_hkt() -> str:
    return datetime.now(HKT).isoformat(timespec="seconds")

def parse_time(value: Any) -> datetime | None:
    try:
        result = datetime.fromisoformat(str(value or "").replace("Z", "+00:00"))
        return result.replace(tzinfo=HKT) if result.tzinfo is None else result
    except (TypeError, ValueError):
        return None

STARTING_BANKROLL = 50_000.0
FIXED_STAKE = 1_000.0
STRATEGY = "granular-condition-v1"
DECISION_STAGE = "T-5"
PORTFOLIO = "footbreak_condition_simulation"
AUDIT_LIMIT = 1_600
LOG_LIMIT = 100


def _finite(value: Any) -> float | None:
    try:
        result = float(value)
    except (TypeError, ValueError):
        return None
    return result if math.isfinite(result) else None


def historical_rows_from_accuracy(history_path: Path) -> list[dict[str, Any]]:
    """Project immutable Footbreak accuracy history into granular miner rows.

    The accuracy archive is the only historical evidence source.  It already
    contains only settled stage grades; the current fixture is excluded by the
    caller and never becomes a same-run sample.
    """
    try:
        payload = json.loads(history_path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return []
    rows: list[dict[str, Any]] = []
    for match in (payload.get("matches") or []):
        if not isinstance(match, dict):
            continue
        for stage in (match.get("stages") or []):
            if not isinstance(stage, dict):
                continue
            rows.append({
                "match_id": match.get("match_id"), "kickoff": match.get("kickoff"),
                "predicted_at": stage.get("predicted_at"), "stage": stage.get("stage"),
                "market_grades": stage.get("market_grades") or [],
            })
    return rows


def _history_rows(history_path: Path, fixture: str) -> list[dict[str, Any]]:
    """Read immutable settled Footbreak grades and exclude the live fixture."""
    return [
        row for row in historical_rows_from_accuracy(history_path)
        if str(row.get("match_id") or row.get("history_key") or "") != fixture
    ]


def _live_rows(watch: dict[str, Any]) -> list[dict[str, Any]]:
    return [{
        "match_id": str(watch.get("match_id") or ""),
        "stage": stage.get("stage"),
        "kickoff": watch.get("kickoff") or watch.get("kickoff_hkt"),
        "predicted_at": stage.get("ts") or stage.get("source_snapshot_at"),
        "market_predictions": stage.get("market_predictions") or [],
    } for stage in (watch.get("stages") or []) if isinstance(stage, dict)]


def _valid_selected(stage: dict[str, Any], market: str) -> tuple[dict[str, Any] | None, str | None]:
    selected = [
        item for item in (stage.get("market_predictions") or [])
        if isinstance(item, dict) and str(item.get("code") or "").upper() == market
    ]
    if len(selected) != 1:
        return None, "selected_market_missing_or_ambiguous"
    item = selected[0]
    odds, line = _finite(item.get("odds")), _finite(item.get("line", item.get("condition")))
    side = str(item.get("side") or "").upper()
    valid_sides = {"H", "A"} if market == "HDC" else {"H", "L"}
    observed = item.get("observed_at")
    kickoff = parse_time(stage.get("kickoff") or stage.get("kickoff_hkt"))
    observed_at = parse_time(observed)
    if observed_at is None:
        numeric_observed = _finite(observed)
        if numeric_observed is not None and numeric_observed > 0:
            if numeric_observed >= 10_000_000_000:
                numeric_observed /= 1000
            try:
                observed_at = datetime.fromtimestamp(numeric_observed, kickoff.tzinfo) if kickoff else None
            except (OverflowError, OSError, ValueError):
                observed_at = None
    if odds is None or odds <= 1:
        return None, "selected_odds_invalid_or_missing"
    if line is None or side not in valid_sides:
        return None, "selected_line_or_side_invalid"
    if kickoff is None or observed_at is None or observed_at >= kickoff:
        return None, "selected_quote_not_provably_pre_kickoff"
    return item, None


def _best(items: Iterable[dict[str, Any]]) -> dict[str, Any] | None:
    choices = list(items)
    if not choices:
        return None
    return max(choices, key=lambda item: (
        float((item.get("total") or {}).get("accuracy") or -1),
        int((item.get("total") or {}).get("decided") or -1),
        int(item.get("specificity") or -1),
        str(item.get("label") or ""),
    ))


def _selection_signature(market: str, item: dict[str, Any]) -> tuple[str, float] | None:
    """Canonical selected-direction signature used only for fail-closed audit."""
    side = str(item.get("selected_side") or item.get("side") or "").upper()
    line = _finite(item.get("selected_line", item.get("line", item.get("condition"))))
    valid_sides = {"H", "A"} if market == "HDC" else {"H", "L"}
    if side not in valid_sides or line is None:
        return None
    # ``selected_line`` emitted by match_upcoming is already the selected
    # team's perspective; external compatibility callers can still provide a
    # raw home-team line, so normalize that only when the explicit selected
    # value was absent.
    if "selected_line" not in item and market == "HDC" and side == "A":
        line = -line
    return side, round(line, 8)


def _audit_selection(market: str, item: dict[str, Any]) -> dict[str, Any]:
    """Store an auditable selected quote without making internal codes public."""
    side = str(item.get("side") or "").upper()
    line = _finite(item.get("line", item.get("condition")))
    role = _role(market, side, line)
    selected_line = -line if market == "HDC" and side == "A" and line is not None else line
    return {
        "selected_side": side,
        "selected_line": selected_line,
        "selected_odds": _finite(item.get("odds")),
        "selected_role": role,
        "selected_label": (
            f"{MARKET_LABELS[market]} · {role or '—'}"
            + (f" {selected_line:g}" if selected_line is not None else "")
        ),
    }


def evaluate_new_t5(
    ledger: dict[str, Any], watch: dict[str, Any], history_path: Path | None = None,
    *, history_rows: Iterable[dict[str, Any]] | None = None,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """Return fixed-stake bets and per-market audit records for one new T-5.

    ``mine`` is deliberately the sole statistics source, so only canonical,
    historical, pre-kickoff, settled ``GRADED`` rows contribute.  The strict
    inclusion rule is repeated defensively even though ``mine`` already
    applies it, making malformed imported rankings fail closed.
    """
    fixture = str(watch.get("match_id") or "")
    league = str(watch.get("league") or "").strip()
    home = str(watch.get("home") or "").strip()
    away = str(watch.get("away") or "").strip()
    stages = [stage for stage in (watch.get("stages") or []) if isinstance(stage, dict)]
    current = next((stage for stage in stages if stage.get("stage") == DECISION_STAGE), None)
    if not fixture or current is None:
        return [], [{"market": "*", "status": "SKIPPED", "reason": "missing_new_t5_snapshot"}]
    # A public condition bet must be self-describing in the dashboard and the
    # true-new-bet alert.  Never create a row that cannot safely name its
    # fixture; in particular, a missing league must not become an alert later.
    if not league or not home or not away:
        return [], [{
            "market": "*", "status": "SKIPPED",
            "reason": "missing_fixture_context_for_public_condition_bet",
        }]
    # Stage snapshots deliberately carry only prediction payloads.  Bind their
    # parent fixture time here before validating quote evidence: an odds quote
    # is eligible only when it can be proved to predate this kickoff.
    current = {
        **current,
        "kickoff": current.get("kickoff") or watch.get("kickoff") or watch.get("kickoff_hkt"),
    }
    rows = list(history_rows) if history_rows is not None else _history_rows(history_path or Path("prediction_history.json"), fixture)
    rows = [row for row in rows if str(row.get("match_id") or row.get("history_key") or "") != fixture]
    ranking = mine(rows, system="footbreak").get("ranking") or []
    matches = match_upcoming(_live_rows(watch), ranking, system="footbreak", decision_stage=DECISION_STAGE).get(fixture, [])
    current_at = iso_hkt()
    created, audit = [], []
    existing_keys = {str(bet.get("bet_id") or "") for bet in (ledger.get("bets") or [])}
    for market in MARKETS:
        selected, reason = _valid_selected(current, market)
        candidates = [
            item for item in matches
            if str(item.get("market") or "") == market
            and float((item.get("total") or {}).get("accuracy") or 0) > .60
            and int((item.get("total") or {}).get("decided") or 0) >= 10
        ]
        if selected is None:
            audit.append({"market": market, "market_label": MARKET_LABELS[market], "status": "SKIPPED", "reason": reason})
            continue
        if not candidates:
            audit.append({"market": market, "market_label": MARKET_LABELS[market], "status": "SKIPPED", "reason": "no_historical_condition_above_60pct_with_10_decided"})
            continue
        # Future extension point: retain an explicit selection payload if an
        # upstream matcher supplies more than the single canonical snapshot.
        # Any disagreeing side/line is never resolved by ranking.
        signatures = {
            signature for item in candidates
            if (signature := _selection_signature(market, item)) is not None
        }
        if len(signatures) > 1:
            conflicts = [
                {
                    "role": _role(market, side, line if market != "HDC" or side == "H" else -line),
                    "line": line,
                }
                for side, line in sorted(signatures)
            ]
            audit.append({
                "market": market, "market_label": MARKET_LABELS[market],
                "status": "SKIPPED", "reason": "conflicting_condition_direction_or_line",
                "conflicting_selections": conflicts,
            })
            continue
        condition = _best(candidates)
        assert condition is not None
        bid = f"{fixture}|{market}|{DECISION_STAGE}|{STRATEGY}"
        if bid in existing_keys:
            audit.append({"market": market, "market_label": MARKET_LABELS[market], "status": "SKIPPED", "reason": "idempotent_existing_bet", "bet_id": bid})
            continue
        total = condition["total"]
        selection = _audit_selection(market, selected)
        bet = {
            "bet_id": bid, "portfolio": PORTFOLIO, "strategy": STRATEGY,
            "match_id": fixture, "league": league, "home": home,
            "away": away, "kickoff": watch.get("kickoff") or watch.get("kickoff_hkt"),
            "fixture_id": watch.get("fixture_id"), "league_id": watch.get("league_id"), "market": selected.get("market") or market,
            "market_label": MARKET_LABELS[market],
            "code": market, "condition": selected.get("line", selected.get("condition")),
            "line": selected.get("line", selected.get("condition")), "side": selected.get("side"),
            # Canonical side/code remain internal for settlement.  Persist a
            # fully explained Chinese selection separately for every public
            # card, audit row and history record.
            "selected_line": selection["selected_line"], "selected_role": selection["selected_role"],
            "label": selection["selected_label"],
            "odds": float(selected["odds"]), "stake": FIXED_STAKE,
            "stage": DECISION_STAGE, "first_stage": DECISION_STAGE, "status": "PENDING",
            "simulation_only": True, "real_betting_enabled": False, "created_at": current_at,
            "condition_label": condition.get("label"), "condition_key": condition.get("key"),
            "condition_accuracy": total.get("accuracy"), "condition_hits": total.get("hits"),
            "condition_decided": total.get("decided"), "condition_badge": condition.get("badge"),
            "condition_odds_tier": condition.get("odds_tier"),
            "history": [{
                "ts": current_at, "stage": DECISION_STAGE, "action": "條件模擬注建立",
                "reason": "已結算歷史條件命中率高於60%，且至少10個有效樣本",
                "condition": condition.get("label"),
            }],
        }
        created.append(bet)
        existing_keys.add(bid)
        audit.append({"market": market, "market_label": MARKET_LABELS[market], "status": "CREATED", "reason": "historical_condition_eligible", "bet_id": bid,
                      "condition_label": condition.get("label"), "accuracy": total.get("accuracy"),
                      "hits": total.get("hits"), "decided": total.get("decided"),
                      **selection})
    return created, audit
