"""One-time, user-approved production merge for Crown condition #4.

This module is intentionally separate from ``crown_condition4_recovery``.
It does not mint, bypass, or reinterpret that CLI's external authority.  Its
only executable contract is the exact operator-approved 2026-08-28 cohort.
"""
from __future__ import annotations

import argparse
import copy
import hashlib
import importlib.util
import json
import os
import stat
import tempfile
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path
from types import ModuleType
from typing import Any

from analysis import crown_condition4_recovery as recovery
from analysis.wilson_registry_manifest import build_manifest
from crown.config import Settings, settings
from crown.state import paths, state_lock

SCHEMA = "crown-condition-4-operator-merge-report-v1"
APPROVAL_CONTEXT = (
    "user-approved:crown-condition-4:40-observations:"
    "39-settled:21-hits:18-non-hits:1-postponed:20260828"
)
APPROVED_AT = "2026-08-28T10:28:00+08:00"
LEDGER_PATH = Path("/var/lib/footbreak/crown/ledger.json")
HISTORY_NAME = "prediction_history.json"
EXPECTED_FINAL = {
    "observations": 121,
    "decided": 120,
    "hits": 73,
    "pending": 1,
}


class OperatorMergeBlocked(recovery.RecoveryBlocked):
    """The one-time operator contract was not matched exactly."""


class PostWriteVerificationFailure(OperatorMergeBlocked):
    """The new inode failed verification and the original had to be restored."""


def _strict_object(raw: bytes, label: str) -> dict[str, Any]:
    def pairs(items: list[tuple[str, Any]]) -> dict[str, Any]:
        value: dict[str, Any] = {}
        for key, item in items:
            if key in value:
                raise OperatorMergeBlocked(f"{label}_duplicate_json_key")
            value[key] = item
        return value

    try:
        value = json.loads(raw.decode("utf-8"), object_pairs_hook=pairs)
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise OperatorMergeBlocked(f"{label}_invalid_json") from exc
    if not isinstance(value, dict):
        raise OperatorMergeBlocked(f"{label}_not_object")
    return value


def _read_regular(path: Path, label: str) -> tuple[bytes, dict[str, Any]]:
    flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
    try:
        fd = os.open(path, flags)
    except OSError as exc:
        raise OperatorMergeBlocked(f"{label}_open_failed") from exc
    try:
        info = os.fstat(fd)
        if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1:
            raise OperatorMergeBlocked(f"{label}_unsafe_inode")
        chunks: list[bytes] = []
        while True:
            chunk = os.read(fd, 1024 * 1024)
            if not chunk:
                break
            chunks.append(chunk)
        raw = b"".join(chunks)
    finally:
        os.close(fd)
    return raw, _strict_object(raw, label)


def _approval_binding(signature: str, rows: list[dict[str, Any]]) -> str:
    return recovery.canonical_hash({
        "context": APPROVAL_CONTEXT,
        "approved_at": APPROVED_AT,
        "condition_signature": signature,
        "ordered_candidate_hashes": [
            row["replay_candidate_hash"] for row in rows
        ],
        "expected": recovery.EXPECTED,
        "deletions_authorized": False,
    })


