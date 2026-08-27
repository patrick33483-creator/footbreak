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

from .quarter_line import validate as validate_quarter_line_profile

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
CONDITION_AUDIT_LIMIT = 1600
FUNNEL_REJECTION_LIMIT = 8
PRODUCTION_IDENTITY_MANIFEST_SCHEMA_VERSION = 1
PRODUCTION_IDENTITY_MANIFEST_VERSION = "wilson-production-identity-v1"
CONDITION_IDENTITY_MIGRATION_SCHEMA_VERSION = 1
CONDITION_IDENTITY_MIGRATION_VERSION = "footbreak-retired-duplicate-identity-v1"
CONDITION_IDENTITY_MIGRATION_ALLOWLIST = (
    {
        "source_condition_number": 1,
        "source_signature": "cdcddb937b8f8259db5cf799",
        "source_definition_hash": "cdcddb937b8f8259db5cf7998b242f6e69a203076fa3441747f2feb69a9d0833",
        "source_initial_evidence_hash": "ae49569b8e6bf68096ad3aff6e2536492f3494fec8ec854b7c3d9efcbbb1182b",
        "target_condition_number": 7,
        "target_signature": "a7a8aae669b985ff87f8be6e",
        "target_definition_hash": "a7a8aae669b985ff87f8be6e13b503a207aa166cae7fab069fb745034a3ab19e",
    },
    {
        "source_condition_number": 2,
        "source_signature": "83dee96f7aef2a6e5f997f02",
        "source_definition_hash": "83dee96f7aef2a6e5f997f0214e363c362f353c6a5e07973d3d7302a954bceb7",
        "source_initial_evidence_hash": "578982c8a5a37fb6a5604d8c348fdf0d58352a58a867304cec99623bcc367603",
        "target_condition_number": 14,
        "target_signature": "a79e13125a194532c8194036",
        "target_definition_hash": "a79e13125a194532c81940366fd01cc819a5c8eb0090aa033cf6456ba1daf18a",
    },
)
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
    "missing_formal_row_identity": (
        "evidence_integrity", "正式紀錄缺少不可變識別碼",
    ),
    "invalid_formal_row_identity": (
        "evidence_integrity", "正式紀錄識別碼不符合正式建立規則",
    ),
    "invalid_formal_admission_binding": (
        "evidence_integrity", "正式紀錄與凍結條件或入場證據無法驗證",
    ),
    "admitted_evidence_version_mismatch": (
        "evidence_integrity", "正式紀錄的入場證據版本或 hash 無法驗證",
    ),
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
    "duplicate_or_conflicting_formal_identity": (
        "duplicate_guard", "相同正式識別碼的紀錄重複或互相衝突",
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


def _quarter_line_profile(
    profile: Any, *, market: str | None = None, side: str | None = None,
    line: Any = None,
) -> dict[str, Any] | None:
    """Recompute and normalize an immutable quarter-line settlement profile."""
    validated = validate_quarter_line_profile(
        profile, market=market, side=side, line=line,
    )
    return copy.deepcopy(validated) if validated is not None else None


def _selected_settlement_profile(
    market: str, selected: dict[str, Any],
) -> tuple[dict[str, Any] | None, str | None]:
    """Require auditable payout weights for HIL .25/.75 lines."""
    line = _number(selected.get("line", selected.get("condition")))
    if str(market).upper() != "HIL" or line is None:
        return None, None
    fraction = abs(line) - math.floor(abs(line))
    if not (
        abs(fraction - 0.25) <= 1e-8 or abs(fraction - 0.75) <= 1e-8
    ):
        return None, None
    profile = _quarter_line_profile(
        selected.get("quarter_line_settlement"),
        market=market, side=str(selected.get("side") or ""), line=line,
    )
    if profile is None:
        return None, "quarter_line_settlement_profile_unavailable"
    return profile, None


def _snapshot_binding_valid(binding: Any, system: str) -> bool:
    if not isinstance(binding, dict) or set(binding) != {
        "schema_version", "system", "snapshot_id", "snapshot_hash",
    }:
        return False
    snapshot_id = binding.get("snapshot_id")
    snapshot_hash = binding.get("snapshot_hash")
    return (
        binding.get("schema_version") == 1
        and binding.get("system") == system
        and isinstance(snapshot_id, str) and bool(snapshot_id)
        and isinstance(snapshot_hash, str) and len(snapshot_hash) == 64
        and all(char in "0123456789abcdef" for char in snapshot_hash)
    )


def _quarter_snapshot_binding_valid(
    ledger: dict[str, Any] | None, row: dict[str, Any], system: str,
) -> bool:
    """Bind a quarter-line formal row to its exact immutable native T-5 row."""
    binding = row.get("native_snapshot_binding")
    if not _snapshot_binding_valid(binding, system) or not isinstance(ledger, dict):
        return False
    watch = (ledger.get("watch") or {}).get(str(row.get("match_id") or ""))
    if not isinstance(watch, dict):
        return False
    snapshot_id_key = (
        "formal_admission_snapshot_id" if system == "crown"
        else "native_snapshot_id"
    )
    snapshot_hash_key = (
        "formal_admission_snapshot_hash" if system == "crown"
        else "native_snapshot_hash"
    )
    matches = [
        stage for stage in watch.get("stages") or []
        if isinstance(stage, dict)
        and stage.get("stage") == DECISION_STAGE
        and stage.get(snapshot_id_key) == binding["snapshot_id"]
        and stage.get(snapshot_hash_key) == binding["snapshot_hash"]
    ]
    if len(matches) != 1:
        return False
    stage = matches[0]
    if stage.get("ts") != row.get("native_stage_at"):
        return False

    if system == "crown":
        mutable = {
            "collection_attempts",
            "formal_admission_pending",
            "formal_admission_snapshot_id",
            "formal_admission_snapshot_hash",
            "formal_admission_watch_context_hash",
            "formal_admission_status",
            "formal_admission_reason",
            "formal_admission_completed_at",
            "wilson_validation",
        }
        payload = {key: value for key, value in stage.items() if key not in mutable}
    else:
        payload = {
            key: value for key, value in stage.items()
            if key not in {"native_snapshot_id", "native_snapshot_hash"}
        }
    recalculated_hash = hashlib.sha256(json.dumps(
        payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"),
        default=str,
    ).encode()).hexdigest()
    if recalculated_hash != binding["snapshot_hash"]:
        return False

    line = _number(row.get("line", row.get("condition")))
    selected = [
        item for item in stage.get("market_predictions") or []
        if isinstance(item, dict)
        and str(item.get("code") or "").upper() == "HIL"
        and str(item.get("side") or "").upper() == str(row.get("side") or "").upper()
        and _same_optional_number(item.get("line", item.get("condition")), line)
        and _same_optional_number(item.get("odds"), row.get("odds"))
        and item.get("quarter_line_settlement") == row.get("quarter_line_settlement")
    ]
    return len(selected) == 1


def admission_arithmetic(
    hits: int, decided: int, odds: Any, *,
    settlement_profile: dict[str, Any] | None = None,
) -> dict[str, Any] | None:
    """Calculate the exact Wilson admission inequality without rounded inputs.

    Full-win/full-loss markets retain the historical binary formula.  A
    validated Asian totals quarter-line profile adjusts the break-even hit
    rate and minimum price for half-win/half-loss settlement.
    """
    decimal = _number(odds)
    interval = wilson95(hits, decided)
    if decimal is None or decimal <= 1.0 or interval is None:
        return None
    profile = None
    if settlement_profile is not None:
        profile = _quarter_line_profile(settlement_profile)
        if profile is None:
            return None
    lower, upper = interval
    win_fraction = (
        float(profile["win_fraction_raw"]) if profile is not None else 1.0
    )
    loss_fraction = (
        float(profile["loss_fraction_raw"]) if profile is not None else 1.0
    )
    break_even = loss_fraction / (
        loss_fraction + (decimal - 1.0) * win_fraction
    )
    required = break_even + EDGE_BUFFER
    target = lower - EDGE_BUFFER
    minimum = (
        1.0 + ((1.0 - target) * loss_fraction) / (target * win_fraction)
        if target > 0 else None
    )
    binary_minimum = 1.0 / target if target > 0 else None
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
        "binary_minimum_acceptable_odds_raw": binary_minimum,
        "settlement_adjusted": profile is not None,
        "settlement_profile": profile,
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
    # First execution of the release atomically freezes the compatibility
    # boundary. Rows written by the previous binary quarter-line code before
    # this instant remain valid; every later quarter row must carry schema v2.
    ns.setdefault("quarter_settlement_activation_at", activation)
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
    if "legacy_ordinary_batch_aggregate" in payload:
        hashable["legacy_ordinary_batch_aggregate"] = payload.get(
            "legacy_ordinary_batch_aggregate"
        )
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
    authority_context: Any = None,
) -> dict[str, Any] | None:
    if any(
        isinstance(row, dict) and "legacy_ordinary_batch_aggregate" in row
        for row in (frozen.get("evidence_versions") or [])
    ):
        definition, validated, reason = _validate_frozen_identity_and_chain(
            frozen, str(frozen.get("signature") or ""),
            str((frozen.get("definition") or {}).get("system") or ""),
            authority_context=authority_context,
        )
        if definition is None or validated is None or reason is not None:
            return None
        return validated[-1]
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
    authority_context: Any = None,
) -> list[dict[str, Any]]:
    """Project *only* validated frozen formal conditions for native matching.

    The public granular ranking is deliberately mutable research.  It may be
    empty, re-sorted, or contain new R# cards without changing prospective
    admission.  A formal condition is instead the persisted immutable
    definition and historical evidence which were frozen before any native
    decision.  The projection restores the matcher input shape without
    silently creating, rewriting, or promoting an identity.
    """
    retired, migration_reason = _validate_condition_identity_migrations(
        ledger, system, authority_context=authority_context,
    )
    if retired is None:
        return []
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
        retirement = retired.get(signature)
        if retirement is not None:
            if (
                rebuilt != retirement["target_signature"]
                or _rebuilt_definition
                != ns["conditions"][retirement["target_signature"]]["definition"]
            ):
                return []
            continue
        if rebuilt != signature:
            continue
        if _historical(candidate, _rebuilt_definition, str(now or ns["activation_at"])) is None:
            continue
        output.append(candidate)
    return output


def formal_matcher_axes(
    candidate: dict[str, Any], *, system: str,
    decision_stage: str = DECISION_STAGE,
) -> dict[str, str] | None:
    """Return the exact immutable axes accepted by the formal matcher."""
    required = {
        "system", "market", "path", "decision", "direction", "role", "bucket",
        "tier",
    }
    allowed = required | {"movement", "tier_path"}
    result: dict[str, str] = {}
    aliases = {
        "stage": "decision", "decision_stage": "decision",
        "observed_path": "path", "odds_tier": "tier",
        "line_bucket": "bucket", "tier_path": "tier_path",
        "odds_trajectory": "tier_path",
    }
    key_parts = candidate.get("key")
    if not isinstance(key_parts, list):
        return None
    for raw in key_parts:
        if not isinstance(raw, str) or "=" not in raw:
            return None
        key, value = raw.split("=", 1)
        key, value = aliases.get(key.strip(), key.strip()), value.strip()
        if not key or not value or (key in result and result[key] != value):
            return None
        result[key] = value
    if (
        not required.issubset(result)
        or not set(result).issubset(allowed)
        or result["system"] != system
        or result["market"] not in {"HDC", "HIL", "CHL"}
    ):
        return None
    if result["decision"] != decision_stage or result["path"].split("→")[-1] != decision_stage:
        return None
    stages = result["path"].split("→")
    tier_path = result.get("tier_path")
    if len(stages) > 1:
        # Granular descriptor level 2 intentionally binds the terminal tier
        # but not the full tier trajectory.  Only level 3 adds ``tier_path``.
        # An absent trajectory therefore remains an exact level-2 identity;
        # when present, retain the full level-3 cardinality and terminal-tier
        # checks rather than synthesizing or widening it.
        if tier_path:
            if len(tier_path.split("→")) != len(stages):
                return None
            if tier_path.split("→")[-1] != result["tier"]:
                return None
    elif tier_path:
        # A single-stage condition cannot be widened by a synthetic
        # trajectory field; it is malformed immutable state.
        return None
    return result


