#!/usr/bin/env python3
"""Apply proof-gated condition #2 results from a disposable refresh.

Both the full prediction history and Crown Wilson ledger are updated only
after exact immutable identities, result proofs, and the empty prospective
boundary are verified.  The caller is responsible for service locking and
backups.
"""
from __future__ import annotations

import argparse
import copy
import json
from pathlib import Path
from typing import Any

from crown.lines import settle_total
from crown.prediction_history import _grade_market
from analysis.recover_crown_condition2_history import (
    EXPECTED_BASELINE,
    EXPECTED_DUPLICATE,
    MIGRATION,
    _condition,
    _hash,
    _read,
    _rows,
    _same_number,
    _stamp,
    _time,
    _write_atomic,
)
from analysis.wilson_validation import _evidence_values, _version_hash


RESULT_FIELDS = {
    "actual", "score", "correct", "result_status", "verified_at",
    "result_source", "result_detail", "market_grades",
    "result_missing_reason", "result_attempted_at",
}


def _identity(row: dict[str, Any]) -> tuple[str, str]:
    fixture = str(row.get("match_id") or row.get("history_key") or "")
    stage = str(row.get("stage") or "")
    return fixture, stage


def _assert_empty_prospective(frozen: dict[str, Any]) -> None:
    for key in ("prospective", "prospective_observations"):
        value = frozen.get(key)
        if isinstance(value, dict) and value:
            raise ValueError(f"condition2_{key}_not_empty")
        if isinstance(value, list) and value:
            raise ValueError(f"condition2_{key}_not_empty")
    progress = frozen.get("pending_rollover_progress") or {}
    if int(progress.get("eligible_decided") or 0) != 0:
        raise ValueError("condition2_pending_rollover_progress_not_empty")


def _audit_map(report: dict[str, Any]) -> dict[str, dict[str, Any]]:
    if report.get("mode") != "read_only_copy_audit":
        raise ValueError("unexpected audit mode")
    rows = report.get("rows")
    if not isinstance(rows, list):
        raise ValueError("audit rows missing")
    output = {
        str(row.get("match_id") or ""): row
        for row in rows
        if isinstance(row, dict) and row.get("audit_status") == "resolved"
    }
    if len(output) != int(report.get("resolved") or -1):
        raise ValueError("resolved audit rows are ambiguous")
    return output


def _apply_manual_overrides(
    report: dict[str, Any],
    refreshed_history: dict[str, Any],
    overrides: dict[str, Any],
) -> int:
    """Turn explicit user-attested scores into auditable resolved rows."""
    if not overrides:
        return 0
    audit_rows = report.get("rows")
    if not isinstance(audit_rows, list):
        raise ValueError("audit rows missing")
    history_rows = _rows(refreshed_history)
    applied = 0
    for fixture, raw in overrides.items():
        if not isinstance(raw, dict):
            raise ValueError(f"manual override invalid: {fixture}")
        matches = [
            row for row in audit_rows
            if isinstance(row, dict) and str(row.get("match_id") or "") == str(fixture)
        ]
        if len(matches) != 1 or matches[0].get("audit_status") != "unresolved":
            raise ValueError(f"manual override is not exactly one unresolved row: {fixture}")
        audit = matches[0]
        try:
            home = int(raw["home_score"])
            away = int(raw["away_score"])
        except (KeyError, TypeError, ValueError) as exc:
            raise ValueError(f"manual score invalid: {fixture}") from exc
        if home < 0 or away < 0:
            raise ValueError(f"manual score negative: {fixture}")
        attested_at = str(raw.get("attested_at") or "")
        if (
            _time(attested_at) is None
            or _time(audit.get("kickoff")) is None
            or _time(attested_at) < _time(audit.get("kickoff"))
        ):
            raise ValueError(f"manual attestation predates kickoff: {fixture}")
        source = str(raw.get("source") or "")
        if source != "user_attested_manual_score":
            raise ValueError(f"manual source not allowed: {fixture}")
        settlement = settle_total(
            float(audit["line"]), str(audit["side"]), home, away,
        )
        proof = {
            "fixture_identity": {
                key: audit.get(key)
                for key in (
                    "match_id", "kickoff", "stage_at", "league", "home", "away",
                    "side", "line", "odds",
                )
            },
            "home_score": home,
            "away_score": away,
            "attested_at": attested_at,
            "source": source,
            "attestation_reference": raw.get("attestation_reference"),
        }
        audit.update({
            "audit_status": "resolved",
            "reason": None,
            "result": settlement,
            "settled_at": attested_at,
            "result_proof_hash": _hash(proof),
            "score": f"{home}-{away}",
            "result_source": source,
        })

        updated_history = 0
        actual = "主勝" if home > away else ("和局" if home == away else "客勝")
        score = {"home_score": home, "away_score": away, "corners_total": None}
        for row in history_rows:
            if _identity(row)[0] != str(fixture):
                continue
            if _time(row.get("kickoff")) != _time(audit.get("kickoff")):
                raise ValueError(f"manual history kickoff mismatch: {fixture}")
            row.update({
                "actual": actual,
                "score": f"{home}-{away}",
                "correct": (
                    row.get("forecast") == actual if row.get("forecast") else None
                ),
                "result_status": "已核對",
                "verified_at": attested_at,
                "result_source": source,
                "result_detail": {
                    "home_score": home,
                    "away_score": away,
                    "corners_total": None,
                    "attestation_reference": raw.get("attestation_reference"),
                },
                "market_grades": [
                    _grade_market(prediction, score)
                    for prediction in (row.get("market_predictions") or [])
                    if isinstance(prediction, dict)
                ],
                "result_missing_reason": None,
            })
            updated_history += 1
        if updated_history == 0:
            raise ValueError(f"manual history fixture missing: {fixture}")
        applied += 1

    report["resolved"] = int(report.get("resolved") or 0) + applied
    report["unresolved"] = int(report.get("unresolved") or 0) - applied
    if report["unresolved"] < 0:
        raise ValueError("manual overrides exceed unresolved audit rows")
    report["manual_overrides_applied"] = applied
    return applied


