#!/usr/bin/env python3
"""Remove the proven duplicate holdout from Crown condition #2 evidence.

The correction changes only the immutable evidence version and its active
pointer.  Existing prospective observations and rollover progress are
preserved byte-for-byte so new first-look admissions continue toward the next
20-result rollover.
"""
from __future__ import annotations

import argparse
import copy
import json
from datetime import datetime
from pathlib import Path
from typing import Any

from analysis.recover_crown_condition2_history import (
    _canonical_hash,
    _condition,
    _evidence_values,
    _hash,
    _read,
    _version_hash,
    _write_atomic,
)
from analysis.wilson_validation import _validate_frozen_identity_and_chain


MIGRATION = "crown-condition2-remove-duplicate-holdout-v1"
EXPECTED_BEFORE = {
    "v1_hits": 141,
    "v1_decided": 231,
    "v2_batch_hits": 176,
    "v2_batch_decided": 299,
    "v2_cumulative_hits": 317,
    "v2_cumulative_decided": 530,
}
REMOVED = {"hits": 44, "decided": 71, "pushes": 4}
EXPECTED_AFTER = {
    "v2_batch_hits": 132,
    "v2_batch_decided": 228,
    "v2_cumulative_hits": 273,
    "v2_cumulative_decided": 459,
}


def _int(value: Any) -> int:
    if isinstance(value, bool):
        raise ValueError("boolean is not an evidence count")
    return int(value)


def _prospective_snapshot(frozen: dict[str, Any]) -> dict[str, Any]:
    return {
        key: copy.deepcopy(frozen.get(key))
        for key in (
            "prospective",
            "prospective_observations",
            "pending_rollover_progress",
            "rollover_status",
        )
    }


def _recovered_counts(frozen: dict[str, Any]) -> dict[str, int]:
    rows = frozen.get("historical_recovery_rows")
    if not isinstance(rows, list):
        raise ValueError("condition #2 recovered fixture rows are unavailable")
    results = [
        str(row.get("result") or "")
        for row in rows
        if isinstance(row, dict)
    ]
    hits = sum(value in {"Won", "Half Won"} for value in results)
    losses = sum(value in {"Lost", "Half Lost"} for value in results)
    pushes = sum(value == "Refunded" for value in results)
    pending = sum(value == "PENDING" for value in results)
    return {
        "hits": hits,
        "losses": losses,
        "decided": hits + losses,
        "pushes": pushes,
        "pending": pending,
    }