def match_formal_registry(
    rows: Iterable[dict[str, Any]], registry: Iterable[dict[str, Any]], *, system: str,
    decision_stage: str = DECISION_STAGE,
) -> dict[str, list[dict[str, Any]]]:
    """Match the complete immutable formal identity, including price path."""
    from .granular_conditions import _descriptor, _paths, canonical_panels

    materialized = [row for row in rows if isinstance(row, dict)]
    by_fixture: dict[str, list[dict[str, Any]]] = {}
    for row in materialized:
        by_fixture.setdefault(str(row.get("match_id") or ""), []).append(row)
    invalid_chronology: set[str] = set()
    order = {"首預": 0, "T-30": 1, "T-5": 2}
    for fixture, fixture_rows in by_fixture.items():
        staged: list[tuple[int, datetime]] = []
        kickoffs: set[str] = set()
        for row in fixture_rows:
            stage = str(row.get("stage") or "")
            saved = _time(
                row.get("predicted_at") or row.get("ts")
                or row.get("source_snapshot_at")
            )
            kickoff = _time(row.get("kickoff") or row.get("kickoff_hkt"))
            if stage in order and saved is not None:
                staged.append((order[stage], saved))
            if kickoff is not None:
                kickoffs.add(kickoff.isoformat())
            for selected in row.get("market_predictions") or []:
                if not isinstance(selected, dict) or saved is None:
                    continue
                observed = _time(selected.get("observed_at"))
                if observed is not None and observed > saved:
                    invalid_chronology.add(fixture)
        staged.sort()
        if len(kickoffs) > 1 or any(
            right[1] <= left[1] for left, right in zip(staged, staged[1:])
        ):
            invalid_chronology.add(fixture)
    materialized = [
        row for row in materialized
        if str(row.get("match_id") or "") not in invalid_chronology
    ]
    candidates = [
        (candidate, formal_matcher_axes(
            candidate, system=system, decision_stage=decision_stage,
        )) for candidate in registry
        if isinstance(candidate, dict) and isinstance(candidate.get("__formal_frozen_signature"), str)
    ]
    candidates = [(candidate, key) for candidate, key in candidates if key is not None]
    output: dict[str, list[dict[str, Any]]] = {}
    for panel in canonical_panels(materialized, settled_only=False):
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
        card["pending_rollover_evidence"] = _project_pending_rollover_rows(
            ledger,
            system,
            str(card.get("condition_signature") or ""),
            card.get("active_evidence"),
            card.get("pending_progress"),
        )
    return projected


def _dashboard_evidence_row(
    row: dict[str, Any], *, evidence_identity: str | None = None,
) -> dict[str, Any]:
    """Return only the safe, user-facing fields for one evidence observation."""
    projected = {
        "stage": row.get("stage"),
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
    }
    if evidence_identity is not None:
        projected["evidence_identity"] = evidence_identity
    return projected


def _legacy_formal_binding_repair_copy(
    row: dict[str, Any], *, system: str, signature: str,
    frozen: dict[str, Any], projection_time: datetime,
    require_settled: bool, ledger: dict[str, Any],
    require_absent_native_stage_key: bool = False,
) -> tuple[dict[str, Any] | None, tuple[str, ...], str | None]:
    """Prove the exact legacy binding omissions on a copy of ``row``.

    This helper never mutates its input and never accepts a second defect.
    In particular, the durable migration can require a truly absent
    ``native_stage_at`` key while the temporary dashboard compatibility path
    retains its historical missing-or-null behavior.
    """
    admitted, reason = validate_formal_row(
        row, system=system, signature=signature, frozen=frozen,
        projection_time=projection_time, require_settled=require_settled,
        ledger=ledger,
    )
    if admitted is not None:
        return copy.deepcopy(row), (), None
    if reason != "invalid_formal_admission_binding":
        return None, (), reason

    repaired = copy.deepcopy(row)
    changed: list[str] = []
    stored_definition = row.get("frozen_condition_definition")
    if stored_definition == {}:
        repaired["frozen_condition_definition"] = copy.deepcopy(
            frozen.get("definition"),
        )
        changed.append("frozen_condition_definition")
    elif stored_definition != frozen.get("definition"):
        return None, (), "unsupported_frozen_condition_definition"

    marker = row.get("rollover_provenance")
    native_key_absent = "native_stage_at" not in row
    native_missing = native_key_absent or (
        not require_absent_native_stage_key and row.get("native_stage_at") is None
    )
    if native_missing:
        if (
            not isinstance(marker, dict)
            or _time(marker.get("stage_at")) is None
        ):
            return None, (), "invalid_legacy_native_stage_source"
        repaired["native_stage_at"] = marker["stage_at"]
        changed.append("native_stage_at")
    elif (
        not isinstance(marker, dict)
        or row.get("native_stage_at") != marker.get("stage_at")
    ):
        return None, (), "conflicting_native_stage_at"

    if not changed:
        return None, (), reason
    admitted, repaired_reason = validate_formal_row(
        repaired, system=system, signature=signature, frozen=frozen,
        projection_time=projection_time, require_settled=require_settled,
        ledger=ledger,
    )
    if admitted is None or repaired_reason is not None:
        return None, (), repaired_reason or "invalid_formal_admission_binding"
    return repaired, tuple(changed), None


def _pending_proof_failure(
    reason: str, *, expected_decided: int = 0, expected_hits: int = 0,
    required: int = ROLLOVER_BATCH_SIZE,
) -> dict[str, Any]:
    return {
        "complete": False, "reason": reason, "eligible": [],
        "ordered_fixture_market_hashes": [], "excluded": {},
        "repairs": [], "expected_decided": expected_decided,
        "expected_hits": expected_hits, "required": required,
    }


def _signature_rows_for_rollover(
    ledger: dict[str, Any], signature: str,
) -> list[dict[str, Any]]:
    """Mirror ``recompute_namespace``'s unfiltered signature row scope."""
    namespace = ledger.get(NAMESPACE)
    observations = (
        namespace.get("observations", [])
        if isinstance(namespace, dict) else []
    )
    rows: list[dict[str, Any]] = []
    for collection in (ledger.get("bets", []), observations):
        if not isinstance(collection, list):
            continue
        rows.extend(
            row for row in collection
            if isinstance(row, dict)
            and str(row.get("frozen_condition_signature") or "") == signature
        )
    return rows


def _prove_pending_rollover_cohort(
    ledger: dict[str, Any], system: str, signature: str,
    frozen: dict[str, Any], active: dict[str, Any], pending: dict[str, Any],
    *, projection_time: datetime, allow_legacy_omissions: bool = False,
    require_absent_native_stage_key: bool = False,
) -> dict[str, Any]:
    """Prove exact pending identities, outcomes, and full exclusion counters."""
    try:
        expected_decided = int(pending.get("eligible_decided") or 0)
        expected_hits = int(pending.get("eligible_hits") or 0)
        required = int(pending.get("required") or ROLLOVER_BATCH_SIZE)
    except (AttributeError, TypeError, ValueError):
        return _pending_proof_failure("malformed_pending_summary")
    if (
        expected_decided < 0 or expected_hits < 0
        or expected_hits > expected_decided or required <= 0
    ):
        return _pending_proof_failure(
            "malformed_pending_summary",
            expected_decided=expected_decided, expected_hits=expected_hits,
            required=required,
        )
    expected_excluded = pending.get("excluded")
    if not isinstance(expected_excluded, dict):
        return _pending_proof_failure(
            "malformed_pending_summary",
            expected_decided=expected_decided, expected_hits=expected_hits,
            required=required,
        )

    source_rows = _signature_rows_for_rollover(ledger, signature)
    validated_rows: list[dict[str, Any]] = []
    originals: dict[int, dict[str, Any]] = {}
    repairs: list[dict[str, Any]] = []
    for row in source_rows:
        if str(row.get("frozen_condition_signature") or "") != signature:
            continue
        admitted, reason = validate_formal_row(
            row, system=system, signature=signature, frozen=frozen,
            projection_time=projection_time, require_settled=True,
            ledger=ledger,
        )
        validated = copy.deepcopy(row) if admitted is not None else None
        fields: tuple[str, ...] = ()
        if validated is None and allow_legacy_omissions:
            validated, fields, _repair_reason = (
                _legacy_formal_binding_repair_copy(
                    row, system=system, signature=signature, frozen=frozen,
                    projection_time=projection_time, require_settled=True,
                    ledger=ledger,
                    require_absent_native_stage_key=(
                        require_absent_native_stage_key
                    ),
                )
            )
        if validated is None:
            continue
        validated_rows.append(validated)
        originals[id(validated)] = row
        if fields:
            repairs.append({
                "row": row, "repaired": validated, "fields": fields,
            })

    selected, excluded = _eligible_rollover_rows(
        validated_rows, system, signature, active,
    )
    eligible = [
        {
            **item,
            "row": originals[id(item["row"])],
            "validated_row": item["row"],
        }
        for item in selected
    ]
    ordered = [item["fixture_market_hash"] for item in eligible]
    actual_hits = sum(bool(item["hit"]) for item in eligible)
    if (
        len(eligible) != expected_decided
        or actual_hits != expected_hits
        or excluded != expected_excluded
    ):
        return {
            **_pending_proof_failure(
                "pending_row_identity_mismatch",
                expected_decided=expected_decided,
                expected_hits=expected_hits, required=required,
            ),
            "excluded": excluded, "repairs": repairs,
        }
    return {
        "complete": True, "reason": None, "eligible": eligible,
        "ordered_fixture_market_hashes": ordered, "excluded": excluded,
        "repairs": repairs, "expected_decided": expected_decided,
        "expected_hits": expected_hits, "required": required,
    }


def _project_pending_rollover_rows(
    ledger: dict[str, Any], system: str, signature: str,
    active: Any, pending: Any,
) -> dict[str, Any] | None:
    """Resolve the current not-yet-merged cohort to safe dashboard rows.

    The authoritative pending counter is persisted by ``_rollover_condition``.
    This read-only projection independently rebuilds the same eligible cohort
    and exposes it only when row count and hit count exactly match that counter.
    """
    if not isinstance(active, dict) or not isinstance(pending, dict):
        return None
    try:
        expected_decided = int(pending.get("eligible_decided") or 0)
        expected_hits = int(pending.get("eligible_hits") or 0)
        required = int(pending.get("required") or ROLLOVER_BATCH_SIZE)
    except (TypeError, ValueError):
        return {
            "expected_decided": 0, "expected_hits": 0,
            "required": ROLLOVER_BATCH_SIZE, "rows": [], "complete": False,
            "unavailable_reason": "malformed_pending_summary",
        }
    if (
        expected_decided < 0
        or expected_hits < 0
        or expected_hits > expected_decided
        or required <= 0
    ):
        return {
            "expected_decided": expected_decided,
            "expected_hits": expected_hits,
            "required": required,
            "rows": [],
            "complete": False,
            "unavailable_reason": "malformed_pending_summary",
        }
    namespace = ledger.get(NAMESPACE)
    frozen = (
        (namespace.get("conditions") or {}).get(signature)
        if isinstance(namespace, dict)
        and namespace.get("system") == system
        and isinstance(namespace.get("conditions"), dict)
        else None
    )
    projection_time = _time(_now())
    if not isinstance(frozen, dict) or projection_time is None:
        return {
            "expected_decided": expected_decided,
            "expected_hits": expected_hits,
            "required": required,
            "rows": [], "complete": False,
            "unavailable_reason": "pending_condition_identity_unavailable",
        }
    proof = _prove_pending_rollover_cohort(
        ledger, system, signature, frozen, active, pending,
        projection_time=projection_time, allow_legacy_omissions=True,
    )
    expected_decided = proof["expected_decided"]
    expected_hits = proof["expected_hits"]
    required = proof["required"]
    eligible = proof["eligible"]
    rows = [
        _dashboard_evidence_row(
            item["row"],
            evidence_identity=_canonical_hash({
                "system": system,
                "condition_signature": signature,
                "evidence_hash": active.get("evidence_hash"),
                "fixture_market_hash": item["fixture_market_hash"],
            }),
        )
        for item in eligible
    ]
    if not proof["complete"]:
        return {
            "expected_decided": expected_decided,
            "expected_hits": expected_hits,
            "required": required,
            "rows": [],
            "complete": False,
            "unavailable_reason": proof["reason"],
        }
    return {
        "expected_decided": expected_decided,
        "expected_hits": expected_hits,
        "required": required,
        "rows": rows,
        "complete": True,
        "unavailable_reason": None,
    }


