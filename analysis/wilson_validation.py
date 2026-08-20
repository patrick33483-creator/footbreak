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
BINARY_HIT_RESULTS = {"Won", "Half Won"}
BINARY_MISS_RESULTS = {"Lost", "Half Lost"}
BINARY_DECIDED_RESULTS = BINARY_HIT_RESULTS | BINARY_MISS_RESULTS


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
    """Canonical immutable definition, preserving all supplied matcher axes."""
    key = candidate.get("key")
    return {
        "system": system,
        "version": str(candidate.get("version") or candidate.get("condition_version") or "granular-condition-v1"),
        "market": str(candidate.get("market") or ""),
        "stage": str(candidate.get("decision_stage") or candidate.get("stage") or DECISION_STAGE),
        "path": str(candidate.get("observed_path") or candidate.get("path") or ""),
        "direction": str(candidate.get("direction") or candidate.get("selected_side") or candidate.get("side") or ""),
        "role": str(candidate.get("role") or candidate.get("selected_role") or ""),
        "line_bucket": str(candidate.get("line_bucket") or candidate.get("bucket") or ""),
        "odds_tier": str(candidate.get("odds_tier") or ""),
        "movement": str(candidate.get("movement") or ""),
        "odds_trajectory": str(candidate.get("odds_trajectory") or candidate.get("tier_path") or ""),
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
            frozen["pending_rollover_progress"] = {
                "eligible_decided": len(eligible),
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
            frozen["pending_rollover_progress"] = {
                "eligible_decided": len(eligible),
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
    grouped: dict[str, list[dict[str, Any]]] = {}
    for row in bets:
        grouped.setdefault(str(row.get("frozen_condition_signature") or ""), []).append(row)
    for signature, frozen in ns["conditions"].items():
        if isinstance(frozen, dict):
            _rollover_condition(
                frozen, bets, system, str(signature), now=_now(),
                migration_boundary=ns["activation_at"],
            )
            frozen["prospective"] = _prospective(grouped.get(signature, []))
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

    Observations are explicitly segregated from ``bets``.  They are a durable
    explanation/notification outbox only and therefore cannot alter stake,
    bankroll, settlement, or any formal portfolio metric.
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
        "stage": DECISION_STAGE,
        "created_at": now,
        "frozen_condition_signature": signature,
        "condition_number": frozen.get("condition_number"),
        "frozen_condition_definition": copy.deepcopy(admission["definition"]),
        "frozen_historical_evidence": copy.deepcopy(admission["history"]),
        "wilson_admission": copy.deepcopy(arithmetic),
        "evidence_version": admission.get("evidence_version"),
        "evidence_hash": admission.get("evidence_hash"),
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
