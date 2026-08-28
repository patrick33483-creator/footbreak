#!/usr/bin/env python3
"""Proof-gated settlement of verified Crown condition #6 recovery rows."""
from __future__ import annotations

import argparse
import copy
import json
from collections import Counter
from datetime import datetime
from pathlib import Path
from typing import Any

from analysis.legacy_batch_runtime import load_production_legacy_batch_authority
from analysis.recover_crown_condition6_history import _hash, _read, _write_atomic
from analysis.wilson_validation import (
    _canonical_hash,
    _evidence_values,
    _time,
    _version_hash,
    active_evidence_version,
    recompute_namespace,
)
from crown.lines import settle_handicap


SYSTEM = "crown"
SIGNATURE = "09ba238cb8400670519ce95a"
MIGRATION = "crown-condition6-verified-results-20260828-v1"
MIGRATION_FIELD = "condition6_verified_results_20260828_v1"
BINARY_HITS = {"Won", "Half Won"}
BINARY_LOSSES = {"Lost", "Half Lost"}
SETTLEMENTS = BINARY_HITS | BINARY_LOSSES | {"Refunded"}
EXPECTED_AFTER = {
    "recovered_hits": 52,
    "recovered_losses": 52,
    "recovered_decided": 104,
    "recovered_pushes": 0,
    "recovered_pending": 1,
    "cumulative_hits": 113,
    "cumulative_decided": 201,
}


def _condition(ledger: dict[str, Any]) -> tuple[dict[str, Any], dict[str, Any]]:
    namespace = ledger.get("wilson_validation")
    if not isinstance(namespace, dict) or namespace.get("system") != SYSTEM:
        raise ValueError("Crown Wilson namespace missing")
    frozen = (namespace.get("conditions") or {}).get(SIGNATURE)
    if not isinstance(frozen, dict) or frozen.get("condition_number") != 6:
        raise ValueError("Crown condition #6 missing or signature changed")
    if frozen.get("signature") != SIGNATURE:
        raise ValueError("Crown condition #6 signature changed")
    return namespace, frozen


def _same_number(left: Any, right: Any) -> bool:
    try:
        return abs(float(left) - float(right)) <= 1e-9
    except (TypeError, ValueError):
        return False


def _active(
    frozen: dict[str, Any], namespace: dict[str, Any], authority: Any
) -> dict[str, Any]:
    value = active_evidence_version(
        frozen,
        migration_boundary=namespace["activation_at"],
        authority_context=authority,
    )
    if not isinstance(value, dict):
        raise ValueError("condition #6 active evidence unavailable")
    return {
        "version": value.get("version"),
        "hits": value.get("cumulative_hits"),
        "decided": value.get("cumulative_decided"),
        "wilson95_lower_raw": value.get("wilson95_lower_raw"),
        "minimum_acceptable_odds_raw": value.get("minimum_acceptable_odds_raw"),
        "minimum_acceptable_odds_display": value.get(
            "minimum_acceptable_odds_display"
        ),
        "pending_rollover_progress": copy.deepcopy(
            frozen.get("pending_rollover_progress") or {}
        ),
    }


def _manifest(path: Path) -> dict[str, Any]:
    value = _read(path)
    if (
        value.get("schema_version") != 1
        or value.get("condition_number") != 6
        or value.get("condition_signature") != SIGNATURE
        or value.get("decision_stage") != "T-30"
        or value.get("market") != "HDC"
        or value.get("selection") != "H"
        or value.get("score_scope")
        != "90_minutes_including_stoppage_time_excluding_extra_time"
    ):
        raise ValueError("verified result manifest identity invalid")
    rows = value.get("results")
    if not isinstance(rows, list) or len(rows) != 57:
        raise ValueError("verified result manifest must contain exactly 57 rows")
    ids = [str(row.get("match_id") or "") for row in rows]
    if len(set(ids)) != 57 or not all(ids):
        raise ValueError("verified result fixture identities invalid")
    postponed = value.get("postponed_pending")
    if (
        not isinstance(postponed, list)
        or len(postponed) != 1
        or postponed[0].get("match_id") != "3072870"
        or postponed[0].get("reason") != "postponed_no_result"
    ):
        raise ValueError("postponed fixture identity invalid")
    verified_at = _time(value.get("verified_at"))
    if verified_at is None:
        raise ValueError("verified_at invalid")
    for row in rows:
        sources = row.get("sources")
        if (
            not isinstance(sources, list)
            or len(sources) < 2
            or not all(
                str(source).startswith(("http://", "https://"))
                for source in sources
            )
        ):
            raise ValueError(f"two result sources required: {row.get('match_id')}")
        home, away = int(row["home_score"]), int(row["away_score"])
        calculated = settle_handicap(float(row["line"]), "H", home, away)
        if calculated != row.get("expected_result") or calculated not in SETTLEMENTS:
            raise ValueError(
                f"precomputed settlement mismatch: {row.get('match_id')}"
            )
    return value