def _prove_explicit_rollover_batch(
    ledger: dict[str, Any], system: str, signature: str,
    frozen: dict[str, Any], version: dict[str, Any], *,
    projection_time: datetime, allow_legacy_omissions: bool = False,
    require_absent_native_stage_key: bool = False,
    reserved_hashes: set[str] | frozenset[str] | None = None,
    authority_context: Any = None,
) -> dict[str, Any]:
    """Prove every row and ordering claim in one identity-bearing batch."""
    failure = {
        "complete": False, "reason": "malformed_batch_summary", "eligible": [],
        "ordered_fixture_market_hashes": [], "repairs": [],
        "expected_decided": 0, "expected_hits": 0,
        "version": version.get("version") if isinstance(version, dict) else None,
    }
    definition, versions, chain_reason = _validate_frozen_identity_and_chain(
        frozen, signature, system, authority_context=authority_context,
    )
    if definition is None or versions is None or chain_reason is not None:
        return {**failure, "reason": chain_reason or "invalid_evidence_chain"}
    if not any(item is version or item == version for item in versions):
        return {**failure, "reason": "batch_not_in_validated_chain"}
    version_number = _strict_int(version.get("version"))
    decided = _strict_int(version.get("batch_decided"))
    hits = _strict_int(version.get("batch_hits"))
    hashes = version.get("batch_fixture_market_hashes")
    base = {
        **failure, "expected_decided": decided or 0, "expected_hits": hits or 0,
    }
    if (
        version_number is None or version_number <= 1
        or decided is None or hits is None or decided <= 0
        or not 0 <= hits <= decided
        or not isinstance(hashes, list) or len(hashes) != decided
        or any(not _sha256_hex(value) for value in hashes)
        or len(set(hashes)) != len(hashes)
    ):
        return base
    if reserved_hashes and set(hashes).intersection(reserved_hashes):
        return {**base, "reason": "historical_authority_identity_reuse"}
    predecessor = versions[version_number - 2]
    predecessor_boundary = predecessor.get("activation_boundary_at")
    if _time(predecessor_boundary) is None:
        return {**base, "reason": "batch_predecessor_boundary_invalid"}
    for other in versions:
        if other is version or other == version:
            continue
        other_hashes = other.get("batch_fixture_market_hashes")
        if isinstance(other_hashes, list) and set(hashes).intersection(other_hashes):
            return {**base, "reason": "cross_version_batch_identity_reuse"}

    wanted = set(hashes)
    matched: dict[str, list[tuple[dict[str, Any], dict[str, Any], tuple[str, ...]]]] = {}
    source_rows = _signature_rows_for_rollover(ledger, signature)
    for row in source_rows:
        if str(row.get("frozen_condition_signature") or "") != signature:
            continue
        marker = row.get("rollover_provenance")
        fixture_hash = marker.get("fixture_market_hash") if isinstance(marker, dict) else None
        if fixture_hash not in wanted:
            continue
        admitted, reason = validate_formal_row(
            row, system=system, signature=signature, frozen=frozen,
            projection_time=projection_time, require_settled=True, ledger=ledger,
            authority_context=authority_context,
        )
        repaired = copy.deepcopy(row) if admitted is not None else None
        fields: tuple[str, ...] = ()
        if repaired is None and allow_legacy_omissions:
            repaired, fields, _repair_reason = _legacy_formal_binding_repair_copy(
                row, system=system, signature=signature, frozen=frozen,
                projection_time=projection_time, require_settled=True,
                ledger=ledger,
                require_absent_native_stage_key=require_absent_native_stage_key,
            )
        if repaired is None:
            return {**base, "reason": reason or "batch_row_validation_failed"}
        matched.setdefault(str(fixture_hash), []).append((row, repaired, fields))
    if any(len(matched.get(value) or []) != 1 for value in hashes):
        return {**base, "reason": "batch_row_identity_mismatch"}

    rows = [matched[value][0] for value in hashes]
    ordered_rows = sorted(
        rows,
        key=lambda item: (
            _time(item[1]["rollover_provenance"]["stage_at"]),
            item[1]["rollover_provenance"]["fixture_market_hash"],
        ),
    )
    ordered = [
        item[1]["rollover_provenance"]["fixture_market_hash"]
        for item in ordered_rows
    ]
    actual_hits = sum(item[1].get("result") in BINARY_HIT_RESULTS for item in rows)
    final_stage = ordered_rows[-1][1]["rollover_provenance"]["stage_at"]
    if any(
        not _strictly_after(
            item[1]["rollover_provenance"]["stage_at"],
            predecessor_boundary,
        )
        for item in ordered_rows
    ):
        return {**base, "reason": "batch_row_not_after_predecessor_boundary"}
    if ordered != hashes:
        return {**base, "reason": "batch_order_mismatch"}
    if actual_hits != hits:
        return {**base, "reason": "batch_outcome_mismatch"}
    if final_stage != version.get("activation_boundary_at"):
        return {**base, "reason": "batch_activation_boundary_mismatch"}
    repairs = [
        {"row": original, "repaired": repaired, "fields": fields}
        for original, repaired, fields in rows if fields
    ]
    return {
        "complete": True, "reason": None, "eligible": [
            {
                "row": original, "validated_row": repaired,
                "fixture_market_hash": repaired["rollover_provenance"][
                    "fixture_market_hash"
                ],
                "stage_at": repaired["rollover_provenance"]["stage_at"],
                "hit": repaired.get("result") in BINARY_HIT_RESULTS,
            }
            for original, repaired, _fields in ordered_rows
        ],
        "ordered_fixture_market_hashes": ordered, "repairs": repairs,
        "expected_decided": decided, "expected_hits": hits,
        "version": version.get("version"),
    }


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
            and _formal_stage_provenance_valid(row, system)
            and not row.get("post_hoc_backfill")
            and not row.get("exclude_from_simulation")
            and str(row.get("frozen_condition_signature") or "") == signature
            and row.get("result") in BINARY_DECIDED_RESULTS
            and isinstance(marker, dict)
            and marker.get("condition_signature") == signature
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
        rows.append(_dashboard_evidence_row(row))
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
        card["pending_rollover_evidence"] = _project_pending_rollover_rows(
            ledger,
            system,
            str(card.get("condition_signature") or ""),
            card.get("active_evidence"),
            card.get("pending_progress"),
        )
    return projected


def _strict_int(value: Any) -> int | None:
    """Return a real JSON-style integer, never a bool/coerced string/float."""
    return value if isinstance(value, int) and not isinstance(value, bool) else None


def _sha256_hex(value: Any, *, length: int = 64) -> bool:
    return (
        isinstance(value, str)
        and len(value) == length
        and all(char in "0123456789abcdef" for char in value)
    )


def _same_optional_number(actual: Any, expected: Any) -> bool:
    if actual is None or expected is None:
        return actual is None and expected is None
    left, right = _number(actual), _number(expected)
    return (
        left is not None and right is not None
        and math.isfinite(left) and math.isfinite(right)
        and math.isclose(left, right, rel_tol=1e-12, abs_tol=1e-12)
    )


def _funnel_unavailable(system: str, reason: str) -> dict[str, Any]:
    return {
        "schema_version": 1,
        "read_only": True,
        "system": system,
        "condition_count": 0,
        "conditions": [],
        "unavailable_reason": reason,
    }


def _validate_frozen_identity_and_chain(
    frozen: dict[str, Any], signature: str, system: str, *,
    authority_context: Any = None,
) -> tuple[dict[str, Any] | None, list[dict[str, Any]] | None, str | None]:
    """Validate one immutable definition and its complete evidence hash chain."""
    definition = frozen.get("definition")
    expected_definition_keys = {
        "system", "version", "market", "stage", "path", "direction", "role",
        "line_bucket", "odds_tier", "movement", "odds_trajectory", "miner_key",
    }
    if (
        not isinstance(definition, dict)
        or set(definition) != expected_definition_keys
        or definition.get("system") != system
        or not isinstance(definition.get("version"), str)
        or not isinstance(definition.get("market"), str)
        or not isinstance(definition.get("stage"), str)
        or not isinstance(definition.get("miner_key"), list)
        or any(not isinstance(item, str) for item in definition["miner_key"])
    ):
        return None, None, "frozen_condition_definition_invalid"
    raw = json.dumps(
        definition, ensure_ascii=False, sort_keys=True, separators=(",", ":"),
    )
    rebuilt_signature = hashlib.sha256(raw.encode()).hexdigest()[:24]
    if (
        not _sha256_hex(signature, length=24)
        or rebuilt_signature != signature
        or frozen.get("signature") != signature
    ):
        return None, None, "frozen_condition_signature_mismatch"

    versions = frozen.get("evidence_versions")
    if (
        not isinstance(versions, list)
        or not versions
        or any(not isinstance(row, dict) for row in versions)
    ):
        return copy.deepcopy(definition), None, "evidence_version_chain_invalid"

    validated: list[dict[str, Any]] = []
    previous: dict[str, Any] | None = None
    previous_boundary: datetime | None = None
    previous_created_at: datetime | None = None
    for index, row in enumerate(versions, start=1):
        version = _strict_int(row.get("version"))
        batch_hits = _strict_int(row.get("batch_hits"))
        batch_decided = _strict_int(row.get("batch_decided"))
        cumulative_hits = _strict_int(row.get("cumulative_hits"))
        cumulative_decided = _strict_int(row.get("cumulative_decided"))
        boundary = _time(row.get("activation_boundary_at"))
        created_at = _time(row.get("created_at"))
        hashes = row.get("batch_fixture_market_hashes")
        aggregate_reason = None
        is_legacy_ordinary_aggregate = (
            "legacy_ordinary_batch_aggregate" in row
        )
        if is_legacy_ordinary_aggregate:
            from .legacy_batch_aggregate import validate_aggregate_version
            aggregate_reason = validate_aggregate_version(
                row, system, signature, authority_context,
            )
            if aggregate_reason is not None:
                return copy.deepcopy(definition), None, aggregate_reason
        if (
            version != index
            or row.get("condition_signature") != signature
            or batch_hits is None or batch_decided is None
            or cumulative_hits is None or cumulative_decided is None
            or not 0 <= batch_hits <= batch_decided
            or not 0 <= cumulative_hits <= cumulative_decided
            or boundary is None or created_at is None
            or created_at < boundary
            or not isinstance(hashes, list)
            or any(not _sha256_hex(value) for value in hashes)
            or len(set(hashes)) != len(hashes)
            or not _sha256_hex(row.get("evidence_hash"))
            or row["evidence_hash"] != _version_hash(row)
        ):
            return copy.deepcopy(definition), None, "evidence_version_chain_invalid"
        legacy_batch = (
            row.get("initial_migration_full_cohort") is True
            and row.get("batch_fixture_market_ids_unavailable_from_legacy_aggregate") is True
        )
        legacy_cohort = row.get("legacy_prospective_cohort")
        legacy_batch_valid = (
            legacy_batch
            and index == 2
            and previous is not None
            and previous.get("version") == 1
            and previous.get("migration_baseline") is True
            and created_at == boundary
            and isinstance(legacy_cohort, dict)
            and set(legacy_cohort) == {"hits", "decided", "pushes"}
            and _strict_int(legacy_cohort.get("hits")) == batch_hits
            and _strict_int(legacy_cohort.get("decided")) == batch_decided
            and _strict_int(legacy_cohort.get("pushes")) is not None
            and legacy_cohort["pushes"] >= 0
        )
        if (
            (
                len(hashes) != batch_decided
                and not legacy_batch
                and not is_legacy_ordinary_aggregate
            )
            or (
                is_legacy_ordinary_aggregate
                and (hashes != [] or batch_decided != ROLLOVER_BATCH_SIZE)
            )
            or (legacy_batch and hashes)
            or (legacy_batch and not legacy_batch_valid)
            or (
                not legacy_batch
                and (
                    row.get("initial_migration_full_cohort") is not None
                    or row.get("batch_fixture_market_ids_unavailable_from_legacy_aggregate") is not None
                    or row.get("legacy_prospective_cohort") is not None
                )
            )
            or (previous_boundary is not None and boundary < previous_boundary)
            or (previous_created_at is not None and created_at < previous_created_at)
            or (
                previous_boundary is not None
                and boundary == previous_boundary
                and not legacy_batch
            )
        ):
            return copy.deepcopy(definition), None, "evidence_version_chain_invalid"
        values = _evidence_values(cumulative_hits, cumulative_decided)
        if (
            not _same_optional_number(
                row.get("wilson95_lower_raw"), values["wilson95_lower_raw"],
            )
            or not _same_optional_number(
                row.get("minimum_acceptable_odds_raw"),
                values["minimum_acceptable_odds_raw"],
            )
        ):
            return copy.deepcopy(definition), None, "evidence_version_chain_invalid"
        if previous is None:
            if (
                row.get("prior_version") is not None
                or row.get("prior_evidence_hash") is not None
            ):
                return copy.deepcopy(definition), None, "evidence_version_chain_invalid"
        else:
            if (
                row.get("prior_version") != previous["version"]
                or row.get("prior_evidence_hash") != previous["evidence_hash"]
                or cumulative_hits != previous["cumulative_hits"] + batch_hits
                or cumulative_decided != previous["cumulative_decided"] + batch_decided
            ):
                return copy.deepcopy(definition), None, "evidence_version_chain_invalid"
        validated.append(row)
        previous, previous_boundary, previous_created_at = row, boundary, created_at

    active = validated[-1]
    if (
        _strict_int(frozen.get("active_evidence_version")) != active["version"]
        or frozen.get("active_evidence_hash") != active["evidence_hash"]
    ):
        return copy.deepcopy(definition), None, "active_evidence_pointer_mismatch"
    pointer = frozen.get("active_evidence")
    if pointer is not None:
        pointer_keys = (
            "version", "cumulative_hits", "cumulative_decided",
            "wilson95_lower_raw", "minimum_acceptable_odds_raw",
            "minimum_acceptable_odds_display", "activation_boundary_at",
            "created_at", "evidence_hash",
        )
        if (
            not isinstance(pointer, dict)
            or any(key not in pointer for key in pointer_keys)
            or any(pointer.get(key) != active.get(key) for key in pointer_keys)
        ):
            return copy.deepcopy(definition), None, "active_evidence_pointer_mismatch"
    return copy.deepcopy(definition), validated, None


