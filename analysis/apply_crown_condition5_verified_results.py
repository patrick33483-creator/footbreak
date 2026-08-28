#!/usr/bin/env python3
"""Proof-gated settlement of 28 verified Crown condition #5 observations."""
from __future__ import annotations

import argparse
import copy
import json
from collections import Counter
from datetime import datetime
from pathlib import Path
from typing import Any

from analysis.legacy_batch_runtime import load_production_legacy_batch_authority
from analysis.recover_crown_condition5_history import (
    SIGNATURE, _hash, _read, _write_atomic,
)
from analysis.wilson_validation import (
    _canonical_hash, _time, active_evidence_version, recompute_namespace,
    validate_formal_row,
)
from crown.lines import settle_total


SYSTEM = "crown"
MIGRATION = "crown-condition5-verified-results-20260828-v1"
BINARY_HITS = {"Won", "Half Won"}
BINARY_LOSSES = {"Lost", "Half Lost"}
SETTLEMENTS = BINARY_HITS | BINARY_LOSSES | {"Refunded"}


def _condition(ledger: dict[str, Any]) -> tuple[dict[str, Any], dict[str, Any]]:
    namespace = ledger.get("wilson_validation")
    if not isinstance(namespace, dict):
        raise ValueError("Wilson namespace missing")
    frozen = (namespace.get("conditions") or {}).get(SIGNATURE)
    if not isinstance(frozen, dict) or frozen.get("condition_number") != 5:
        raise ValueError("Crown condition #5 missing or signature changed")
    return namespace, frozen


def _same_number(left: Any, right: Any) -> bool:
    try:
        return abs(float(left) - float(right)) <= 1e-9
    except (TypeError, ValueError):
        return False


def _active(frozen: dict[str, Any], namespace: dict[str, Any], authority: Any) -> dict[str, Any]:
    value = active_evidence_version(
        frozen, migration_boundary=namespace["activation_at"],
        authority_context=authority,
    )
    if not isinstance(value, dict):
        raise ValueError("condition #5 active evidence unavailable")
    return {
        "version": value.get("version"),
        "hits": value.get("cumulative_hits"),
        "decided": value.get("cumulative_decided"),
        "wilson95_lower_raw": value.get("wilson95_lower_raw"),
        "minimum_acceptable_odds_raw": value.get("minimum_acceptable_odds_raw"),
        "minimum_acceptable_odds_display": value.get("minimum_acceptable_odds_display"),
        "pending_rollover_progress": copy.deepcopy(
            frozen.get("pending_rollover_progress") or {}
        ),
    }


def _manifest(path: Path) -> dict[str, Any]:
    value = _read(path)
    if (
        value.get("schema_version") != 1
        or value.get("condition_number") != 5
        or value.get("condition_signature") != SIGNATURE
        or value.get("decision_stage") != "T-30"
        or value.get("market") != "HIL"
        or value.get("selection") != "H"
        or value.get("score_scope")
        != "90_minutes_including_stoppage_time_excluding_extra_time"
    ):
        raise ValueError("verified result manifest identity invalid")
    rows = value.get("results")
    if not isinstance(rows, list) or len(rows) != 28:
        raise ValueError("verified result manifest must contain exactly 28 rows")
    ids = [str(row.get("match_id") or "") for row in rows if isinstance(row, dict)]
    if len(ids) != 28 or len(set(ids)) != 28 or not all(ids):
        raise ValueError("verified result manifest fixture identities invalid")
    postponed = value.get("postponed_pending")
    if postponed != [{"match_id": "3041538", "reason": "postponed_no_result"}]:
        raise ValueError("postponed condition #5 fixture identity invalid")
    verified_at = _time(value.get("verified_at"))
    if verified_at is None:
        raise ValueError("verified_at invalid")
    for row in rows:
        sources = row.get("sources")
        if not isinstance(sources, list) or len(sources) < 2:
            raise ValueError(f"two result sources required: {row.get('match_id')}")
        if not all(str(source).startswith(("http://", "https://")) for source in sources):
            raise ValueError(f"result source URL invalid: {row.get('match_id')}")
        try:
            home, away = int(row["home_score"]), int(row["away_score"])
        except (KeyError, TypeError, ValueError) as exc:
            raise ValueError(f"score invalid: {row.get('match_id')}") from exc
        if home < 0 or away < 0:
            raise ValueError(f"score negative: {row.get('match_id')}")
        calculated = settle_total(float(row["line"]), "H", home, away)
        if calculated != row.get("expected_result") or calculated not in SETTLEMENTS:
            raise ValueError(f"precomputed settlement mismatch: {row.get('match_id')}")
    return value


