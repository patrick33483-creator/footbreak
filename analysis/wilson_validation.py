"""Immutable Wilson-test simulation portfolio primitives.

This module is deliberately separate from :mod:`independent_validation`.
That namespace is the retired v1 strategy and remains readable/settleable,
whereas this namespace owns only the Wilson 測試攻略 prospective experiment.
"""
from __future__ import annotations

import copy
import hashlib
import json
import math
from datetime import datetime, timezone
from typing import Any, Iterable

SCHEMA_VERSION = 2
NAMESPACE = "wilson_validation"
STRATEGY = "wilson-test-strategy-v1"
DISPLAY_NAME = "Wilson 測試攻略"
STARTING_BANKROLL = 50_000.0
FIXED_STAKE = 500.0
FIXTURE_STAKE_CAP = 1_500.0
FIXTURE_MARKET_CAP = 3
DECISION_STAGE = "T-5"
MIN_DECIDED = 50
EDGE_BUFFER = 0.03
Z_95 = 1.959963984540054
LEGACY_STRATEGY = "independent-validation-v1"
ROLLOVER_BATCH_SIZE = 20
ROLLOVER_AUDIT_LIMIT = 64
FUNNEL_REJECTION_LIMIT = 8
BINARY_HIT_RESULTS = {"Won", "Half Won"}
BINARY_MISS_RESULTS = {"Lost", "Half Lost"}
BINARY_DECIDED_RESULTS = BINARY_HIT_RESULTS | BINARY_MISS_RESULTS

FUNNEL_AUDIT_REJECTIONS = {
    "stage_not_strictly_after_evidence_activation_boundary": (
        "evidence_boundary", "T-5 不在目前證據版本啟用界線之後",
    ),
    "active_evidence_unavailable": (
        "evidence_integrity", "有效證據版本未可用",
    ),
    "active_evidence_arithmetic_invalid": (
        "evidence_integrity", "有效證據無法形成正式 Wilson 算術",
    ),
    "wilson_gate_not_passed": (
        "execution_gate", "完全相同條件吻合，但賠率未通過 Wilson 門檻",
    ),
    "idempotent_existing_market": (
        "duplicate_guard", "相同賽事市場已有正式紀錄",
    ),
    "fixture_cap_reached": (
        "portfolio_guard", "每場市場或注碼上限已達",
    ),
}

FUNNEL_SETTLEMENT_REJECTIONS = {
    "not_settled": ("settlement_state", "正式紀錄尚未結算"),
    "not_binary_decided": ("settlement_state", "結算不是有效二元判定"),
    "missing_or_invalid_provenance": (
        "evidence_integrity", "缺少或無效的原生 T-5 證據標記",
    ),
    "before_snapshot_boundary": (
        "evidence_boundary", "T-5 不在條件初始證據界線之後",
    ),
    "duplicate_or_conflicting_fixture_market": (
        "duplicate_guard", "fixture-market 證據重複或互相衝突",
    ),
}


def _number(value: Any) -> float | None:
    try:
        answer = float(value)
    except (TypeError, ValueError):
        return None
    return answer if math.isfinite(answer) else None


def _now() -> str:
    return datetime.now(timezone.utc).astimezone().isoformat(timespec="seconds")


