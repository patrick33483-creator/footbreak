"""Durable, native-only Crown direct T-5 notification outbox.

This is deliberately separate from the Wilson validation namespace.  The
legacy direct signal is a three-stage HDC consensus notification; it is not a
research ranking and never changes condition counts, settlement, stake or PnL.
"""
from __future__ import annotations

import math
from typing import Any

from .common import iso_hkt, parse_time


NAMESPACE = "native_t5_direct_notifications"
SCHEMA_VERSION = 1
OUTBOX_LIMIT = 1600
POLICY = "legacy-hdc-exact-three-stage-v1"


def ensure_namespace(ledger: dict[str, Any], *, now: str | None = None) -> dict[str, Any]:
    """Create a prospective-only namespace without inspecting old watches."""
    existing = ledger.get(NAMESPACE)
    if not isinstance(existing, dict):
        existing = {
            "schema_version": SCHEMA_VERSION,
            # This marker is intentionally set only by a post-deployment
            # native state commit.  The producer below also accepts only a
            # brand-new T-5 stage, so historical stages cannot be replayed.
            "activation_at": now or iso_hkt(),
            "outbox": [],
        }
        ledger[NAMESPACE] = existing
    existing.setdefault("schema_version", SCHEMA_VERSION)
    existing.setdefault("activation_at", now or iso_hkt())
    existing.setdefault("outbox", [])
    if not isinstance(existing["outbox"], list):
        existing["outbox"] = []
    return existing


def _number(value: Any, *, positive: bool = False) -> float | None:
    try:
        parsed = float(value)
    except (TypeError, ValueError):
        return None
    if not math.isfinite(parsed) or (positive and parsed <= 0):
        return None
    return parsed


def _identity(watch: dict[str, Any], stage: dict[str, Any]) -> str | None:
    fields = ("match_id", "kickoff_hkt", "home", "away")
    values = {field: str(stage.get(field) or "").strip() for field in fields}
    if not all(values.values()) or str(watch.get("match_id") or "").strip() != values["match_id"]:
        return None
    for field in ("kickoff_hkt", "home", "away"):
        watch_value = str(
            watch.get(field) or (watch.get("kickoff") if field == "kickoff_hkt" else "")
        ).strip()
        if watch_value != values[field]:
            return None
    return "|".join(values[field] for field in fields)


def _one_hdc(stage: dict[str, Any]) -> dict[str, Any] | None:
    rows = [
        row for row in (stage.get("market_predictions") or [])
        if isinstance(row, dict) and str(row.get("code") or "") == "HDC"
    ]
    return rows[0] if len(rows) == 1 else None


def _eligible_hdc(
    watch: dict[str, Any], stage: dict[str, Any], activation_at: str,
) -> tuple[str, list[dict[str, Any]], float, float] | None:
    """Reproduce the last working direct policy: exact 3-stage HDC consensus."""
    if stage.get("stage") != "T-5":
        return None
    identity = _identity(watch, stage)
    kickoff = parse_time(str(stage.get("kickoff_hkt") or ""))
    stage_at = parse_time(str(stage.get("ts") or ""))
    activated = parse_time(str(activation_at or ""))
    if (
        identity is None
        or kickoff is None
        or stage_at is None
        or activated is None
        or stage_at < activated
        or stage_at >= kickoff
    ):
        return None
    selections: list[dict[str, Any]] = []
    for name in ("首預", "T-30", "T-5"):
        rows = [
            row for row in (watch.get("stages") or [])
            if isinstance(row, dict) and row.get("stage") == name
        ]
        if len(rows) != 1 or _identity(watch, rows[0]) != identity:
            return None
        selected = _one_hdc(rows[0])
        if selected is None:
            return None
        selections.append(selected)
    sides = [str(row.get("side") or "") for row in selections]
    lines = [_number(row.get("line", row.get("condition"))) for row in selections]
    if (
        any(side not in {"H", "A"} for side in sides)
        or any(line is None for line in lines)
        or len(set(sides)) != 1
        or not (lines[0] == lines[1] == lines[2])
    ):
        return None
    odds = _number(selections[-1].get("odds"), positive=True)
    if odds is None:
        return None
    return identity, selections, float(lines[-1]), odds


def record_new_native_t5(
    ledger: dict[str, Any],
    watch: dict[str, Any],
    stage: dict[str, Any],
    *,
    formal_rows: list[dict[str, Any]] | None = None,
) -> dict[str, Any] | None:
    """Append exactly one prospective direct event for an eligible native T-5.

    ``formal_rows`` is only annotation from the already-completed independent
    Wilson admission.  Direct eligibility itself never consumes rankings,
    results, counterpart prices, or a second provider.
    """
    namespace = ensure_namespace(ledger)
    resolved = _eligible_hdc(watch, stage, str(namespace.get("activation_at") or ""))
    if resolved is None:
        return None
    identity, _, home_line, odds = resolved
    side = str(_one_hdc(stage).get("side"))  # guarded by _eligible_hdc
    direct_signal_id = f"crown|{identity}|T-5|HDC|three-stage-v1|{side}|{home_line:g}"
    outbox = namespace["outbox"]
    prior = next(
        (
            row for row in outbox
            if isinstance(row, dict) and row.get("direct_signal_id") == direct_signal_id
        ),
        None,
    )
    if prior is not None:
        return prior
    matching = [
        row for row in (formal_rows or [])
        if isinstance(row, dict)
        and str(row.get("match_id") or "") == str(watch.get("match_id") or "")
        and str(row.get("code") or row.get("market") or "") == "HDC"
        and str(row.get("stage") or "") == "T-5"
    ]
    condition_numbers = sorted({
        int(row["condition_number"]) for row in matching
        if str(row.get("condition_number") or "").strip().isdigit()
    })
    formal_notification_ids = [
        str(row.get("bet_id") or row.get("observation_id") or "")
        for row in matching
        if str(row.get("bet_id") or row.get("observation_id") or "")
    ]
    selected_line = -home_line if side == "A" else home_line
    team_value = watch.get("home") if side == "H" else watch.get("away")
    team = str(team_value or "").strip()
    row = {
        "schema_version": SCHEMA_VERSION,
        "direct_signal_id": direct_signal_id,
        "notification_required": True,
        "notification_kind": "native_crown_t5_direct",
        "legacy_policy": POLICY,
        "activation_at": namespace["activation_at"],
        "created_at": iso_hkt(),
        "stage_at": stage.get("ts"),
        "native_pre_kickoff_t5": True,
        "match_id": watch.get("match_id"),
        "league": watch.get("league"),
        "home": watch.get("home"),
        "away": watch.get("away"),
        "kickoff": watch.get("kickoff") or watch.get("kickoff_hkt"),
        "market": "HDC",
        "side": side,
        "home_line": home_line,
        "selected_line": selected_line,
        "selected_team": team,
        "odds": odds,
        "formal": bool(condition_numbers),
        "condition_numbers": condition_numbers,
        # When formal, this links a single direct signal to the formal durable
        # outbox acknowledgements, avoiding a second Telegram message.
        "formal_notification_ids": formal_notification_ids,
        "formal_decision": (
            "NO_BET_LOW_ODDS"
            if any(row.get("bet_status") == "NO_BET_LOW_ODDS" for row in matching)
            else "PAPER_SIMULATION" if condition_numbers else None
        ),
        "execution": "NO_BET_DIRECT_OBSERVATION" if not condition_numbers else None,
        "simulation_only": True,
        "real_betting_enabled": False,
    }
    outbox.append(row)
    namespace["outbox"] = outbox[-OUTBOX_LIMIT:]
    return row