def _draft_operator_row(
    ledger: dict[str, Any],
    candidate: dict[str, Any],
    signature: str,
    frozen: dict[str, Any],
    binding: str,
) -> dict[str, Any]:
    active = frozen["evidence_versions"][1]
    history = copy.deepcopy(frozen["historical_evidence"])
    history.update({
        "hits": active["cumulative_hits"],
        "decided": active["cumulative_decided"],
        "evidence_version": active["version"],
        "evidence_hash": active["evidence_hash"],
    })
    arithmetic = recovery.admission_arithmetic(
        active["cumulative_hits"],
        active["cumulative_decided"],
        candidate["t5_odds"],
    )
    if not isinstance(arithmetic, dict) or arithmetic.get("passes") is not False:
        raise OperatorMergeBlocked("candidate_is_not_low_odds_under_active_v2")
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
        key: candidate.get(key)
        for key in ("match_id", "league", "home", "away", "kickoff_hkt")
    }
    watch["kickoff"] = candidate["kickoff_hkt"]
    row = recovery.record_match_observation(
        ledger,
        "crown",
        watch,
        "HDC",
        {
            "market": "HDC",
            "side": candidate["selected_side"],
            "line": candidate["selected_line"],
            "odds": candidate["t5_odds"],
        },
        admission,
        now=candidate["t5_recorded_at"],
        market_label="讓球",
        selected_role=candidate.get("selected_role"),
        selected_line=candidate["selected_line"],
    )
    if row is None:
        raise OperatorMergeBlocked("formal_observation_constructor_rejected_candidate")
    row["rollover_provenance"] = recovery._rollover_marker(
        "crown",
        str(candidate["match_id"]),
        "HDC",
        signature,
        str(candidate["t5_recorded_at"]),
        active,
    )
    row["recovered_missing_observation"] = {
        "schema_version": 3,
        "migration": "crown-condition-4-user-approved-operator-merge-v1",
        "operator_approval_context": APPROVAL_CONTEXT,
        "operator_approval_binding_sha256": binding,
        "candidate_sha256": candidate["replay_candidate_hash"],
        "admission_proved_without_result": True,
        "legacy_19_identities_unavailable": True,
        "deletion_performed": False,
        "settlement_recomputed_from_score": isinstance(
            candidate.get("hdc_grade"), dict
        ),
    }
    grade = candidate.get("hdc_grade")
    if isinstance(grade, dict):
        hit = recovery._score_hit(candidate)
        row.update({
            "status": "SETTLED",
            "result": "Won" if hit else "Lost",
            "settled_at": APPROVED_AT,
            "settlement_source": "condition_4_score_line_recomputed_operator_v1",
        })
    else:
        row.update({
            "status": "PENDING",
            "pending_reason": "user_approved_postponed_fixture",
            "postponement_proof": {
                "schema_version": 1,
                "operator_approval_context": APPROVAL_CONTEXT,
                "operator_approval_binding_sha256": binding,
                "match_id": candidate["match_id"],
                "market": "HDC",
                "league": candidate["league"],
                "home": candidate["home"],
                "away": candidate["away"],
                "kickoff_hkt": candidate["kickoff_hkt"],
                "result_status": "POSTPONED",
                "score": None,
            },
        })
    return row


def _cohort(
    ledger: dict[str, Any], signature: str
) -> dict[str, int]:
    rows = [
        row
        for row in ledger["wilson_validation"].get("observations") or []
        if isinstance(row, dict)
        and row.get("frozen_condition_signature") == signature
    ]
    return {
        "observations": 81 + len(rows),
        "decided": 81 + sum(
            row.get("status") == "SETTLED"
            and row.get("result") in {"Won", "Lost", "Half Won", "Half Lost"}
            for row in rows
        ),
        "hits": 52 + sum(
            row.get("status") == "SETTLED"
            and row.get("result") in {"Won", "Half Won"}
            for row in rows
        ),
        "pending": sum(row.get("status") == "PENDING" for row in rows),
    }


