"""Proof-gated, read-only-first recovery of Crown Wilson condition #4.

The production ledger is intentionally not discovered by this module.  A
reviewer supplies a captured ledger and the output of
``deploy/audit-crown-condition-replay.py``.  Dry-run is the default.  Apply can
only write a distinct output file and requires an authority document bound to
the exact input bytes and exact dry-run plan.
"""
from __future__ import annotations

import argparse
import base64
import copy
import hashlib
import json
import math
import os
import secrets
import stat
from collections import Counter
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from analysis.wilson_validation import (
    _fixture_market_hash, _rollover_marker, _time, admission_arithmetic,
    recompute_namespace, record_match_observation, validate_formal_row,
)
from analysis.wilson_registry_manifest import build_manifest

SCHEMA = "crown-condition-4-recovery-plan-v1"
AUTHORITY_SCHEMA = "crown-condition-4-recovery-authority-v2"
AUTHORITY_PAYLOAD_SCHEMA = "crown-condition-4-external-approval-v2"
TRUSTED_KEY_PIN_PATH = Path(
    "/etc/footbreak/crown-condition-4-recovery-ed25519-public-key.sha256"
)
EXPECTED = {
    "condition_number": 4,
    "market": "HDC",
    "legacy_v1_hits": 41,
    "legacy_v1_decided": 62,
    "legacy_v2_batch_hits": 11,
    "legacy_v2_batch_decided": 19,
    "legacy_active_hits": 52,
    "legacy_active_decided": 81,
    "replay_observations": 40,
    "replay_decided": 39,
    "replay_hits": 21,
    "first_batch_decided": 20,
    "first_batch_hits": 9,
    "tail_decided": 19,
    "tail_hits": 12,
    "final_observations": 121,
    "final_decided": 120,
    "final_hits": 73,
    "final_pending": 1,
}
PENDING_HOME_ALIASES = {
    "atlanta reserves",
    "阿特兰大竞技后备队",
    "亚特兰大竞技后备队",
}
PENDING_AWAY_ALIASES = {
    "estudiantes de caseros reserves",
    "卡塞罗斯学生队后备队",
}
REPLAY_KEYS = {
    "schema", "read_only", "provider_calls", "writes", "generated_at",
    "condition_number", "condition_signature", "activation_boundary_hkt",
    "definition", "minimum_acceptable_odds_raw", "history_source_rows",
    "learning_result_rows", "excluded_matching_before_activation",
    "v2_duplicate_audit", "summary", "matching_fixtures",
    "missing_formal_fixtures", "unknown_result_fixtures",
}
SUMMARY_KEYS = {
    "matching_fixture_count", "wilson_price_pass_fixture_count",
    "low_price_observation_fixture_count", "recorded_expected_fixture_count",
    "missing_expected_record_fixture_count", "unknown_result_fixture_count",
    "recorded_unknown_result_fixture_count",
}
DUPLICATE_AUDIT_KEYS = {
    "stored_v2_fixture_identities_available", "stored_v2_cumulative_decided",
    "stored_v2_cumulative_hits", "reconstructed_pre_boundary_fixture_count",
    "reconstructed_pre_boundary_decided", "reconstructed_pre_boundary_hits",
    "reconstructed_pre_boundary_duplicate_fixture_ids",
    "post_boundary_duplicate_fixture_ids", "cross_boundary_duplicate_fixture_ids",
}
CANDIDATE_KEYS = {
    "match_id", "league", "home", "away", "kickoff_hkt", "t5_recorded_at",
    "stage_path", "role_path", "selected_line_path", "market",
    "selected_side", "selected_line", "selected_role", "t5_odds",
    "passes_wilson_price", "expected_record_type", "formal_row_count",
    "formal_row_ids", "formal_statuses", "matching_record_count",
    "missing_expected_record", "result_known", "result_source",
    "result_status", "score", "hdc_grade", "replay_candidate_hash",
}
GRADE_KEYS = {"grade_status", "hit", "result"}
RECOVERY_PROOF_KEYS = {
    "schema_version", "migration", "replay_sha256", "candidate_sha256",
    "authority_payload_sha256", "admission_proved_without_result",
    "legacy_19_identities_unavailable", "deletion_performed",
    "settlement_recomputed_from_score",
}
RECOVERED_ROW_COMMON_KEYS = {
    "away", "bet_status", "code", "condition", "condition_number", "created_at",
    "evidence_hash", "evidence_version", "first_native_pre_kickoff_t5",
    "formal_bet", "frozen_condition_definition", "frozen_condition_signature",
    "frozen_historical_evidence", "history", "hkjc_match_id", "home", "kickoff",
    "league", "line", "market", "market_label", "match_id",
    "native_snapshot_binding", "native_stage_at", "no_bet_reason",
    "observation_id", "odds", "portfolio", "quarter_line_settlement",
    "recovered_missing_observation", "rollover_provenance", "selected_line",
    "selected_role", "selected_side", "side", "simulation_only", "stage",
    "status", "strategy", "wilson_admission",
}
RECOVERED_SETTLED_ROW_KEYS = RECOVERED_ROW_COMMON_KEYS | {
    "result", "settled_at", "settlement_source",
}
RECOVERED_PENDING_ROW_KEYS = RECOVERED_ROW_COMMON_KEYS | {
    "pending_reason", "postponement_proof",
}
AUTHORITY_KEYS = {"schema", "payload", "public_key_base64", "signature_base64"}
AUTHORITY_PAYLOAD_KEYS = {
    "schema", "context", "nonce", "expires_at", "ledger_sha256",
    "replay_sha256", "ordered_candidate_hashes", "pending_proof", "expected",
    "proposed_ledger_sha256", "deletions_authorized", "apply_authorized",
}
AUTHORITY_CONTEXT = (
    "footbreak-live:crown:condition-4:missing-low-odds-observation-recovery:20260828"
)
PENDING_PROOF_KEYS = {
    "match_id", "market", "league", "home", "away", "kickoff_hkt",
    "result_status", "score", "reason", "adverse_weather", "source",
    "source_sha256", "evidence_sha256",
}


class RecoveryBlocked(ValueError):
    """The supplied immutable evidence is insufficient or conflicting."""


class PublicationFailure(RecoveryBlocked):
    """Transaction failed; original error and every cleanup failure are retained."""

    def __init__(
        self, original: BaseException, cleanup_failures: list[str],
        residue: list[str],
    ) -> None:
        self.original_error = f"{type(original).__name__}:{original}"
        self.cleanup_failures = cleanup_failures
        self.residue = residue
        status = (
            "transaction_failed_cleanup_incomplete"
            if cleanup_failures or residue else "transaction_failed_rolled_back"
        )
        super().__init__(
            f"{status};original={self.original_error};"
            f"cleanup={cleanup_failures};residue={residue}"
        )