def _merge_history(
    production: dict[str, Any],
    refreshed: dict[str, Any],
    fixture_ids: set[str],
) -> int:
    production_rows = _rows(production)
    refreshed_rows = {
        _identity(row): row
        for row in _rows(refreshed)
        if _identity(row)[0] in fixture_ids
    }
    updated = 0
    for row in production_rows:
        key = _identity(row)
        if key[0] not in fixture_ids:
            continue
        source = refreshed_rows.get(key)
        if not source:
            raise ValueError(f"refreshed history identity missing: {key}")
        if (
            _time(row.get("kickoff")) != _time(source.get("kickoff"))
            or _time(_stamp(row)) != _time(_stamp(source))
        ):
            raise ValueError(f"refreshed history immutable identity changed: {key}")
        if source.get("result_status") not in {"已核對", "不計"}:
            continue
        for field in RESULT_FIELDS:
            if field in source:
                row[field] = copy.deepcopy(source[field])
        updated += 1
    return updated


def _update_recovery_rows(
    frozen: dict[str, Any],
    resolved: dict[str, dict[str, Any]],
) -> dict[str, int]:
    rows = frozen.get("historical_recovery_rows")
    if not isinstance(rows, list):
        raise ValueError("historical recovery rows missing")
    applied = 0
    for row in rows:
        if not isinstance(row, dict):
            continue
        fixture = str(row.get("match_id") or "")
        audit = resolved.get(fixture)
        if not audit:
            continue
        if row.get("result") != "PENDING":
            raise ValueError(f"resolved fixture is no longer pending: {fixture}")
        checks = (
            _time(row.get("kickoff")) == _time(audit.get("kickoff")),
            _time(row.get("stage_at")) == _time(audit.get("stage_at")),
            str(row.get("side") or "").upper() == str(audit.get("side") or "").upper(),
            _same_number(row.get("line"), audit.get("line")),
            _same_number(row.get("odds"), audit.get("odds")),
        )
        if not all(checks):
            raise ValueError(f"resolved fixture identity mismatch: {fixture}")
        row["result"] = str(audit["result"])
        row["settled_at"] = audit.get("settled_at")
        row["normal_grade_source_hash"] = audit.get("result_proof_hash")
        row["result_recovery_source"] = audit.get("result_source")
        applied += 1
    if applied != len(resolved):
        raise ValueError("not every resolved audit row was applied")

    hits = sum(
        row.get("result") in {"Won", "Half Won"}
        for row in rows if isinstance(row, dict)
    )
    losses = sum(
        row.get("result") in {"Lost", "Half Lost"}
        for row in rows if isinstance(row, dict)
    )
    pushes = sum(
        row.get("result") == "Refunded"
        for row in rows if isinstance(row, dict)
    )
    pending = sum(
        row.get("result") == "PENDING"
        for row in rows if isinstance(row, dict)
    )
    return {
        "applied": applied, "hits": hits, "losses": losses,
        "decided": hits + losses, "pushes": pushes, "pending": pending,
        "settled": hits + losses + pushes,
    }