def verify_final_ledger(
    ledger: dict[str, Any],
    *,
    signature: str,
    binding: str,
) -> None:
    manifest = build_manifest(ledger, "crown")
    if not manifest.get("valid"):
        raise OperatorMergeBlocked("final_ledger_strict_manifest_invalid")
    frozen = ledger["wilson_validation"]["conditions"].get(signature)
    if not isinstance(frozen, dict):
        raise OperatorMergeBlocked("final_condition_missing")
    active = frozen.get("active_evidence")
    progress = frozen.get("pending_rollover_progress")
    rows = [
        row
        for row in ledger["wilson_validation"].get("observations") or []
        if isinstance(row, dict)
        and row.get("frozen_condition_signature") == signature
    ]
    recovered = [
        row
        for row in rows
        if isinstance(row.get("recovered_missing_observation"), dict)
        and row["recovered_missing_observation"].get(
            "operator_approval_binding_sha256"
        )
        == binding
    ]
    recovered_counts = Counter(
        "pending" if row.get("status") == "PENDING"
        else "hit" if row.get("result") == "Won"
        else "non_hit"
        for row in recovered
    )
    pending = [row for row in recovered if row.get("status") == "PENDING"]
    if (
        _cohort(ledger, signature) != EXPECTED_FINAL
        or len(recovered) != 40
        or recovered_counts
        != Counter({"hit": 21, "non_hit": 18, "pending": 1})
        or len(pending) != 1
        or not recovery._pending_fixture(pending[0])
        or not isinstance(active, dict)
        or (active.get("cumulative_hits"), active.get("cumulative_decided"))
        != (61, 101)
        or not isinstance(progress, dict)
        or (progress.get("eligible_hits"), progress.get("eligible_decided"))
        != (12, 19)
        or len(frozen.get("evidence_versions") or []) != 3
        or frozen["evidence_versions"][-1].get("batch_hits") != 9
        or frozen["evidence_versions"][-1].get("batch_decided") != 20
    ):
        raise OperatorMergeBlocked("final_exact_count_or_rollover_mismatch")


def plan_operator_merge(
    ledger: dict[str, Any], replay: dict[str, Any]
) -> tuple[dict[str, Any], dict[str, Any], str, str]:
    before = recovery.canonical_hash(ledger), recovery.canonical_hash(replay)
    input_manifest = build_manifest(ledger, "crown")
    if not input_manifest.get("valid"):
        reasons = input_manifest.get("rejection_reasons")
        if not isinstance(reasons, dict):
            reasons = {"manifest_reason_shape_invalid": 1}
        sanitized = ",".join(
            f"{key}:{value}"
            for key, value in sorted(reasons.items())
            if isinstance(key, str)
            and key.replace("_", "").isalnum()
            and isinstance(value, int)
            and not isinstance(value, bool)
        )
        condition_reasons = ";".join(
            f"{row.get('condition_number')}="
            + ",".join(
                reason
                for reason in row.get("rejection_reasons", [])
                if isinstance(reason, str)
                and reason.replace("_", "").isalnum()
            )
            for row in input_manifest.get("conditions", [])
            if isinstance(row, dict) and row.get("rejection_reasons")
        )
        raise OperatorMergeBlocked(
            f"input_ledger_strict_manifest_invalid:{sanitized or 'unknown:1'}"
            f":conditions:{condition_reasons or 'unknown'}"
        )
    proposed = copy.deepcopy(ledger)
    signature, frozen, namespace = recovery._condition(proposed)
    rows = recovery._validate_replay(replay, signature, frozen)
    if replay.get("summary") != {
        "matching_fixture_count": 40,
        "wilson_price_pass_fixture_count": 0,
        "low_price_observation_fixture_count": 40,
        "recorded_expected_fixture_count": 0,
        "missing_expected_record_fixture_count": 40,
        "unknown_result_fixture_count": 1,
        "recorded_unknown_result_fixture_count": 0,
    } or any(
        row.get("formal_row_count") != 0
        or row.get("formal_row_ids") != []
        or row.get("formal_statuses") != []
        or row.get("matching_record_count") != 0
        for row in rows
    ):
        raise OperatorMergeBlocked("approved_replay_manifest_mismatch")
    settled = [row for row in rows if row.get("result_known") is True]
    hits = sum(recovery._score_hit(row) for row in settled)
    if len(rows) != 40 or len(settled) != 39 or hits != 21:
        raise OperatorMergeBlocked("approved_replay_count_mismatch")
    candidates = {recovery._candidate_identity(row) for row in rows}
    existing = [
        recovery._candidate_identity(row)
        for collection in (proposed.get("bets") or [], namespace.get("observations") or [])
        for row in collection
        if isinstance(row, dict)
        and recovery._candidate_identity(row) in candidates
    ]
    if existing:
        raise OperatorMergeBlocked("candidate_fixture_market_already_exists")
    binding = _approval_binding(signature, rows)
    for candidate in rows:
        _draft_operator_row(proposed, candidate, signature, frozen, binding)
    recovery.recompute_namespace(proposed, "crown", now=APPROVED_AT)
    verify_final_ledger(proposed, signature=signature, binding=binding)
    if (recovery.canonical_hash(ledger), recovery.canonical_hash(replay)) != before:
        raise RuntimeError("operator_plan_mutated_input")
    report = {
        "schema": SCHEMA,
        "status": "PLANNED",
        "condition_number": 4,
        "changes": {
            "added": 40,
            "settled": 39,
            "hits": 21,
            "non_hits": 18,
            "pending": 1,
            "deleted": 0,
        },
        "final": copy.deepcopy(EXPECTED_FINAL),
        "rollover": {
            "active_hits": 61,
            "active_decided": 101,
            "tail_hits": 12,
            "tail_decided": 19,
        },
        "safety": {
            "canonical_state_lock": True,
            "strict_manifest_before_after": True,
            "candidate_duplicates": 0,
            "external_authority_fabricated": False,
        },
    }
    return report, proposed, signature, binding


