#!/usr/bin/env python3
"""Fail-closed, offline production preflight for Footbreak condition #17.

The input must be an immutable copy captured while holding the production
Footbreak writer lock.  This program never writes the input ledger.  Synthetic
rows are derived from compatible production rows only inside a deep copy and
are never included in output.
"""
from __future__ import annotations

import argparse
import copy
import hashlib
import json
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from analysis import wilson_validation as wv


SYSTEM = "footbreak"
CONDITION_NUMBER = 17
EXPECTED_DECIDED = 18
EXPECTED_HITS = 10
MAX_LEDGER_BYTES = 128 * 1024 * 1024
EXCLUSION_KEYS = {
    "missing_or_invalid_provenance",
    "before_snapshot_boundary",
    "not_binary_decided",
    "duplicate_or_conflicting_fixture_market",
}


class PreflightFailure(RuntimeError):
    """A safe, non-sensitive failure code."""


def _canonical_bytes(value: Any) -> bytes:
    return json.dumps(
        value, ensure_ascii=False, sort_keys=True, separators=(",", ":"),
    ).encode("utf-8")


def _sha256(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def _require(condition: bool, code: str) -> None:
    if not condition:
        raise PreflightFailure(code)


def _condition_17(ledger: dict[str, Any]) -> tuple[dict[str, Any], str]:
    namespace = ledger.get(wv.NAMESPACE)
    _require(
        isinstance(namespace, dict)
        and namespace.get("schema_version") == wv.SCHEMA_VERSION
        and namespace.get("system") == SYSTEM,
        "namespace_invalid",
    )
    order = namespace.get("condition_order")
    conditions = namespace.get("conditions")
    _require(
        isinstance(order, list)
        and isinstance(conditions, dict)
        and len(order) >= CONDITION_NUMBER
        and isinstance(order[CONDITION_NUMBER - 1], str),
        "condition_17_registry_position_invalid",
    )
    signature = order[CONDITION_NUMBER - 1]
    frozen = conditions.get(signature)
    _require(
        isinstance(frozen, dict)
        and frozen.get("condition_number") == CONDITION_NUMBER,
        "condition_17_missing",
    )
    return frozen, signature


def _ranking_row(frozen: dict[str, Any]) -> dict[str, Any]:
    definition = frozen.get("definition")
    _require(
        isinstance(definition, dict)
        and isinstance(definition.get("miner_key"), list),
        "condition_17_definition_invalid",
    )
    return {**copy.deepcopy(definition), "key": copy.deepcopy(definition["miner_key"])}


def _same_signature_rows(
    ledger: dict[str, Any], signature: str,
) -> list[dict[str, Any]]:
    namespace = ledger[wv.NAMESPACE]
    rows: list[dict[str, Any]] = []
    for collection in (
        ledger.get("bets"),
        namespace.get("observations", []),
    ):
        _require(isinstance(collection, list), "formal_evidence_container_invalid")
        for row in collection:
            _require(isinstance(row, dict), "formal_evidence_row_invalid")
            if row.get("frozen_condition_signature") == signature:
                rows.append(row)
    return rows


def _project_card(
    ledger: dict[str, Any], frozen: dict[str, Any], signature: str,
) -> dict[str, Any]:
    before = _canonical_bytes(ledger)
    cards = wv.project_frozen_ranking_evidence(
        ledger, SYSTEM, [_ranking_row(frozen)],
    )
    after = _canonical_bytes(ledger)
    _require(before == after, "projection_mutated_ledger")
    matching = [
        card for card in cards
        if card.get("condition_number") == CONDITION_NUMBER
        and card.get("condition_signature") == signature
    ]
    _require(len(matching) == 1, "condition_17_projection_unavailable")
    return matching[0]


def _verify_manifest_and_identity(
    ledger: dict[str, Any],
    frozen: dict[str, Any],
    signature: str,
    *,
    expected_manifest_hash: str,
    expected_signature: str,
    expected_initial_evidence_hash: str,
) -> tuple[str, str]:
    namespace = ledger[wv.NAMESPACE]
    expected, validated, reason = wv._expected_production_identity_manifest(
        namespace, SYSTEM,
    )
    _require(
        reason is None and isinstance(expected, dict) and isinstance(validated, dict),
        "canonical_manifest_unavailable",
    )
    manifest = namespace.get("production_identity_manifest")
    _require(
        isinstance(manifest, dict)
        and _canonical_bytes(manifest) == _canonical_bytes(expected),
        "canonical_manifest_mismatch",
    )
    _require(
        expected.get("manifest_hash") == expected_manifest_hash,
        "trusted_manifest_hash_mismatch",
    )
    _require(signature == expected_signature, "trusted_condition_signature_mismatch")
    versions = frozen.get("evidence_versions")
    _require(
        isinstance(versions, list)
        and len(versions) == 1
        and isinstance(versions[0], dict),
        "condition_17_not_at_v1",
    )
    initial_hash = versions[0].get("evidence_hash")
    _require(
        initial_hash == expected_initial_evidence_hash,
        "trusted_initial_evidence_hash_mismatch",
    )
    entry = manifest.get("entries")
    _require(
        isinstance(entry, list)
        and len(entry) >= CONDITION_NUMBER
        and entry[CONDITION_NUMBER - 1] == {
            "condition_number": CONDITION_NUMBER,
            "condition_signature": signature,
            "definition_hash": wv._canonical_hash(frozen.get("definition")),
            "initial_evidence_hash": initial_hash,
        },
        "condition_17_manifest_entry_mismatch",
    )
    definition, chain, chain_reason = wv._validate_frozen_identity_and_chain(
        frozen, signature, SYSTEM,
    )
    _require(
        chain_reason is None
        and isinstance(definition, dict)
        and isinstance(chain, list)
        and len(chain) == 1,
        "condition_17_identity_chain_invalid",
    )
    active = chain[-1]
    pointer = frozen.get("active_evidence")
    pointer_keys = {
        "version", "cumulative_hits", "cumulative_decided",
        "wilson95_lower_raw", "minimum_acceptable_odds_raw",
        "minimum_acceptable_odds_display", "activation_boundary_at",
        "created_at", "evidence_hash",
    }
    _require(
        frozen.get("signature") == signature
        and frozen.get("definition") == definition
        and frozen.get("active_evidence_version") == 1
        and frozen.get("active_evidence_hash") == initial_hash
        and isinstance(pointer, dict)
        and set(pointer) == pointer_keys
        and all(pointer.get(key) == active.get(key) for key in pointer_keys),
        "condition_17_active_pointer_invalid",
    )
    return str(expected["manifest_hash"]), str(initial_hash)


def _verify_exact_anomaly_cohort(
    ledger: dict[str, Any],
    frozen: dict[str, Any],
    signature: str,
    projection_time: datetime,
) -> list[dict[str, Any]]:
    active = frozen["evidence_versions"][-1]
    same_signature = _same_signature_rows(ledger, signature)
    compatible = [
        row for row in same_signature
        if wv._project_footbreak_17_schema1_settlement_anomaly(
            row,
            signature=signature,
            frozen=frozen,
            active=active,
            projection_time=projection_time,
            ledger=ledger,
        ) is not None
    ]
    resolved = wv._resolve_footbreak_17_legacy_anomaly_cohort(
        compatible,
        signature=signature,
        frozen=frozen,
        active=active,
        projection_time=projection_time,
        ledger=ledger,
    )
    _require(
        len(same_signature) == EXPECTED_DECIDED,
        "extra_or_missing_same_signature_rows",
    )
    _require(
        resolved is not None
        and len(compatible) == EXPECTED_DECIDED
        and {id(row) for row in resolved} == {id(row) for row in compatible}
        and sum(row.get("result") in wv.BINARY_HIT_RESULTS for row in compatible)
        == EXPECTED_HITS,
        "condition_17_anomaly_cohort_mismatch",
    )
    return compatible


def _verify_durable_progress(
    ledger: dict[str, Any], frozen: dict[str, Any], signature: str,
) -> None:
    pending = frozen.get("pending_rollover_progress")
    _require(isinstance(pending, dict), "durable_progress_missing")
    exclusions = pending.get("excluded")
    _require(
        pending.get("eligible_decided") == EXPECTED_DECIDED
        and pending.get("eligible_hits") == EXPECTED_HITS
        and pending.get("required") == wv.ROLLOVER_BATCH_SIZE
        and pending.get("display") == "18/20"
        and isinstance(exclusions, dict)
        and set(exclusions) == EXCLUSION_KEYS
        and all(
            isinstance(value, int) and not isinstance(value, bool) and value == 0
            for value in exclusions.values()
        ),
        "durable_18_of_20_progress_invalid",
    )
    card = _project_card(ledger, frozen, signature)
    detail = card.get("pending_rollover_evidence")
    _require(
        isinstance(detail, dict)
        and detail.get("complete") is True
        and detail.get("expected_decided") == EXPECTED_DECIDED
        and detail.get("expected_hits") == EXPECTED_HITS
        and isinstance(detail.get("rows"), list)
        and len(detail["rows"]) == EXPECTED_DECIDED
        and sum(row.get("hit") is True for row in detail["rows"]) == EXPECTED_HITS,
        "condition_17_projection_progress_invalid",
    )


def _safe_synthetic_times(
    rows: list[dict[str, Any]], frozen: dict[str, Any], now: datetime,
) -> list[datetime]:
    timestamps = [
        wv._time((row.get("rollover_provenance") or {}).get("stage_at"))
        for row in rows
    ]
    timestamps.append(wv._time(frozen["evidence_versions"][-1].get("created_at")))
    _require(all(value is not None for value in timestamps), "synthetic_time_source_invalid")
    base = max(value for value in timestamps if value is not None)
    available = now.astimezone(timezone.utc) - base
    _require(available > timedelta(microseconds=8), "synthetic_time_window_unavailable")
    step = min(timedelta(seconds=1), available / 8)
    return [base + step, base + step * 5]


def _adapt_snapshot_binding(
    ledger: dict[str, Any],
    source: dict[str, Any],
    synthetic: dict[str, Any],
    *,
    fixture: str,
    stage_at: str,
    kickoff: str,
    suffix: int,
) -> None:
    binding = synthetic.get("native_snapshot_binding")
    if binding is None:
        return
    _require(isinstance(binding, dict), "synthetic_snapshot_binding_invalid")
    source_watch = (ledger.get("watch") or {}).get(str(source.get("match_id") or ""))
    _require(isinstance(source_watch, dict), "synthetic_snapshot_source_missing")
    watch = copy.deepcopy(source_watch)
    watch["match_id"] = fixture
    for key in ("kickoff", "kickoff_hkt"):
        if key in watch:
            watch[key] = kickoff
    matches = [
        stage for stage in watch.get("stages") or []
        if isinstance(stage, dict)
        and stage.get("stage") == wv.DECISION_STAGE
        and stage.get("native_snapshot_id") == binding.get("snapshot_id")
        and stage.get("native_snapshot_hash") == binding.get("snapshot_hash")
    ]
    _require(len(matches) == 1, "synthetic_snapshot_source_ambiguous")
    stage = matches[0]
    stage["ts"] = stage_at
    snapshot_id = f"condition17-preflight-{suffix}"
    stage["native_snapshot_id"] = snapshot_id
    payload = {
        key: value for key, value in stage.items()
        if key not in {"native_snapshot_id", "native_snapshot_hash"}
    }
    snapshot_hash = _sha256(json.dumps(
        payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"),
        default=str,
    ).encode())
    stage["native_snapshot_hash"] = snapshot_hash
    synthetic["native_snapshot_binding"] = {
        "schema_version": 1,
        "system": SYSTEM,
        "snapshot_id": snapshot_id,
        "snapshot_hash": snapshot_hash,
    }
    watches = ledger.setdefault("watch", {})
    _require(isinstance(watches, dict), "synthetic_watch_container_invalid")
    watches[fixture] = watch


def _derive_strict_synthetic_row(
    ledger: dict[str, Any],
    source: dict[str, Any],
    frozen: dict[str, Any],
    signature: str,
    *,
    suffix: int,
    stage_time: datetime,
    projection_time: datetime,
) -> dict[str, Any]:
    row = copy.deepcopy(source)
    fixture = f"condition17-preflight-synthetic-{suffix}"
    stage_at = stage_time.isoformat()
    created_at = (stage_time + timedelta(microseconds=1)).isoformat()
    kickoff = (stage_time + timedelta(microseconds=2)).isoformat()
    settled_at = (stage_time + timedelta(microseconds=3)).isoformat()
    _require(
        wv._time(settled_at) <= projection_time,
        "synthetic_time_window_unavailable",
    )
    row["match_id"] = fixture
    row["created_at"] = created_at
    if "admission_at" in row:
        row["admission_at"] = created_at
    if "kickoff" in row:
        row["kickoff"] = kickoff
    if "kickoff_hkt" in row:
        row["kickoff_hkt"] = kickoff
    if "kickoff" not in row and "kickoff_hkt" not in row:
        row["kickoff"] = kickoff
    row["settled_at"] = settled_at
    row["status"] = "SETTLED"
    row["result"] = "Lost"
    row["native_stage_at"] = stage_at
    marker = row.get("rollover_provenance")
    _require(isinstance(marker, dict), "synthetic_marker_missing")
    marker["stage_at"] = stage_at
    marker["fixture_market_hash"] = wv._fixture_market_hash(
        SYSTEM, fixture, str(row.get("market") or row.get("code") or ""),
    )
    if row.get("formal_bet") is False:
        row["observation_id"] = (
            f"{fixture}|{row.get('market') or row.get('code')}|{row.get('stage')}"
            f"|{signature}|low-odds"
        )
    else:
        row["bet_id"] = (
            f"{fixture}|{row.get('market') or row.get('code')}|"
            f"{wv.DECISION_STAGE}|{wv.STRATEGY}"
        )
    _adapt_snapshot_binding(
        ledger, source, row, fixture=fixture, stage_at=stage_at,
        kickoff=kickoff, suffix=suffix,
    )
    admitted, reason = wv.validate_formal_row(
        row,
        system=SYSTEM,
        signature=signature,
        frozen=frozen,
        projection_time=projection_time,
        require_settled=True,
        ledger=ledger,
    )
    _require(
        admitted is not None and reason is None,
        "strict_synthetic_row_derivation_failed",
    )
    return row


def _append_like_source(
    ledger: dict[str, Any], source: dict[str, Any], row: dict[str, Any],
) -> None:
    if source.get("formal_bet") is False:
        ledger[wv.NAMESPACE]["observations"].append(row)
    else:
        ledger["bets"].append(row)


def _simulate_rollover(
    ledger: dict[str, Any],
    frozen: dict[str, Any],
    signature: str,
    anomaly_rows: list[dict[str, Any]],
    projection_time: datetime,
) -> None:
    simulation = copy.deepcopy(ledger)
    simulated_frozen, simulated_signature = _condition_17(simulation)
    _require(simulated_signature == signature, "simulation_identity_drift")
    immutable_history = copy.deepcopy(simulated_frozen.get("historical_evidence"))
    immutable_v1 = copy.deepcopy(simulated_frozen["evidence_versions"][0])
    original_rows = _same_signature_rows(simulation, signature)
    immutable_rows = copy.deepcopy(original_rows)
    times = _safe_synthetic_times(anomaly_rows, frozen, projection_time)
    source = anomaly_rows[0]

    row_19 = _derive_strict_synthetic_row(
        simulation, source, simulated_frozen, signature,
        suffix=19, stage_time=times[0], projection_time=projection_time,
    )
    _append_like_source(simulation, source, row_19)
    wv.recompute_namespace(simulation, SYSTEM)
    _require(
        simulated_frozen.get("active_evidence_version") == 1
        and simulated_frozen.get("pending_rollover_progress", {}).get(
            "eligible_decided"
        ) == 19
        and simulated_frozen["pending_rollover_progress"].get("eligible_hits") == 10
        and simulated_frozen["pending_rollover_progress"].get("display") == "19/20",
        "synthetic_row_19_progress_invalid",
    )
    card_19 = _project_card(simulation, simulated_frozen, signature)
    detail_19 = card_19.get("pending_rollover_evidence")
    _require(
        isinstance(detail_19, dict)
        and detail_19.get("complete") is True
        and len(detail_19.get("rows") or []) == 19
        and sum(row.get("hit") is True for row in detail_19["rows"]) == 10,
        "synthetic_row_19_projection_invalid",
    )

    row_20 = _derive_strict_synthetic_row(
        simulation, source, simulated_frozen, signature,
        suffix=20, stage_time=times[1], projection_time=projection_time,
    )
    _append_like_source(simulation, source, row_20)
    wv.recompute_namespace(simulation, SYSTEM)
    versions = simulated_frozen.get("evidence_versions")
    audit = simulated_frozen.get("rollover_audit")
    _require(
        isinstance(versions, list)
        and len(versions) == 2
        and versions[0] == immutable_v1
        and versions[1].get("version") == 2
        and versions[1].get("prior_version") == 1
        and versions[1].get("prior_evidence_hash") == immutable_v1["evidence_hash"]
        and versions[1].get("batch_decided") == 20
        and versions[1].get("batch_hits") == 10
        and len(versions[1].get("batch_fixture_market_hashes") or []) == 20
        and len(set(versions[1]["batch_fixture_market_hashes"])) == 20
        and versions[1].get("evidence_hash") == wv._version_hash(versions[1])
        and simulated_frozen.get("active_evidence_version") == 2
        and simulated_frozen.get("active_evidence_hash") == versions[1]["evidence_hash"]
        and isinstance(audit, list)
        and len(audit) == 1
        and audit[0] == versions[1]
        and simulated_frozen.get("pending_rollover_progress", {}).get(
            "eligible_decided"
        ) == 0
        and simulated_frozen["pending_rollover_progress"].get("eligible_hits") == 0
        and simulated_frozen["pending_rollover_progress"].get("display") == "0/20",
        "synthetic_row_20_rollover_invalid",
    )
    card_20 = _project_card(simulation, simulated_frozen, signature)
    pending = card_20.get("pending_rollover_evidence")
    merged = card_20.get("last_merged_evidence")
    _require(
        isinstance(pending, dict)
        and pending.get("complete") is True
        and pending.get("rows") == []
        and isinstance(merged, dict)
        and merged.get("complete") is True
        and len(merged.get("rows") or []) == 20
        and sum(row.get("hit") is True for row in merged["rows"]) == 10,
        "synthetic_v2_projection_invalid",
    )
    _require(
        simulated_frozen.get("historical_evidence") == immutable_history
        and simulated_frozen["evidence_versions"][0] == immutable_v1
        and original_rows == immutable_rows,
        "synthetic_simulation_mutated_history",
    )


def run_preflight(
    ledger_path: Path,
    *,
    expected_manifest_hash: str,
    expected_signature: str,
    expected_initial_evidence_hash: str,
) -> dict[str, Any]:
    stat_before = ledger_path.stat()
    _require(stat_before.st_size <= MAX_LEDGER_BYTES, "ledger_size_exceeded")
    raw = ledger_path.read_bytes()
    _require(len(raw) == stat_before.st_size, "ledger_read_size_changed")
    ledger_digest = _sha256(raw)
    ledger = json.loads(raw)
    _require(isinstance(ledger, dict), "ledger_not_object")
    source_before = _canonical_bytes(ledger)
    frozen, signature = _condition_17(ledger)
    manifest_hash, initial_hash = _verify_manifest_and_identity(
        ledger, frozen, signature,
        expected_manifest_hash=expected_manifest_hash,
        expected_signature=expected_signature,
        expected_initial_evidence_hash=expected_initial_evidence_hash,
    )
    now = datetime.now(timezone.utc)
    anomalies = _verify_exact_anomaly_cohort(
        ledger, frozen, signature, projection_time=now,
    )
    _verify_durable_progress(ledger, frozen, signature)
    _simulate_rollover(ledger, frozen, signature, anomalies, now)
    _require(_canonical_bytes(ledger) == source_before, "source_object_mutated")
    _require(
        ledger_path.read_bytes() == raw
        and ledger_path.stat().st_size == stat_before.st_size,
        "snapshot_file_mutated",
    )
    return {
        "schema": "footbreak-condition17-production-preflight-v1",
        "result": "GO",
        "read_only": True,
        "ledger_sha256": ledger_digest,
        "manifest_hash": manifest_hash,
        "condition_signature": signature,
        "initial_evidence_hash": initial_hash,
        "condition_number": CONDITION_NUMBER,
        "compatible_anomaly_rows": EXPECTED_DECIDED,
        "pending_hits": EXPECTED_HITS,
        "durable_progress": "18/20",
        "synthetic_progress": ["19/20", "0/20"],
        "synthetic_rollover_version": 2,
        "synthetic_derivation": "deep-copy-in-memory",
        "production_mutation": False,
        "synthetic_data_output": False,
    }


def _safe_summary(result: str, reason: str | None = None) -> dict[str, Any]:
    summary: dict[str, Any] = {
        "schema": "footbreak-condition17-production-preflight-v1",
        "result": result,
        "read_only": True,
        "production_mutation": False,
        "synthetic_data_output": False,
    }
    if reason:
        summary["reason"] = reason
    return summary


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--ledger", required=True, type=Path)
    parser.add_argument("--expected-manifest-hash", required=True)
    parser.add_argument("--expected-condition-signature", required=True)
    parser.add_argument("--expected-initial-evidence-hash", required=True)
    parser.add_argument("--output", type=Path)
    args = parser.parse_args(argv)
    try:
        for value, length in (
            (args.expected_manifest_hash, 64),
            (args.expected_condition_signature, 24),
            (args.expected_initial_evidence_hash, 64),
        ):
            _require(wv._sha256_hex(value, length=length), "trusted_hash_input_invalid")
        result = run_preflight(
            args.ledger,
            expected_manifest_hash=args.expected_manifest_hash,
            expected_signature=args.expected_condition_signature,
            expected_initial_evidence_hash=args.expected_initial_evidence_hash,
        )
        rc = 0
    except PreflightFailure as exc:
        result = _safe_summary("NO-GO", str(exc))
        rc = 1
    except Exception:
        result = _safe_summary("NO-GO", "preflight_input_or_invariant_failure")
        rc = 1
    payload = json.dumps(result, ensure_ascii=False, sort_keys=True) + "\n"
    if args.output is not None:
        args.output.write_text(payload, encoding="utf-8")
    sys.stdout.write(payload)
    return rc


if __name__ == "__main__":
    raise SystemExit(main())