def correct(ledger: dict[str, Any], *, apply: bool) -> dict[str, Any]:
    before_hash = _canonical_hash(ledger)
    namespace, frozen = _condition(ledger)
    marker = namespace.get("condition2_dedup_correction_v1")
    if isinstance(marker, dict) and marker.get("completed") is True:
        return {
            "mode": "apply" if apply else "audit",
            "status": "already_completed",
            "migration": MIGRATION,
            "stored": copy.deepcopy(marker),
            "ledger_hash": before_hash,
        }

    versions = frozen.get("evidence_versions")
    if (
        not isinstance(versions, list)
        or len(versions) != 2
        or any(not isinstance(row, dict) for row in versions)
    ):
        raise ValueError("condition #2 must have exactly V1 and V2")
    v1, old_v2 = copy.deepcopy(versions[0]), copy.deepcopy(versions[1])
    observed = {
        "v1_hits": _int(v1.get("cumulative_hits")),
        "v1_decided": _int(v1.get("cumulative_decided")),
        "v2_batch_hits": _int(old_v2.get("batch_hits")),
        "v2_batch_decided": _int(old_v2.get("batch_decided")),
        "v2_cumulative_hits": _int(old_v2.get("cumulative_hits")),
        "v2_cumulative_decided": _int(old_v2.get("cumulative_decided")),
    }
    if observed != EXPECTED_BEFORE:
        raise ValueError(
            f"condition #2 pre-correction evidence changed: {observed}"
        )
    if v1.get("evidence_hash") != _version_hash(v1):
        raise ValueError("condition #2 V1 evidence hash is invalid")
    if old_v2.get("evidence_hash") != _version_hash(old_v2):
        raise ValueError("condition #2 V2 evidence hash is invalid")

    recovered = _recovered_counts(frozen)
    if (
        recovered["hits"] != EXPECTED_AFTER["v2_batch_hits"]
        or recovered["decided"] != EXPECTED_AFTER["v2_batch_decided"]
    ):
        raise ValueError(
            f"recovered independent cohort changed: {recovered}"
        )
    legacy = old_v2.get("legacy_prospective_cohort")
    expected_legacy = {
        "hits": EXPECTED_BEFORE["v2_batch_hits"],
        "decided": EXPECTED_BEFORE["v2_batch_decided"],
        "pushes": recovered["pushes"] + REMOVED["pushes"],
    }
    if legacy != expected_legacy:
        raise ValueError(
            f"condition #2 duplicate cohort proof changed: {legacy}"
        )

    prospective_before = _prospective_snapshot(frozen)
    if frozen.get("rollover_status") != "active":
        raise ValueError(
            "condition #2 prospective rollover is not active before correction"
        )
    corrected_at = datetime.now().astimezone().isoformat()
    values = _evidence_values(
        EXPECTED_AFTER["v2_cumulative_hits"],
        EXPECTED_AFTER["v2_cumulative_decided"],
    )
    v2 = {
        **old_v2,
        "batch_hits": EXPECTED_AFTER["v2_batch_hits"],
        "batch_decided": EXPECTED_AFTER["v2_batch_decided"],
        "cumulative_hits": EXPECTED_AFTER["v2_cumulative_hits"],
        "cumulative_decided": EXPECTED_AFTER["v2_cumulative_decided"],
        "wilson95_lower_raw": values["wilson95_lower_raw"],
        "minimum_acceptable_odds_raw": values[
            "minimum_acceptable_odds_raw"
        ],
        "minimum_acceptable_odds_display": values["display"][
            "minimum_acceptable_odds"
        ],
        "legacy_prospective_cohort": {
            "hits": recovered["hits"],
            "decided": recovered["decided"],
            "pushes": recovered["pushes"],
        },
        "condition2_dedup_correction": {
            "schema_version": 1,
            "migration": MIGRATION,
            "corrected_at": corrected_at,
            "removed_duplicate_holdout": copy.deepcopy(REMOVED),
            "independent_fixture_count": 459,
            "old_evidence_hash": old_v2["evidence_hash"],
            "recovered_rows_proof_hash": _hash(
                frozen["historical_recovery_rows"]
            ),
        },
    }
    v2["evidence_hash"] = _version_hash(v2)

    frozen["evidence_versions"] = [v1, v2]
    frozen["active_evidence_version"] = 2
    frozen["active_evidence_hash"] = v2["evidence_hash"]
    frozen["active_evidence"] = {
        key: copy.deepcopy(v2.get(key))
        for key in (
            "version",
            "cumulative_hits",
            "cumulative_decided",
            "wilson95_lower_raw",
            "minimum_acceptable_odds_raw",
            "minimum_acceptable_odds_display",
            "activation_boundary_at",
            "created_at",
            "evidence_hash",
        )
    }
    frozen["rollover_audit"] = [copy.deepcopy(v2)]
    frozen.setdefault("superseded_duplicate_evidence_versions", []).append({
        "migration": MIGRATION,
        "superseded_at": corrected_at,
        "reason": "duplicate_holdout_already_contained_in_v1_baseline",
        "version": old_v2,
    })

    prospective_after = _prospective_snapshot(frozen)
    if prospective_after != prospective_before:
        raise ValueError("prospective observations changed during correction")
    _definition, validated, reason = _validate_frozen_identity_and_chain(
        frozen,
        str(frozen.get("signature") or ""),
        "crown",
    )
    if reason is not None or not validated or validated[-1] != v2:
        raise ValueError(f"corrected evidence chain is invalid: {reason}")

    after = {
        "hits": v2["cumulative_hits"],
        "decided": v2["cumulative_decided"],
        "batch_hits": v2["batch_hits"],
        "batch_decided": v2["batch_decided"],
        "wilson95_lower_raw": v2["wilson95_lower_raw"],
        "minimum_acceptable_odds_raw": v2[
            "minimum_acceptable_odds_raw"
        ],
        "minimum_acceptable_odds_display": v2[
            "minimum_acceptable_odds_display"
        ],
        "pending_rollover_progress": copy.deepcopy(
            frozen.get("pending_rollover_progress")
        ),
        "prospective_observation_count": len(
            frozen.get("prospective_observations") or {}
        ),
        "rollover_status": frozen.get("rollover_status"),
    }
    marker_value = {
        "completed": True,
        "migration": MIGRATION,
        "completed_at": corrected_at,
        "removed_duplicate_holdout": copy.deepcopy(REMOVED),
        "before": copy.deepcopy(observed),
        "after": copy.deepcopy(after),
        "prospective_state_hash": _hash(prospective_after),
        "old_evidence_hash": old_v2["evidence_hash"],
        "new_evidence_hash": v2["evidence_hash"],
    }
    if apply:
        namespace["condition2_dedup_correction_v1"] = marker_value
    return {
        "mode": "apply" if apply else "audit",
        "status": "applied" if apply else "audit_ready",
        "migration": MIGRATION,
        "removed": copy.deepcopy(REMOVED),
        "recovered_independent_cohort": recovered,
        "after": after,
        "prospective_preserved": prospective_after == prospective_before,
        "before_ledger_hash": before_hash,
        "after_ledger_hash": _canonical_hash(ledger),
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--ledger", type=Path, required=True)
    parser.add_argument("--apply", action="store_true")
    args = parser.parse_args()
    ledger = _read(args.ledger)
    working = ledger if args.apply else copy.deepcopy(ledger)
    report = correct(working, apply=args.apply)
    if args.apply and report.get("status") == "applied":
        _write_atomic(args.ledger, working)
    print(json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