@dataclass
class _TrustedDirectory:
    path: Path
    descriptor: int
    identity: tuple[int, int]


@dataclass
class _OutputTarget:
    path: Path
    parent: _TrustedDirectory


@dataclass
class _StagedOutput:
    target: _OutputTarget
    stage_name: str
    descriptor: int
    identity: tuple[int, int]
    raw: bytes
    final_descriptor: int | None = None
    final_published: bool = False


def canonical_hash(value: Any) -> str:
    return hashlib.sha256(json.dumps(
        value, ensure_ascii=False, sort_keys=True, separators=(",", ":"),
    ).encode("utf-8")).hexdigest()


def bytes_hash(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def _number(value: Any) -> float | None:
    try:
        result = float(value)
    except (TypeError, ValueError):
        return None
    return result if math.isfinite(result) else None


def _condition(ledger: dict[str, Any]) -> tuple[str, dict[str, Any], dict[str, Any]]:
    ns = ledger.get("wilson_validation")
    if not isinstance(ns, dict) or ns.get("system") != "crown":
        raise RecoveryBlocked("missing_or_wrong_crown_wilson_namespace")
    matches = [
        (str(signature), row)
        for signature, row in (ns.get("conditions") or {}).items()
        if isinstance(row, dict) and row.get("condition_number") == 4
    ]
    if len(matches) != 1:
        raise RecoveryBlocked("condition_4_not_unique")
    signature, frozen = matches[0]
    definition = frozen.get("definition")
    versions = frozen.get("evidence_versions")
    active = frozen.get("active_evidence")
    if (
        not isinstance(definition, dict)
        or definition.get("market") != "HDC"
        or not isinstance(versions, list)
        or len(versions) not in {2, 3}
        or not isinstance(active, dict)
    ):
        raise RecoveryBlocked("condition_4_frozen_shape_mismatch")
    v1, v2 = versions[:2]
    if not all(isinstance(item, dict) for item in (v1, v2)):
        raise RecoveryBlocked("condition_4_evidence_versions_malformed")
    expected_counts = (
        (v1.get("cumulative_hits"), v1.get("cumulative_decided")),
        (v2.get("batch_hits"), v2.get("batch_decided")),
        (v2.get("cumulative_hits"), v2.get("cumulative_decided")),
    )
    if expected_counts != ((41, 62), (11, 19), (52, 81)):
        raise RecoveryBlocked("condition_4_legacy_counts_mismatch")
    recovered_v3 = versions[2] if len(versions) == 3 else None
    active_is_expected = (
        active.get("version") == 2
        and (active.get("cumulative_hits"), active.get("cumulative_decided")) == (52, 81)
    ) or (
        isinstance(recovered_v3, dict)
        and active.get("version") == 3
        and active.get("evidence_hash") == recovered_v3.get("evidence_hash")
        and (active.get("cumulative_hits"), active.get("cumulative_decided")) == (61, 101)
        and (recovered_v3.get("batch_hits"), recovered_v3.get("batch_decided")) == (9, 20)
    )
    if (
        v1.get("version") != 1
        or v2.get("version") != 2
        or not active_is_expected
        or v2.get("initial_migration_full_cohort") is not True
        or v2.get("batch_fixture_market_ids_unavailable_from_legacy_aggregate") is not True
        or v2.get("batch_fixture_market_hashes") != []
    ):
        raise RecoveryBlocked("legacy_19_identity_unavailability_not_proven")
    if (
        frozen.get("signature") != signature
        or frozen.get("active_evidence_hash") != active.get("evidence_hash")
    ):
        raise RecoveryBlocked("condition_4_active_evidence_binding_mismatch")
    return signature, frozen, ns


def _pending_fixture(row: dict[str, Any]) -> bool:
    home = " ".join(str(row.get("home") or "").lower().split())
    away = " ".join(str(row.get("away") or "").lower().split())
    return home in PENDING_HOME_ALIASES and away in PENDING_AWAY_ALIASES


def _candidate_identity(row: dict[str, Any]) -> tuple[str, str]:
    return str(row.get("match_id") or "").strip(), str(row.get("market") or "").upper()


def _score_hit(row: dict[str, Any]) -> bool:
    score = str(row.get("score") or "")
    parts = score.split("-")
    if len(parts) != 2:
        raise RecoveryBlocked("settled_score_invalid")
    try:
        home_score, away_score = (int(value.strip()) for value in parts)
    except ValueError as exc:
        raise RecoveryBlocked("settled_score_invalid") from exc
    if home_score < 0 or away_score < 0:
        raise RecoveryBlocked("settled_score_invalid")
    line = _number(row.get("selected_line"))
    side = str(row.get("selected_side") or "").upper()
    if line is None or side not in {"H", "A"}:
        raise RecoveryBlocked("settled_selection_invalid")
    margin = (
        home_score - away_score if side == "H" else away_score - home_score
    ) + line
    if abs(margin) <= 1e-12:
        raise RecoveryBlocked("settled_score_is_nonbinary_push")
    return margin > 0


def _exact_existing(
    ledger: dict[str, Any], candidate: dict[str, Any], signature: str,
    replay_sha256: str, authority_payload_sha256: str,
    pending_proof: dict[str, Any], settled_at: str,
) -> tuple[str, dict[str, Any] | None]:
    """Return new, skip, or conflict for the exact fixture+market identity."""
    fixture, market = _candidate_identity(candidate)
    ns = ledger["wilson_validation"]
    rows = [
        item for collection in (ledger.get("bets") or [], ns.get("observations") or [])
        for item in collection if isinstance(item, dict)
        and str(item.get("match_id") or "") == fixture
        and str(item.get("market") or item.get("code") or "").upper() == market
    ]
    if not rows:
        return "new", None
    if len(rows) != 1:
        return "conflict", None
    row = rows[0]
    frozen = (ns.get("conditions") or {}).get(signature)
    expected_keys = (
        RECOVERED_SETTLED_ROW_KEYS
        if isinstance(candidate.get("hdc_grade"), dict)
        else RECOVERED_PENDING_ROW_KEYS
    )
    exact = isinstance(frozen, dict) and set(row) == expected_keys
    if exact:
        reconstruction = copy.deepcopy(ledger)
        reconstruction_ns = reconstruction["wilson_validation"]
        reconstruction_ns["observations"] = [
            item for item in reconstruction_ns.get("observations") or []
            if item is not row
            and not (
                isinstance(item, dict)
                and str(item.get("match_id") or "") == fixture
                and str(item.get("market") or item.get("code") or "").upper()
                == market
            )
        ]
        expected = _draft_row(
            reconstruction, candidate, signature,
            reconstruction_ns["conditions"][signature], replay_sha256,
            settled_at, authority_payload_sha256, pending_proof,
        )
        # Every recovery-authored field is deterministically reconstructed from
        # the externally signed replay/authority. No digest stored in the
        # mutable row participates in this equality.
        exact = row == expected
    if exact:
        admitted, _reason = validate_formal_row(
            row, system="crown", signature=signature, frozen=frozen,
            projection_time=datetime.now(timezone.utc),
            require_settled=row.get("status") == "SETTLED", ledger=ledger,
        )
        exact = admitted is not None
    return ("skip", row) if exact else ("conflict", row)


def _validate_replay(
    replay: dict[str, Any], signature: str, frozen: dict[str, Any],
) -> list[dict[str, Any]]:
    summary = replay.get("summary")
    audit = replay.get("v2_duplicate_audit")
    rows = replay.get("matching_fixtures")
    if (
        set(replay) != REPLAY_KEYS
        or
        replay.get("schema") != "crown_condition_read_only_replay_v1"
        or replay.get("read_only") is not True
        or replay.get("writes") != 0
        or replay.get("provider_calls") != 0
        or replay.get("condition_number") != 4
        or replay.get("condition_signature") != signature
        or replay.get("definition") != frozen.get("definition")
        or not isinstance(summary, dict)
        or not isinstance(audit, dict)
        or not isinstance(rows, list)
        or set(summary) != SUMMARY_KEYS
        or set(audit) != DUPLICATE_AUDIT_KEYS
    ):
        raise RecoveryBlocked("replay_header_or_condition_binding_invalid")
    if (
        summary.get("matching_fixture_count") != 40
        or summary.get("wilson_price_pass_fixture_count") != 0
        or summary.get("low_price_observation_fixture_count") != 40
        or summary.get("missing_expected_record_fixture_count") != 40
        or summary.get("unknown_result_fixture_count") != 1
        or audit.get("stored_v2_fixture_identities_available") is not False
        or audit.get("stored_v2_cumulative_hits") != 52
        or audit.get("stored_v2_cumulative_decided") != 81
        or audit.get("reconstructed_pre_boundary_fixture_count") != 62
        or audit.get("reconstructed_pre_boundary_hits") != 41
        or audit.get("reconstructed_pre_boundary_decided") != 62
        or audit.get("reconstructed_pre_boundary_duplicate_fixture_ids") != []
        or audit.get("post_boundary_duplicate_fixture_ids") != []
        or audit.get("cross_boundary_duplicate_fixture_ids") != []
    ):
        raise RecoveryBlocked("replay_counts_or_duplicate_audit_mismatch")
    minimum = _number(replay.get("minimum_acceptable_odds_raw"))
    identities: set[tuple[str, str]] = set()
    pending = 0
    for row in rows:
        if not isinstance(row, dict) or set(row) != CANDIDATE_KEYS:
            raise RecoveryBlocked("replay_candidate_not_object")
        identity = _candidate_identity(row)
        odds = _number(row.get("t5_odds"))
        line = _number(row.get("selected_line"))
        stage_at = _time(row.get("t5_recorded_at"))
        kickoff = _time(row.get("kickoff_hkt"))
        grade = row.get("hdc_grade")
        proof_payload = {
            key: row.get(key) for key in (
                "match_id", "league", "home", "away", "market",
                "selected_side", "selected_line",
                "selected_role", "t5_odds", "t5_recorded_at", "kickoff_hkt",
                "stage_path", "role_path", "selected_line_path", "score", "hdc_grade",
                "result_known", "result_source", "result_status",
            )
        }
        if (
            not identity[0] or identity[1] != "HDC" or identity in identities
            or str(row.get("selected_side") or "").upper() not in {"H", "A"}
            or line is None or odds is None or odds <= 1 or minimum is None
            or odds >= minimum or row.get("passes_wilson_price") is not False
            or row.get("expected_record_type") != "observation"
            or row.get("missing_expected_record") is not True
            or row.get("matching_record_count") != 0
            or stage_at is None or kickoff is None or stage_at >= kickoff
            or row.get("stage_path", [])[-1:] != ["T-5"]
            or row.get("replay_candidate_hash") != canonical_hash(proof_payload)
        ):
            raise RecoveryBlocked("replay_candidate_immutable_proof_invalid")
        identities.add(identity)
        if row.get("result_known") is False:
            if (
                not _pending_fixture(row) or grade is not None
                or row.get("score") is not None
                or row.get("result_status") != "POSTPONED"
                or row.get("result_source") is not None
            ):
                raise RecoveryBlocked("unknown_result_is_not_exact_postponed_fixture")
            pending += 1
        elif (
            row.get("result_known") is not True
            or not isinstance(grade, dict)
            or grade.get("grade_status") != "GRADED"
            or grade.get("hit") not in (True, False)
            or set(grade) != GRADE_KEYS
            or grade.get("result") not in {"Won", "Lost"}
            or row.get("result_status") != "SETTLED"
            or row.get("result_source") not in {
                "prediction_history", "learning_db",
                "operator_verified_public_result",
            }
            or row.get("score") is None
            or grade.get("hit") is not _score_hit(row)
            or grade.get("result") != ("Won" if _score_hit(row) else "Lost")
        ):
            raise RecoveryBlocked("settled_candidate_grade_invalid")
    if pending != 1:
        raise RecoveryBlocked("postponed_fixture_count_mismatch")
    if (
        replay.get("missing_formal_fixtures") != rows
        or replay.get("unknown_result_fixtures")
        != [row for row in rows if row.get("result_known") is False]
    ):
        raise RecoveryBlocked("replay_row_projection_mismatch")
    return sorted(rows, key=lambda row: (
        _time(row["t5_recorded_at"]), _candidate_identity(row)[0],
    ))


def _draft_row(
    ledger: dict[str, Any], candidate: dict[str, Any], signature: str,
    frozen: dict[str, Any], replay_hash: str, settled_at: str,
    authority_payload_sha256: str, pending_proof: dict[str, Any],
) -> dict[str, Any]:
    # Every missed row occurred while immutable v2 was active.  A rerun after
    # the first 20 rows formed v3 must still compare exact existing rows against
    # their original v2 admission, never reinterpret them under v3.
    active = frozen["evidence_versions"][1]
    history = copy.deepcopy(frozen["historical_evidence"])
    history.update({
        "hits": active["cumulative_hits"],
        "decided": active["cumulative_decided"],
        "evidence_version": active["version"],
        "evidence_hash": active["evidence_hash"],
    })
    arithmetic = admission_arithmetic(
        active["cumulative_hits"], active["cumulative_decided"],
        candidate["t5_odds"],
    )
    if not isinstance(arithmetic, dict) or arithmetic.get("passes") is not False:
        raise RecoveryBlocked("candidate_is_not_low_odds_under_active_v2")
    admission = {
        "signature": signature,
        "definition": copy.deepcopy(frozen["definition"]),
        "history": history,
        "arithmetic": arithmetic,
        "evidence_version": active["version"],
        "evidence_hash": active["evidence_hash"],
        "stage_at": candidate["t5_recorded_at"],
        "native_snapshot_binding": None,
    }
    watch = {
        key: candidate.get(key) for key in (
            "match_id", "league", "home", "away", "kickoff_hkt",
        )
    }
    watch["kickoff"] = candidate["kickoff_hkt"]
    row = record_match_observation(
        ledger, "crown", watch, "HDC",
        {
            "market": "HDC", "side": candidate["selected_side"],
            "line": candidate["selected_line"], "odds": candidate["t5_odds"],
        },
        admission, now=candidate["t5_recorded_at"], market_label="讓球",
        selected_role=candidate.get("selected_role"),
        selected_line=candidate["selected_line"],
    )
    if row is None:
        raise RecoveryBlocked("formal_observation_constructor_rejected_candidate")
    row["rollover_provenance"] = _rollover_marker(
        "crown", str(candidate["match_id"]), "HDC", signature,
        str(candidate["t5_recorded_at"]), active,
    )
    row["recovered_missing_observation"] = {
        "schema_version": 2,
        "migration": "crown-condition-4-missed-observation-v2",
        "replay_sha256": replay_hash,
        "candidate_sha256": candidate["replay_candidate_hash"],
        "authority_payload_sha256": authority_payload_sha256,
        "admission_proved_without_result": True,
        "legacy_19_identities_unavailable": True,
        "deletion_performed": False,
        "settlement_recomputed_from_score": isinstance(
            candidate.get("hdc_grade"), dict,
        ),
    }
    grade = candidate.get("hdc_grade")
    if isinstance(grade, dict):
        hit = _score_hit(candidate)
        row.update({
            "status": "SETTLED",
            "result": "Won" if hit else "Lost",
            "settled_at": settled_at,
            "settlement_source": "condition_4_score_line_recomputed_v2",
        })
    else:
        row.update({
            "status": "PENDING",
            "pending_reason": "externally_proved_adverse_weather_postponement",
            "postponement_proof": copy.deepcopy(pending_proof),
        })
    expected_keys = (
        RECOVERED_SETTLED_ROW_KEYS
        if isinstance(grade, dict) else RECOVERED_PENDING_ROW_KEYS
    )
    if set(row) != expected_keys:
        raise RecoveryBlocked("deterministic_recovered_row_schema_drift")
    return row


def _payload_context_hash(payload: dict[str, Any]) -> str:
    return canonical_hash({
        key: payload[key] for key in (
            "schema", "context", "ordered_candidate_hashes", "pending_proof",
            "expected", "deletions_authorized",
        )
    })


def _validate_authority_payload(
    payload: Any, *, ledger_sha256: str, replay_sha256: str,
    require_apply: bool,
) -> dict[str, Any]:
    if (
        not isinstance(payload, dict)
        or set(payload) != AUTHORITY_PAYLOAD_KEYS
        or payload.get("schema") != AUTHORITY_PAYLOAD_SCHEMA
        or payload.get("context") != AUTHORITY_CONTEXT
        or not isinstance(payload.get("nonce"), str)
        or len(payload["nonce"]) < 32
        or _time(payload.get("expires_at")) is None
        or _time(payload["expires_at"]) <= datetime.now(timezone.utc)
        or payload.get("ledger_sha256") != ledger_sha256
        or payload.get("replay_sha256") != replay_sha256
        or payload.get("expected") != EXPECTED
        or payload.get("deletions_authorized") is not False
        or payload.get("apply_authorized") is not require_apply
        or not isinstance(payload.get("ordered_candidate_hashes"), list)
        or len(payload["ordered_candidate_hashes"]) != 40
        or len(set(payload["ordered_candidate_hashes"])) != 40
        or any(
            not isinstance(value, str) or len(value) != 64
            for value in payload["ordered_candidate_hashes"]
        )
        or not isinstance(payload.get("pending_proof"), dict)
        or set(payload["pending_proof"]) != PENDING_PROOF_KEYS
    ):
        raise RecoveryBlocked("external_authority_payload_invalid")
    proof = payload["pending_proof"]
    if (
        not str(proof.get("match_id") or "")
        or proof.get("market") != "HDC"
        or proof.get("home") != "Atlanta Reserves"
        or proof.get("away") != "Estudiantes de Caseros Reserves"
        or not str(proof.get("league") or "")
        or _time(proof.get("kickoff_hkt")) is None
        or proof.get("result_status") != "POSTPONED"
        or proof.get("score") is not None
        or proof.get("reason") != "adverse_weather"
        or proof.get("adverse_weather") is not True
        or not str(proof.get("source") or "")
        or not isinstance(proof.get("source_sha256"), str)
        or len(proof["source_sha256"]) != 64
        or not isinstance(proof.get("evidence_sha256"), str)
        or len(proof["evidence_sha256"]) != 64
    ):
        raise RecoveryBlocked("external_pending_proof_invalid")
    proposed_hash = payload.get("proposed_ledger_sha256")
    if proposed_hash is not None and (
        not isinstance(proposed_hash, str) or len(proposed_hash) != 64
    ):
        raise RecoveryBlocked("external_proposed_ledger_hash_invalid")
    return payload


def verify_external_authority(
    authority: Any, *, trusted_public_key_sha256: str,
    ledger_sha256: str, replay_sha256: str, require_apply: bool,
) -> dict[str, Any]:
    if not isinstance(authority, dict) or set(authority) != AUTHORITY_KEYS:
        raise RecoveryBlocked("external_authority_schema_invalid")
    if authority.get("schema") != AUTHORITY_SCHEMA:
        raise RecoveryBlocked("external_authority_schema_invalid")
    try:
        key_bytes = base64.b64decode(
            authority["public_key_base64"], validate=True,
        )
        signature = base64.b64decode(
            authority["signature_base64"], validate=True,
        )
    except (KeyError, ValueError) as exc:
        raise RecoveryBlocked("external_authority_encoding_invalid") from exc
    if (
        len(key_bytes) != 32
        or bytes_hash(key_bytes) != trusted_public_key_sha256
        or len(signature) != 64
    ):
        raise RecoveryBlocked("external_authority_untrusted_key")
    payload = _validate_authority_payload(
        authority.get("payload"), ledger_sha256=ledger_sha256,
        replay_sha256=replay_sha256, require_apply=require_apply,
    )
    try:
        from cryptography.hazmat.primitives.asymmetric.ed25519 import (
            Ed25519PublicKey,
        )
        Ed25519PublicKey.from_public_bytes(key_bytes).verify(
            signature,
            json.dumps(
                payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"),
            ).encode("utf-8"),
        )
    except Exception as exc:
        raise RecoveryBlocked("external_authority_signature_invalid") from exc
    return payload


def _plan_with_payload(
    ledger: dict[str, Any], replay: dict[str, Any], *,
    authority_payload: dict[str, Any],
    ledger_sha256: str | None = None, replay_sha256: str | None = None,
    enforce_proposed_hash: bool = True,
) -> tuple[dict[str, Any], dict[str, Any]]:
    """Return a report and proposed ledger without mutating either input."""
    before = canonical_hash(ledger), canonical_hash(replay)
    ledger_hash = ledger_sha256 or canonical_hash(ledger)
    replay_hash = replay_sha256 or canonical_hash(replay)
    input_manifest = build_manifest(ledger, "crown")
    if not input_manifest.get("valid"):
        raise RecoveryBlocked("input_ledger_strict_manifest_invalid")
    _validate_authority_payload(
        authority_payload, ledger_sha256=ledger_hash,
        replay_sha256=replay_hash,
        require_apply=bool(authority_payload.get("apply_authorized")),
    )
    proposed = copy.deepcopy(ledger)
    signature, frozen, _ns = _condition(proposed)
    rows = _validate_replay(replay, signature, frozen)
    by_hash = {row["replay_candidate_hash"]: row for row in rows}
    ordered = authority_payload["ordered_candidate_hashes"]
    if set(by_hash) != set(ordered):
        raise RecoveryBlocked("externally_approved_order_identity_mismatch")
    rows = [by_hash[value] for value in ordered]
    pending_candidate = next(
        (row for row in rows if row.get("result_known") is False), None,
    )
    pending_proof = authority_payload["pending_proof"]
    if not isinstance(pending_candidate, dict) or any(
        pending_proof.get(key) != pending_candidate.get(key)
        for key in (
            "match_id", "market", "league", "home", "away", "kickoff_hkt",
            "result_status", "score",
        )
    ):
        raise RecoveryBlocked("externally_approved_pending_identity_mismatch")
    authority_context_hash = _payload_context_hash(authority_payload)
    settled_at = str(replay.get("generated_at") or "")
    if _time(settled_at) is None:
        raise RecoveryBlocked("replay_generated_at_invalid")
    actions: list[dict[str, Any]] = []
    counts = Counter()
    for candidate in rows:
        disposition, _existing = _exact_existing(
            proposed, candidate, signature, replay_hash, authority_context_hash,
            pending_proof, settled_at,
        )
        if disposition == "conflict":
            raise RecoveryBlocked(
                f"exact_fixture_market_conflict:{_candidate_identity(candidate)[0]}:HDC"
            )
        if disposition == "skip":
            counts["skipped_exact"] += 1
            actions.append({
                "match_id": candidate["match_id"], "market": "HDC",
                "action": "SKIP_EXACT_EXISTING", "deleted": False,
            })
            continue
        row = _draft_row(
            proposed, candidate, signature, frozen, replay_hash, settled_at,
            authority_context_hash, pending_proof,
        )
        counts["added"] += 1
        counts["pending" if row["status"] == "PENDING" else "settled"] += 1
        counts["hits"] += row.get("result") == "Won"
        actions.append({
            "match_id": candidate["match_id"], "market": "HDC",
            "fixture_market_hash": _fixture_market_hash(
                "crown", candidate["match_id"], "HDC",
            ),
            "action": "ADD_PENDING" if row["status"] == "PENDING" else "ADD_SETTLED",
            "result": row.get("result"), "deleted": False,
        })
    if counts["added"] not in {0, 40}:
        # Mixed old/new cohorts are unsafe: a partial prior application must be
        # reviewed as one exact-idempotent state, never silently completed.
        raise RecoveryBlocked("partial_existing_recovery_requires_review")
    recompute_namespace(proposed, "crown", now=settled_at)
    frozen_after = proposed["wilson_validation"]["conditions"][signature]
    active = frozen_after["active_evidence"]
    progress = frozen_after.get("pending_rollover_progress")
    observations = [
        row for row in proposed["wilson_validation"].get("observations") or []
        if isinstance(row, dict)
        and row.get("frozen_condition_signature") == signature
    ]
    cohort = {
        "observations": 81 + len(observations),
        "decided": 81 + sum(
            row.get("status") == "SETTLED"
            and row.get("result") in {"Won", "Lost", "Half Won", "Half Lost"}
            for row in observations
        ),
        "hits": 52 + sum(
            row.get("status") == "SETTLED"
            and row.get("result") in {"Won", "Half Won"}
            for row in observations
        ),
        "pending": sum(row.get("status") == "PENDING" for row in observations),
    }
    expected_final = {
        "observations": 121, "decided": 120, "hits": 73, "pending": 1,
    }
    if (
        cohort != expected_final
        or (active.get("cumulative_hits"), active.get("cumulative_decided")) != (61, 101)
        or not isinstance(progress, dict)
        or (progress.get("eligible_hits"), progress.get("eligible_decided")) != (12, 19)
        or len(frozen_after.get("evidence_versions") or []) != 3
        or frozen_after["evidence_versions"][-1].get("batch_hits") != 9
        or frozen_after["evidence_versions"][-1].get("batch_decided") != 20
    ):
        raise RecoveryBlocked("post_recovery_rollover_assertion_failed")
    proposed_manifest = build_manifest(proposed, "crown")
    if not proposed_manifest.get("valid"):
        raise RecoveryBlocked("proposed_ledger_strict_manifest_invalid")
    if (canonical_hash(ledger), canonical_hash(replay)) != before:
        raise RuntimeError("dry_run_mutated_input")
    report = {
        "schema": SCHEMA,
        "mode": "dry-run",
        "read_only_input": True,
        "production_touched": False,
        "condition_number": 4,
        "condition_signature": signature,
        "input": {
            "ledger_sha256": ledger_hash,
            "replay_sha256": replay_hash,
        },
        "external_authority": {
            "payload_context_sha256": authority_context_hash,
            "ordered_candidate_hashes": copy.deepcopy(ordered),
            "pending_evidence_sha256": pending_proof["evidence_sha256"],
        },
        "legacy": {
            "v1": {"hits": 41, "decided": 62},
            "v2_batch": {"hits": 11, "decided": 19},
            "active_v2": {"hits": 52, "decided": 81},
            "batch_identities_available": False,
            "delete_or_guess_legacy_rows": False,
        },
        "changes": dict(counts),
        "final_cohort": cohort,
        "rollover": {
            "first_20": {"hits": 9, "decided": 20, "sealed": True},
            "active_cumulative": {
                "hits": active.get("cumulative_hits"),
                "decided": active.get("cumulative_decided"),
            },
            "tail": {
                "hits": progress.get("eligible_hits") if isinstance(progress, dict) else None,
                "decided": progress.get("eligible_decided") if isinstance(progress, dict) else None,
                "pending_fixtures": cohort["pending"],
                "sealed": False,
            },
        },
        "safety": {
            "delete_count": 0,
            "exact_fixture_market_only": True,
            "legacy_19_preserved": True,
            "partial_apply_allowed": False,
        },
        "actions": actions,
    }
    report["proposed_ledger_sha256"] = canonical_hash(proposed)
    if (
        enforce_proposed_hash
        and authority_payload.get("proposed_ledger_sha256")
        != report["proposed_ledger_sha256"]
    ):
        raise RecoveryBlocked("externally_approved_proposed_ledger_hash_mismatch")
    report["plan_sha256"] = canonical_hash(report)
    return report, proposed


def plan_recovery(
    ledger: dict[str, Any], replay: dict[str, Any], *, authority: dict[str, Any],
    trusted_public_key_sha256: str, ledger_sha256: str | None = None,
    replay_sha256: str | None = None, require_apply: bool = False,
) -> tuple[dict[str, Any], dict[str, Any]]:
    ledger_hash = ledger_sha256 or canonical_hash(ledger)
    replay_hash = replay_sha256 or canonical_hash(replay)
    payload = verify_external_authority(
        authority, trusted_public_key_sha256=trusted_public_key_sha256,
        ledger_sha256=ledger_hash, replay_sha256=replay_hash,
        require_apply=require_apply,
    )
    return _plan_with_payload(
        ledger, replay, authority_payload=payload, ledger_sha256=ledger_hash,
        replay_sha256=replay_hash, enforce_proposed_hash=True,
    )


def _strict_json_bytes(raw: bytes, label: str) -> Any:
    def pairs(items: list[tuple[str, Any]]) -> dict[str, Any]:
        output: dict[str, Any] = {}
        for key, value in items:
            if key in output:
                raise RecoveryBlocked(f"{label}_duplicate_json_key:{key}")
            output[key] = value
        return output
    try:
        return json.loads(raw.decode("utf-8"), object_pairs_hook=pairs)
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise RecoveryBlocked(f"{label}_invalid_json") from exc


def _reject_symlink_components(path: Path) -> None:
    absolute = path.absolute()
    current = Path(absolute.anchor)
    for part in absolute.parts[1:]:
        current /= part
        if current.is_symlink():
            raise RecoveryBlocked(f"symlink_path_component:{path}")


def _open_input(path: Path, label: str) -> tuple[int, bytes, os.stat_result]:
    _reject_symlink_components(path)
    fd = os.open(path, os.O_RDONLY | os.O_CLOEXEC | os.O_NOFOLLOW)
    try:
        stat = os.fstat(fd)
        if not __import__("stat").S_ISREG(stat.st_mode):
            raise RecoveryBlocked(f"{label}_not_regular_file")
        raw = b""
        while True:
            chunk = os.read(fd, 1024 * 1024)
            if not chunk:
                break
            raw += chunk
        os.lseek(fd, 0, os.SEEK_SET)
        return fd, raw, stat
    except Exception:
        os.close(fd)
        raise


def _production_like_output(path: Path) -> bool:
    lowered = str(path.absolute()).lower()
    name = path.name.lower()
    return (
        lowered.startswith(("/var/lib/", "/opt/footbreak/", "/etc/"))
        or name in {"ledger.json", "sim_ledger.json", "prediction_history.json"}
        or ("production" in name and "ledger" in name)
    )


def _preflight_output(
    path: Path, input_paths: list[Path], input_stats: list[os.stat_result],
) -> Path:
    if ".." in path.parts:
        raise RecoveryBlocked(f"output_parent_alias_rejected:{path}")
    if _production_like_output(path):
        raise RecoveryBlocked(f"production_like_output_rejected:{path}")
    _reject_symlink_components(path.parent)
    if not path.parent.is_dir():
        raise RecoveryBlocked(f"output_parent_missing:{path.parent}")
    try:
        os.lstat(path)
    except FileNotFoundError:
        pass
    else:
        raise RecoveryBlocked(f"output_must_be_new:{path}")
    resolved_parent = path.parent.resolve(strict=True)
    candidate = resolved_parent / path.name
    for input_path, input_stat in zip(input_paths, input_stats):
        if candidate == input_path.resolve(strict=True):
            raise RecoveryBlocked(f"output_aliases_input:{path}")
        try:
            stat = os.stat(candidate, follow_symlinks=False)
        except FileNotFoundError:
            continue
        if (stat.st_dev, stat.st_ino) == (input_stat.st_dev, input_stat.st_ino):
            raise RecoveryBlocked(f"output_hardlinks_input:{path}")
    return candidate


def _open_trusted_directory(path: Path) -> _TrustedDirectory:
    descriptor = os.open(
        path, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC,
    )
    try:
        opened = os.fstat(descriptor)
        by_path = os.stat(path, follow_symlinks=False)
        if (
            not stat.S_ISDIR(opened.st_mode)
            or (opened.st_dev, opened.st_ino) != (by_path.st_dev, by_path.st_ino)
            or opened.st_uid not in {0, os.geteuid()}
            or opened.st_mode & 0o022
        ):
            raise RecoveryBlocked(f"unsafe_output_parent:{path}")
        return _TrustedDirectory(
            path=path, descriptor=descriptor,
            identity=(opened.st_dev, opened.st_ino),
        )
    except Exception:
        os.close(descriptor)
        raise


def _create_output(target: Path | _OutputTarget) -> int:
    """Create through a retained trusted dirfd in transactional use."""
    if isinstance(target, _OutputTarget):
        return os.open(
            target.path.name,
            os.O_RDWR | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW | os.O_CLOEXEC,
            0o600, dir_fd=target.parent.descriptor,
        )
    parent = _open_trusted_directory(target.parent)
    try:
        return os.open(
            target.name,
            os.O_RDWR | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW | os.O_CLOEXEC,
            0o600, dir_fd=parent.descriptor,
        )
    finally:
        os.close(parent.descriptor)


def _write_retained(fd: int, payload: Any) -> None:
    raw = _payload_bytes(payload)
    offset = 0
    while offset < len(raw):
        offset += os.write(fd, raw[offset:])
    os.fsync(fd)


def _payload_bytes(payload: Any) -> bytes:
    return (json.dumps(
        payload, ensure_ascii=False, indent=2, sort_keys=True,
    ) + "\n").encode("utf-8")


def _verify_stage(stage: _StagedOutput, *, expected_nlink: int) -> None:
    opened = os.fstat(stage.descriptor)
    by_name = os.stat(
        stage.stage_name, dir_fd=stage.target.parent.descriptor,
        follow_symlinks=False,
    )
    if (
        not stat.S_ISREG(opened.st_mode)
        or stat.S_IMODE(opened.st_mode) != 0o600
        or opened.st_uid != os.geteuid()
        or opened.st_nlink != expected_nlink
        or by_name.st_nlink != expected_nlink
        or (opened.st_dev, opened.st_ino) != stage.identity
        or (by_name.st_dev, by_name.st_ino) != stage.identity
        or _retained_bytes(stage.descriptor) != stage.raw
    ):
        raise RecoveryBlocked("staged_output_identity_or_readback_mismatch")


def _verify_final(stage: _StagedOutput, *, expected_nlink: int) -> None:
    descriptor = os.open(
        stage.target.path.name,
        os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC,
        dir_fd=stage.target.parent.descriptor,
    )
    try:
        opened = os.fstat(descriptor)
        by_name = os.stat(
            stage.target.path.name, dir_fd=stage.target.parent.descriptor,
            follow_symlinks=False,
        )
        staged = os.fstat(stage.descriptor)
        os.lseek(descriptor, 0, os.SEEK_SET)
        chunks = []
        while True:
            chunk = os.read(descriptor, 1024 * 1024)
            if not chunk:
                break
            chunks.append(chunk)
        if (
            not stat.S_ISREG(opened.st_mode)
            or stat.S_IMODE(opened.st_mode) != 0o600
            or opened.st_uid != os.geteuid()
            or opened.st_nlink != expected_nlink
            or by_name.st_nlink != expected_nlink
            or staged.st_nlink != expected_nlink
            or (opened.st_dev, opened.st_ino) != stage.identity
            or (by_name.st_dev, by_name.st_ino) != stage.identity
            or (staged.st_dev, staged.st_ino) != stage.identity
            or b"".join(chunks) != stage.raw
            or _retained_bytes(stage.descriptor) != stage.raw
        ):
            raise RecoveryBlocked("published_output_identity_or_readback_mismatch")
        if stage.final_descriptor is not None:
            os.close(stage.final_descriptor)
        stage.final_descriptor = descriptor
        descriptor = -1
    finally:
        if descriptor >= 0:
            os.close(descriptor)


def _unlink_exact(
    parent: _TrustedDirectory, name: str, identity: tuple[int, int], *,
    label: str, failures: list[str], replacements: list[str],
) -> None:
    try:
        current = os.stat(
            name, dir_fd=parent.descriptor, follow_symlinks=False,
        )
    except FileNotFoundError:
        return
    except Exception as exc:
        failures.append(f"{label}_stat:{type(exc).__name__}:{exc}")
        return
    if (current.st_dev, current.st_ino) != identity:
        replacements.append(f"{label}_replacement_preserved:{name}")
        return
    try:
        os.unlink(name, dir_fd=parent.descriptor)
    except Exception as exc:
        failures.append(f"{label}_unlink:{type(exc).__name__}:{exc}")


def _close_best_effort(
    descriptor: int | None, label: str, failures: list[str],
) -> None:
    if descriptor is None or descriptor < 0:
        return
    try:
        os.close(descriptor)
    except Exception as exc:
        failures.append(f"{label}_close:{type(exc).__name__}:{exc}")


def _transaction_cleanup(
    stages: list[_StagedOutput], directories: list[_TrustedDirectory],
    original: BaseException,
) -> PublicationFailure:
    failures: list[str] = []
    replacements: list[str] = []
    for stage in reversed(stages):
        if stage.final_published:
            _unlink_exact(
                stage.target.parent, stage.target.path.name, stage.identity,
                label="final", failures=failures, replacements=replacements,
            )
    for stage in stages:
        _unlink_exact(
            stage.target.parent, stage.stage_name, stage.identity,
            label="stage", failures=failures, replacements=replacements,
        )
    residue: list[str] = []
    for stage in stages:
        try:
            linked = os.fstat(stage.descriptor).st_nlink
        except Exception as exc:
            failures.append(f"stage_residue_fstat:{type(exc).__name__}:{exc}")
        else:
            if linked:
                residue.append(
                    f"{stage.target.parent.path}:{stage.identity}:nlink={linked}"
                )
    for stage in stages:
        _close_best_effort(stage.final_descriptor, "final_fd", failures)
        _close_best_effort(stage.descriptor, "stage_fd", failures)
    for directory in directories:
        try:
            os.fsync(directory.descriptor)
        except Exception as exc:
            failures.append(f"directory_fsync:{type(exc).__name__}:{exc}")
        _close_best_effort(directory.descriptor, "directory_fd", failures)
    failures.extend(replacements)
    return PublicationFailure(original, failures, residue)


def _publish_outputs_transactionally(
    outputs: list[tuple[Path, Any]], *, proposal_path: Path | None,
) -> None:
    """Publish through retained trusted dirfds; verify hardlink identity."""
    directories: list[_TrustedDirectory] = []
    by_parent: dict[Path, _TrustedDirectory] = {}
    stages: list[_StagedOutput] = []
    try:
        for parent_path in sorted({final.parent for final, _payload in outputs}):
            directory = _open_trusted_directory(parent_path)
            directories.append(directory)
            by_parent[parent_path] = directory
        for final, payload in outputs:
            stage_name = f".{final.name}.crown4-stage-{secrets.token_hex(16)}"
            raw = _payload_bytes(payload)
            parent = by_parent[final.parent]
            create_target = _OutputTarget(parent.path / stage_name, parent)
            descriptor = _create_output(create_target)
            opened = os.fstat(descriptor)
            stage = _StagedOutput(
                target=_OutputTarget(final, parent), stage_name=stage_name,
                descriptor=descriptor, identity=(opened.st_dev, opened.st_ino),
                raw=raw,
            )
            stages.append(stage)
            _write_retained(descriptor, payload)
            _verify_stage(stage, expected_nlink=1)
            os.fsync(parent.descriptor)

        ordered = sorted(
            stages, key=lambda item: item.target.path == proposal_path,
        )
        for stage in ordered:
            os.link(
                stage.stage_name, stage.target.path.name,
                src_dir_fd=stage.target.parent.descriptor,
                dst_dir_fd=stage.target.parent.descriptor,
                follow_symlinks=False,
            )
            stage.final_published = True
            _verify_final(stage, expected_nlink=2)
        for directory in directories:
            os.fsync(directory.descriptor)
        for stage in stages:
            unlink_failures: list[str] = []
            replacements: list[str] = []
            _unlink_exact(
                stage.target.parent, stage.stage_name, stage.identity,
                label="stage_commit", failures=unlink_failures,
                replacements=replacements,
            )
            if unlink_failures or replacements:
                raise RecoveryBlocked(
                    f"stage_commit_cleanup_failed:{unlink_failures + replacements}"
                )
            _verify_final(stage, expected_nlink=1)
        for directory in directories:
            os.fsync(directory.descriptor)
    except BaseException as original:
        raise _transaction_cleanup(stages, directories, original) from original
    cleanup_failures: list[str] = []
    for stage in stages:
        _close_best_effort(stage.final_descriptor, "final_fd", cleanup_failures)
        _close_best_effort(stage.descriptor, "stage_fd", cleanup_failures)
    for directory in directories:
        _close_best_effort(directory.descriptor, "directory_fd", cleanup_failures)
    if cleanup_failures:
        raise PublicationFailure(
            RecoveryBlocked("post_commit_fd_cleanup_failed"),
            cleanup_failures, [],
        )


def _retained_bytes(fd: int) -> bytes:
    os.lseek(fd, 0, os.SEEK_SET)
    chunks = []
    while True:
        chunk = os.read(fd, 1024 * 1024)
        if not chunk:
            break
        chunks.append(chunk)
    os.lseek(fd, 0, os.SEEK_SET)
    return b"".join(chunks)


def _trusted_key_pin() -> str:
    fd, raw, stat = _open_input(TRUSTED_KEY_PIN_PATH, "trusted_key_pin")
    try:
        if stat.st_uid != 0 or stat.st_mode & 0o022:
            raise RecoveryBlocked("trusted_key_pin_not_root_controlled")
        value = raw.decode("ascii").strip()
        if len(value) != 64 or any(char not in "0123456789abcdef" for char in value):
            raise RecoveryBlocked("trusted_key_pin_invalid")
        return value
    finally:
        os.close(fd)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--ledger", type=Path, required=True)
    parser.add_argument("--replay", type=Path, required=True)
    parser.add_argument("--report", type=Path)
    parser.add_argument("--apply-to-copy", type=Path)
    parser.add_argument("--authority", type=Path, required=True)
    args = parser.parse_args()
    input_paths = [args.ledger, args.replay, args.authority]
    opened: list[tuple[int, bytes, os.stat_result]] = []
    try:
        for path, label in zip(
            input_paths, ("ledger", "replay", "authority"),
        ):
            opened.append(_open_input(path, label))
        input_fds = [item[0] for item in opened]
        input_bytes = [item[1] for item in opened]
        input_stats = [item[2] for item in opened]
        if len({(stat.st_dev, stat.st_ino) for stat in input_stats}) != 3:
            raise RecoveryBlocked("input_inode_alias_rejected")
        requested_outputs = [
            path for path in (args.report, args.apply_to_copy) if path is not None
        ]
        outputs = [
            _preflight_output(path, input_paths, input_stats)
            for path in requested_outputs
        ]
        if len(set(outputs)) != len(outputs):
            raise RecoveryBlocked("output_path_alias_rejected")
        canonical_report = (
            _preflight_output(args.report, input_paths, input_stats)
            if args.report is not None else None
        )
        canonical_proposal = (
            _preflight_output(args.apply_to_copy, input_paths, input_stats)
            if args.apply_to_copy is not None else None
        )
        ledger = _strict_json_bytes(input_bytes[0], "ledger")
        replay = _strict_json_bytes(input_bytes[1], "replay")
        authority = _strict_json_bytes(input_bytes[2], "authority")
        if not isinstance(ledger, dict) or not isinstance(replay, dict):
            raise RecoveryBlocked("ledger_and_replay_must_be_objects")
        ledger_hash, replay_hash = (
            bytes_hash(input_bytes[0]), bytes_hash(input_bytes[1]),
        )
        report, proposed = plan_recovery(
            ledger, replay, authority=authority,
            trusted_public_key_sha256=_trusted_key_pin(),
            ledger_sha256=ledger_hash, replay_sha256=replay_hash,
            require_apply=args.apply_to_copy is not None,
        )
        if args.apply_to_copy:
            report["mode"] = "apply-to-copy"
            report["output_ledger"] = str(canonical_proposal)
        if any(
            _retained_bytes(fd) != before
            for fd, before in zip(input_fds, input_bytes)
        ):
            raise RecoveryBlocked("input_changed_before_output")
        publication: list[tuple[Path, Any]] = []
        if canonical_report is not None:
            publication.append((canonical_report, report))
        if canonical_proposal is not None:
            publication.append((canonical_proposal, proposed))
        _publish_outputs_transactionally(
            publication, proposal_path=canonical_proposal,
        )
        print(json.dumps(report, ensure_ascii=False, sort_keys=True))
        return 0
    finally:
        for fd, _raw, _stat in opened:
            try:
                os.close(fd)
            except OSError:
                # Never let an input-close error mask a publication or cleanup
                # failure; all remaining retained inputs still get closed.
                pass


if __name__ == "__main__":
    raise SystemExit(main())