def validate_production_identity_manifest_v1(
    ns: dict[str, Any], system: str,
) -> tuple[dict[str, Any] | None, str | None]:
    """Validate the immutable definition/v1 root without later-chain trust."""
    conditions, order = ns.get("conditions"), ns.get("condition_order")
    if (
        not isinstance(conditions, dict) or not isinstance(order, list)
        or not order or len(order) != len(set(order))
        or set(order) != set(conditions)
    ):
        return None, "frozen_condition_registry_malformed"
    entries = []
    for number, signature in enumerate(order, 1):
        frozen = conditions.get(signature)
        if not isinstance(frozen, dict) or _strict_int(
            frozen.get("condition_number")
        ) != number:
            return None, "frozen_condition_number_registry_malformed"
        definition = frozen.get("definition")
        versions = frozen.get("evidence_versions")
        if (
            not isinstance(definition, dict)
            or _canonical_hash(definition)[:24] != signature
            or frozen.get("signature") != signature
            or not isinstance(versions, list) or not versions
            or not isinstance(versions[0], dict)
        ):
            return None, "production_identity_v1_invalid"
        first = versions[0]
        if (
            first.get("version") != 1
            or first.get("condition_signature") != signature
            or first.get("prior_version") is not None
            or first.get("prior_evidence_hash") is not None
            or first.get("batch_fixture_market_hashes") != []
            or first.get("batch_hits") != 0
            or first.get("batch_decided") != 0
            or first.get("evidence_hash") != _version_hash(first)
        ):
            return None, "production_identity_v1_invalid"
        entries.append({
            "condition_number": number,
            "condition_signature": signature,
            "definition_hash": _canonical_hash(definition),
            "initial_evidence_hash": first["evidence_hash"],
        })
    body = {
        "schema_version": PRODUCTION_IDENTITY_MANIFEST_SCHEMA_VERSION,
        "manifest_version": PRODUCTION_IDENTITY_MANIFEST_VERSION,
        "system": system,
        "immutable": True,
        "entries": entries,
    }
    expected = {**body, "manifest_hash": _canonical_hash(body)}
    if ns.get("production_identity_manifest") != expected:
        return None, "immutable_production_identity_manifest_mismatch"
    return copy.deepcopy(expected), None


def _expected_production_identity_manifest(
    ns: dict[str, Any], system: str, *, authority_context: Any = None,
) -> tuple[dict[str, Any] | None, dict[str, tuple[dict[str, Any], list[dict[str, Any]]]] | None, str | None]:
    """Build the deterministic identity root from an already-frozen registry."""
    conditions, order = ns.get("conditions"), ns.get("condition_order")
    if not isinstance(conditions, dict) or not isinstance(order, list):
        return None, None, "frozen_condition_registry_unavailable"
    if (
        not order
        or any(not isinstance(signature, str) for signature in order)
        or len(order) != len(set(order))
        or set(order) != set(conditions)
        or any(not isinstance(conditions.get(signature), dict) for signature in order)
    ):
        return None, None, "frozen_condition_registry_malformed"

    entries: list[dict[str, Any]] = []
    validated: dict[str, tuple[dict[str, Any], list[dict[str, Any]]]] = {}
    for expected_number, signature in enumerate(order, start=1):
        frozen = conditions[signature]
        number = _strict_int(frozen.get("condition_number"))
        if number != expected_number:
            return None, None, "frozen_condition_number_registry_malformed"
        definition, versions, reason = _validate_frozen_identity_and_chain(
            frozen, signature, system, authority_context=authority_context,
        )
        if reason is not None or definition is None or versions is None:
            return None, None, reason or "frozen_identity_unavailable"
        validated[signature] = (definition, versions)
        entries.append({
            "condition_number": number,
            "condition_signature": signature,
            "definition_hash": _canonical_hash(definition),
            "initial_evidence_hash": versions[0]["evidence_hash"],
        })
    body = {
        "schema_version": PRODUCTION_IDENTITY_MANIFEST_SCHEMA_VERSION,
        "manifest_version": PRODUCTION_IDENTITY_MANIFEST_VERSION,
        "system": system,
        "immutable": True,
        "entries": entries,
    }
    return {**body, "manifest_hash": _canonical_hash(body)}, validated, None


def create_production_identity_manifest(
    ledger: dict[str, Any], system: str, *, authorized_manifest: dict[str, Any] | None = None,
    trusted_manifest_hash: str | None = None,
) -> dict[str, Any]:
    """Persist an independently authorized production identity root exactly once.

    Authority must be supplied independently as either the complete canonical
    manifest document or its trusted expected hash.  The current mutable
    registry can prove equality with that authority, but can never authorize
    itself. Existing manifests are verified and never overwritten.
    """
    if (authorized_manifest is None) == (trusted_manifest_hash is None):
        raise ValueError("supply exactly one independent manifest authority")
    ns = ledger.get(NAMESPACE)
    if (
        not isinstance(ns, dict)
        or ns.get("schema_version") != SCHEMA_VERSION
        or ns.get("system") != system
    ):
        raise ValueError("validated Wilson namespace required")
    expected, _validated, reason = _expected_production_identity_manifest(ns, system)
    if expected is None:
        raise ValueError(reason or "validated frozen registry required")
    expected_bytes = json.dumps(
        expected, ensure_ascii=False, sort_keys=True, separators=(",", ":"),
    ).encode("utf-8")
    if authorized_manifest is not None:
        if not isinstance(authorized_manifest, dict):
            raise ValueError("authorized production identity manifest must be an object")
        authorized_bytes = json.dumps(
            authorized_manifest, ensure_ascii=False, sort_keys=True, separators=(",", ":"),
        ).encode("utf-8")
        if authorized_bytes != expected_bytes:
            raise ValueError("authorized production identity manifest mismatch")
    elif (
        not _sha256_hex(trusted_manifest_hash)
        or trusted_manifest_hash != expected["manifest_hash"]
    ):
        raise ValueError("trusted production identity manifest hash mismatch")
    existing = ns.get("production_identity_manifest")
    if existing is not None:
        if existing != expected:
            raise ValueError("immutable production identity manifest mismatch")
        return copy.deepcopy(existing)
    ns["production_identity_manifest"] = copy.deepcopy(expected)
    return copy.deepcopy(expected)


def _historical_activity_hashes(
    ledger: dict[str, Any], source_signature: str,
) -> list[str]:
    """Seal every historical source row without treating it as target evidence."""
    ns = ledger.get(NAMESPACE)
    observations = ns.get("observations") if isinstance(ns, dict) else None
    output: list[str] = []
    for container, rows in (
        ("bets", ledger.get("bets")),
        ("wilson_validation.observations", observations),
    ):
        if not isinstance(rows, list):
            continue
        for row in rows:
            if (
                isinstance(row, dict)
                and str(row.get("frozen_condition_signature") or "")
                == source_signature
            ):
                output.append(_canonical_hash({"container": container, "row": row}))
    return sorted(output)


def _known_condition_identity_migration_required(
    ns: dict[str, Any], system: str,
) -> bool:
    """Recognize only the closed production duplicate pairs, not generic ledgers."""
    if system != "footbreak":
        return False
    conditions, order = ns.get("conditions"), ns.get("condition_order")
    if not isinstance(conditions, dict) or not isinstance(order, list):
        return False
    for entry in CONDITION_IDENTITY_MIGRATION_ALLOWLIST:
        source_number = entry["source_condition_number"]
        target_number = entry["target_condition_number"]
        if (
            len(order) < max(source_number, target_number)
            or order[source_number - 1] != entry["source_signature"]
            or order[target_number - 1] != entry["target_signature"]
            or entry["source_signature"] not in conditions
            or entry["target_signature"] not in conditions
        ):
            return False
    return True


def _source_activity_timestamps_valid(
    ledger: dict[str, Any], source_signature: str, effective_at: Any,
) -> bool:
    """Require at least one parseable admission/creation time, all pre-cutoff."""
    cutoff = _time(effective_at)
    if cutoff is None:
        return False
    ns = ledger.get(NAMESPACE)
    observations = ns.get("observations") if isinstance(ns, dict) else None
    for rows in (ledger.get("bets"), observations):
        if not isinstance(rows, list):
            continue
        for row in rows:
            if (
                not isinstance(row, dict)
                or str(row.get("frozen_condition_signature") or "")
                != source_signature
            ):
                continue
            raw_timestamps = [
                row.get(key) for key in (
                    "created_at", "admission_at", "native_stage_at",
                ) if row.get(key) is not None
            ]
            marker = row.get("rollover_provenance")
            if isinstance(marker, dict) and marker.get("stage_at") is not None:
                raw_timestamps.append(marker["stage_at"])
            parsed = [_time(value) for value in raw_timestamps]
            if (
                not raw_timestamps
                or any(value is None for value in parsed)
                or any(value >= cutoff for value in parsed if value is not None)
            ):
                return False
    return True


def _migration_activity_root(activity: dict[str, Any]) -> str:
    return _canonical_hash({
        "scope": activity["scope"],
        "row_count": activity["row_count"],
        "row_hashes": activity["row_hashes"],
    })


def _validate_condition_identity_migration_document(
    ledger: dict[str, Any], system: str, document: Any, *,
    authority_context: Any = None,
) -> tuple[dict[str, dict[str, Any]] | None, str | None]:
    """Validate the independently authorized, closed duplicate-retirement root."""
    if system != "footbreak":
        return None, "condition_identity_migrations_cross_system"
    ns = ledger.get(NAMESPACE)
    if not isinstance(ns, dict):
        return None, "condition_identity_migrations_namespace_unavailable"
    document_keys = {
        "schema_version", "migration_version", "system", "immutable",
        "effective_at", "authority", "entries", "manifest_hash",
    }
    authority_keys = {
        "kind", "release_commit", "production_identity_manifest_hash",
    }
    entry_keys = {
        "source_condition_number", "source_signature",
        "source_definition_hash", "source_initial_evidence_hash", "relation",
        "target_condition_number", "target_signature", "target_definition_hash",
        "canonicalization", "historical_activity", "future_admission",
        "evidence_merge",
    }
    activity_keys = {
        "scope", "row_count", "row_hashes", "rows_are_evidence", "root_hash",
    }
    if not isinstance(document, dict) or set(document) != document_keys:
        return None, "condition_identity_migrations_malformed"
    body = {key: value for key, value in document.items() if key != "manifest_hash"}
    if (
        document.get("schema_version")
        != CONDITION_IDENTITY_MIGRATION_SCHEMA_VERSION
        or document.get("migration_version")
        != CONDITION_IDENTITY_MIGRATION_VERSION
        or document.get("system") != "footbreak"
        or document.get("immutable") is not True
        or _time(document.get("effective_at")) is None
        or not _sha256_hex(document.get("manifest_hash"))
        or document.get("manifest_hash") != _canonical_hash(body)
    ):
        return None, "condition_identity_migrations_header_invalid"
    authority = document.get("authority")
    if (
        not isinstance(authority, dict) or set(authority) != authority_keys
        or authority.get("kind") != "reviewed-manifest-sha256"
        or not _sha256_hex(authority.get("release_commit"), length=40)
        or authority.get("release_commit") == "0" * 40
        or not _sha256_hex(authority.get("production_identity_manifest_hash"))
    ):
        return None, "condition_identity_migrations_authority_invalid"
    expected_production, reason = validate_production_identity_manifest_v1(
        ns, system,
    )
    if expected_production is None:
        return None, reason or "production_identity_manifest_invalid"
    if (
        ns.get("production_identity_manifest") != expected_production
        or authority["production_identity_manifest_hash"]
        != expected_production["manifest_hash"]
    ):
        return None, "condition_identity_migrations_production_manifest_mismatch"
    entries = document.get("entries")
    if not isinstance(entries, list) or len(entries) != len(
        CONDITION_IDENTITY_MIGRATION_ALLOWLIST
    ):
        return None, "condition_identity_migrations_entries_invalid"
    conditions, order = ns.get("conditions"), ns.get("condition_order")
    if not isinstance(conditions, dict) or not isinstance(order, list):
        return None, "condition_identity_migrations_registry_invalid"
    if (
        len(order) != 17
        or len(conditions) != 17
        or len(CONDITION_IDENTITY_MIGRATION_ALLOWLIST) != 2
    ):
        return None, "condition_identity_migrations_registry_cardinality_invalid"
    result: dict[str, dict[str, Any]] = {}
    targets: set[str] = set()
    for entry, allowed in zip(entries, CONDITION_IDENTITY_MIGRATION_ALLOWLIST):
        if not isinstance(entry, dict) or set(entry) != entry_keys:
            return None, "condition_identity_migrations_entry_malformed"
        for key, expected_value in allowed.items():
            if entry.get(key) != expected_value:
                return None, "condition_identity_migrations_entry_not_allowlisted"
        if (
            entry.get("relation") != "retired_duplicate_of"
            or entry.get("canonicalization")
            != "condition_definition_from_source_miner_key"
            or entry.get("future_admission") != "target_only"
            or entry.get("evidence_merge") != "none"
        ):
            return None, "condition_identity_migrations_policy_invalid"
        source, target = entry["source_signature"], entry["target_signature"]
        if (
            source == target or source in result or target in targets
            or target in result or source in targets
        ):
            return None, "condition_identity_migrations_chain_or_cycle"
        source_frozen, target_frozen = conditions.get(source), conditions.get(target)
        if not isinstance(source_frozen, dict) or not isinstance(target_frozen, dict):
            return None, "condition_identity_migrations_unknown_identity"
        if (
            order[entry["source_condition_number"] - 1] != source
            or order[entry["target_condition_number"] - 1] != target
        ):
            return None, "condition_identity_migrations_number_mismatch"
        source_definition, source_versions, source_reason = (
            _validate_frozen_identity_and_chain(
                source_frozen, source, system,
                authority_context=authority_context,
            )
        )
        target_definition, target_versions, target_reason = (
            _validate_frozen_identity_and_chain(
                target_frozen, target, system,
                authority_context=authority_context,
            )
        )
        if (
            source_reason is not None or target_reason is not None
            or source_definition is None or target_definition is None
            or source_versions is None or target_versions is None
        ):
            return None, source_reason or target_reason or "frozen_identity_invalid"
        if (
            _canonical_hash(source_definition) != entry["source_definition_hash"]
            or _canonical_hash(target_definition) != entry["target_definition_hash"]
            or source_versions[0]["evidence_hash"]
            != entry["source_initial_evidence_hash"]
        ):
            return None, "condition_identity_migrations_frozen_hash_mismatch"
        rebuilt, rebuilt_definition = condition_signature(system, {
            **copy.deepcopy(source_definition),
            "key": copy.deepcopy(source_definition.get("miner_key")),
        })
        target_roundtrip, target_roundtrip_definition = condition_signature(
            system, {
                **copy.deepcopy(target_definition),
                "key": copy.deepcopy(target_definition.get("miner_key")),
            },
        )
        if (
            rebuilt != target or rebuilt_definition != target_definition
            or target_roundtrip != target
            or target_roundtrip_definition != target_definition
            or formal_matcher_axes(
                {**target_definition, "key": target_definition.get("miner_key")},
                system=system,
                decision_stage=str(target_definition.get("stage") or ""),
            ) is None
        ):
            return None, "condition_identity_migrations_canonicalization_invalid"
        activity = entry.get("historical_activity")
        hashes = _historical_activity_hashes(ledger, source)
        if (
            not isinstance(activity, dict) or set(activity) != activity_keys
            or activity.get("scope")
            != ["bets", "wilson_validation.observations"]
            or type(activity.get("row_count")) is not int
            or activity["row_count"] < 0
            or not isinstance(activity.get("row_hashes"), list)
            or any(not _sha256_hex(value) for value in activity["row_hashes"])
            or activity["row_hashes"] != sorted(activity["row_hashes"])
            or activity.get("rows_are_evidence") is not False
            or activity["row_count"] != len(activity["row_hashes"])
            or activity["row_count"] != len(hashes)
            or activity["row_hashes"] != hashes
            or activity.get("root_hash") != _migration_activity_root(activity)
            or not _source_activity_timestamps_valid(
                ledger, source, document["effective_at"],
            )
        ):
            return None, "condition_identity_migrations_historical_activity_drift"
        result[source] = copy.deepcopy(entry)
        targets.add(target)
    if set(result) != {
        row["source_signature"] for row in CONDITION_IDENTITY_MIGRATION_ALLOWLIST
    }:
        return None, "condition_identity_migrations_missing_entry"
    if len(order) - len(result) != 15:
        return None, "condition_identity_migrations_registry_cardinality_invalid"
    return result, None