def _fsync_directory(path: Path) -> None:
    fd = os.open(path, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def _write_all(fd: int, raw: bytes) -> None:
    view = memoryview(raw)
    while view:
        written = os.write(fd, view)
        if written <= 0:
            raise OSError("short write")
        view = view[written:]


def _create_root_backup(ledger_path: Path, raw: bytes) -> Path:
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S.%fZ")
    backup = ledger_path.with_name(f"{ledger_path.name}.condition4.{stamp}.bak")
    flags = (
        os.O_WRONLY
        | os.O_CREAT
        | os.O_EXCL
        | getattr(os, "O_NOFOLLOW", 0)
    )
    fd = os.open(backup, flags, 0o600)
    try:
        os.fchmod(fd, 0o600)
        os.fchown(fd, 0, 0)
        _write_all(fd, raw)
        os.fsync(fd)
        info = os.fstat(fd)
        if info.st_uid != 0 or stat.S_IMODE(info.st_mode) != 0o600:
            raise OperatorMergeBlocked("backup_not_root_owned_mode_0600")
    except BaseException:
        os.close(fd)
        try:
            backup.unlink()
        except OSError:
            pass
        raise
    else:
        os.close(fd)
    _fsync_directory(ledger_path.parent)
    return backup


def _atomic_replace_bytes(
    path: Path,
    raw: bytes,
    *,
    uid: int,
    gid: int,
    mode: int,
) -> None:
    fd, temporary = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    try:
        os.fchmod(fd, mode)
        os.fchown(fd, uid, gid)
        _write_all(fd, raw)
        os.fsync(fd)
        os.close(fd)
        fd = -1
        os.replace(temporary, path)
        _fsync_directory(path.parent)
    finally:
        if fd >= 0:
            os.close(fd)
        try:
            os.unlink(temporary)
        except FileNotFoundError:
            pass


def _ledger_bytes(ledger: dict[str, Any]) -> bytes:
    return (
        json.dumps(ledger, ensure_ascii=False, indent=2, sort_keys=True) + "\n"
    ).encode("utf-8")


def _load_replay_module(path: Path) -> ModuleType:
    info = path.stat(follow_symlinks=False)
    if (
        not stat.S_ISREG(info.st_mode)
        or info.st_uid != 0
        or info.st_mode & 0o022
    ):
        raise OperatorMergeBlocked("replay_program_not_root_owned_immutable")
    spec = importlib.util.spec_from_file_location(
        "crown_condition4_locked_replay", path
    )
    if spec is None or spec.loader is None:
        raise OperatorMergeBlocked("replay_program_load_failed")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def apply_operator_merge(config: Settings, replay_program: Path) -> dict[str, Any]:
    if os.geteuid() != 0:
        raise OperatorMergeBlocked("root_required")
    ledger_path = paths(config)["ledger"]
    if ledger_path != LEDGER_PATH:
        raise OperatorMergeBlocked("canonical_production_ledger_path_required")
    replay_module = _load_replay_module(replay_program)
    with state_lock(config) as acquired:
        if not acquired:
            raise OperatorMergeBlocked("canonical_state_lock_not_acquired")
        original_raw, ledger = _read_regular(ledger_path, "ledger")
        ledger_info = ledger_path.stat(follow_symlinks=False)
        ledger_mode = stat.S_IMODE(ledger_info.st_mode)
        _history_raw, history = _read_regular(
            config.state_dir / HISTORY_NAME, "prediction_history"
        )
        replay = replay_module.replay(
            4,
            None,
            Path("/var/lib/footbreak/learning/predictions.sqlite"),
            locked_persisted_snapshot=(ledger, history),
        )
        report, proposed, signature, binding = plan_operator_merge(ledger, replay)
        backup = _create_root_backup(ledger_path, original_raw)
        proposed_raw = _ledger_bytes(proposed)
        write_started = False
        try:
            write_started = True
            _atomic_replace_bytes(
                ledger_path,
                proposed_raw,
                uid=ledger_info.st_uid,
                gid=ledger_info.st_gid,
                mode=ledger_mode,
            )
            stored_raw, stored = _read_regular(ledger_path, "written_ledger")
            stored_info = ledger_path.stat(follow_symlinks=False)
            if (
                stored_raw != proposed_raw
                or hashlib.sha256(stored_raw).digest()
                != hashlib.sha256(proposed_raw).digest()
                or stored_info.st_uid != ledger_info.st_uid
                or stored_info.st_gid != ledger_info.st_gid
                or stat.S_IMODE(stored_info.st_mode) != ledger_mode
            ):
                raise OperatorMergeBlocked("written_ledger_bytes_mismatch")
            verify_final_ledger(stored, signature=signature, binding=binding)
        except BaseException as exc:
            if write_started:
                try:
                    _atomic_replace_bytes(
                        ledger_path,
                        original_raw,
                        uid=ledger_info.st_uid,
                        gid=ledger_info.st_gid,
                        mode=ledger_mode,
                    )
                    restored_raw, restored = _read_regular(
                        ledger_path, "restored_ledger"
                    )
                    restored_info = ledger_path.stat(follow_symlinks=False)
                    if (
                        restored_raw != original_raw
                        or restored_info.st_uid != ledger_info.st_uid
                        or restored_info.st_gid != ledger_info.st_gid
                        or stat.S_IMODE(restored_info.st_mode) != ledger_mode
                    ):
                        raise OperatorMergeBlocked("rollback_bytes_mismatch")
                    if not build_manifest(restored, "crown").get("valid"):
                        raise OperatorMergeBlocked(
                            "rollback_ledger_manifest_invalid"
                        )
                except BaseException as rollback_exc:
                    raise PostWriteVerificationFailure(
                        f"post_write_failure:{type(exc).__name__};"
                        f"rollback_failure:{type(rollback_exc).__name__}"
                    ) from rollback_exc
            raise PostWriteVerificationFailure(
                f"post_write_failure:{type(exc).__name__};rollback=verified"
            ) from exc
    report["status"] = "APPLIED_AND_VERIFIED"
    report["backup"] = {
        "created": True,
        "root_owned": True,
        "mode": "0600",
        "timestamped": True,
    }
    return report


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--replay-program", type=Path, required=True)
    args = parser.parse_args()
    try:
        report = apply_operator_merge(settings(), args.replay_program)
    except Exception as exc:
        # Never emit fixture rows, ledger bytes, paths containing secrets, or
        # exception detail that could include them.
        error_code = (
            str(exc)
            if isinstance(exc, OperatorMergeBlocked)
            else type(exc).__name__
        )
        print(json.dumps({
            "schema": SCHEMA,
            "status": "BLOCKED",
            "error": type(exc).__name__,
            "error_code": error_code,
        }, sort_keys=True))
        return 2
    print(json.dumps(report, sort_keys=True, separators=(",", ":")))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