def _rebuild_v2(frozen: dict[str, Any]) -> dict[str, int]:
    rows = frozen.get("historical_recovery_rows")
    versions = frozen.get("evidence_versions")
    if not isinstance(rows, list) or len(rows) != 105:
        raise ValueError("condition #6 recovery cohort must contain 105 rows")
    if not isinstance(versions, list) or len(versions) < 2:
        raise ValueError("condition #6 evidence versions missing")
    counts = Counter(str(row.get("result") or "PENDING") for row in rows)
    unknown = set(counts) - SETTLEMENTS - {"PENDING"}
    if unknown:
        raise ValueError(f"unexpected recovery settlements: {sorted(unknown)}")
    hits = sum(counts[value] for value in BINARY_HITS)
    losses = sum(counts[value] for value in BINARY_LOSSES)
    pushes = counts["Refunded"]
    pending = counts["PENDING"]
    decided = hits + losses
    values = _evidence_values(61 + hits, 97 + decided)

    v2 = versions[1]
    v2.update({
        "batch_hits": hits,
        "batch_decided": decided,
        "cumulative_hits": 61 + hits,
        "cumulative_decided": 97 + decided,
        "wilson95_lower_raw": values["wilson95_lower_raw"],
        "minimum_acceptable_odds_raw": values["minimum_acceptable_odds_raw"],
        "minimum_acceptable_odds_display": values["display"][
            "minimum_acceptable_odds"
        ],
        "legacy_prospective_cohort": {
            "hits": hits,
            "decided": decided,
            "pushes": pushes,
        },
    })
    recovery = v2.get("condition6_independent_history_recovery")
    if not isinstance(recovery, dict):
        raise ValueError("condition #6 recovery proof missing")
    recovery["recovered"] = {
        "hits": hits,
        "losses": losses,
        "decided": decided,
        "pushes": pushes,
        "pending": pending,
    }
    row_hashes = sorted(_hash(row) for row in rows)
    recovery["fixture_rows_root_hash"] = _hash(row_hashes)
    recovery["fixture_row_hashes"] = row_hashes
    v2["evidence_hash"] = _version_hash(v2)
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
    return {
        "recovered_hits": hits,
        "recovered_losses": losses,
        "recovered_decided": decided,
        "recovered_pushes": pushes,
        "recovered_pending": pending,
        "cumulative_hits": 61 + hits,
        "cumulative_decided": 97 + decided,
    }