def _validate_condition_identity_migrations(
    ledger: dict[str, Any], system: str, *, authority_context: Any = None,
) -> tuple[dict[str, dict[str, Any]] | None, str | None]:
    """Absent is valid; present malformed retirement metadata fails closed."""
    ns = ledger.get(NAMESPACE)
    if not isinstance(ns, dict):
        return {}, None
    document = ns.get("condition_identity_migrations")
    if document is None:
        if _known_condition_identity_migration_required(ns, system):
            return None, "required_condition_identity_migration_missing"
        return {}, None
    return _validate_condition_identity_migration_document(
        ledger, system, document, authority_context=authority_context,
    )


def _identity_projection(
    ns: dict[str, Any], retired: dict[str, dict[str, Any]],
) -> list[dict[str, Any]]:
    return [
        {
            "condition_number": index,
            "condition_signature": signature,
            "identity_status": (
                "retired_duplicate" if signature in retired else "active"
            ),
            "canonical_successor_signature": (
                retired[signature]["target_signature"]
                if signature in retired else None
            ),
        }
        for index, signature in enumerate(ns.get("condition_order") or [], start=1)
    ]


def plan_condition_identity_migration(
    ledger: dict[str, Any], system: str, authorized_manifest: dict[str, Any], *,
    expected_release_commit: str,
) -> dict[str, Any]:
    """Purely prove an externally authored retirement document and its projection."""
    before = copy.deepcopy(ledger)
    ns = ledger.get(NAMESPACE)
    if not isinstance(ns, dict) or ns.get("condition_identity_migrations") is not None:
        raise ValueError("condition identity migration must be absent when planning")
    if not isinstance(authorized_manifest, dict):
        raise ValueError("authorized migration document must be an object")
    if (
        not _sha256_hex(expected_release_commit, length=40)
        or expected_release_commit == "0" * 40
        or authorized_manifest.get("authority", {}).get("release_commit")
        != expected_release_commit
    ):
        raise ValueError("authorized migration release commit mismatch")
    retired, reason = _validate_condition_identity_migration_document(
        ledger, system, authorized_manifest,
    )
    if retired is None:
        raise ValueError(reason or "condition identity migration invalid")
    if ledger != before:
        raise RuntimeError("condition identity migration planning mutated ledger")
    return {
        "status": "ready",
        "system": system,
        "post_document": copy.deepcopy(authorized_manifest),
        "condition_identity_migrations": copy.deepcopy(authorized_manifest),
        "before_identity_projection_hash": _canonical_hash(
            _identity_projection(ns, {}),
        ),
        "after_identity_projection_hash": _canonical_hash(
            _identity_projection(ns, retired),
        ),
        "historical_condition_count": len(ns["condition_order"]),
        "active_condition_count": len(ns["condition_order"]) - len(retired),
        "retired_duplicate_count": len(retired),
    }


def apply_condition_identity_migration(
    ledger: dict[str, Any], system: str, authorized_manifest: dict[str, Any] | None = None,
    trusted_manifest_hash: str | None = None, *, candidate_manifest: dict[str, Any] | None = None,
    expected_release_commit: str | None = None,
) -> dict[str, Any]:
    """Insert the authorized immutable root once; never repair or overwrite it."""
    if (authorized_manifest is None) == (trusted_manifest_hash is None):
        raise ValueError("supply exactly one independent migration authority")
    ns = ledger.get(NAMESPACE)
    if not isinstance(ns, dict):
        raise ValueError("validated Wilson namespace required")
    existing = ns.get("condition_identity_migrations")
    if existing is not None:
        retired, reason = _validate_condition_identity_migrations(ledger, system)
        if retired is None:
            raise ValueError(reason or "immutable condition identity migration invalid")
        if authorized_manifest is not None and existing != authorized_manifest:
            raise ValueError("immutable condition identity migration mismatch")
        if trusted_manifest_hash is not None and (
            not _sha256_hex(trusted_manifest_hash)
            or existing.get("manifest_hash") != trusted_manifest_hash
        ):
            raise ValueError("trusted condition identity migration hash mismatch")
        return copy.deepcopy(existing)
    if expected_release_commit is None:
        raise ValueError("expected release commit required for initial insertion")
    document = authorized_manifest
    if trusted_manifest_hash is not None:
        if (
            not isinstance(candidate_manifest, dict)
            or not _sha256_hex(trusted_manifest_hash)
            or candidate_manifest.get("manifest_hash") != trusted_manifest_hash
        ):
            raise ValueError("trusted condition identity migration hash mismatch")
        document = candidate_manifest
    elif candidate_manifest is not None:
        raise ValueError("candidate manifest is only valid with trusted hash authority")
    if not isinstance(document, dict):
        raise ValueError("authorized migration document required for initial insertion")
    plan_condition_identity_migration(
        ledger, system, document,
        expected_release_commit=expected_release_commit,
    )
    ns["condition_identity_migrations"] = copy.deepcopy(document)
    retired, reason = _validate_condition_identity_migrations(ledger, system)
    if retired is None:
        ns.pop("condition_identity_migrations", None)
        raise ValueError(reason or "condition identity migration readback invalid")
    return copy.deepcopy(document)


def _unavailable_condition_card(
    number: int, reason: str,
) -> dict[str, Any]:
    stage = {
        "available": False, "availability": "unavailable", "count": None,
        "reason": reason,
    }
    progress = {
        **stage, "required": ROLLOVER_BATCH_SIZE, "display": None,
    }
    return {
        "condition_number": number,
        "identity_available": False,
        "condition_signature": None,
        "condition_version": None,
        "definition": None,
        "frozen_at": None,
        "unavailable_reason": reason,
        "active_evidence": {
            "version": None, "unavailable_reason": reason,
        },
        "stages": {
            "eligible_post_activation_t5_observations": copy.deepcopy(stage),
            "exact_condition_matches": copy.deepcopy(stage),
            "recorded_formal_evidence": copy.deepcopy(stage),
            "settled_valid_evidence": copy.deepcopy(stage),
            "current_rollover_progress": progress,
        },
        "rejections": {
            "bounded": True,
            "visible_limit": FUNNEL_REJECTION_LIMIT,
            "audit_window_limit": CONDITION_AUDIT_LIMIT,
            "audit_truncation_possible": False,
            "items": [],
            "omitted_reason_kinds": 0,
        },
    }


def _validate_formal_admission_binding(
    row: dict[str, Any], *, observation: bool, system: str, signature: str,
    definition: dict[str, Any], frozen: dict[str, Any],
    version_by_number: dict[int, dict[str, Any]], projection_time: datetime,
    ledger: dict[str, Any] | None,
) -> tuple[dict[str, Any] | None, str | None]:
    """Validate the immutable native admission before any funnel count."""
    fixture = row.get("match_id")
    market = row.get("market")
    marker = row.get("rollover_provenance")
    evidence_version = _strict_int(row.get("evidence_version"))
    admitted = version_by_number.get(evidence_version)
    stage_at = _time(marker.get("stage_at")) if isinstance(marker, dict) else None
    created_at = _time(row.get("created_at"))
    admission_at = _time(row.get("admission_at")) if row.get("admission_at") is not None else None
    kickoff = _time(row.get("kickoff") or row.get("kickoff_hkt"))
    condition_number_value = _strict_int(row.get("condition_number"))
    expected_number = _strict_int(frozen.get("condition_number"))
    historical = row.get("frozen_historical_evidence")
    baseline_history = frozen.get("historical_evidence")
    immutable_historical = (
        {
            key: value for key, value in historical.items()
            if key not in {"hits", "decided", "evidence_version", "evidence_hash"}
        }
        if isinstance(historical, dict) else None
    )
    immutable_baseline = (
        {
            key: value for key, value in baseline_history.items()
            if key not in {"hits", "decided", "evidence_version", "evidence_hash"}
        }
        if isinstance(baseline_history, dict) else None
    )
    if (
        not isinstance(fixture, str) or not fixture
        or market not in {"HDC", "HIL", "CHL"}
        or (row.get("code") is not None and row.get("code") != market)
        or (observation and row.get("formal_bet") is not False)
        or (not observation and row.get("formal_bet") is False)
        or not _formal_stage_provenance_valid(row, system)
        or row.get("post_hoc_backfill") not in (None, False)
        or row.get("exclude_from_simulation") not in (None, False)
        or row.get("frozen_condition_signature") != signature
        or row.get("frozen_condition_definition") != definition
        or definition.get("market") != market
        or condition_number_value != expected_number
        or admitted is None
        or row.get("evidence_hash") != admitted.get("evidence_hash")
        or not isinstance(marker, dict)
        or not _formal_marker_shape_valid(marker)
        or row.get("native_stage_at") != marker.get("stage_at")
        or not isinstance(historical, dict)
        or not isinstance(baseline_history, dict)
        or immutable_historical != immutable_baseline
        or _strict_int(historical.get("hits")) != admitted.get("cumulative_hits")
        or _strict_int(historical.get("decided")) != admitted.get("cumulative_decided")
        or historical.get("evidence_version") != evidence_version
        or historical.get("evidence_hash") != admitted.get("evidence_hash")
        or marker.get("admitted_evidence_version") != evidence_version
        or marker.get("admitted_evidence_hash") != admitted.get("evidence_hash")
        or marker.get("fixture_market_hash")
        != _fixture_market_hash(system, fixture, market)
        or stage_at is None or created_at is None or kickoff is None
        or not _strictly_after(
            marker.get("stage_at"), admitted.get("activation_boundary_at"),
        )
        or stage_at > created_at
        or (admission_at is not None and not stage_at <= admission_at <= created_at)
        or created_at >= kickoff
        or stage_at >= kickoff
        or stage_at > projection_time
        or created_at > projection_time
        or _time(admitted.get("created_at")) is None
        or _time(admitted.get("created_at")) > stage_at
        or any(
            _time(later.get("created_at")) is None
            or _time(later.get("activation_boundary_at")) is None
            or (
                _strict_int(later.get("version")) is not None
                and _strict_int(later.get("version")) > evidence_version
                and _time(later.get("created_at")) <= stage_at
                and stage_at >= _time(later.get("activation_boundary_at"))
            )
            for later in version_by_number.values()
            if isinstance(later, dict)
            and _strict_int(later.get("version")) is not None
            and _strict_int(later.get("version")) > evidence_version
        )
    ):
        return None, "invalid_formal_admission_binding"

    arithmetic = row.get("wilson_admission")
    row_line = _number(row.get("line", row.get("condition")))
    row_fraction = (
        abs(row_line) - math.floor(abs(row_line))
        if row_line is not None else None
    )
    quarter_line = (
        market == "HIL" and row_fraction is not None
        and (
            abs(row_fraction - 0.25) <= 1e-8
            or abs(row_fraction - 0.75) <= 1e-8
        )
    )
    settlement_schema = row.get("quarter_line_settlement_schema_version")
    quarter_activation_at = _time(
        ((ledger or {}).get(NAMESPACE) or {}).get(
            "quarter_settlement_activation_at"
        )
    )
    created_before_quarter_activation = (
        created_at is not None
        and quarter_activation_at is not None
        and created_at < quarter_activation_at
    )
    legacy_quarter = (
        quarter_line
        and settlement_schema is None
        and row.get("quarter_line_settlement") is None
        and row.get("native_snapshot_binding") is None
        and created_before_quarter_activation
    )
    if quarter_line and not legacy_quarter and (
        settlement_schema != 2
        or (
        _quarter_line_profile(
            row.get("quarter_line_settlement"),
            market=market, side=row.get("side"), line=row_line,
        ) is None
        or not _quarter_snapshot_binding_valid(ledger, row, system)
        )
    ):
        return None, "invalid_formal_admission_binding"
    expected_arithmetic = admission_arithmetic(
        admitted["cumulative_hits"], admitted["cumulative_decided"], row.get("odds"),
        settlement_profile=row.get("quarter_line_settlement"),
    )
    legacy_arithmetic = (
        isinstance(arithmetic, dict)
        and "binary_minimum_acceptable_odds_raw" not in arithmetic
        and "settlement_adjusted" not in arithmetic
        and "settlement_profile" not in arithmetic
        and (not quarter_line or legacy_quarter)
    )
    arithmetic_keys = (
        "hits", "decided", "hit_rate_raw", "wilson95_lower_raw",
        "wilson95_upper_raw", "actual_decimal_odds_raw", "break_even_rate_raw",
        "required_rate_raw", "minimum_acceptable_odds_raw",
        *((() if legacy_arithmetic else ("binary_minimum_acceptable_odds_raw",))),
        "passes",
    )
    if (
        not isinstance(arithmetic, dict)
        or expected_arithmetic is None
        or (not legacy_arithmetic and arithmetic.get("settlement_adjusted") is not expected_arithmetic.get(
            "settlement_adjusted"
        ))
        or (not legacy_arithmetic and arithmetic.get("settlement_profile") != expected_arithmetic.get(
            "settlement_profile"
        ))
        or (
            row.get("stage") == DECISION_STAGE
            and arithmetic.get("passes") is not (not observation)
        )
        or any(
            (
                arithmetic.get(key) != expected_arithmetic.get(key)
                if key in {"hits", "decided", "passes"}
                else not _same_optional_number(
                    arithmetic.get(key), expected_arithmetic.get(key),
                )
            )
            for key in arithmetic_keys
        )
    ):
        return None, "invalid_formal_admission_binding"
    return admitted, None