def apply_verified_results(
    ledger: dict[str, Any], manifest: dict[str, Any], *, apply: bool,
) -> dict[str, Any]:
    before_hash = _canonical_hash(ledger)
    namespace, frozen = _condition(ledger)
    authority = load_production_legacy_batch_authority(ledger)
    before = _active(frozen, namespace, authority)
    projection_time = datetime.now().astimezone()
    verified_at = str(manifest["verified_at"])
    if _time(verified_at) is None or _time(verified_at) > projection_time:
        raise ValueError("verified_at is later than this execution")

    target_rows: dict[str, dict[str, Any]] = {}
    for row in namespace.get("observations") or []:
        if not isinstance(row, dict):
            continue
        if (
            row.get("frozen_condition_signature") == SIGNATURE
            and row.get("stage") == "T-30"
            and str(row.get("market") or row.get("code") or "") == "HIL"
        ):
            fixture = str(row.get("match_id") or "")
            if fixture in target_rows:
                raise ValueError(f"duplicate condition #5 observation fixture: {fixture}")
            target_rows[fixture] = row

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
    manifest_ids = {str(item["match_id"]) for item in manifest["results"]}
    for item in manifest["results"]:
        fixture = str(item["match_id"])
        row = target_rows.get(fixture)
        if not isinstance(row, dict):
            raise ValueError(f"condition #5 observation missing: {fixture}")
        if (
            row.get("formal_bet") is not False
            or str(row.get("side") or "").upper() != "H"
            or not _same_number(row.get("line"), item.get("line"))
        ):
            raise ValueError(f"condition #5 immutable identity mismatch: {fixture}")
        kickoff = _time(row.get("kickoff") or row.get("kickoff_hkt"))
        if kickoff is None or kickoff > _time(verified_at):
            raise ValueError(f"result proof predates kickoff: {fixture}")
        calculated = settle_total(
            float(row["line"]), "H",
            int(item["home_score"]), int(item["away_score"]),
        )
        if calculated != item["expected_result"]:
            raise ValueError(f"runtime settlement mismatch: {fixture}")
        proof = {
            "match_id": fixture,
            "condition_signature": SIGNATURE,
            "stage": "T-30",
            "market": "HIL",
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
        if row.get("status") == "SETTLED":
            if (
                row.get("result") != calculated
                or row.get("result_proof_hash") not in (None, proof_hash)
            ):
                raise ValueError(f"existing result conflicts: {fixture}")
            state = "already_same"
            result["already_settled_same"] += 1
        elif (
            row.get("status") == "PENDING"
            and row.get("result") in (None, "PENDING")
        ):
            admitted, reason = validate_formal_row(
                row, system=SYSTEM, signature=SIGNATURE, frozen=frozen,
                projection_time=projection_time, require_settled=False,
                ledger=ledger, authority_context=authority,
            )
            if admitted is None:
                raise ValueError(f"pending formal row invalid {fixture}: {reason}")
            row.update({
                "status": "SETTLED",
                "result": calculated,
                "settled_at": verified_at,
                "score": f"{item['home_score']}-{item['away_score']}",
                "settlement_source": "cross_source_verified_manual_backfill",
                "result_recovery_source": "condition5_verified_results_20260828",
                "result_proof_hash": proof_hash,
                "normal_grade_source_hash": proof_hash,
                "score_scope": manifest["score_scope"],
                "result_sources": list(item["sources"]),
            })
            row.setdefault("history", []).append({
                "ts": verified_at,
                "stage": "SETTLED",
                "action": "條件 #5 待補賽果：雙來源核實90分鐘比分",
                "result": calculated,
                "score": row["score"],
                "proof_hash": proof_hash,
            })
            admitted, reason = validate_formal_row(
                row, system=SYSTEM, signature=SIGNATURE, frozen=frozen,
                projection_time=projection_time, require_settled=True,
                ledger=ledger, authority_context=authority,
            )
            if admitted is None:
                raise ValueError(f"settled formal row invalid {fixture}: {reason}")
            result["newly_settled"] += 1
        else:
            raise ValueError(
                f"condition #5 observation has unexpected state {fixture}: "
                f"{row.get('status')}/{row.get('result')}"
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

    unexpected = manifest_ids - set(target_rows)
    if unexpected:
        raise ValueError(f"manifest observations unresolved: {sorted(unexpected)}")
    for item in manifest["postponed_pending"]:
        fixture = str(item["match_id"])
        row = target_rows.get(fixture)
        if not isinstance(row, dict):
            raise ValueError(f"postponed condition #5 observation missing: {fixture}")
        if row.get("status") != "PENDING" or row.get("result") != "PENDING":
            raise ValueError(f"postponed fixture is no longer pending: {fixture}")
        result["postponed_pending"].append({
            "match_id": fixture,
            "status": row.get("status"),
            "result": row.get("result"),
        })

    if result["newly_settled"] + result["already_settled_same"] != 28:
        raise ValueError("not every verified fixture reached an exact settlement")
    recompute_namespace(ledger, SYSTEM, authority_context=authority)
    result["after"] = _active(frozen, namespace, authority)
    result["settlement_counts"] = dict(result["settlement_counts"])
    result["after_ledger_hash"] = _canonical_hash(ledger)
    result["before_ledger_hash"] = before_hash
    result["status"] = (
        "applied" if apply and result["newly_settled"] > 0
        else "already_applied" if result["newly_settled"] == 0
        else "audit_ready"
    )
    if apply and result["newly_settled"] > 0:
        namespace["condition5_verified_results_20260828_v1"] = {
            "completed": True,
            "migration": MIGRATION,
            "completed_at": projection_time.isoformat(),
            "verified_at": verified_at,
            "newly_settled": result["newly_settled"],
            "already_settled_same": result["already_settled_same"],
            "settlement_counts": copy.deepcopy(result["settlement_counts"]),
            "postponed_pending": copy.deepcopy(result["postponed_pending"]),
            "fixture_proof_root_hash": _hash(result["fixtures"]),
            "after": copy.deepcopy(result["after"]),
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
        ledger, _manifest(args.manifest), apply=args.apply,
    )
    if args.apply and result["status"] == "applied":
        _write_atomic(args.ledger, ledger)
    print(json.dumps(result, ensure_ascii=False, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