def apply_verified_results(
    ledger: dict[str, Any], manifest: dict[str, Any], *, apply: bool
) -> dict[str, Any]:
    before_hash = _canonical_hash(ledger)
    namespace, frozen = _condition(ledger)
    authority = load_production_legacy_batch_authority(ledger)
    before = _active(frozen, namespace, authority)
    projection_time = datetime.now().astimezone()
    verified_at = str(manifest["verified_at"])
    if _time(verified_at) is None or _time(verified_at) > projection_time:
        raise ValueError("verified_at is later than execution")
    if (before["hits"], before["decided"]) not in {(87, 144), (113, 201)}:
        raise ValueError(f"unexpected condition #6 starting evidence: {before}")
    if (frozen.get("pending_rollover_progress") or {}).get("display") != "0/20":
        raise ValueError("prospective progress changed before result recovery")

    recovery_rows = frozen.get("historical_recovery_rows")
    target_rows = {
        str(row.get("match_id") or ""): row
        for row in recovery_rows
        if isinstance(row, dict)
    }
    if len(target_rows) != 105:
        raise ValueError("condition #6 recovery fixture identities invalid")

    result: dict[str, Any] = {
        "mode": "apply" if apply else "audit",
        "migration": MIGRATION,
        "condition_signature": SIGNATURE,
        "before": before,
        "newly_settled": 0,
        "already_settled_same": 0,
        "settlement_counts": Counter(),
        "fixtures": [],
        "postponed_pending": [],
    }
    for item in manifest["results"]:
        fixture = str(item["match_id"])
        row = target_rows.get(fixture)
        if not isinstance(row, dict):
            raise ValueError(f"condition #6 recovery row missing: {fixture}")
        if (
            str(row.get("side") or "").upper() != "H"
            or not _same_number(row.get("line"), item.get("line"))
            or row.get("kickoff") != item.get("kickoff_hkt")
        ):
            raise ValueError(f"condition #6 immutable identity mismatch: {fixture}")
        kickoff = _time(row.get("kickoff"))
        if kickoff is None or kickoff > _time(verified_at):
            raise ValueError(f"result proof predates kickoff: {fixture}")
        calculated = settle_handicap(
            float(row["line"]),
            "H",
            int(item["home_score"]),
            int(item["away_score"]),
        )
        if calculated != item["expected_result"]:
            raise ValueError(f"runtime settlement mismatch: {fixture}")
        proof = {
            "match_id": fixture,
            "condition_signature": SIGNATURE,
            "stage": "T-30",
            "market": "HDC",
            "side": "H",
            "line": float(row["line"]),
            "score": [int(item["home_score"]), int(item["away_score"])],
            "score_scope": manifest["score_scope"],
            "verified_at": verified_at,
            "sources": list(item["sources"]),
            "settlement": calculated,
        }
        proof_hash = _hash(proof)
        state = "new"
        if row.get("result") in SETTLEMENTS:
            if (
                row.get("result") != calculated
                or row.get("result_proof_hash") not in (None, proof_hash)
            ):
                raise ValueError(f"existing result conflicts: {fixture}")
            state = "already_same"
            result["already_settled_same"] += 1
        elif row.get("result") == "PENDING":
            row.update({
                "result": calculated,
                "settled_at": verified_at,
                "score": f"{item['home_score']}-{item['away_score']}",
                "settlement_source": "cross_source_verified_manual_backfill",
                "result_recovery_source": MIGRATION,
                "result_proof_hash": proof_hash,
                "normal_grade_source_hash": proof_hash,
                "score_scope": manifest["score_scope"],
                "result_sources": list(item["sources"]),
            })
            result["newly_settled"] += 1
        else:
            raise ValueError(
                f"unexpected recovery row state {fixture}: {row.get('result')}"
            )
        result["settlement_counts"][calculated] += 1
        result["fixtures"].append({
            "match_id": fixture,
            "score": f"{item['home_score']}-{item['away_score']}",
            "line": float(row["line"]),
            "result": calculated,
            "state": state,
            "proof_hash": proof_hash,
        })

    for item in manifest["postponed_pending"]:
        fixture = str(item["match_id"])
        row = target_rows.get(fixture)
        if not isinstance(row, dict) or row.get("result") != "PENDING":
            raise ValueError(f"postponed fixture is not pending: {fixture}")
        result["postponed_pending"].append({
            "match_id": fixture,
            "result": row.get("result"),
            "sources": list(item["sources"]),
        })
    if result["newly_settled"] + result["already_settled_same"] != 57:
        raise ValueError("not every verified fixture reached exact settlement")

    rebuilt = _rebuild_v2(frozen)
    if rebuilt != EXPECTED_AFTER:
        raise ValueError(f"condition #6 rebuilt evidence mismatch: {rebuilt}")
    recompute_namespace(ledger, SYSTEM, authority_context=authority)
    after = _active(frozen, namespace, authority)
    if (
        after["version"] != 2
        or after["hits"] != 113
        or after["decided"] != 201
        or after["pending_rollover_progress"].get("display") != "0/20"
    ):
        raise ValueError(f"condition #6 result recovery mismatch: {after}")
    result["after"] = after
    result["settlement_counts"] = dict(result["settlement_counts"])
    result["before_ledger_hash"] = before_hash
    result["status"] = (
        "applied"
        if apply and result["newly_settled"] > 0
        else "already_applied"
        if result["newly_settled"] == 0
        else "audit_ready"
    )
    if apply and result["newly_settled"] > 0:
        namespace[MIGRATION_FIELD] = {
            "completed": True,
            "migration": MIGRATION,
            "completed_at": projection_time.isoformat(),
            "verified_at": verified_at,
            "newly_settled": result["newly_settled"],
            "settlement_counts": copy.deepcopy(result["settlement_counts"]),
            "postponed_pending": copy.deepcopy(result["postponed_pending"]),
            "fixture_proof_root_hash": _hash(result["fixtures"]),
            "after": copy.deepcopy(after),
        }
    result["after_ledger_hash"] = _canonical_hash(ledger)
    return result


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--ledger", type=Path, required=True)
    parser.add_argument("--manifest", type=Path, required=True)
    parser.add_argument("--apply", action="store_true")
    args = parser.parse_args()
    ledger = _read(args.ledger)
    result = apply_verified_results(
        ledger, _manifest(args.manifest), apply=args.apply
    )
    if args.apply and result["status"] == "applied":
        _write_atomic(args.ledger, ledger)
    print(json.dumps(result, ensure_ascii=False, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