def project_condition_funnel(
    ledger: dict[str, Any], system: str, *, authority_context: Any = None,
) -> dict[str, Any]:
    """Return a pure, fail-closed funnel from already-durable Wilson evidence."""
    ns = ledger.get(NAMESPACE)
    if (
        not isinstance(ns, dict)
        or ns.get("schema_version") != SCHEMA_VERSION
        or str(ns.get("system") or "") != system
    ):
        return _funnel_unavailable(system, "wilson_namespace_unavailable")
    conditions = ns.get("conditions")
    order = ns.get("condition_order")
    expected_manifest, validated_registry, registry_reason = (
        _expected_production_identity_manifest(
            ns, system, authority_context=authority_context,
        )
    )
    if expected_manifest is None or validated_registry is None:
        return _funnel_unavailable(
            system, registry_reason or "frozen_condition_registry_unavailable",
        )
    manifest = ns.get("production_identity_manifest")
    if (
        not isinstance(manifest, dict)
        or json.dumps(manifest, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
        != json.dumps(expected_manifest, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    ):
        return _funnel_unavailable(system, "production_identity_manifest_unavailable_or_mismatch")
    retired, migration_reason = _validate_condition_identity_migrations(
        ledger, system, authority_context=authority_context,
    )
    if retired is None:
        return _funnel_unavailable(
            system, migration_reason or "condition_identity_migrations_invalid",
        )
    numbers = [conditions[signature]["condition_number"] for signature in order]

    bets = ledger.get("bets")
    observations = ns.get("observations", [])
    audit = ns.get("audit")
    if not isinstance(bets, list) or not isinstance(observations, list):
        return _funnel_unavailable(system, "formal_evidence_containers_malformed")
    if any(not isinstance(row, dict) for row in bets + observations):
        return _funnel_unavailable(system, "formal_evidence_rows_malformed")
    audit_rows_valid = isinstance(audit, list) and all(
        isinstance(row, dict) for row in audit
    )
    retained_audit = list(audit) if audit_rows_valid else []
    audit_metadata = ns.get("audit_retention")
    dropped_count = (
        _strict_int(audit_metadata.get("dropped_count"))
        if isinstance(audit_metadata, dict) else None
    )
    metadata_limit = (
        _strict_int(audit_metadata.get("retained_limit"))
        if isinstance(audit_metadata, dict) else None
    )
    if not audit_rows_valid or len(retained_audit) > CONDITION_AUDIT_LIMIT:
        audit_availability = "unavailable"
        audit_reason = "condition_audit_unavailable_or_malformed"
        audit_truncated = False
    elif len(retained_audit) < CONDITION_AUDIT_LIMIT:
        audit_availability = "available"
        audit_reason = None
        audit_truncated = False
    elif (
        metadata_limit == CONDITION_AUDIT_LIMIT
        and dropped_count is not None and dropped_count > 0
    ):
        audit_availability = "bounded"
        audit_reason = None
        audit_truncated = True
    else:
        audit_availability = "unavailable"
        audit_reason = "condition_audit_completeness_unproven_at_retention_limit"
        audit_truncated = False

    formal_rows = [
        row for row in bets
        if row.get("portfolio") == portfolio_name(system)
        and row.get("strategy") == STRATEGY
    ] + [
        row for row in observations
        if row.get("portfolio") == f"{system}_wilson_observations"
        and row.get("strategy") == STRATEGY
        and row.get("formal_bet") is False
    ]
    unavailable_upstream = {
        "available": False,
        "count": None,
        "availability": "unavailable",
        "reason": "condition_attribution_not_persisted_before_exact_match",
    }

    projection_time = datetime.now(timezone.utc)
    output: list[dict[str, Any]] = []
    for signature, number in zip(order, numbers):
        frozen = conditions[signature]
        definition, versions = validated_registry[signature]
        active = versions[-1]
        version_by_number = {row["version"]: row for row in versions}
        retirement = retired.get(signature)
        if retirement is not None:
            unavailable = {
                "available": False,
                "availability": "unavailable",
                "count": None,
                "reason": "retired_duplicate_historical_lineage_only",
            }
            output.append({
                "condition_number": number,
                "identity_available": True,
                "identity_status": "retired_duplicate",
                "condition_signature": signature,
                "condition_version": definition["version"],
                "definition": definition,
                "frozen_at": frozen.get("frozen_at"),
                "unavailable_reason": None,
                "canonical_successor_condition_number": retirement[
                    "target_condition_number"
                ],
                "canonical_successor_signature": retirement["target_signature"],
                "future_admission": "target_only",
                "historical_evidence": copy.deepcopy(
                    frozen.get("historical_evidence"),
                ),
                "historical_activity": copy.deepcopy(
                    retirement["historical_activity"],
                ),
                "active_evidence": {
                    "version": None,
                    "unavailable_reason": (
                        "retired_duplicate_historical_lineage_only"
                    ),
                },
                "stages": {
                    "eligible_post_activation_t5_observations": copy.deepcopy(unavailable),
                    "exact_condition_matches": copy.deepcopy(unavailable),
                    "recorded_formal_evidence": copy.deepcopy(unavailable),
                    "settled_valid_evidence": copy.deepcopy(unavailable),
                    "current_rollover_progress": {
                        **copy.deepcopy(unavailable),
                        "required": ROLLOVER_BATCH_SIZE,
                        "display": None,
                    },
                },
                "rejections": {
                    "bounded": True,
                    "visible_limit": FUNNEL_REJECTION_LIMIT,
                    "audit_window_limit": CONDITION_AUDIT_LIMIT,
                    "audit_truncation_possible": False,
                    "items": [],
                    "omitted_reason_kinds": 0,
                },
            })
            continue

        exact_keys: set[tuple[str, str]] = set()
        legacy_unverifiable_exact = 0
        audit_rejections: dict[str, int] = {}
        if audit_availability != "unavailable":
            for row in retained_audit:
                if str(row.get("frozen_condition_signature") or "") != signature:
                    continue
                fixture = str(row.get("match_id") or "")
                market = str(row.get("market") or "")
                exact_outcome = (
                    (
                        row.get("status") == "CREATED"
                        and row.get("reason") == "wilson_candidate_frozen"
                    )
                    or (
                        row.get("status") == "MATCHED_NO_BET"
                        and row.get("reason") in {
                            "wilson_gate_not_passed",
                            "early_stage_formal_observation",
                        }
                    )
                )
                binding = row.get("exact_match_binding")
                audit_version = (
                    _strict_int(binding.get("evidence_version"))
                    if isinstance(binding, dict) else None
                )
                audit_evidence = version_by_number.get(audit_version)
                audit_stage_at = (
                    binding.get("native_stage_at") if isinstance(binding, dict) else None
                )
                audit_ts, audit_stage_time = _time(row.get("ts")), _time(audit_stage_at)
                binding_stage = (
                    binding.get("decision_stage")
                    if isinstance(binding, dict)
                    and binding.get("schema_version") == 2
                    else DECISION_STAGE
                )
                binding_keys_valid = (
                    set(binding) == {
                        "schema_version", "condition_signature", "evidence_version",
                        "evidence_hash", "native_stage_at", "definition_hash",
                        "fixture_market_hash",
                    }
                    if isinstance(binding, dict)
                    and binding.get("schema_version") == 1
                    else isinstance(binding, dict)
                    and set(binding) == {
                        "schema_version", "condition_signature", "evidence_version",
                        "evidence_hash", "native_stage_at", "definition_hash",
                        "fixture_market_hash", "decision_stage",
                    }
                )
                exact_evidence_valid = (
                    isinstance(binding, dict)
                    and binding_keys_valid
                    and binding.get("schema_version") in {1, 2}
                    and binding_stage == definition.get("stage")
                    and str(definition.get("path") or "").split("→")[-1] == binding_stage
                    and binding.get("condition_signature") == signature
                    and binding.get("definition_hash") == _canonical_hash(definition)
                    and definition.get("market") == market
                    and market in {"HDC", "HIL", "CHL"}
                    and binding.get("fixture_market_hash")
                    == _fixture_market_hash(system, fixture, market)
                    and audit_evidence is not None
                    and binding.get("evidence_hash") == audit_evidence["evidence_hash"]
                    and _strictly_after(
                        audit_stage_at, audit_evidence.get("activation_boundary_at"),
                    )
                    and audit_ts is not None
                    and audit_stage_time is not None
                    and audit_ts >= audit_stage_time
                    and (
                        row.get("status") != "MATCHED_NO_BET"
                        or _strict_int(row.get("evidence_version")) == audit_version
                    )
                )
                if (
                    exact_outcome and exact_evidence_valid
                    and fixture and market and market != "*"
                ):
                    exact_keys.add((fixture, market))
                elif exact_outcome:
                    legacy_unverifiable_exact += 1
                reason = str(row.get("reason") or "")
                if reason in FUNNEL_AUDIT_REJECTIONS:
                    audit_rejections[reason] = audit_rejections.get(reason, 0) + 1

        rows = [
            row for row in formal_rows
            if str(row.get("frozen_condition_signature") or "") == signature
        ]
        settlement_rejections = {
            code: 0 for code in FUNNEL_SETTLEMENT_REJECTIONS
        }
        grouped_identities: dict[tuple[str, str], list[dict[str, Any]]] = {}
        for row in rows:
            observation = row.get("formal_bet") is False
            raw_identity = row.get("observation_id") if observation else row.get("bet_id")
            if not isinstance(raw_identity, str) or not raw_identity.strip():
                settlement_rejections["missing_formal_row_identity"] += 1
                continue
            fixture = row.get("match_id")
            market = row.get("market")
            expected_identity = (
                f"{fixture}|{market}|{row.get('stage')}|{signature}|"
                f"{'low-odds' if row.get('stage') == DECISION_STAGE else 'formal-observation'}"
                if observation else
                f"{fixture}|{market}|{DECISION_STAGE}|{STRATEGY}"
            )
            if (
                not isinstance(fixture, str) or not fixture
                or market not in {"HDC", "HIL", "CHL"}
                or raw_identity != expected_identity
            ):
                settlement_rejections["invalid_formal_row_identity"] += 1
                continue
            identity = ("observation" if observation else "bet", expected_identity)
            grouped_identities.setdefault(identity, []).append(row)

        normalized_rows: list[
            tuple[tuple[str, str], dict[str, Any], dict[str, Any]]
        ] = []
        for identity, grouped in grouped_identities.items():
            if len(grouped) > 1:
                settlement_rejections[
                    "duplicate_or_conflicting_formal_identity"
                ] += len(grouped)
                continue
            candidate = grouped[0]
            admitted, binding_reason = validate_formal_row(
                candidate, system=system, signature=signature, frozen=frozen,
                projection_time=projection_time, require_settled=False,
                ledger=ledger,
                authority_context=authority_context,
            )
            if admitted is None:
                settlement_rejections[
                    binding_reason or "invalid_formal_admission_binding"
                ] += 1
                continue
            normalized_rows.append((identity, candidate, admitted))
        recorded_bets = sum(
            identity[0] == "bet" for identity, _row, _admitted in normalized_rows
        )
        recorded_observations = sum(
            identity[0] == "observation"
            for identity, _row, _admitted in normalized_rows
        )

        settlement_candidates: dict[str, list[tuple[dict[str, Any], dict[str, Any]]]] = {}
        for _identity, row, admitted in normalized_rows:
            settled_admitted, settled_reason = validate_formal_row(
                row, system=system, signature=signature, frozen=frozen,
                projection_time=projection_time, require_settled=True,
                ledger=ledger,
                authority_context=authority_context,
            )
            if settled_admitted is None:
                settlement_rejections[
                    settled_reason or "missing_or_invalid_provenance"
                ] += 1
                continue
            if row.get("status") != "SETTLED":
                settlement_rejections["not_settled"] += 1
                continue
            if row.get("result") not in BINARY_DECIDED_RESULTS:
                settlement_rejections["not_binary_decided"] += 1
                continue
            marker = row["rollover_provenance"]
            created_at = _time(row.get("created_at"))
            kickoff = _time(row.get("kickoff") or row.get("kickoff_hkt"))
            settled_at = _time(row.get("settled_at"))
            if (
                created_at is None or kickoff is None or settled_at is None
                or created_at > settled_at
                or kickoff > settled_at
                or settled_at > projection_time
            ):
                settlement_rejections["missing_or_invalid_provenance"] += 1
                continue
            settlement_candidates.setdefault(
                str(marker["fixture_market_hash"]), [],
            ).append((row, admitted))

        settled_valid: list[tuple[dict[str, Any], dict[str, Any]]] = []
        for grouped in settlement_candidates.values():
            if len(grouped) != 1:
                settlement_rejections[
                    "duplicate_or_conflicting_fixture_market"
                ] += len(grouped)
                continue
            settled_valid.append(grouped[0])

        active_pending = [
            (row, admitted) for row, admitted in settled_valid
            if admitted["version"] == active["version"]
            and admitted["evidence_hash"] == active["evidence_hash"]
            and _strictly_after(
                row["rollover_provenance"].get("stage_at"),
                active.get("activation_boundary_at"),
            )
        ]
        pending_count = len(active_pending)
        pending_hits = sum(
            row.get("result") in BINARY_HIT_RESULTS for row, _admitted in active_pending
        )
        pending = frozen.get("pending_rollover_progress")
        progress_reason = "persisted_rollover_progress_unavailable_or_inconsistent"
        progress_valid = isinstance(pending, dict)
        progress_count = _strict_int(pending.get("eligible_decided")) if progress_valid else None
        progress_hits = _strict_int(pending.get("eligible_hits")) if progress_valid else None
        progress_required = _strict_int(pending.get("required")) if progress_valid else None
        progress_display = pending.get("display") if progress_valid else None
        blocked_reason = pending.get("blocked_reason") if progress_valid else None
        accuracy = pending.get("accuracy") if progress_valid else None
        if isinstance(blocked_reason, str) and blocked_reason:
            progress_valid = False
            progress_reason = "persisted_rollover_progress_blocked"
        expected_accuracy = pending_hits / pending_count if pending_count else None
        progress_valid = bool(
            progress_valid
            and progress_count is not None
            and progress_hits is not None
            and progress_required == ROLLOVER_BATCH_SIZE
            and 0 <= progress_hits <= progress_count < progress_required
            and progress_count == pending_count
            and progress_hits == pending_hits
            and progress_display == f"{progress_count}/{progress_required}"
            and _same_optional_number(accuracy, expected_accuracy)
        )
        if progress_valid:
            progress = {
                "available": True,
                "availability": "available",
                "count": progress_count,
                "required": progress_required,
                "display": progress_display,
                "eligible_hits": progress_hits,
                "blocked_reason": None,
                "source": "validated_persisted_pending_rollover_progress",
            }
        else:
            progress = {
                "available": False,
                "availability": "unavailable",
                "count": None,
                "required": ROLLOVER_BATCH_SIZE,
                "display": None,
                "reason": progress_reason,
                "blocked_reason": blocked_reason if isinstance(blocked_reason, str) else None,
            }

        rejection_rows = []
        for source_name, counts, definitions in (
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
                    "source": source_name,
                })
        rejection_rows.sort(key=lambda row: (
            -int(row["count"]), str(row["category"]), str(row["code"]),
        ))
        omitted = max(0, len(rejection_rows) - FUNNEL_REJECTION_LIMIT)
        if audit_availability == "unavailable":
            exact_stage = {
                "available": False,
                "availability": "unavailable",
                "count": None,
                "scope": "condition_attributed_audit",
                "retained_audit_entries": len(retained_audit),
                "window_limit": CONDITION_AUDIT_LIMIT,
                "truncation_possible": False,
                "reason": audit_reason,
                "verified_count": None,
                "legacy_unverifiable_count": None,
            }
        elif legacy_unverifiable_exact:
            exact_stage = {
                "available": False,
                "availability": "partial",
                "count": None,
                "scope": "partially_verified_condition_attributed_audit",
                "retained_audit_entries": len(retained_audit),
                "window_limit": CONDITION_AUDIT_LIMIT,
                "truncation_possible": audit_truncated,
                "reason": "legacy_or_invalid_exact_match_binding",
                "verified_count": len(exact_keys),
                "legacy_unverifiable_count": legacy_unverifiable_exact,
            }
        else:
            exact_stage = {
                "available": True,
                "availability": audit_availability,
                "count": len(exact_keys),
                "scope": (
                    "proven_truncated_condition_attributed_audit_window"
                    if audit_truncated else "complete_retained_condition_attributed_audit"
                ),
                "retained_audit_entries": len(retained_audit),
                "window_limit": CONDITION_AUDIT_LIMIT,
                "truncation_possible": audit_truncated,
                "reason": None,
                "verified_count": len(exact_keys),
                "legacy_unverifiable_count": 0,
            }

        identity_unverifiable = (
            settlement_rejections["missing_formal_row_identity"]
            + settlement_rejections["invalid_formal_row_identity"]
        )
        recorded_stage = {
            "available": not bool(identity_unverifiable),
            "availability": "partial" if identity_unverifiable else "available",
            "count": None if identity_unverifiable else len(normalized_rows),
            "verified_count": len(normalized_rows),
            "legacy_unverifiable_count": identity_unverifiable,
            "formal_bets": recorded_bets,
            "formal_observations": recorded_observations,
            "scope": (
                "partially_verified_canonical_formal_row_identities"
                if identity_unverifiable
                else "persisted_signature_bound_identified_formal_rows"
            ),
            "reason": (
                "legacy_or_invalid_formal_row_identity"
                if identity_unverifiable else None
            ),
        }

        card = {
            "condition_number": number,
            "identity_available": True,
            "condition_signature": signature,
            "condition_version": definition["version"],
            "definition": definition,
            "frozen_at": frozen.get("frozen_at"),
            "unavailable_reason": None,
            "active_evidence": {
                key: copy.deepcopy(active.get(key)) for key in (
                    "version", "evidence_hash", "cumulative_hits",
                    "cumulative_decided", "activation_boundary_at",
                )
            },
            "stages": {
                "eligible_post_activation_t5_observations": copy.deepcopy(unavailable_upstream),
                "exact_condition_matches": exact_stage,
                "recorded_formal_evidence": recorded_stage,
                "settled_valid_evidence": {
                    "available": True,
                    "availability": "available",
                    "count": len(settled_valid),
                    "hits": sum(
                        row.get("result") in BINARY_HIT_RESULTS
                        for row, _admitted in settled_valid
                    ),
                    "scope": "unique_binary_rows_exactly_bound_to_validated_admitted_evidence",
                    "reason": None,
                },
                "current_rollover_progress": progress,
            },
            "rejections": {
                "bounded": True,
                "visible_limit": FUNNEL_REJECTION_LIMIT,
                "audit_window_limit": CONDITION_AUDIT_LIMIT,
                "audit_truncation_possible": audit_truncated,
                "items": rejection_rows[:FUNNEL_REJECTION_LIMIT],
                "omitted_reason_kinds": omitted,
            },
        }
        card["identity_status"] = "active"
        output.append(card)
    return {
        "schema_version": 1,
        "read_only": True,
        "system": system,
        "condition_count": len(output),
        "historical_condition_count": len(output),
        "active_condition_count": len(output) - len(retired),
        "retired_duplicate_count": len(retired),
        "audit_window_limit": CONDITION_AUDIT_LIMIT,
        "conditions": output,
        "unavailable_reason": None,
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
        and _formal_stage_provenance_valid(row, system)
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
    evidence_version: dict[str, Any], *, decision_stage: str = DECISION_STAGE,
) -> dict[str, Any]:
    if decision_stage != DECISION_STAGE:
        return {
            "schema_version": 2,
            "system": system,
            "condition_signature": signature,
            "decision_stage": decision_stage,
            "first_native_pre_kickoff_stage": True,
            "stage_at": stage_at,
            "fixture_market_hash": _fixture_market_hash(system, fixture, market),
            "admitted_evidence_version": evidence_version.get("version"),
            "admitted_evidence_hash": evidence_version.get("evidence_hash"),
        }
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


def _formal_stage_provenance_valid(row: dict[str, Any], system: str) -> bool:
    """Accept legacy T-5 provenance or exact stage-aware v2 provenance."""
    marker = row.get("rollover_provenance")
    if not isinstance(marker, dict):
        return False
    schema_version = marker.get("schema_version")
    if (
        isinstance(schema_version, bool)
        or not isinstance(schema_version, int)
    ):
        return False
    stage = str(row.get("stage") or "")
    if schema_version == 1:
        # Preserve the exact legacy reader contract for existing T-5 rows.
        return (
            stage == DECISION_STAGE
            and row.get("first_native_pre_kickoff_t5") is True
            and marker.get("native_pre_kickoff_t5") is True
            and marker.get("system") == system
            and marker.get("condition_signature")
            == row.get("frozen_condition_signature")
        )
    definition = row.get("frozen_condition_definition")
    if not isinstance(definition, dict):
        return False
    if (
        stage not in {"首預", "T-30", "T-5"}
        or str(definition.get("stage") or definition.get("decision") or "") != stage
        or str(definition.get("path") or "").split("→")[-1] != stage
        or marker.get("system") != system
        or marker.get("condition_signature")
        != row.get("frozen_condition_signature")
        or marker.get("stage_at") != row.get("native_stage_at")
        or marker.get("fixture_market_hash") != _fixture_market_hash(
            system, str(row.get("match_id") or ""),
            str(row.get("market") or row.get("code") or ""),
        )
    ):
        return False
    return (
        schema_version == 2
        and marker.get("decision_stage") == stage
        and marker.get("first_native_pre_kickoff_stage") is True
        and row.get("first_native_pre_kickoff_stage") is True
    )


def _formal_marker_shape_valid(marker: dict[str, Any]) -> bool:
    """Require canonical immutable marker fields in audits/funnel binding."""
    common = {
        "schema_version", "system", "condition_signature", "stage_at",
        "fixture_market_hash", "admitted_evidence_version",
        "admitted_evidence_hash",
    }
    if marker.get("schema_version") == 1:
        return set(marker) == common | {"native_pre_kickoff_t5"}
    if marker.get("schema_version") == 2:
        return set(marker) == common | {
            "decision_stage", "first_native_pre_kickoff_stage",
        }
    return False


def validate_formal_row(
    row: dict[str, Any], *, system: str, signature: str,
    frozen: dict[str, Any], projection_time: datetime,
    require_settled: bool = False, ledger: dict[str, Any] | None = None,
    authority_context: Any = None,
) -> tuple[dict[str, Any] | None, str | None]:
    """Strict shared validator for runtime rollover, funnel, and registry.

    This is side-effect free. A row must be the exact canonical formal identity
    admitted against one immutable evidence version before it can influence
    authoritative x20 state.
    """
    definition = frozen.get("definition")
    versions = frozen.get("evidence_versions")
    if not isinstance(definition, dict) or not isinstance(versions, list):
        return None, "invalid_formal_admission_binding"
    if any(
        isinstance(version, dict)
        and "legacy_ordinary_batch_aggregate" in version
        for version in versions
    ):
        _definition, validated, chain_reason = (
            _validate_frozen_identity_and_chain(
                frozen, signature, system,
                authority_context=authority_context,
            )
        )
        if validated is None:
            return None, chain_reason or "authority_required"
    version_by_number = {
        item.get("version"): item for item in versions
        if isinstance(item, dict) and _strict_int(item.get("version")) is not None
    }
    observation = row.get("formal_bet") is False
    fixture = row.get("match_id")
    market = row.get("market") or row.get("code")
    stage = row.get("stage")
    identity = row.get("observation_id") if observation else row.get("bet_id")
    expected_identity = (
        f"{fixture}|{market}|{stage}|{signature}|"
        f"{'low-odds' if stage == DECISION_STAGE else 'formal-observation'}"
        if observation else
        f"{fixture}|{market}|{DECISION_STAGE}|{STRATEGY}"
    )
    if (
        not isinstance(identity, str)
        or identity != expected_identity
        or row.get("strategy") != STRATEGY
        or row.get("portfolio") != (
            f"{system}_wilson_observations"
            if observation else portfolio_name(system)
        )
        or (not observation and stage != DECISION_STAGE)
        or row.get("status") not in {"PENDING", "SETTLED", "VOIDED"}
        or (
            not observation and (
                row.get("simulation_only") is not True
                or row.get("real_betting_enabled") is not False
            )
        )
    ):
        return None, "invalid_formal_row_identity"
    admitted, reason = _validate_formal_admission_binding(
        row,
        observation=observation,
        system=system,
        signature=signature,
        definition=definition,
        frozen=frozen,
        version_by_number=version_by_number,
        projection_time=projection_time,
        ledger=ledger,
    )
    if admitted is None:
        return None, reason or "invalid_formal_admission_binding"
    if require_settled and row.get("status") != "SETTLED":
        return None, "not_settled"
    if require_settled and row.get("status") == "SETTLED":
        created_at = _time(row.get("created_at"))
        kickoff = _time(row.get("kickoff") or row.get("kickoff_hkt"))
        settled_at = _time(row.get("settled_at"))
        stage_at = _time((row.get("rollover_provenance") or {}).get("stage_at"))
        if (
            created_at is None or kickoff is None or settled_at is None
            or stage_at is None
            or not stage_at <= created_at < kickoff <= settled_at <= projection_time
        ):
            return None, "missing_or_invalid_provenance"
    return admitted, None


def _eligible_rollover_rows(
    bets: Iterable[dict[str, Any]], system: str, signature: str,
    active: dict[str, Any],
    reserved_hashes: set[str] | frozenset[str] | None = None,
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
        if not (
            _formal_stage_provenance_valid(row, system)
            and marker.get("condition_signature") == signature
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
        if reserved_hashes and fixture_hash in reserved_hashes:
            excluded["historical_authority_identity_reuse"] = (
                excluded.get("historical_authority_identity_reuse", 0) + 1
            )
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
    ledger: dict[str, Any], authority_context: Any = None,
) -> bool:
    """Append deterministic 20-row evidence versions, never edit old ones."""
    definition = frozen.get("definition")
    if not isinstance(definition, dict):
        return False
    own_stage = str(definition.get("stage") or "")
    if formal_matcher_axes(
        {**definition, "key": definition.get("miner_key")},
        system=system, decision_stage=own_stage,
    ) is None:
        return False
    rows = list(bets)
    projection_time = _time(now)
    if projection_time is None:
        return False
    # Any malformed same-signature activity blocks the entire condition. It
    # must never be cherry-picked around to create an authoritative version.
    for row in rows:
        admitted, _reason = validate_formal_row(
            row, system=system, signature=signature, frozen=frozen,
            projection_time=projection_time,
            require_settled=row.get("status") == "SETTLED",
            ledger=ledger,
            authority_context=authority_context,
        )
        if admitted is None:
            return False
    active = active_evidence_version(
        frozen, migration_boundary=migration_boundary,
        authority_context=authority_context,
    )
    if active is None:
        return False
    last_excluded: dict[str, int] = {}
    created = 0
    reserved_hashes: frozenset[str] = frozenset()
    if authority_context is not None:
        from .legacy_batch_aggregate import require_authority_context
        reserved_hashes = require_authority_context(
            authority_context
        ).reservations.get(signature, frozenset())
    while True:
        eligible, excluded = _eligible_rollover_rows(
            rows, system, signature, active, reserved_hashes,
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
        if reserved_hashes.intersection(
            item["fixture_market_hash"] for item in batch
        ):
            frozen["rollover_status"] = (
                "blocked_historical_authority_identity_reuse"
            )
            return False
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
    return True


def apply_active_evidence(
    ledger: dict[str, Any], system: str, admission: dict[str, Any], *,
    stage_at: str, now: str, authority_context: Any = None,
) -> tuple[dict[str, Any] | None, str | None]:
    """Use only the active version for a new native T-5 decision.

    The first observation freezes its discovery baseline. Afterwards, every
    admission must be strictly later than the latest evidence boundary. This
    prevents a late rerun from applying a newly learned threshold backward.
    """
    signature = str(admission["signature"])
    retired, migration_reason = _validate_condition_identity_migrations(
        ledger, system, authority_context=authority_context,
    )
    if retired is None:
        return None, migration_reason or "condition_identity_migrations_invalid"
    if signature in retired:
        return None, "retired_duplicate_target_only"
    ns = ensure_namespace(ledger, system, now=now)
    existed = isinstance(ns["conditions"].get(signature), dict)
    frozen = freeze_condition(ledger, system, admission, now=now)
    active = active_evidence_version(
        frozen, migration_boundary=ns["activation_at"],
        authority_context=authority_context,
    )
    if active is None:
        return None, "active_evidence_unavailable"
    active_created = _time(active.get("created_at"))
    decision_time = _time(stage_at)
    if (
        active_created is None or decision_time is None
        or active_created > decision_time
    ):
        return None, "evidence_version_not_created_at_decision_time"
    if existed and not _strictly_after(stage_at, active.get("activation_boundary_at")):
        return None, "stage_not_strictly_after_evidence_activation_boundary"
    arithmetic = admission_arithmetic(
        int(active["cumulative_hits"]), int(active["cumulative_decided"]),
        admission["arithmetic"].get("actual_decimal_odds_raw"),
        settlement_profile=admission["arithmetic"].get("settlement_profile"),
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


def recompute_namespace(
    ledger: dict[str, Any], system: str, *, authority_context: Any = None,
) -> dict[str, Any]:
    retired, migration_reason = _validate_condition_identity_migrations(
        ledger, system, authority_context=authority_context,
    )
    if retired is None:
        raise ValueError(
            migration_reason or "condition identity migration metadata invalid"
        )
    ns = ensure_namespace(ledger, system)
    has_aggregate = any(
        isinstance(version, dict)
        and "legacy_ordinary_batch_aggregate" in version
        for frozen in (ns.get("conditions") or {}).values()
        if isinstance(frozen, dict)
        for version in (frozen.get("evidence_versions") or [])
    )
    if has_aggregate:
        from .legacy_batch_aggregate import require_authority_context
        require_authority_context(authority_context)
        identity, identity_reason = validate_production_identity_manifest_v1(
            ns, system,
        )
        if identity is None:
            raise ValueError(identity_reason or "production_identity_v1_invalid")
        for signature, frozen in ns["conditions"].items():
            _definition, _versions, reason = (
                _validate_frozen_identity_and_chain(
                    frozen, signature, system,
                    authority_context=authority_context,
                )
            )
            if reason is not None:
                raise ValueError(reason)
    bets = [
        row for row in active_bets(ledger, system)
        if str(row.get("frozen_condition_signature") or "") not in retired
    ]
    observations = [
        row for row in active_observations(ledger, system)
        if str(row.get("frozen_condition_signature") or "") not in retired
    ]
    evidence_rows = bets + observations
    grouped: dict[str, list[dict[str, Any]]] = {}
    raw_grouped: dict[str, list[dict[str, Any]]] = {}
    observation_grouped: dict[str, list[dict[str, Any]]] = {}
    for row in evidence_rows:
        grouped.setdefault(str(row.get("frozen_condition_signature") or ""), []).append(row)
    for collection in (ledger.get("bets") or [], ns.get("observations") or []):
        if isinstance(collection, list):
            for row in collection:
                if isinstance(row, dict) and row.get("frozen_condition_signature"):
                    raw_grouped.setdefault(
                        str(row.get("frozen_condition_signature")), [],
                    ).append(row)
    for row in observations:
        observation_grouped.setdefault(
            str(row.get("frozen_condition_signature") or ""), []
        ).append(row)
    for signature, frozen in ns["conditions"].items():
        if str(signature) in retired:
            continue
        if isinstance(frozen, dict):
            valid_activity = _rollover_condition(
                frozen, raw_grouped.get(str(signature), []), system, str(signature), now=_now(),
                migration_boundary=ns["activation_at"],
                ledger=ledger, authority_context=authority_context,
            )
            if not valid_activity:
                continue
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
    if has_aggregate:
        from .legacy_batch_aggregate import _stats_conditions_projection
        metrics["conditions"] = _stats_conditions_projection(ns["conditions"])
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
    settlement_profile, settlement_reason = _selected_settlement_profile(
        market, selected,
    )
    if settlement_reason is not None:
        return [], settlement_reason
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
        arithmetic = admission_arithmetic(
            baseline["hits"], baseline["decided"], odds,
            settlement_profile=settlement_profile,
        )
        if arithmetic is None:
            continue
        matches.append({
            "signature": signature, "definition": rows[0][1], "history": baseline,
            "arithmetic": arithmetic, "candidate": rows[0][0],
            "native_snapshot_binding": copy.deepcopy(
                selected.get("native_snapshot_binding")
            ),
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
    decision_stage: str = DECISION_STAGE, authority_context: Any = None,
) -> dict[str, Any] | None:
    """Persist a matched Wilson condition which did not become a formal bet.

    Observations are explicitly segregated from ``bets``. They are settled as
    condition-validation evidence only: never stake, bankroll, PnL, ROI, or
    execution. A frozen native T-5 match remains an observation even when the
    exact execution price is below its admission minimum.
    """
    arithmetic = admission.get("arithmetic")
    if (
        decision_stage not in {"首預", "T-30", "T-5"}
        or not isinstance(arithmetic, dict)
        or (decision_stage == DECISION_STAGE and arithmetic.get("passes"))
    ):
        return None
    fixture = str(watch.get("match_id") or "")
    if not fixture:
        return None
    frozen = freeze_condition(ledger, system, admission, now=now)
    signature = str(admission["signature"])
    kind = "low-odds" if decision_stage == DECISION_STAGE else "formal-observation"
    observation_id = f"{fixture}|{market}|{decision_stage}|{signature}|{kind}"
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
        **(
            {}
            if decision_stage == DECISION_STAGE
            else {"real_betting_enabled": False}
        ),
        "bet_status": (
            "NO_BET_LOW_ODDS"
            if decision_stage == DECISION_STAGE else "FORMAL_OBSERVATION"
        ),
        "no_bet_reason": (
            "因賠率不足，不投注"
            if decision_stage == DECISION_STAGE else "早段正式觀察，不投注"
        ),
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
        "stage": decision_stage,
        "status": "PENDING",
        **(
            {"first_native_pre_kickoff_t5": True}
            if decision_stage == DECISION_STAGE
            else {"first_native_pre_kickoff_stage": True}
        ),
        "created_at": now,
        "native_stage_at": admission.get("stage_at"),
        "frozen_condition_signature": signature,
        "condition_number": frozen.get("condition_number"),
        "frozen_condition_definition": copy.deepcopy(admission["definition"]),
        "frozen_historical_evidence": copy.deepcopy(admission["history"]),
        "wilson_admission": copy.deepcopy(arithmetic),
        "quarter_line_settlement": copy.deepcopy(
            arithmetic.get("settlement_profile")
        ),
        **(
            {"quarter_line_settlement_schema_version": 2}
            if arithmetic.get("settlement_adjusted") is True else {}
        ),
        "native_snapshot_binding": copy.deepcopy(
            admission.get("native_snapshot_binding")
        ),
        "evidence_version": admission.get("evidence_version"),
        "evidence_hash": admission.get("evidence_hash"),
        "rollover_provenance": _rollover_marker(
            system, fixture, market, signature, str(admission.get("stage_at") or now),
            active_evidence_version(
                frozen, migration_boundary=ns["activation_at"],
                authority_context=authority_context,
            ) or {},
            decision_stage=decision_stage,
        ),
        "history": [{
            "ts": now, "stage": decision_stage,
            "action": (
                "Wilson 正式條件驗證觀察建立（低賠率不投注）"
                if decision_stage == DECISION_STAGE
                else "Wilson 早段正式條件驗證觀察建立（不投注）"
            ),
            "bet_status": (
                "NO_BET_LOW_ODDS"
                if decision_stage == DECISION_STAGE else "FORMAL_OBSERVATION"
            ),
        }],
    }
    observations.append(row)
    ns["observations"] = observations[-1600:]
    return row


def commit_bet(
    ledger: dict[str, Any], system: str, watch: dict[str, Any], market: str,
    selected: dict[str, Any], admission: dict[str, Any], *, now: str,
    market_label: str, selected_label: str, selected_role: str | None,
    selected_line: float, authority_context: Any = None,
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
        authority_context=authority_context,
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
        "admission_at": now, "native_stage_at": stage_at,
        "frozen_condition_signature": signature,
        "condition_number": frozen.get("condition_number"),
        "frozen_condition_definition": copy.deepcopy(admission["definition"]),
        "frozen_historical_evidence": copy.deepcopy(admission["history"]),
        "wilson_admission": arithmetic,
        "quarter_line_settlement": copy.deepcopy(
            arithmetic.get("settlement_profile")
        ),
        **(
            {"quarter_line_settlement_schema_version": 2}
            if arithmetic.get("settlement_adjusted") is True else {}
        ),
        "native_snapshot_binding": copy.deepcopy(
            admission.get("native_snapshot_binding")
        ),
        "evidence_version": admission.get("evidence_version"),
        "evidence_hash": admission.get("evidence_hash"),
        "rollover_provenance": _rollover_marker(
            system, fixture, market, signature, stage_at, evidence,
        ),
        "history": [{"ts": now, "stage": DECISION_STAGE, "action": "Wilson 模擬注建立",
                     "reason": "首次原生賽前 T-5；凍結歷史證據 Wilson 門檻通過"}],
    }