def _canonical_hash(value: Any) -> str:
    """Return a deterministic irreversible digest suitable for public audit."""
    raw = json.dumps(
        value, ensure_ascii=False, sort_keys=True, separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(raw).hexdigest()


def _time(value: Any) -> datetime | None:
    try:
        parsed = datetime.fromisoformat(str(value or "").replace("Z", "+00:00"))
    except (TypeError, ValueError):
        return None
    return parsed.replace(tzinfo=timezone.utc) if parsed.tzinfo is None else parsed.astimezone(timezone.utc)


def _strictly_after(value: Any, boundary: Any) -> bool:
    left, right = _time(value), _time(boundary)
    return left is not None and right is not None and left > right


def portfolio_name(system: str) -> str:
    return f"{system}_wilson_test"


def wilson95(hits: int, decided: int) -> tuple[float, float] | None:
    """Return full precision Wilson 95% interval; callers round only for display."""
    if decided <= 0 or hits < 0 or hits > decided:
        return None
    p = hits / decided
    denom = 1.0 + Z_95 * Z_95 / decided
    center = (p + Z_95 * Z_95 / (2.0 * decided)) / denom
    margin = Z_95 * math.sqrt(
        (p * (1.0 - p) + Z_95 * Z_95 / (4.0 * decided)) / decided
    ) / denom
    return max(0.0, center - margin), min(1.0, center + margin)


def admission_arithmetic(hits: int, decided: int, odds: Any) -> dict[str, Any] | None:
    """Calculate the exact Wilson admission inequality without rounded inputs."""
    decimal = _number(odds)
    interval = wilson95(hits, decided)
    if decimal is None or decimal <= 1.0 or interval is None:
        return None
    lower, upper = interval
    break_even = 1.0 / decimal
    required = break_even + EDGE_BUFFER
    minimum = 1.0 / (lower - EDGE_BUFFER) if lower > EDGE_BUFFER else None
    return {
        "hits": hits,
        "decided": decided,
        "hit_rate_raw": hits / decided,
        "wilson95_lower_raw": lower,
        "wilson95_upper_raw": upper,
        "actual_decimal_odds_raw": decimal,
        "break_even_rate_raw": break_even,
        "required_rate_raw": required,
        "minimum_acceptable_odds_raw": minimum,
        # A tolerance is intentionally not used: equality is a valid pass.
        "passes": lower >= required,
        "display": {
            "hit_rate_pct": round(100.0 * hits / decided, 1),
            "wilson95_lower_pct": round(100.0 * lower, 1),
            "wilson95_upper_pct": round(100.0 * upper, 1),
            "break_even_pct": round(100.0 * break_even, 1),
            "required_pct": round(100.0 * required, 1),
            "minimum_acceptable_odds": round(minimum, 2) if minimum is not None else None,
            "actual_decimal_odds": round(decimal, 2),
        },
    }


def _legacy_rows(ledger: dict[str, Any], system: str) -> list[dict[str, Any]]:
    return [
        row for row in ledger.get("bets") or []
        if isinstance(row, dict)
        and row.get("portfolio") == f"{system}_independent_validation"
        and row.get("strategy") == LEGACY_STRATEGY
    ]


def _legacy_archive(ledger: dict[str, Any], system: str, cutover_at: str) -> dict[str, Any]:
    """A read-only v1 snapshot made exactly once at the Wilson cutover."""
    rows = _legacy_rows(ledger, system)
    legacy_ns = ledger.get("independent_validation")
    legacy_stats = copy.deepcopy(legacy_ns.get("stats") if isinstance(legacy_ns, dict) else {})
    bankroll = _number(legacy_stats.get("starting_bankroll")) if isinstance(legacy_stats, dict) else None
    return {
        "label": "已封存／退役 previous strategy（唯讀）",
        "read_only": True,
        "strategy": LEGACY_STRATEGY,
        "cutover_at": cutover_at,
        "new_entries_disabled": True,
        "entry_notifications_disabled": True,
        "pending_settlement_retained": True,
        "legacy_bets": copy.deepcopy(rows),
        "legacy_bet_count": len(rows),
        "legacy_pending_count": sum(row.get("status") == "PENDING" for row in rows),
        "legacy_bankroll": bankroll,
        "legacy_stats": legacy_stats,
    }


def ensure_namespace(ledger: dict[str, Any], system: str, *, now: str | None = None) -> dict[str, Any]:
    """Install an idempotent, non-destructive Wilson cutover namespace."""
    ledger.setdefault("bets", [])
    ns = ledger.get(NAMESPACE)
    if ns is None:
        ns = {}
        ledger[NAMESPACE] = ns
    if not isinstance(ns, dict):
        raise ValueError("Wilson namespace must be an object")
    if ns.get("schema_version") not in (None, 1, SCHEMA_VERSION):
        raise ValueError("unsupported Wilson namespace schema")
    if ns.get("system") not in (None, system):
        raise ValueError("Wilson namespace system mismatch")
    activation = now or _now()
    # v1 had frozen discovery evidence but no prospective evidence-version
    # ledger.  It is deliberately upgraded as a new baseline only: no legacy
    # settled rows may be replayed into a rollover without explicit native
    # provenance and an existing snapshot boundary.
    legacy_rollover_upgrade = ns.get("schema_version") == 1
    if ns.get("schema_version") in (None, 1):
        ns["schema_version"] = SCHEMA_VERSION
    ns.setdefault("system", system)
    ns.setdefault("display_name", DISPLAY_NAME)
    ns.setdefault("activation_at", activation)
    ns.setdefault("cutover_at", ns["activation_at"])
    if legacy_rollover_upgrade:
        ns.setdefault("rollover_migration_at", activation)
    ns.setdefault("starting_bankroll", STARTING_BANKROLL)
    ns.setdefault("fixed_stake", FIXED_STAKE)
    ns.setdefault("fixture_stake_cap", FIXTURE_STAKE_CAP)
    ns.setdefault("fixture_market_cap", FIXTURE_MARKET_CAP)
    ns.setdefault("minimum_decided", MIN_DECIDED)
    ns.setdefault("edge_buffer", EDGE_BUFFER)
    ns.setdefault("conditions", {})
    # ``condition_order`` is the durable public identity order.  A condition
    # number is assigned once when immutable historical evidence is frozen,
    # never from a browser list position or a live ranking sort.
    ns.setdefault("condition_order", [])
    ns.setdefault("audit", [])
    ns.setdefault("notifications", {"sent": []})
    # This one-time snapshot labels old v1 clearly without modifying a row,
    # stat, or pending settlement obligation.
    ns.setdefault("retired_v1", _legacy_archive(ledger, system, ns["cutover_at"]))
    if not isinstance(ns["conditions"], dict):
        raise ValueError("Wilson conditions must be an object")
    if not isinstance(ns["audit"], list):
        raise ValueError("Wilson audit must be an array")
    if not isinstance(ns["condition_order"], list):
        raise ValueError("Wilson condition order must be an array")
    _ensure_condition_order(ns)
    migration_boundary = str(
        ns.get("rollover_migration_at") or ns["activation_at"]
    )
    for frozen in ns["conditions"].values():
        if isinstance(frozen, dict):
            _ensure_evidence_versions(frozen, migration_boundary=migration_boundary)
    return ns


def _ensure_condition_order(ns: dict[str, Any]) -> list[str]:
    """Repair old ledgers deterministically without renumbering a condition."""
    conditions = ns.get("conditions") if isinstance(ns.get("conditions"), dict) else {}
    existing = [
        str(signature) for signature in (ns.get("condition_order") or [])
        if str(signature) in conditions
    ]
    missing = sorted(
        (str(signature) for signature in conditions if str(signature) not in existing),
        key=lambda signature: (
            str((conditions.get(signature) or {}).get("frozen_at") or ""),
            signature,
        ),
    )
    order = existing + missing
    ns["condition_order"] = order
    for index, signature in enumerate(order, start=1):
        frozen = conditions.get(signature)
        if isinstance(frozen, dict):
            # Old records receive their first stable number exactly once.
            frozen.setdefault("condition_number", index)
    return order


def condition_number(ns: dict[str, Any], signature: str) -> int | None:
    """Return the persisted, never-render-derived Wilson condition number."""
    conditions = ns.get("conditions") if isinstance(ns.get("conditions"), dict) else {}
    frozen = conditions.get(str(signature))
    if isinstance(frozen, dict):
        try:
            return int(frozen.get("condition_number"))
        except (TypeError, ValueError):
            pass
    for index, value in enumerate(_ensure_condition_order(ns), start=1):
        if value == str(signature):
            return index
    return None


def _evidence_values(hits: int, decided: int) -> dict[str, Any]:
    """Wilson evidence fields shared by immutable versions and the dashboard."""
    interval = wilson95(hits, decided)
    lower = interval[0] if interval else None
    minimum = 1.0 / (lower - EDGE_BUFFER) if lower is not None and lower > EDGE_BUFFER else None
    return {
        "hits": hits,
        "decided": decided,
        "hit_rate_raw": hits / decided if decided else None,
        "wilson95_lower_raw": lower,
        "wilson95_upper_raw": interval[1] if interval else None,
        "minimum_acceptable_odds_raw": minimum,
        "display": {
            "hit_rate_pct": round(100.0 * hits / decided, 1) if decided else None,
            "wilson95_lower_pct": round(100.0 * lower, 1) if lower is not None else None,
            "wilson95_upper_pct": round(100.0 * interval[1], 1) if interval else None,
            "minimum_acceptable_odds": round(minimum, 2) if minimum is not None else None,
        },
    }


def _pending_rate_forecast(
    hits: int, decided: int, pending: dict[str, Any],
) -> dict[str, Any] | None:
    """Project one full batch if its current pending hit rate holds."""
    pending_accuracy = _number(pending.get("accuracy"))
    try:
        pending_decided = int(pending.get("eligible_decided") or 0)
        pending_hits = int(pending.get("eligible_hits") or 0)
        required = int(pending.get("required") or ROLLOVER_BATCH_SIZE)
    except (TypeError, ValueError):
        return None
    if (
        pending_decided <= 0 or pending_accuracy is None
        or pending_hits < 0 or pending_hits > pending_decided
        or not 0.0 <= pending_accuracy <= 1.0 or required <= 0
    ):
        return None
    # Binary outcomes require a whole-number final batch. Round half up so the
    # server projection is deterministic and matches the explanatory display.
    projected_batch_hits = min(
        required, max(0, math.floor(pending_accuracy * required + 0.5)),
    )
    values = _evidence_values(
        hits + projected_batch_hits, decided + required,
    )
    return {
        "basis_pending_hits": pending_hits,
        "basis_pending_decided": pending_decided,
        "basis_pending_accuracy": pending_accuracy,
        "projected_batch_hits": projected_batch_hits,
        "projected_batch_decided": required,
        "projected_cumulative_hits": values["hits"],
        "projected_cumulative_decided": values["decided"],
        "projected_wilson95_lower_raw": values["wilson95_lower_raw"],
        "projected_minimum_acceptable_odds_raw": values[
            "minimum_acceptable_odds_raw"
        ],
        "projected_minimum_acceptable_odds_display": values["display"][
            "minimum_acceptable_odds"
        ],
        "method": "current_pending_hit_rate_nearest_whole_batch_hit",
    }


def _version_hash(payload: dict[str, Any]) -> str:
    """Hash only immutable evidence content, never raw fixture/provider ids."""
    hashable = {
        key: payload.get(key) for key in (
            "condition_signature", "version", "prior_version",
            "prior_evidence_hash", "batch_fixture_market_hashes", "batch_hits",
            "batch_decided", "cumulative_hits", "cumulative_decided",
            "wilson95_lower_raw", "minimum_acceptable_odds_raw",
            "activation_boundary_at",
        )
    }
    return _canonical_hash(hashable)


def _initial_evidence_version(
    frozen: dict[str, Any], *, migration_boundary: str,
) -> dict[str, Any] | None:
    history = frozen.get("historical_evidence")
    if not isinstance(history, dict):
        return None
    try:
        hits, decided = int(history.get("hits")), int(history.get("decided"))
    except (TypeError, ValueError):
        return None
    if hits < 0 or decided < 0 or hits > decided:
        return None
    # The historical discovery artifact is the prior evidence snapshot. A new
    # native T-5 that first freezes the condition is later than that snapshot
    # and is eligible prospectively; using the write-time ``frozen_at`` would
    # incorrectly exclude this first real validation decision.
    artifact = history.get("artifact") if isinstance(history.get("artifact"), dict) else {}
    boundary = str(
        artifact.get("as_of") or frozen.get("frozen_at") or migration_boundary
    )
    values = _evidence_values(hits, decided)
    version = {
        "version": 1,
        "condition_signature": str(frozen.get("signature") or ""),
        "prior_version": None,
        "prior_evidence_hash": None,
        "batch_fixture_market_hashes": [],
        "batch_hits": 0,
        "batch_decided": 0,
        "cumulative_hits": hits,
        "cumulative_decided": decided,
        "wilson95_lower_raw": values["wilson95_lower_raw"],
        "minimum_acceptable_odds_raw": values["minimum_acceptable_odds_raw"],
        "minimum_acceptable_odds_display": values["display"]["minimum_acceptable_odds"],
        "activation_boundary_at": boundary,
        "created_at": boundary,
        "migration_baseline": True,
    }
    version["evidence_hash"] = _version_hash(version)
    return version


def _initial_migration_version(
    frozen: dict[str, Any], baseline: dict[str, Any], *, migration_boundary: str,
) -> dict[str, Any] | None:
    """One-time upgrade of the already-completed validation cohort.

    The pre-rollover release retained only aggregate prospective metrics, not
    per-row provenance.  Product policy explicitly permits its *single*
    aggregate merge during this migration.  It is never treated as 20-row
    batches, and it establishes a fresh boundary for all future batches.
    """
    prospective = frozen.get("prospective")
    if not isinstance(prospective, dict):
        return None
    try:
        hits, decided = int(prospective.get("hits")), int(prospective.get("decided"))
        base_hits, base_decided = (
            int(baseline["cumulative_hits"]), int(baseline["cumulative_decided"]),
        )
    except (KeyError, TypeError, ValueError):
        return None
    if hits < 0 or decided <= 0 or hits > decided:
        return None
    # Legacy aggregate prospective metrics may include pushes separately. The
    # stated denominator is already decided, so only valid binary totals merge.
    values = _evidence_values(base_hits + hits, base_decided + decided)
    version = {
        "version": 2,
        "condition_signature": baseline.get("condition_signature"),
        "prior_version": 1,
        "prior_evidence_hash": baseline.get("evidence_hash"),
        "batch_fixture_market_hashes": [],
        "batch_fixture_market_ids_unavailable_from_legacy_aggregate": True,
        "batch_hits": hits,
        "batch_decided": decided,
        "cumulative_hits": base_hits + hits,
        "cumulative_decided": base_decided + decided,
        "wilson95_lower_raw": values["wilson95_lower_raw"],
        "minimum_acceptable_odds_raw": values["minimum_acceptable_odds_raw"],
        "minimum_acceptable_odds_display": values["display"]["minimum_acceptable_odds"],
        "activation_boundary_at": migration_boundary,
        "created_at": migration_boundary,
        "initial_migration_full_cohort": True,
        "legacy_prospective_cohort": {
            "hits": hits, "decided": decided,
            "pushes": int(prospective.get("pushes") or 0),
        },
    }
    version["evidence_hash"] = _version_hash(version)
    return version


def _ensure_evidence_versions(
    frozen: dict[str, Any], *, migration_boundary: str,
) -> list[dict[str, Any]]:
    """Install a v1 baseline without treating historical rows as new evidence."""
    versions = frozen.get("evidence_versions")
    if not isinstance(versions, list):
        versions = []
        frozen["evidence_versions"] = versions
    valid = [row for row in versions if isinstance(row, dict)]
    if len(valid) != len(versions):
        # A malformed version chain is ambiguous. Do not make it eligible for
        # a rollover; retain only a visible fail-closed state.
        frozen["evidence_versions"] = valid
        frozen["rollover_status"] = "blocked_malformed_evidence_versions"
        return valid
    if not versions:
        initial = _initial_evidence_version(
            frozen, migration_boundary=migration_boundary,
        )
        if initial is None:
            frozen["rollover_status"] = "blocked_invalid_historical_baseline"
            return []
        versions.append(initial)
        migrated = _initial_migration_version(
            frozen, initial, migration_boundary=migration_boundary,
        )
        if migrated is not None:
            versions.append(migrated)
            frozen["rollover_audit"] = [copy.deepcopy(migrated)]
            # Do not fold the migration cohort again through the ordinary
            # prospective display, which must restart at zero for the new
            # 20-decision rollover cycle.
            frozen["prospective_before_rollover_migration"] = copy.deepcopy(
                frozen.get("prospective"),
            )
            frozen["prospective"] = {}
            frozen["pending_rollover_progress"] = {
                "eligible_decided": 0, "required": ROLLOVER_BATCH_SIZE,
                "display": f"0/{ROLLOVER_BATCH_SIZE}",
                "eligible_hits": 0, "accuracy": None,
                "initial_migration_full_cohort": True,
            }
    active = versions[-1]
    frozen["active_evidence_version"] = active.get("version")
    frozen["active_evidence_hash"] = active.get("evidence_hash")
    frozen["active_evidence"] = {
        key: copy.deepcopy(active.get(key)) for key in (
            "version", "cumulative_hits", "cumulative_decided",
            "wilson95_lower_raw", "minimum_acceptable_odds_raw",
            "minimum_acceptable_odds_display", "activation_boundary_at",
            "created_at", "evidence_hash",
        )
    }
    return versions


def active_evidence_version(
    frozen: dict[str, Any], *, migration_boundary: str,
) -> dict[str, Any] | None:
    versions = _ensure_evidence_versions(
        frozen, migration_boundary=migration_boundary,
    )
    return versions[-1] if versions else None


def freeze_condition(
    ledger: dict[str, Any], system: str, admission: dict[str, Any], *, now: str,
) -> dict[str, Any]:
    """Freeze one matched historical condition even if its quote is too low.

    This deliberately does not create a bet.  It captures the authoritative
    raw admission arithmetic and gives the immutable condition its permanent
    number, so rejected observations and accepted simulations identify the
    same condition everywhere.
    """
    ns = ensure_namespace(ledger, system, now=now)
    signature = str(admission["signature"])
    frozen = ns["conditions"].get(signature)
    if not isinstance(frozen, dict):
        frozen = {
            "signature": signature,
            "frozen_at": now,
            "definition": copy.deepcopy(admission["definition"]),
            "historical_evidence": copy.deepcopy(admission["history"]),
            "admission_arithmetic": copy.deepcopy(admission["arithmetic"]),
            "prospective": {},
        }
        ns["conditions"][signature] = frozen
    order = _ensure_condition_order(ns)
    if signature not in order:
        order.append(signature)
        ns["condition_order"] = order
    frozen.setdefault("condition_number", order.index(signature) + 1)
    active_evidence_version(frozen, migration_boundary=ns["activation_at"])
    return frozen


def condition_definition(system: str, candidate: dict[str, Any]) -> dict[str, Any]:
    """Canonical immutable definition, preserving all supplied matcher axes.

    A granular-ranking row normally carries its exact identity in ``key``.
    A matched upcoming row also carries *the live selected side*; that is
    useful for quote validation but must not change the historical condition
    signature.  Reading the canonical key first keeps discovery, dashboard,
    and admission attached to the same immutable condition.
    """
    key = candidate.get("key")
    axes: dict[str, str] = {}
    aliases = {
        "decision_stage": "stage", "decision": "stage",
        "observed_path": "path", "tier": "odds_tier",
        "bucket": "line_bucket", "tier_path": "odds_trajectory",
    }
    if isinstance(key, list):
        for raw in key:
            if not isinstance(raw, str) or "=" not in raw:
                continue
            name, value = raw.split("=", 1)
            name, value = aliases.get(name.strip(), name.strip()), value.strip()
            if name and value and (name not in axes or axes[name] == value):
                axes[name] = value
    def value(axis: str, *sources: str, default: str = "") -> str:
        if axes.get(axis):
            return axes[axis]
        for source in sources:
            raw = candidate.get(source)
            if raw not in (None, ""):
                return str(raw)
        return default
    return {
        "system": value("system", default=system),
        "version": str(candidate.get("version") or candidate.get("condition_version") or "granular-condition-v1"),
        "market": value("market", "market"),
        "stage": value("stage", "decision_stage", "stage", default=DECISION_STAGE),
        "path": value("path", "observed_path", "path"),
        "direction": value("direction", "direction"),
        "role": value("role", "role", "selected_role"),
        "line_bucket": value("line_bucket", "line_bucket", "bucket"),
        "odds_tier": value("odds_tier", "odds_tier"),
        "movement": value("movement", "movement"),
        "odds_trajectory": value("odds_trajectory", "odds_trajectory", "tier_path"),
        "miner_key": [str(v) for v in key] if isinstance(key, list) else [str(key or "")],
    }


def condition_signature(system: str, candidate: dict[str, Any]) -> tuple[str, dict[str, Any]]:
    definition = condition_definition(system, candidate)
    raw = json.dumps(definition, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(raw.encode()).hexdigest()[:24], definition


def _artifact(candidate: dict[str, Any], definition: dict[str, Any], stage_at: str) -> dict[str, Any]:
    source = candidate.get("source_artifact") if isinstance(candidate.get("source_artifact"), dict) else {}
    raw = json.dumps(
        {"definition": definition, "total": candidate.get("total"), "source": source},
        ensure_ascii=False, sort_keys=True, separators=(",", ":"),
    ).encode()
    return {
        "hash": str(source.get("hash") or source.get("sha256") or hashlib.sha256(raw).hexdigest()),
        "version": str(source.get("version") or candidate.get("source_artifact_version") or "granular-ranking-v1"),
        "as_of": str(source.get("as_of") or candidate.get("source_artifact_as_of") or stage_at),
    }


def _historical(candidate: dict[str, Any], definition: dict[str, Any], stage_at: str) -> dict[str, Any] | None:
    total = candidate.get("total") if isinstance(candidate.get("total"), dict) else {}
    # A discovery producer may carry source fixture-market ids.  They are
    # evidence, not a second sample: duplicate ids are deduped and conflicting
    # outcomes fail closed rather than inflating the hit rate.
    raw_rows = candidate.get("fixture_markets")
    if isinstance(raw_rows, list):
        unique: dict[str, bool] = {}
        for row in raw_rows:
            if not isinstance(row, dict):
                return None
            fid = str(row.get("fixture_market_id") or row.get("id") or "")
            won = row.get("won")
            if not fid or not isinstance(won, bool) or (fid in unique and unique[fid] != won):
                return None
            unique[fid] = won
        if unique:
            total = {**total, "hits": sum(unique.values()), "decided": len(unique)}
    try:
        hits, decided = int(total.get("hits")), int(total.get("decided"))
    except (TypeError, ValueError):
        return None
    if decided < MIN_DECIDED or hits < 0 or hits > decided:
        return None
    return {
        "hits": hits, "decided": decided,
        "pushes": int(total.get("pushes") or 0),
        "artifact": _artifact(candidate, definition, stage_at),
        "label": candidate.get("label") or "凍結歷史條件",
    }


def _ranking_holdout(candidate: dict[str, Any]) -> dict[str, int] | None:
    """Validate the completed discovery holdout before the one-time merge."""
    holdout = candidate.get("holdout")
    if not isinstance(holdout, dict):
        return None
    try:
        hits, decided = int(holdout.get("hits")), int(holdout.get("decided"))
        pushes = int(holdout.get("pushes") or 0)
    except (TypeError, ValueError):
        return None
    if hits < 0 or decided <= 0 or hits > decided or pushes < 0:
        return None
    return {"hits": hits, "decided": decided, "pushes": pushes}


def sync_granular_ranking_evidence(
    ledger: dict[str, Any], system: str, ranking: Iterable[dict[str, Any]], *,
    now: str,
) -> list[dict[str, Any]]:
    """Persist the ranking conditions which the history cards actually show.

    The historical ranking is recomputed from raw settled rows and therefore
    cannot own mutable prospective state.  This adapter gives each exact
    ranking key a stable Wilson condition identity in the ledger, merges the
    pre-cutover completed holdout exactly once, and later leaves that evidence
    untouched while a fresh ranking is regenerated.

    ``granular_ranking_initial_migration_completed_at`` is intentionally a
    namespace-wide one-time latch.  A condition discovered after the migration
    must not retrospectively treat an arbitrary regenerated holdout as native
    prospective evidence.
    """
    ns = ensure_namespace(ledger, system, now=now)
    initial = not bool(ns.get("granular_ranking_initial_migration_completed_at"))
    candidates = [row for row in ranking if isinstance(row, dict)]
    registered: list[dict[str, Any]] = []
    pending_order: list[str] = []
    for candidate in candidates:
        definition = condition_definition(system, candidate)
        # A malformed/cross-system ranking cannot become evidence.
        if definition["system"] != system or not definition["market"]:
            continue
        signature, definition = condition_signature(system, candidate)
        history = _historical(candidate, definition, now)
        if history is None:
            continue
        frozen = ns["conditions"].get(signature)
        if not isinstance(frozen, dict):
            # A regenerated ranking is a research view, not a formal
            # condition factory.  Only the one initial frozen discovery
            # snapshot may introduce a condition identity.  Thereafter the
            # immutable registry is authoritative for admission.
            if not initial:
                continue
            frozen = {
                "signature": signature,
                "frozen_at": now,
                "definition": copy.deepcopy(definition),
                "historical_evidence": copy.deepcopy(history),
                # This raw discovery snapshot is retained only as immutable
                # migration audit.  The public card is projected from active
                # evidence and never reuses it as its current validation line.
                "ranking_discovery_snapshot": {
                    "total": copy.deepcopy(candidate.get("total") or {}),
                    "holdout": copy.deepcopy(candidate.get("holdout") or {}),
                    "label": candidate.get("label"),
                },
                "prospective": (
                    _ranking_holdout(candidate) if initial else {}
                ) or {},
            }
            ns["conditions"][signature] = frozen
            pending_order.append(signature)
        elif initial and not frozen.get("evidence_versions") and not frozen.get(
            "ranking_discovery_snapshot"
        ):
            # A pre-rollover condition may already have been frozen by an
            # early T-5 before the dashboard migration ran. It still has no
            # evidence chain, so the explicitly authorised completed cohort
            # can be attached once; a later rerun may never replace it.
            frozen["ranking_discovery_snapshot"] = {
                "total": copy.deepcopy(candidate.get("total") or {}),
                "holdout": copy.deepcopy(candidate.get("holdout") or {}),
                "label": candidate.get("label"),
            }
            frozen["prospective"] = _ranking_holdout(candidate) or {}
            pending_order.append(signature)
        # Do not replace definitions, historical counts, or a completed
        # migration from a later ranking rebuild.  Existing state wins.
        registered.append({
            "signature": signature,
            "condition_number": frozen.get("condition_number"),
        })

    # Preserve the current card numbering at migration, then never derive it
    # from later ranking sort order. Existing (already frozen) identities keep
    # their original positions.
    order = [
        str(signature) for signature in ns.get("condition_order") or []
        if str(signature) in ns["conditions"]
    ]
    for signature in pending_order:
        if signature not in order:
            order.append(signature)
    ns["condition_order"] = order
    for index, signature in enumerate(order, start=1):
        frozen = ns["conditions"].get(signature)
        if isinstance(frozen, dict):
            frozen.setdefault("condition_number", index)

    for signature in pending_order:
        frozen = ns["conditions"].get(signature)
        if isinstance(frozen, dict):
            _ensure_evidence_versions(
                # The granular ranking may first become available after the
                # Wilson namespace itself was installed. Its full-cohort
                # merge activates *now*, never at an earlier namespace
                # cutover, so no historical/backfilled row can pass through
                # as new prospective evidence.
                frozen, migration_boundary=now,
            )
    # Do not consume the one-time migration before a ranking is actually
    # available. An empty/stale dashboard build must leave the next valid
    # completed cohort eligible for its required initial merge.
    if initial and registered:
        ns["granular_ranking_initial_migration_completed_at"] = now
        ns["granular_ranking_initial_migration_version"] = 1
    # Fill numbers after ordering/migration so a caller can project directly.
    for item in registered:
        frozen = ns["conditions"].get(item["signature"])
        if isinstance(frozen, dict):
            item["condition_number"] = frozen.get("condition_number")
    return registered


def formal_registry_candidates(
    ledger: dict[str, Any], system: str, *, now: str | None = None,
) -> list[dict[str, Any]]:
    """Project *only* validated frozen formal conditions for native matching.

    The public granular ranking is deliberately mutable research.  It may be
    empty, re-sorted, or contain new R# cards without changing prospective
    admission.  A formal condition is instead the persisted immutable
    definition and historical evidence which were frozen before any native
    decision.  The projection restores the matcher input shape without
    silently creating, rewriting, or promoting an identity.
    """
    ns = ensure_namespace(ledger, system, now=now)
    output: list[dict[str, Any]] = []
    for raw_signature, frozen in (ns.get("conditions") or {}).items():
        if not isinstance(frozen, dict):
            continue
        signature = str(raw_signature)
        definition = frozen.get("definition")
        history = frozen.get("historical_evidence")
        if not isinstance(definition, dict) or not isinstance(history, dict):
            continue
        if str(definition.get("system") or "") != system:
            continue
        key = definition.get("miner_key")
        if not isinstance(key, list) or not key:
            continue
        # Recompute from immutable stored axes.  A stale namespace/signature
        # migration must fail closed rather than accidentally matching a
        # similarly named live ranking card.
        candidate = {
            **copy.deepcopy(definition),
            "key": copy.deepcopy(key),
            "source_artifact": copy.deepcopy(history.get("artifact") or {}),
            "total": {
                "hits": history.get("hits"),
                "decided": history.get("decided"),
                "pushes": history.get("pushes") or 0,
            },
            "__formal_frozen_signature": signature,
            "__formal_frozen_definition": copy.deepcopy(definition),
            "__formal_frozen_history": copy.deepcopy(history),
        }
        rebuilt, _rebuilt_definition = condition_signature(system, candidate)
        if rebuilt != signature:
            continue
        if _historical(candidate, _rebuilt_definition, str(now or ns["activation_at"])) is None:
            continue
        output.append(candidate)
    return output


def match_formal_registry(
    rows: Iterable[dict[str, Any]], registry: Iterable[dict[str, Any]], *, system: str,
    decision_stage: str = DECISION_STAGE,
) -> dict[str, list[dict[str, Any]]]:
    """Match the complete immutable formal identity, including price path.

    ``tier`` and (for multi-stage conditions) ``tier_path`` are contemporaneous
    native-observation axes frozen with the condition definition.  They are
    unrelated to the later Wilson minimum-price execution gate: a formal
    match still records an observation when its current execution price is
    too low.
    """
    from .granular_conditions import _descriptor, _paths, canonical_panels

    required = {
        "system", "market", "path", "decision", "direction", "role", "bucket",
        "tier",
    }

    def axes(candidate: dict[str, Any]) -> dict[str, str] | None:
        result: dict[str, str] = {}
        aliases = {
            "stage": "decision", "decision_stage": "decision",
            "observed_path": "path", "odds_tier": "tier",
            "line_bucket": "bucket", "tier_path": "tier_path",
            "odds_trajectory": "tier_path",
        }
        for raw in candidate.get("key") or []:
            if not isinstance(raw, str) or "=" not in raw:
                return None
            key, value = raw.split("=", 1)
            key, value = aliases.get(key.strip(), key.strip()), value.strip()
            if not key or not value or (key in result and result[key] != value):
                return None
            result[key] = value
        if not required.issubset(result) or result["system"] != system:
            return None
        if result["decision"] != decision_stage or result["path"].split("→")[-1] != decision_stage:
            return None
        stages = result["path"].split("→")
        tier_path = result.get("tier_path")
        if len(stages) > 1:
            if not tier_path or len(tier_path.split("→")) != len(stages):
                return None
            if tier_path.split("→")[-1] != result["tier"]:
                return None
        elif tier_path:
            # A single-stage condition cannot be widened by a synthetic
            # trajectory field; it is malformed immutable state.
            return None
        return result

    candidates = [
        (candidate, axes(candidate)) for candidate in registry
        if isinstance(candidate, dict) and isinstance(candidate.get("__formal_frozen_signature"), str)
    ]
    candidates = [(candidate, key) for candidate, key in candidates if key is not None]
    output: dict[str, list[dict[str, Any]]] = {}
    for panel in canonical_panels(rows, settled_only=False):
        fixture = str(panel.get("fixture") or "")
        matches: list[dict[str, Any]] = []
        for path in _paths(panel, decision_stage):
            if path[-1]["stage"] != decision_stage:
                continue
            for level in range(4):
                descriptor, _label, _specificity = _descriptor(system, path, level)
                live = dict(piece.split("=", 1) for piece in descriptor)
                for candidate, frozen_axes in candidates:
                    if all(live.get(axis) == value for axis, value in frozen_axes.items()):
                        matches.append(candidate | {
                            "selected_side": path[-1]["side"],
                            "selected_line": path[-1]["selected_line"],
                            "selected_odds": path[-1]["odds"],
                        })
        if matches:
            dedup = {str(item.get("__formal_frozen_signature") or repr(item.get("key"))): item for item in matches}
            existing = {
                str(item.get("__formal_frozen_signature") or repr(item.get("key"))): item
                for item in output.get(fixture, [])
            }
            existing.update(dedup)
            output[fixture] = list(existing.values())
    return output


def project_granular_ranking_evidence(
    ledger: dict[str, Any], system: str, ranking: Iterable[dict[str, Any]], *,
    now: str,
) -> list[dict[str, Any]]:
    """Synchronize authoritative evidence, then return its ranking projection.

    This is an authority-side adapter: callers that use it may create the
    one-time frozen registry/migration records and therefore must persist the
    enclosing ledger while holding the Crown state lock.  A browser publisher
    must use :func:`project_frozen_ranking_evidence` instead.
    """
    sync_granular_ranking_evidence(ledger, system, ranking, now=now)
    ns = ensure_namespace(ledger, system, now=now)
    projected = _project_frozen_ranking_evidence(ns, system, ranking)
    for card in projected:
        card["last_merged_evidence"] = _project_last_merged_batch_rows(
            ledger,
            system,
            str(card.get("condition_signature") or ""),
            card.get("last_merged_batch"),
        )
    return projected


def _project_last_merged_batch_rows(
    ledger: dict[str, Any], system: str, signature: str,
    batch: Any,
) -> dict[str, Any] | None:
    """Resolve one immutable rollover batch to safe dashboard rows.

    The version's ordered fixture-market hashes are the only join keys.  A
    missing, duplicate, malformed, cross-condition, or outcome-mismatched row
    makes the whole detail fail closed; the dashboard must never fill a V2/V3
    batch from a nearby formal observation.
    """
    if not isinstance(batch, dict) or not batch.get("version"):
        return None
    try:
        expected_decided = int(batch.get("batch_decided") or 0)
        expected_hits = int(batch.get("batch_hits") or 0)
        version = int(batch["version"])
    except (TypeError, ValueError):
        return {
            "version": batch.get("version"), "expected_decided": 0,
            "expected_hits": 0, "rows": [], "complete": False,
            "unavailable_reason": "malformed_batch_summary",
        }
    hashes = batch.get("batch_fixture_market_hashes")
    if not isinstance(hashes, list) or not hashes:
        return {
            "version": version, "expected_decided": expected_decided,
            "expected_hits": expected_hits, "rows": [], "complete": False,
            "unavailable_reason": "legacy_batch_without_row_identity",
        }
    if (
        expected_decided <= 0
        or expected_hits < 0
        or expected_hits > expected_decided
        or len(hashes) != expected_decided
        or any(not isinstance(value, str) or len(value) != 64 for value in hashes)
        or len(set(hashes)) != len(hashes)
    ):
        return {
            "version": version, "expected_decided": expected_decided,
            "expected_hits": expected_hits, "rows": [], "complete": False,
            "unavailable_reason": "malformed_batch_identity",
        }

    namespace = ledger.get(NAMESPACE)
    observations = (
        namespace.get("observations") or []
        if isinstance(namespace, dict) and namespace.get("system") == system else []
    )
    candidates = list(active_bets(ledger, system)) + [
        row for row in observations
        if isinstance(row, dict)
        and row.get("portfolio") == f"{system}_wilson_observations"
        and row.get("strategy") == STRATEGY
        and row.get("formal_bet") is False
    ]
    wanted = set(hashes)
    matched: dict[str, list[dict[str, Any]]] = {}
    for row in candidates:
        marker = row.get("rollover_provenance")
        if not (
            row.get("status") == "SETTLED"
            and row.get("stage") == DECISION_STAGE
            and row.get("first_native_pre_kickoff_t5") is True
            and not row.get("post_hoc_backfill")
            and not row.get("exclude_from_simulation")
            and str(row.get("frozen_condition_signature") or "") == signature
            and row.get("result") in BINARY_DECIDED_RESULTS
            and isinstance(marker, dict)
            and marker.get("schema_version") == 1
            and marker.get("system") == system
            and marker.get("condition_signature") == signature
            and marker.get("native_pre_kickoff_t5") is True
            and marker.get("fixture_market_hash") in wanted
        ):
            continue
        matched.setdefault(str(marker["fixture_market_hash"]), []).append(row)

    if any(len(matched.get(value) or []) != 1 for value in hashes):
        return {
            "version": version, "expected_decided": expected_decided,
            "expected_hits": expected_hits, "rows": [], "complete": False,
            "unavailable_reason": "batch_row_identity_mismatch",
        }

    rows: list[dict[str, Any]] = []
    for value in hashes:
        row = matched[value][0]
        rows.append({
            "league": row.get("league"),
            "home": row.get("home"),
            "away": row.get("away"),
            "kickoff": row.get("kickoff"),
            "market": row.get("market") or row.get("code"),
            "market_label": row.get("market_label"),
            "selected_role": row.get("selected_role"),
            "selected_side": row.get("selected_side") or row.get("side"),
            "selected_line": row.get("selected_line", row.get("line")),
            "odds": row.get("odds"),
            "result": row.get("result"),
            "settled_at": row.get("settled_at"),
            "hit": row.get("result") in BINARY_HIT_RESULTS,
        })
    if sum(bool(row["hit"]) for row in rows) != expected_hits:
        return {
            "version": version, "expected_decided": expected_decided,
            "expected_hits": expected_hits, "rows": [], "complete": False,
            "unavailable_reason": "batch_outcome_mismatch",
        }
    return {
        "version": version, "expected_decided": expected_decided,
        "expected_hits": expected_hits, "rows": rows, "complete": True,
        "unavailable_reason": None,
    }


def _project_frozen_ranking_evidence(
    ns: dict[str, Any], system: str, ranking: Iterable[dict[str, Any]],
) -> list[dict[str, Any]]:
    """Project existing immutable evidence without repairing or creating it."""
    if str(ns.get("system") or "") != system:
        return []
    conditions = ns.get("conditions")
    if not isinstance(conditions, dict):
        return []
    output: list[dict[str, Any]] = []
    for candidate in ranking:
        if not isinstance(candidate, dict):
            continue
        signature, _definition = condition_signature(system, candidate)
        frozen = conditions.get(signature)
        if not isinstance(frozen, dict):
            continue
        # Publication has no authority to synthesize a missing baseline,
        # normalize an old namespace, or select a replacement version.  It
        # displays only the already-durable final immutable version.
        versions = frozen.get("evidence_versions")
        active = versions[-1] if isinstance(versions, list) and versions else None
        if (
            not isinstance(active, dict)
            or str(active.get("condition_signature") or "") != signature
        ):
            continue
        try:
            hits, decided = int(active["cumulative_hits"]), int(active["cumulative_decided"])
        except (KeyError, TypeError, ValueError):
            continue
        if hits < 0 or decided < 0 or hits > decided:
            continue
        interval = wilson95(hits, decided)
        pending = frozen.get("pending_rollover_progress")
        if not isinstance(pending, dict):
            pending = {
                "eligible_decided": 0, "required": ROLLOVER_BATCH_SIZE,
                "display": f"0/{ROLLOVER_BATCH_SIZE}",
                "eligible_hits": 0, "accuracy": None,
            }
        last_batch = (frozen.get("rollover_audit") or [])[-1] if isinstance(
            frozen.get("rollover_audit"), list
        ) and frozen.get("rollover_audit") else None
        current = copy.deepcopy(candidate)
        current["condition_signature"] = signature
        current["condition_number"] = frozen.get("condition_number")
        current["total"] = {
            "hits": hits, "decided": decided,
            "accuracy": hits / decided if decided else None,
            "wilson95": list(interval) if interval else None,
        }
        current["active_evidence"] = copy.deepcopy(active)
        current["last_merged_batch"] = copy.deepcopy(last_batch) if isinstance(last_batch, dict) else None
        current["pending_progress"] = copy.deepcopy(pending)
        # The old holdout stays in immutable migration audit; no current card
        # may call it a progress counter or blend it into new prospective work.
        current["validation_progress"] = {
            "pending_decided": int(pending.get("eligible_decided") or 0),
            "pending_hits": int(pending.get("eligible_hits") or 0),
            "pending_accuracy": _number(pending.get("accuracy")),
            "if_rate_holds": _pending_rate_forecast(hits, decided, pending),
            "required": int(pending.get("required") or ROLLOVER_BATCH_SIZE),
            "display": str(pending.get("display") or f"0/{ROLLOVER_BATCH_SIZE}"),
        }
        # This payload is the active prospective counter after migration, not
        # the old completed holdout. The old aggregate exists only in the
        # immutable evidence version/audit inside the local ledger.
        current["holdout"] = {
            "hits": 0, "decided": 0, "pushes": 0, "accuracy": None,
            "wilson95": None,
        }
        current["holdout_lift"] = None
        output.append(current)
    return output


def project_frozen_ranking_evidence(
    ledger: dict[str, Any], system: str, ranking: Iterable[dict[str, Any]],
) -> list[dict[str, Any]]:
    """Read-only browser projection of already-frozen formal evidence.

    This deliberately neither calls ``ensure_namespace`` nor synchronizes a
    ranking.  Missing, malformed, or incomplete persisted state remains absent
    from the display rather than being repaired by an optional consumer.
    """
    ns = ledger.get(NAMESPACE)
    if not isinstance(ns, dict):
        return []
    projected = _project_frozen_ranking_evidence(ns, system, ranking)
    for card in projected:
        card["last_merged_evidence"] = _project_last_merged_batch_rows(
            ledger,
            system,
            str(card.get("condition_signature") or ""),
            card.get("last_merged_batch"),
        )
    return projected


def project_condition_funnel(
    ledger: dict[str, Any], system: str,
) -> dict[str, Any]:
    """Return a read-only, fail-closed funnel for every frozen condition.

    Only durable native-stage decisions, immutable condition identities,
    formal bet/observation rows, settlement provenance, and rollover/audit
    records are used.  In particular, this projection never calls
    :func:`ensure_namespace`, repairs a condition, or derives a pre-match
    condition attribution from mutable watch/ranking data.
    """
    ns = ledger.get(NAMESPACE)
    unavailable = {
        "available": False,
        "count": None,
        "availability": "unavailable",
        "reason": "condition_attribution_not_persisted_before_exact_match",
    }
    if not isinstance(ns, dict) or str(ns.get("system") or "") != system:
        return {
            "schema_version": 1,
            "read_only": True,
            "system": system,
            "condition_count": 0,
            "conditions": [],
            "unavailable_reason": "wilson_namespace_unavailable",
        }
    conditions = ns.get("conditions")
    if not isinstance(conditions, dict):
        return {
            "schema_version": 1,
            "read_only": True,
            "system": system,
            "condition_count": 0,
            "conditions": [],
            "unavailable_reason": "frozen_condition_registry_unavailable",
        }

    order = [
        str(signature) for signature in (ns.get("condition_order") or [])
        if str(signature) in conditions
    ]
    missing = [
        str(signature) for signature in conditions
        if str(signature) not in order
    ]
    def persisted_number(signature: str) -> int:
        try:
            return int((conditions.get(signature) or {}).get("condition_number"))
        except (TypeError, ValueError):
            return 10**9
    missing.sort(key=lambda signature: (persisted_number(signature), signature))
    signatures = order + missing
    audit = ns.get("audit")
    audit_available = isinstance(audit, list)
    retained_audit = [row for row in (audit or []) if isinstance(row, dict)]
    audit_limit = 1600

    formal_rows: list[dict[str, Any]] = [
        row for row in (ledger.get("bets") or [])
        if isinstance(row, dict)
        and row.get("portfolio") == portfolio_name(system)
        and row.get("strategy") == STRATEGY
    ]
    observations = ns.get("observations")
    formal_rows.extend(
        row for row in (observations or [])
        if isinstance(row, dict)
        and row.get("portfolio") == f"{system}_wilson_observations"
        and row.get("strategy") == STRATEGY
        and row.get("formal_bet") is False
    )

    output: list[dict[str, Any]] = []
    for signature in signatures:
        frozen = conditions.get(signature)
        if not isinstance(frozen, dict):
            continue
        definition = frozen.get("definition")
        definition = copy.deepcopy(definition) if isinstance(definition, dict) else None
        versions = frozen.get("evidence_versions")
        valid_versions = [row for row in (versions or []) if isinstance(row, dict)]
        active = valid_versions[-1] if valid_versions else None
        if not (
            isinstance(active, dict)
            and str(active.get("condition_signature") or "") == signature
        ):
            active = None
        baseline = valid_versions[0] if valid_versions else None
        baseline_boundary = (
            baseline.get("activation_boundary_at")
            if isinstance(baseline, dict)
            and str(baseline.get("condition_signature") or "") == signature
            else None
        )

        exact_keys = set()
        audit_rejections: dict[str, int] = {}
        if audit_available:
            for row in retained_audit:
                if str(row.get("frozen_condition_signature") or "") != signature:
                    continue
                fixture, market = str(row.get("match_id") or ""), str(row.get("market") or "")
                if fixture and market and market != "*":
                    exact_keys.add((fixture, market))
                reason = str(row.get("reason") or "")
                if reason in FUNNEL_AUDIT_REJECTIONS:
                    audit_rejections[reason] = audit_rejections.get(reason, 0) + 1

        rows = [
            row for row in formal_rows
            if str(row.get("frozen_condition_signature") or "") == signature
        ]
        recorded_ids = set()
        recorded_bets = recorded_observations = 0
        for index, row in enumerate(rows):
            identity = str(
                row.get("bet_id") or row.get("observation_id")
                or f"retained-row-{index}"
            )
            if identity in recorded_ids:
                continue
            recorded_ids.add(identity)
            if row.get("formal_bet") is False:
                recorded_observations += 1
            else:
                recorded_bets += 1

        settlement_rejections = {
            code: 0 for code in FUNNEL_SETTLEMENT_REJECTIONS
        }
        settlement_candidates: dict[str, list[dict[str, Any]]] = {}
        baseline_time = _time(baseline_boundary)
        settlement_available = baseline_time is not None
        for row in rows:
            if row.get("status") != "SETTLED":
                settlement_rejections["not_settled"] += 1
                continue
            if row.get("result") not in BINARY_DECIDED_RESULTS:
                settlement_rejections["not_binary_decided"] += 1
                continue
            marker = row.get("rollover_provenance")
            if not (
                isinstance(marker, dict)
                and marker.get("schema_version") == 1
                and marker.get("system") == system
                and marker.get("condition_signature") == signature
                and marker.get("native_pre_kickoff_t5") is True
                and row.get("stage") == DECISION_STAGE
                and row.get("first_native_pre_kickoff_t5") is True
                and not row.get("post_hoc_backfill")
                and not row.get("exclude_from_simulation")
                and isinstance(marker.get("fixture_market_hash"), str)
                and len(marker["fixture_market_hash"]) == 64
                and _time(marker.get("stage_at")) is not None
            ):
                settlement_rejections["missing_or_invalid_provenance"] += 1
                continue
            if baseline_time is None or not _strictly_after(
                marker.get("stage_at"), baseline_boundary,
            ):
                settlement_rejections["before_snapshot_boundary"] += 1
                continue
            settlement_candidates.setdefault(
                str(marker["fixture_market_hash"]), [],
            ).append(row)
        settled_valid = []
        for grouped in settlement_candidates.values():
            if len(grouped) != 1:
                settlement_rejections[
                    "duplicate_or_conflicting_fixture_market"
                ] += len(grouped)
                continue
            settled_valid.append(grouped[0])

        pending = frozen.get("pending_rollover_progress")
        progress: dict[str, Any]
        try:
            progress_count = int(pending.get("eligible_decided"))
            progress_required = int(pending.get("required"))
            progress_display = str(pending.get("display") or "")
            progress_valid = (
                isinstance(pending, dict)
                and progress_count >= 0
                and progress_required == ROLLOVER_BATCH_SIZE
                and progress_display == f"{progress_count}/{progress_required}"
            )
        except (AttributeError, TypeError, ValueError):
            progress_count = 0
            progress_required = ROLLOVER_BATCH_SIZE
            progress_display = ""
            progress_valid = False
        if progress_valid:
            progress = {
                "available": True,
                "availability": "available",
                "count": progress_count,
                "required": progress_required,
                "display": progress_display,
                "eligible_hits": pending.get("eligible_hits"),
                "blocked_reason": pending.get("blocked_reason"),
                "source": "persisted_pending_rollover_progress",
            }
        else:
            progress = {
                "available": False,
                "availability": "unavailable",
                "count": None,
                "required": ROLLOVER_BATCH_SIZE,
                "display": None,
                "reason": "persisted_rollover_progress_unavailable_or_inconsistent",
            }

        rejection_rows = []
        for source, counts, definitions in (
            ("retained_condition_audit", audit_rejections, FUNNEL_AUDIT_REJECTIONS),
            ("formal_settlement_evidence", settlement_rejections, FUNNEL_SETTLEMENT_REJECTIONS),
        ):
            for code, count in counts.items():
                if count <= 0:
                    continue
                category, label = definitions[code]
                rejection_rows.append({
                    "category": category,
                    "code": code,
                    "label": label,
                    "count": count,
                    "source": source,
                })
        rejection_rows.sort(key=lambda row: (
            -int(row["count"]), str(row["category"]), str(row["code"]),
        ))
        omitted_rejection_kinds = max(
            0, len(rejection_rows) - FUNNEL_REJECTION_LIMIT,
        )

        output.append({
            "condition_number": frozen.get("condition_number"),
            "condition_signature": signature,
            "condition_version": (
                definition.get("version") if isinstance(definition, dict) else None
            ),
            "definition": definition,
            "frozen_at": frozen.get("frozen_at"),
            "active_evidence": {
                key: copy.deepcopy(active.get(key)) for key in (
                    "version", "evidence_hash", "cumulative_hits",
                    "cumulative_decided", "activation_boundary_at",
                )
            } if active is not None else {
                "version": None,
                "unavailable_reason": "active_evidence_version_unavailable",
            },
            "stages": {
                # A condition does not exist on an unmatched native T-5 row.
                # Assigning those rows to every frozen condition would inflate
                # the first stage, so absence remains explicit.
                "eligible_post_activation_t5_observations": copy.deepcopy(unavailable),
                "exact_condition_matches": {
                    "available": audit_available,
                    "availability": "bounded" if audit_available else "unavailable",
                    "count": len(exact_keys) if audit_available else None,
                    "scope": "retained_condition_attributed_audit_window",
                    "retained_audit_entries": len(retained_audit),
                    "window_limit": audit_limit,
                    "truncation_possible": len(retained_audit) >= audit_limit,
                    "reason": None if audit_available else "condition_audit_unavailable",
                },
                "recorded_formal_evidence": {
                    "available": True,
                    "availability": "available",
                    "count": len(recorded_ids),
                    "formal_bets": recorded_bets,
                    "formal_observations": recorded_observations,
                    "scope": "persisted_signature_bound_formal_rows",
                },
                "settled_valid_evidence": {
                    "available": settlement_available,
                    "availability": "available" if settlement_available else "unavailable",
                    "count": len(settled_valid) if settlement_available else None,
                    "hits": (
                        sum(row.get("result") in BINARY_HIT_RESULTS for row in settled_valid)
                        if settlement_available else None
                    ),
                    "scope": "unique_binary_settled_rows_after_initial_evidence_boundary",
                    "reason": (
                        None if settlement_available
                        else "initial_evidence_boundary_unavailable"
                    ),
                },
                "current_rollover_progress": progress,
            },
            "rejections": {
                "bounded": True,
                "visible_limit": FUNNEL_REJECTION_LIMIT,
                "audit_window_limit": audit_limit,
                "audit_truncation_possible": len(retained_audit) >= audit_limit,
                "items": rejection_rows[:FUNNEL_REJECTION_LIMIT],
                "omitted_reason_kinds": omitted_rejection_kinds,
            },
        })
    return {
        "schema_version": 1,
        "read_only": True,
        "system": system,
        "condition_count": len(output),
        "audit_window_limit": audit_limit,
        "conditions": output,
    }


def project_dashboard_research_matches(
    matches: Iterable[dict[str, Any]],
) -> list[dict[str, Any]]:
    """Mark structural ranking matches as research, never as an admission.

    ``match_upcoming`` deliberately answers a broader question than the native
    T-5 evaluator: it explains which historical path/range resembles a saved
    card.  It does not prove the exact current side/Asian line, frozen
    evidence version, source observation, and raw Wilson arithmetic required
    by ``matching_admissions``.  Keeping that useful discovery view is fine,
    but a dashboard must not call it a qualified Wilson condition or make it
    notification-eligible.

    The formal card and Telegram projection instead comes only from the
    persisted formal bet or NO_BET_LOW_ODDS observation emitted by the native
    evaluator.  Do not leak a frozen condition number here: the ranking index
    is a research reference and can never stand in for a native admission.
    """
    output: list[dict[str, Any]] = []
    for match in matches:
        if not isinstance(match, dict):
            continue
        item = copy.deepcopy(match)
        item.pop("condition_number", None)
        item["match_class"] = "research_only"
        item["authoritative"] = False
        item["notification_eligible"] = False
        item["display_label"] = "研究吻合／未納入正式 Wilson"
        item["research_rank"] = item.get("condition_rank")
        item["research_identity"] = {
            key: copy.deepcopy(item.get(key))
            for key in (
                "key", "path", "direction", "movement", "market",
                "selected_side", "selected_line", "line_bucket", "odds_tier",
                "source_artifact",
            )
            if item.get(key) is not None
        }
        output.append(item)
    return output


def _selection_signature(market: str, item: dict[str, Any]) -> tuple[str, float] | None:
    side = str(item.get("selected_side") or item.get("side") or "").upper()
    line = _number(item.get("selected_line", item.get("line", item.get("condition"))))
    if side not in ({"H", "A"} if market == "HDC" else {"H", "L"}) or line is None:
        return None
    if market == "HDC" and side == "A" and "selected_line" not in item:
        line = -line
    return side, round(line, 8)


def active_bets(ledger: dict[str, Any], system: str) -> list[dict[str, Any]]:
    return [
        row for row in ledger.get("bets") or []
        if isinstance(row, dict) and row.get("portfolio") == portfolio_name(system)
        and row.get("strategy") == STRATEGY
    ]


def all_settleable_bets(ledger: dict[str, Any], system: str) -> list[dict[str, Any]]:
    """Old pending rows remain settleable, but never appear in Wilson metrics."""
    return _legacy_rows(ledger, system) + active_bets(ledger, system)


def active_observations(ledger: dict[str, Any], system: str) -> list[dict[str, Any]]:
    """Return only new native formal-condition observations, never research rows.

    They are intentionally separate from ``bets``: outcome grading may feed the
    immutable condition-evidence chain, but must never create stake, PnL, ROI,
    or a simulated execution row.  The admission-time provenance requirement is
    intentional: an old explanatory NO_BET row without it is not promoted by a
    later deployment or settlement retry into formal evidence.
    """
    ns = ensure_namespace(ledger, system)
    rows = ns.get("observations") or []
    return [
        row for row in rows
        if isinstance(row, dict)
        and row.get("portfolio") == f"{system}_wilson_observations"
        and row.get("strategy") == STRATEGY
        and row.get("formal_bet") is False
        and row.get("stage") == DECISION_STAGE
        and row.get("first_native_pre_kickoff_t5") is True
        and isinstance(row.get("rollover_provenance"), dict)
        and row.get("status") in {"PENDING", "SETTLED", "VOIDED"}
    ]


def _prospective(bets: Iterable[dict[str, Any]]) -> dict[str, Any]:
    rows = list(bets)
    settled = [row for row in rows if row.get("status") == "SETTLED"]
    decided = [row for row in settled if row.get("result") != "Refunded"]
    hits = sum(row.get("result") in {"Won", "Half Won"} for row in decided)
    pnl = round(sum(_number(row.get("pnl")) or 0.0 for row in settled), 2)
    turnover = round(sum(_number(row.get("stake")) or 0.0 for row in settled), 2)
    interval = wilson95(hits, len(decided))
    return {
        "hits": hits, "decided": len(decided), "pushes": len(settled) - len(decided),
        "hit_rate": hits / len(decided) if decided else None,
        "wilson95": list(interval) if interval else None,
        "pnl": pnl, "turnover": turnover, "roi": pnl / turnover if turnover else None,
        "pending": sum(row.get("status") == "PENDING" for row in rows),
        "settled": len(settled),
    }


def _fixture_market_hash(system: str, fixture: str, market: str) -> str:
    """Never expose provider fixture ids in rollover audit/public payloads."""
    return _canonical_hash({
        "system": system, "fixture": fixture, "market": market,
    })


def _rollover_marker(
    system: str, fixture: str, market: str, signature: str, stage_at: str,
    evidence_version: dict[str, Any],
) -> dict[str, Any]:
    return {
        "schema_version": 1,
        "system": system,
        "condition_signature": signature,
        "native_pre_kickoff_t5": True,
        "stage_at": stage_at,
        "fixture_market_hash": _fixture_market_hash(system, fixture, market),
        "admitted_evidence_version": evidence_version.get("version"),
        "admitted_evidence_hash": evidence_version.get("evidence_hash"),
    }


def _eligible_rollover_rows(
    bets: Iterable[dict[str, Any]], system: str, signature: str,
    active: dict[str, Any],
) -> tuple[list[dict[str, Any]], dict[str, int]]:
    """Return only independently auditable next-batch rows, fail closed.

    Every acceptance condition is stored at admission time.  Legacy rows lack
    this marker by design and cannot be reconstructed from a later settlement.
    """
    candidates: list[tuple[dict[str, Any], dict[str, Any]]] = []
    excluded = {
        "missing_or_invalid_provenance": 0, "before_snapshot_boundary": 0,
        "not_binary_decided": 0, "duplicate_or_conflicting_fixture_market": 0,
    }
    boundary = active.get("activation_boundary_at")
    for row in bets:
        if not isinstance(row, dict) or row.get("status") != "SETTLED":
            continue
        if str(row.get("frozen_condition_signature") or "") != signature:
            continue
        marker = row.get("rollover_provenance")
        if not isinstance(marker, dict) or not (
            marker.get("schema_version") == 1
            and marker.get("system") == system
            and marker.get("condition_signature") == signature
            and marker.get("native_pre_kickoff_t5") is True
            and row.get("stage") == DECISION_STAGE
            and row.get("first_native_pre_kickoff_t5") is True
            and not row.get("post_hoc_backfill")
            and not row.get("exclude_from_simulation")
        ):
            excluded["missing_or_invalid_provenance"] += 1
            continue
        stage_at = marker.get("stage_at")
        fixture_hash = marker.get("fixture_market_hash")
        if (
            not isinstance(fixture_hash, str) or len(fixture_hash) != 64
            or _time(stage_at) is None
        ):
            excluded["missing_or_invalid_provenance"] += 1
            continue
        if not _strictly_after(stage_at, boundary):
            excluded["before_snapshot_boundary"] += 1
            continue
        if row.get("result") not in BINARY_DECIDED_RESULTS:
            # Refunded/push/void states are not binary decisions and are never
            # silently converted into a miss.
            excluded["not_binary_decided"] += 1
            continue
        candidates.append((row, marker))

    by_fixture: dict[str, list[tuple[dict[str, Any], dict[str, Any]]]] = {}
    for row, marker in candidates:
        by_fixture.setdefault(str(marker["fixture_market_hash"]), []).append((row, marker))
    eligible: list[dict[str, Any]] = []
    for fixture_hash, rows in by_fixture.items():
        if len(rows) != 1:
            # Duplicate and conflicting rows both fail closed; one row cannot
            # be chosen after the fact to improve the evidence.
            excluded["duplicate_or_conflicting_fixture_market"] += len(rows)
            continue
        row, marker = rows[0]
        eligible.append({
            "row": row,
            "fixture_market_hash": fixture_hash,
            "stage_at": str(marker["stage_at"]),
            "hit": row.get("result") in BINARY_HIT_RESULTS,
        })
    eligible.sort(key=lambda item: (
        _time(item["stage_at"]), item["fixture_market_hash"],
    ))
    return eligible, excluded


def _rollover_condition(
    frozen: dict[str, Any], bets: Iterable[dict[str, Any]], system: str,
    signature: str, *, now: str, migration_boundary: str,
) -> None:
    """Append deterministic 20-row evidence versions, never edit old ones."""
    active = active_evidence_version(
        frozen, migration_boundary=migration_boundary,
    )
    if active is None:
        return
    last_excluded: dict[str, int] = {}
    created = 0
    while True:
        eligible, excluded = _eligible_rollover_rows(
            bets, system, signature, active,
        )
        last_excluded = excluded
        if len(eligible) < ROLLOVER_BATCH_SIZE:
            pending_hits = sum(int(item["hit"]) for item in eligible)
            frozen["pending_rollover_progress"] = {
                "eligible_decided": len(eligible),
                "eligible_hits": pending_hits,
                "accuracy": pending_hits / len(eligible) if eligible else None,
                "required": ROLLOVER_BATCH_SIZE,
                "display": f"{len(eligible)}/{ROLLOVER_BATCH_SIZE}",
                "excluded": excluded,
            }
            break
        batch = eligible[:ROLLOVER_BATCH_SIZE]
        # A strict timestamp boundary cannot safely split an ambiguous group
        # with the same native T-5 instant. Hold it pending instead of
        # inventing an order which later allows equal-time rows through.
        if (
            len(eligible) > ROLLOVER_BATCH_SIZE
            and batch[-1]["stage_at"] == eligible[ROLLOVER_BATCH_SIZE]["stage_at"]
        ):
            pending_hits = sum(int(item["hit"]) for item in eligible)
            frozen["pending_rollover_progress"] = {
                "eligible_decided": len(eligible),
                "eligible_hits": pending_hits,
                "accuracy": pending_hits / len(eligible),
                "required": ROLLOVER_BATCH_SIZE,
                "display": f"{len(eligible)}/{ROLLOVER_BATCH_SIZE}",
                "excluded": excluded,
                "blocked_reason": "ambiguous_equal_stage_boundary",
            }
            frozen["rollover_status"] = "blocked_ambiguous_equal_stage_boundary"
            break
        try:
            prior_hits = int(active["cumulative_hits"])
            prior_decided = int(active["cumulative_decided"])
            prior_version = int(active["version"])
        except (KeyError, TypeError, ValueError):
            frozen["rollover_status"] = "blocked_invalid_active_evidence"
            break
        batch_hits = sum(item["hit"] for item in batch)
        values = _evidence_values(
            prior_hits + batch_hits, prior_decided + ROLLOVER_BATCH_SIZE,
        )
        next_version = {
            "version": prior_version + 1,
            "condition_signature": signature,
            "prior_version": prior_version,
            "prior_evidence_hash": active.get("evidence_hash"),
            "batch_fixture_market_hashes": [
                item["fixture_market_hash"] for item in batch
            ],
            "batch_hits": batch_hits,
            "batch_decided": ROLLOVER_BATCH_SIZE,
            "cumulative_hits": prior_hits + batch_hits,
            "cumulative_decided": prior_decided + ROLLOVER_BATCH_SIZE,
            "wilson95_lower_raw": values["wilson95_lower_raw"],
            "minimum_acceptable_odds_raw": values["minimum_acceptable_odds_raw"],
            "minimum_acceptable_odds_display": values["display"]["minimum_acceptable_odds"],
            # This is intentionally the final native T-5 time in this
            # immutable batch, not settlement time. It permits a genuinely
            # later remainder (e.g. 26 => 20 + 6) to stay pending.
            "activation_boundary_at": batch[-1]["stage_at"],
            "created_at": now,
        }
        next_version["evidence_hash"] = _version_hash(next_version)
        versions = frozen.get("evidence_versions")
        if not isinstance(versions, list):
            frozen["rollover_status"] = "blocked_malformed_evidence_versions"
            break
        versions.append(next_version)
        active = next_version
        frozen["active_evidence_version"] = next_version["version"]
        frozen["active_evidence_hash"] = next_version["evidence_hash"]
        frozen["active_evidence"] = {
            key: copy.deepcopy(next_version.get(key)) for key in (
                "version", "cumulative_hits", "cumulative_decided",
                "wilson95_lower_raw", "minimum_acceptable_odds_raw",
                "minimum_acceptable_odds_display", "activation_boundary_at",
                "created_at", "evidence_hash",
            )
        }
        audit = frozen.setdefault("rollover_audit", [])
        if not isinstance(audit, list):
            frozen["rollover_status"] = "blocked_malformed_rollover_audit"
            break
        audit.append(copy.deepcopy(next_version))
        frozen["rollover_audit"] = audit[-ROLLOVER_AUDIT_LIMIT:]
        frozen["rollover_status"] = "active"
        created += 1
    if created:
        frozen["last_rollover_count"] = created
    if created == 0 and "rollover_status" not in frozen:
        frozen["rollover_status"] = "active"
    if last_excluded:
        frozen.setdefault("pending_rollover_progress", {}).setdefault(
            "excluded", last_excluded,
        )


def apply_active_evidence(
    ledger: dict[str, Any], system: str, admission: dict[str, Any], *,
    stage_at: str, now: str,
) -> tuple[dict[str, Any] | None, str | None]:
    """Use only the active version for a new native T-5 decision.

    The first observation freezes its discovery baseline. Afterwards, every
    admission must be strictly later than the latest evidence boundary. This
    prevents a late rerun from applying a newly learned threshold backward.
    """
    ns = ensure_namespace(ledger, system, now=now)
    signature = str(admission["signature"])
    existed = isinstance(ns["conditions"].get(signature), dict)
    frozen = freeze_condition(ledger, system, admission, now=now)
    active = active_evidence_version(
        frozen, migration_boundary=ns["activation_at"],
    )
    if active is None:
        return None, "active_evidence_unavailable"
    if existed and not _strictly_after(stage_at, active.get("activation_boundary_at")):
        return None, "stage_not_strictly_after_evidence_activation_boundary"
    arithmetic = admission_arithmetic(
        int(active["cumulative_hits"]), int(active["cumulative_decided"]),
        admission["arithmetic"].get("actual_decimal_odds_raw"),
    )
    if arithmetic is None:
        return None, "active_evidence_arithmetic_invalid"
    updated = copy.deepcopy(admission)
    updated["history"] = {
        **copy.deepcopy(admission["history"]),
        "hits": int(active["cumulative_hits"]),
        "decided": int(active["cumulative_decided"]),
        "evidence_version": active["version"],
        "evidence_hash": active["evidence_hash"],
    }
    updated["arithmetic"] = arithmetic
    updated["evidence_version"] = active["version"]
    updated["evidence_hash"] = active["evidence_hash"]
    updated["stage_at"] = stage_at
    updated["safety_margin"] = (
        arithmetic["wilson95_lower_raw"] - arithmetic["required_rate_raw"]
    )
    return updated, None


def recompute_namespace(ledger: dict[str, Any], system: str) -> dict[str, Any]:
    ns = ensure_namespace(ledger, system)
    bets = active_bets(ledger, system)
    observations = active_observations(ledger, system)
    evidence_rows = bets + observations
    grouped: dict[str, list[dict[str, Any]]] = {}
    observation_grouped: dict[str, list[dict[str, Any]]] = {}
    for row in evidence_rows:
        grouped.setdefault(str(row.get("frozen_condition_signature") or ""), []).append(row)
    for row in observations:
        observation_grouped.setdefault(
            str(row.get("frozen_condition_signature") or ""), []
        ).append(row)
    for signature, frozen in ns["conditions"].items():
        if isinstance(frozen, dict):
            _rollover_condition(
                frozen, grouped.get(str(signature), []), system, str(signature), now=_now(),
                migration_boundary=ns["activation_at"],
            )
            frozen["prospective"] = _prospective(grouped.get(signature, []))
            # Deliberately named separately so any presentation can distinguish
            # evidence observations from the isolated paper-bet/PnL stream.
            frozen["prospective_observations"] = _prospective(
                observation_grouped.get(signature, [])
            )
    metrics = _prospective(bets)
    open_stake = sum(_number(row.get("stake")) or 0.0 for row in bets if row.get("status") == "PENDING")
    metrics.update({
        "portfolio": portfolio_name(system), "strategy": STRATEGY, "display_name": DISPLAY_NAME,
        "activation_at": ns["activation_at"], "cutover_at": ns["cutover_at"],
        "starting_bankroll": STARTING_BANKROLL, "fixed_stake": FIXED_STAKE,
        "fixture_stake_cap": FIXTURE_STAKE_CAP, "fixture_market_cap": FIXTURE_MARKET_CAP,
        "cash": STARTING_BANKROLL + metrics["pnl"] - open_stake,
        "equity": STARTING_BANKROLL + metrics["pnl"], "open_stake": open_stake,
        "conditions": ns["conditions"], "retired_v1": ns["retired_v1"],
        # Existing presentation adapters use these historical field names.
        # Keep aliases in the Wilson namespace rather than calculating from
        # legacy rows or allowing v1 data into prospective results.
        "n_pending": metrics["pending"], "n_settled": metrics["settled"],
        "n_voided": sum(row.get("status") == "VOIDED" for row in bets),
        "n_decided": metrics["decided"],
    })
    ns["stats"] = metrics
    return metrics


def migrate_ledger(ledger: dict[str, Any], system: str, *, now: str | None = None) -> dict[str, Any]:
    """Explicit idempotent migration entry point used by runtime loaders/tests."""
    return ensure_namespace(ledger, system, now=now)


def choose_admission(
    system: str, market: str, selected: dict[str, Any], candidates: Iterable[dict[str, Any],
    ], *, stage_at: str,
) -> tuple[dict[str, Any] | None, str]:
    """Select one exact market condition by raw safety margin, fail closed."""
    matches, reason = matching_admissions(system, market, selected, candidates, stage_at=stage_at)
    eligible = [row for row in matches if row["arithmetic"].get("passes")]
    if not eligible:
        return None, reason
    return eligible[0], "wilson_pass"


def matching_admissions(
    system: str, market: str, selected: dict[str, Any],
    candidates: Iterable[dict[str, Any]], *, stage_at: str,
) -> tuple[list[dict[str, Any]], str]:
    """Return every exact historical condition plus its unrounded admission.

    A quote below the raw Wilson minimum remains a real condition match.  The
    caller can persist/display/notify it as an observation without converting
    it into a formal simulation bet.
    """
    selected_sig = _selection_signature(market, selected)
    odds = _number(selected.get("odds"))
    if selected_sig is None:
        return [], "selected_line_or_side_invalid"
    if odds is None or odds <= 1:
        return [], "selected_odds_invalid_or_missing"
    grouped: dict[str, list[tuple[dict[str, Any], dict[str, Any], dict[str, Any]]]] = {}
    for candidate in candidates:
        if str(candidate.get("market") or "") != market or _selection_signature(market, candidate) != selected_sig:
            continue
        signature, definition = condition_signature(system, candidate)
        formal_signature = candidate.get("__formal_frozen_signature")
        formal_definition = candidate.get("__formal_frozen_definition")
        formal_history = candidate.get("__formal_frozen_history")
        if formal_signature is not None:
            # Registry candidates must survive a round-trip exactly.  Never
            # trust a caller-provided frozen payload when its signature no
            # longer describes the stored immutable axes.
            if (
                str(formal_signature) != signature
                or not isinstance(formal_definition, dict)
                or formal_definition != definition
                or not isinstance(formal_history, dict)
            ):
                continue
            history = copy.deepcopy(formal_history)
            try:
                valid_history = (
                    int(history.get("hits")) >= 0
                    and int(history.get("decided")) >= MIN_DECIDED
                    and int(history.get("hits")) <= int(history.get("decided"))
                    and isinstance(history.get("artifact"), dict)
                )
            except (TypeError, ValueError):
                valid_history = False
            if not valid_history:
                continue
        else:
            history = _historical(candidate, definition, stage_at)
        if history is not None:
            grouped.setdefault(signature, []).append((candidate, definition, history))
    matches: list[dict[str, Any]] = []
    for signature, rows in grouped.items():
        baseline = rows[0][2]
        # Duplicate discovery rows for one condition must agree exactly.  A
        # disagreement is never resolved by cherry-picking a higher hit rate.
        if any(
            row[2]["hits"] != baseline["hits"] or row[2]["decided"] != baseline["decided"]
            or row[2]["artifact"] != baseline["artifact"] for row in rows[1:]
        ):
            continue
        arithmetic = admission_arithmetic(baseline["hits"], baseline["decided"], odds)
        if arithmetic is None:
            continue
        matches.append({
            "signature": signature, "definition": rows[0][1], "history": baseline,
            "arithmetic": arithmetic, "candidate": rows[0][0],
            "safety_margin": arithmetic["wilson95_lower_raw"] - arithmetic["required_rate_raw"],
        })
    if not grouped:
        return [], "no_frozen_historical_condition"
    matches.sort(key=lambda row: (
        -row["safety_margin"], -row["history"]["decided"],
        -row["arithmetic"]["wilson95_lower_raw"], row["signature"],
    ))
    return matches, ("wilson_pass" if any(row["arithmetic"]["passes"] for row in matches)
                     else "wilson_gate_not_passed")


def record_match_observation(
    ledger: dict[str, Any], system: str, watch: dict[str, Any], market: str,
    selected: dict[str, Any], admission: dict[str, Any], *, now: str,
    market_label: str, selected_role: str | None, selected_line: float,
) -> dict[str, Any] | None:
    """Persist a matched Wilson condition which did not become a formal bet.

    Observations are explicitly segregated from ``bets``. They are settled as
    condition-validation evidence only: never stake, bankroll, PnL, ROI, or
    execution. A frozen native T-5 match remains an observation even when the
    exact execution price is below its admission minimum.
    """
    arithmetic = admission.get("arithmetic")
    if not isinstance(arithmetic, dict) or arithmetic.get("passes"):
        return None
    fixture = str(watch.get("match_id") or "")
    if not fixture:
        return None
    frozen = freeze_condition(ledger, system, admission, now=now)
    signature = str(admission["signature"])
    observation_id = f"{fixture}|{market}|{DECISION_STAGE}|{signature}|low-odds"
    ns = ensure_namespace(ledger, system, now=now)
    observations = ns.setdefault("observations", [])
    if not isinstance(observations, list):
        raise ValueError("Wilson observations must be an array")
    prior = next(
        (row for row in observations
         if isinstance(row, dict) and row.get("observation_id") == observation_id),
        None,
    )
    if prior is not None:
        return prior
    row = {
        "observation_id": observation_id,
        "portfolio": f"{system}_wilson_observations",
        "strategy": STRATEGY,
        "formal_bet": False,
        "simulation_only": True,
        "bet_status": "NO_BET_LOW_ODDS",
        "no_bet_reason": "因賠率不足，不投注",
        "match_id": fixture,
        # Crown's exact reciprocal HKJC comparison is allowed only through
        # this persisted identity bridge; no title/team matching is permitted.
        "hkjc_match_id": watch.get("hkjc_match_id"),
        "league": watch.get("league"),
        "home": watch.get("home"),
        "away": watch.get("away"),
        "kickoff": watch.get("kickoff") or watch.get("kickoff_hkt"),
        "code": market,
        "market": market,
        "market_label": market_label,
        "side": selected.get("side"),
        "line": selected.get("line", selected.get("condition")),
        "selected_side": selected.get("side"),
        "selected_line": selected_line,
        "selected_role": selected_role,
        "odds": arithmetic.get("actual_decimal_odds_raw"),
        "condition": selected.get("line", selected.get("condition")),
        "stage": DECISION_STAGE,
        "status": "PENDING",
        "first_native_pre_kickoff_t5": True,
        "created_at": now,
        "frozen_condition_signature": signature,
        "condition_number": frozen.get("condition_number"),
        "frozen_condition_definition": copy.deepcopy(admission["definition"]),
        "frozen_historical_evidence": copy.deepcopy(admission["history"]),
        "wilson_admission": copy.deepcopy(arithmetic),
        "evidence_version": admission.get("evidence_version"),
        "evidence_hash": admission.get("evidence_hash"),
        "rollover_provenance": _rollover_marker(
            system, fixture, market, signature, str(admission.get("stage_at") or now),
            active_evidence_version(frozen, migration_boundary=ns["activation_at"]) or {},
        ),
        "history": [{
            "ts": now, "stage": DECISION_STAGE,
            "action": "Wilson 正式條件驗證觀察建立（低賠率不投注）",
            "bet_status": "NO_BET_LOW_ODDS",
        }],
    }
    observations.append(row)
    ns["observations"] = observations[-1600:]
    return row


def commit_bet(
    ledger: dict[str, Any], system: str, watch: dict[str, Any], market: str,
    selected: dict[str, Any], admission: dict[str, Any], *, now: str,
    market_label: str, selected_label: str, selected_role: str | None, selected_line: float,
) -> dict[str, Any] | None:
    ns = ensure_namespace(ledger, system, now=now)
    if market not in {"HDC", "HIL", "CHL"}:
        return None
    fixture = str(watch.get("match_id") or "")
    existing = active_bets(ledger, system)
    fixture_rows = [row for row in existing if str(row.get("match_id") or "") == fixture]
    if any(str(row.get("code") or row.get("market") or "") == market for row in fixture_rows):
        return None
    if len(fixture_rows) >= FIXTURE_MARKET_CAP or sum(_number(row.get("stake")) or 0 for row in fixture_rows) + FIXED_STAKE > FIXTURE_STAKE_CAP:
        return None
    signature = admission["signature"]
    frozen = freeze_condition(ledger, system, admission, now=now)
    bid = f"{fixture}|{market}|{DECISION_STAGE}|{STRATEGY}"
    if any(str(row.get("bet_id") or "") == bid for row in fixture_rows):
        return None
    arithmetic = copy.deepcopy(admission["arithmetic"])
    # ``commit_bet`` remains usable by isolated unit/offline constructors.
    # Production admission always supplies the persisted native T-5 timestamp
    # via ``apply_active_evidence``; a direct caller receives no historical
    # replay because it is still subject to the stored boundary/provenance.
    stage_at = admission.get("stage_at") or now
    evidence = active_evidence_version(
        frozen, migration_boundary=ns["activation_at"],
    )
    if not isinstance(stage_at, str) or evidence is None:
        return None
    return {
        "bet_id": bid, "portfolio": portfolio_name(system), "strategy": STRATEGY,
        "strategy_name": DISPLAY_NAME, "match_id": fixture, "league": watch.get("league"),
        "hkjc_match_id": watch.get("hkjc_match_id"),
        "home": watch.get("home"), "away": watch.get("away"),
        "kickoff": watch.get("kickoff") or watch.get("kickoff_hkt"),
        "code": market, "market": market, "market_label": market_label, "side": selected.get("side"),
        "line": selected.get("line", selected.get("condition")), "condition": selected.get("line", selected.get("condition")),
        "selected_side": selected.get("side"), "selected_line": selected_line,
        "selected_role": selected_role, "label": selected_label,
        "odds": arithmetic["actual_decimal_odds_raw"], "stake": FIXED_STAKE,
        "stage": DECISION_STAGE, "first_stage": DECISION_STAGE, "status": "PENDING",
        "first_native_pre_kickoff_t5": True,
        "simulation_only": True, "real_betting_enabled": False, "created_at": now,
        "admission_at": now, "frozen_condition_signature": signature,
        "condition_number": frozen.get("condition_number"),
        "frozen_condition_definition": copy.deepcopy(admission["definition"]),
        "frozen_historical_evidence": copy.deepcopy(admission["history"]),
        "wilson_admission": arithmetic,
        "evidence_version": admission.get("evidence_version"),
        "evidence_hash": admission.get("evidence_hash"),
        "rollover_provenance": _rollover_marker(
            system, fixture, market, signature, stage_at, evidence,
        ),
        "history": [{"ts": now, "stage": DECISION_STAGE, "action": "Wilson 模擬注建立",
                     "reason": "首次原生賽前 T-5；凍結歷史證據 Wilson 門檻通過"}],
    }