def _rebuild_v2(frozen: dict[str, Any], counts: dict[str, int]) -> dict[str, Any]:
    versions = frozen.get("evidence_versions")
    if not isinstance(versions, list) or len(versions) != 2:
        raise ValueError("condition #2 V2 chain changed")
    v1, old_v2 = copy.deepcopy(versions[0]), copy.deepcopy(versions[1])
    recovery = old_v2.get("condition2_history_recovery")
    if not isinstance(recovery, dict) or recovery.get("migration") != MIGRATION:
        raise ValueError("condition #2 recovery V2 missing")
    values = _evidence_values(
        EXPECTED_BASELINE["hits"] + EXPECTED_DUPLICATE["hits"] + counts["hits"],
        EXPECTED_BASELINE["decided"] + EXPECTED_DUPLICATE["decided"] + counts["decided"],
    )
    v2 = {
        **old_v2,
        "batch_hits": EXPECTED_DUPLICATE["hits"] + counts["hits"],
        "batch_decided": EXPECTED_DUPLICATE["decided"] + counts["decided"],
        "cumulative_hits": EXPECTED_BASELINE["hits"] + EXPECTED_DUPLICATE["hits"] + counts["hits"],
        "cumulative_decided": (
            EXPECTED_BASELINE["decided"] + EXPECTED_DUPLICATE["decided"]
            + counts["decided"]
        ),
        "wilson95_lower_raw": values["wilson95_lower_raw"],
        "minimum_acceptable_odds_raw": values["minimum_acceptable_odds_raw"],
        "minimum_acceptable_odds_display": values["display"]["minimum_acceptable_odds"],
        "legacy_prospective_cohort": {
            "hits": EXPECTED_DUPLICATE["hits"] + counts["hits"],
            "decided": EXPECTED_DUPLICATE["decided"] + counts["decided"],
            "pushes": EXPECTED_DUPLICATE["pushes"] + counts["pushes"],
        },
    }
    row_hashes = sorted(
        _hash(row) for row in frozen["historical_recovery_rows"]
        if isinstance(row, dict)
    )
    v2["condition2_history_recovery"] = {
        **copy.deepcopy(recovery),
        "recovered": {
            key: counts[key]
            for key in ("hits", "losses", "decided", "pushes", "pending", "settled")
        },
        "fixture_rows_root_hash": _hash(row_hashes),
        "fixture_row_hashes": row_hashes,
        "prior_result_recovery_v2_evidence_hash": old_v2["evidence_hash"],
    }
    v2["evidence_hash"] = _version_hash(v2)
    frozen["evidence_versions"] = [v1, v2]
    frozen["active_evidence_hash"] = v2["evidence_hash"]
    frozen["active_evidence"] = {
        key: copy.deepcopy(v2.get(key))
        for key in (
            "version", "cumulative_hits", "cumulative_decided",
            "wilson95_lower_raw", "minimum_acceptable_odds_raw",
            "minimum_acceptable_odds_display", "activation_boundary_at",
            "created_at", "evidence_hash",
        )
    }
    frozen["rollover_audit"] = [copy.deepcopy(v2)]
    return v2


def apply(
    ledger: dict[str, Any],
    production_history: dict[str, Any],
    refreshed_history: dict[str, Any],
    report: dict[str, Any],
    manual_overrides: dict[str, Any] | None = None,
) -> dict[str, Any]:
    namespace, frozen = _condition(ledger)
    _assert_empty_prospective(frozen)
    manual_applied = _apply_manual_overrides(
        report, refreshed_history, manual_overrides or {},
    )
    resolved = _audit_map(report)
    history_rows_updated = _merge_history(
        production_history, refreshed_history, set(resolved),
    )
    counts = _update_recovery_rows(frozen, resolved)
    v2 = _rebuild_v2(frozen, counts)
    marker = namespace.get("condition2_history_recovery_v1")
    if isinstance(marker, dict):
        marker["settled"] = counts["settled"]
        marker["pending_result"] = counts["pending"]
        marker["after_recovery"] = {
            "active_version": 2,
            "hits": v2["cumulative_hits"],
            "decided": v2["cumulative_decided"],
            "wilson95_lower_raw": v2["wilson95_lower_raw"],
            "minimum_acceptable_odds_raw": v2["minimum_acceptable_odds_raw"],
            "pending_rollover_progress": copy.deepcopy(
                frozen.get("pending_rollover_progress") or {}
            ),
        }
        marker["pending_result_recovery"] = {
            "applied": counts["applied"],
            "proof_root_hash": _hash(sorted(
                str(row.get("result_proof_hash") or "")
                for row in resolved.values()
            )),
        }
    return {
        "status": "applied",
        "history_rows_updated": history_rows_updated,
        "newly_resolved_fixtures": counts["applied"],
        "manual_overrides_applied": manual_applied,
        "recovery_counts": counts,
        "active_v2": {
            "hits": v2["cumulative_hits"],
            "decided": v2["cumulative_decided"],
            "wilson95_lower_raw": v2["wilson95_lower_raw"],
            "minimum_acceptable_odds_raw": v2["minimum_acceptable_odds_raw"],
            "minimum_acceptable_odds_display": v2["minimum_acceptable_odds_display"],
            "evidence_hash": v2["evidence_hash"],
        },
        "prospective_preserved": True,
        "pending_rollover_progress": copy.deepcopy(
            frozen.get("pending_rollover_progress") or {}
        ),
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--ledger", type=Path, required=True)
    parser.add_argument("--production-history", type=Path, required=True)
    parser.add_argument("--refreshed-history", type=Path, required=True)
    parser.add_argument("--audit-report", type=Path, required=True)
    parser.add_argument("--manual-overrides", type=Path)
    args = parser.parse_args()
    ledger = _read(args.ledger)
    production = _read(args.production_history)
    refreshed = _read(args.refreshed_history)
    report = _read(args.audit_report)
    manual_overrides = _read(args.manual_overrides) if args.manual_overrides else {}
    result = apply(
        ledger, production, refreshed, report,
        manual_overrides=manual_overrides,
    )
    _write_atomic(args.production_history, production)
    _write_atomic(args.ledger, ledger)
    print(json.dumps(result, ensure_ascii=False, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
