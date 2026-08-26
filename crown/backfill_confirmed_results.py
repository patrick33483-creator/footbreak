"""One-time, fail-closed Crown result backfill for the verified 42-fixture cohort.

The command is a dry-run unless ``--apply`` is supplied.  Apply additionally
requires the exact ledger SHA-256 printed by a preceding dry-run.  It performs
no provider reads, dashboard publication, or notification delivery.
"""
from __future__ import annotations

import argparse
import copy
import hashlib
import json
import os
import re
import stat
from dataclasses import replace
from datetime import datetime
from pathlib import Path
from typing import Any, Iterable

from .common import HKT, parse_time, write_json_atomic
from .config import Settings, settings
from .challenger_v2 import research_bets
from .ledger import condition_bets, condition_observations, recompute_stats
from .settle import _settle
from .state import paths, settlement_lock, state_lock


EXPECTED_FIXTURES = 42
EXPECTED_CONFIRMED_FIXTURES = 34
EXPECTED_CONFLICT_FIXTURES = 8
EXPECTED_CONFIRMED_ROWS = 66
EXPECTED_CONFLICT_ROWS = 17
EXPECTED_FIXTURE_SHA256 = (
    "6e996b0c8caf7330b39885afc4d3fd03ba9cc9b4d084364050d4f2542991d3d1"
)
SOURCE_PREFIX = "manual_verified_backfill:fixtures_42_verified:"
SETTLEMENT_FIELDS = (
    "status", "result", "pnl", "score", "settled_at", "settlement_source",
    "void_reason", "last_settlement_attempt_at", "settlement_pending_reason",
    "history",
)


class BackfillError(RuntimeError):
    """A safety invariant failed; no ledger update may be written."""


def _strict_json_bytes(raw: bytes, path: Path) -> Any:
    def unique_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
        result: dict[str, Any] = {}
        for key, value in pairs:
            if key in result:
                raise BackfillError(f"duplicate_json_key:{path}:{key}")
            result[key] = value
        return result

    try:
        return json.loads(raw.decode("utf-8"), object_pairs_hook=unique_object)
    except BackfillError:
        raise
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise BackfillError(f"invalid_json:{path}:{exc}") from exc


def _strict_json(path: Path) -> Any:
    try:
        raw = path.read_bytes()
    except OSError as exc:
        raise BackfillError(f"json_file_unreadable:{path}:{exc}") from exc
    return _strict_json_bytes(raw, path)


def _canonical_bytes(value: Any) -> bytes:
    return (
        json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True) + "\n"
    ).encode("utf-8")


def _sha256(raw: bytes) -> str:
    return hashlib.sha256(raw).hexdigest()


def _row_digest(row: dict[str, Any]) -> str:
    return _sha256(json.dumps(
        row, ensure_ascii=False, sort_keys=True, separators=(",", ":"),
        default=str,
    ).encode("utf-8"))


def _identity_digest(fixtures: Iterable[dict[str, Any]]) -> str:
    identity = [{
        "match_id": row["match_id"],
        "kickoff_HKT": row["kickoff_HKT"],
        "home": row["home"],
        "away": row["away"],
        "bet": row["bet"],
        "verification": row["verification"],
        "home_score_90": row["home_score_90"],
        "away_score_90": row["away_score_90"],
        "grade": row["grade"],
    } for row in fixtures]
    return _sha256(json.dumps(
        identity, ensure_ascii=False, sort_keys=True, separators=(",", ":"),
    ).encode("utf-8"))


def _line(value: Any) -> float | None:
    try:
        parts = [float(part.strip().lstrip("+")) for part in str(value).split("/")]
    except (TypeError, ValueError):
        return None
    if not parts or len(parts) > 2:
        return None
    if len(parts) == 2 and abs(abs(parts[0] - parts[1]) - 0.5) > 1e-9:
        return None
    answer = sum(parts) / len(parts)
    return answer if abs(answer * 4 - round(answer * 4)) <= 1e-9 else None


def _fixture_selection(fixture: dict[str, Any]) -> tuple[str, str, float]:
    raw = str(fixture.get("bet") or "").strip()
    if raw.startswith("HDC "):
        raw = raw[4:].strip()
    total = re.fullmatch(r"(Over|Under)\s+([+]?\d+(?:\.\d+)?)", raw)
    if total:
        return "HIL", "H" if total.group(1) == "Over" else "L", float(total.group(2))
    handicap = re.fullmatch(r"(主隊|客隊)\s+([+-]?\d+(?:\.\d+)?)", raw)
    if handicap:
        displayed = float(handicap.group(2))
        # Crown stores the home-team handicap even when the away side is the
        # selection.  "客隊 +0.25" therefore grades against home line -0.25.
        return "HDC", "H" if handicap.group(1) == "主隊" else "A", (
            displayed if handicap.group(1) == "主隊" else -displayed
        )
    raise BackfillError(f"unsupported_fixture_bet:{fixture.get('match_id')}:{raw}")


def _fixture_team_identity(value: Any) -> str:
    """Return the exact recorded ledger name from an annotated verification cell.

    Five verification rows use ``recorded name -> researched identity``.  The
    left side is the immutable Crown ledger identity; the right side is human
    verification context and must never silently replace it in state.
    """
    text = str(value)
    return text.split(" -> ", 1)[0].rstrip()


def _validate_fixtures(payload: Any) -> list[dict[str, Any]]:
    if not isinstance(payload, list) or len(payload) != EXPECTED_FIXTURES:
        raise BackfillError(f"expected_{EXPECTED_FIXTURES}_fixtures")
    required = {
        "match_id", "kickoff_HKT", "home", "away", "bet", "home_score_90",
        "away_score_90", "grade", "verification",
    }
    allowed_grades = {"Won", "Half Won", "Refunded", "Half Lost", "Lost"}
    seen: set[str] = set()
    counts = {"CONFIRMED": 0, "CONFLICT": 0}
    fixtures: list[dict[str, Any]] = []
    for index, original in enumerate(payload):
        if not isinstance(original, dict) or not required.issubset(original):
            raise BackfillError(f"fixture_shape_invalid:{index}")
        fixture = copy.deepcopy(original)
        match_id = str(fixture["match_id"])
        if not match_id or match_id in seen:
            raise BackfillError(f"fixture_match_id_invalid_or_duplicate:{match_id}")
        seen.add(match_id)
        verification = fixture["verification"]
        if verification not in counts:
            raise BackfillError(f"verification_not_allowed:{match_id}:{verification}")
        counts[verification] += 1
        kickoff = parse_time(str(fixture["kickoff_HKT"]))
        if kickoff is None:
            raise BackfillError(f"fixture_kickoff_invalid:{match_id}")
        try:
            home_score = int(fixture["home_score_90"])
            away_score = int(fixture["away_score_90"])
        except (TypeError, ValueError) as exc:
            raise BackfillError(f"fixture_score_invalid:{match_id}") from exc
        if home_score < 0 or away_score < 0:
            raise BackfillError(f"fixture_score_invalid:{match_id}")
        if verification == "CONFIRMED" and fixture["grade"] not in allowed_grades:
            raise BackfillError(f"fixture_grade_invalid:{match_id}:{fixture['grade']}")
        code, side, line = _fixture_selection(fixture)
        if verification == "CONFIRMED":
            probe = {
                "code": code, "side": side, "condition": line, "status": "PENDING",
                "formal_bet": False,
            }
            if not _settle(
                probe, {"home_score": home_score, "away_score": away_score}, "validation"
            ) or probe["result"] != fixture["grade"]:
                raise BackfillError(
                    f"fixture_grade_mismatch:{match_id}:"
                    f"expected={fixture['grade']}:calculated={probe.get('result')}"
                )
        fixture["_identity"] = {
            "match_id": match_id,
            "home": _fixture_team_identity(fixture["home"]),
            "away": _fixture_team_identity(fixture["away"]),
            "kickoff": kickoff,
            "code": code,
            "side": side,
            "line": line,
            "home_score": home_score,
            "away_score": away_score,
        }
        fixtures.append(fixture)
    if counts != {
        "CONFIRMED": EXPECTED_CONFIRMED_FIXTURES,
        "CONFLICT": EXPECTED_CONFLICT_FIXTURES,
    }:
        raise BackfillError(f"fixture_verification_counts_invalid:{counts}")
    return fixtures


def _ledger_rows(ledger: dict[str, Any]) -> list[tuple[str, dict[str, Any]]]:
    collections = [
        ("bets", ledger.get("bets")),
        ("wilson_validation.observations",
         (ledger.get("wilson_validation") or {}).get("observations")
         if isinstance(ledger.get("wilson_validation"), dict) else None),
        ("crown_v2_challenger.research_bets",
         (ledger.get("crown_v2_challenger") or {}).get("research_bets")
         if isinstance(ledger.get("crown_v2_challenger"), dict) else None),
    ]
    output: list[tuple[str, dict[str, Any]]] = []
    seen_objects: set[int] = set()
    for location, value in collections:
        if value is None:
            continue
        if not isinstance(value, list):
            raise BackfillError(f"ledger_collection_not_list:{location}")
        for index, row in enumerate(value):
            if not isinstance(row, dict):
                raise BackfillError(f"ledger_row_not_object:{location}:{index}")
            if id(row) in seen_objects:
                raise BackfillError(f"ledger_row_object_aliased:{location}:{index}")
            seen_objects.add(id(row))
            output.append((f"{location}[{index}]", row))
    return output


def _row_id(row: dict[str, Any]) -> str:
    values = [
        str(row.get(field) or "") for field in
        ("bet_id", "observation_id", "research_id") if row.get(field)
    ]
    if len(values) != 1:
        raise BackfillError(
            f"row_durable_id_missing_or_ambiguous:{row.get('match_id')}"
        )
    return values[0]


def _validate_row(
    location: str, row: dict[str, Any], fixture: dict[str, Any],
) -> dict[str, Any]:
    expected = fixture["_identity"]
    row_id = _row_id(row)
    errors: list[str] = []
    if str(row.get("match_id") or "") != expected["match_id"]:
        errors.append("match_id")
    titan_id = row.get("titan_match_id")
    if titan_id is not None and str(titan_id) != expected["match_id"]:
        errors.append("titan_match_id")
    for field in ("home", "away"):
        if row.get(field) != expected[field]:
            errors.append(field)
    kickoff = parse_time(row.get("kickoff"))
    if kickoff is None or kickoff != expected["kickoff"]:
        errors.append("kickoff")
    if str(row.get("code") or "").upper() != expected["code"]:
        errors.append("market")
    if str(row.get("side") or "").upper() != expected["side"]:
        errors.append("side")
    condition = _line(row.get("condition"))
    if condition is None or abs(condition - expected["line"]) > 1e-9:
        errors.append("line")
    if errors:
        raise BackfillError(
            f"row_identity_mismatch:{location}:{row_id}:{','.join(errors)}"
        )
    return {
        "location": location,
        "row_id": row_id,
        "match_id": expected["match_id"],
        "identity": {
            "home": row["home"], "away": row["away"],
            "kickoff": row["kickoff"], "market": row["code"],
            "side": row["side"], "line": row["condition"],
        },
        "before_sha256": _row_digest(row),
        "before": {field: copy.deepcopy(row.get(field)) for field in SETTLEMENT_FIELDS},
    }


def _already_applied(row: dict[str, Any], fixture: dict[str, Any]) -> bool:
    expected = fixture["_identity"]
    return (
        row.get("status") == "SETTLED"
        and row.get("result") == fixture["grade"]
        and row.get("score") == {
            "goals": f"{expected['home_score']}-{expected['away_score']}",
            "goals_total": expected["home_score"] + expected["away_score"],
        }
        and str(row.get("settlement_source") or "").startswith(SOURCE_PREFIX)
    )


def _plan(ledger: dict[str, Any], fixtures: list[dict[str, Any]]) -> dict[str, Any]:
    if not isinstance(ledger, dict):
        raise BackfillError("ledger_root_not_object")
    by_id = {fixture["_identity"]["match_id"]: fixture for fixture in fixtures}
    rows_by_fixture: dict[str, list[tuple[str, dict[str, Any]]]] = {
        match_id: [] for match_id in by_id
    }
    eligible = {
        id(row) for row in (
            condition_bets(ledger)
            + condition_observations(ledger)
            + research_bets(ledger)
        )
    }
    durable_ids: set[str] = set()
    for location, row in _ledger_rows(ledger):
        match_id = str(row.get("match_id") or "")
        if match_id not in by_id:
            continue
        if id(row) not in eligible:
            raise BackfillError(
                f"corresponding_row_not_settleable:{location}:{match_id}"
            )
        row_id = _row_id(row)
        if row_id in durable_ids:
            raise BackfillError(f"duplicate_durable_row_id:{row_id}")
        durable_ids.add(row_id)
        rows_by_fixture[match_id].append((location, row))

    targets: list[tuple[dict[str, Any], dict[str, Any], dict[str, Any]]] = []
    protected: list[dict[str, Any]] = []
    already: list[dict[str, Any]] = []
    for fixture in fixtures:
        match_id = fixture["_identity"]["match_id"]
        found = rows_by_fixture[match_id]
        if not found:
            raise BackfillError(f"fixture_has_no_corresponding_rows:{match_id}")
        for location, row in found:
            detail = _validate_row(location, row, fixture)
            if fixture["verification"] == "CONFLICT":
                detail["status"] = row.get("status")
                protected.append(detail)
            elif row.get("status") == "PENDING":
                targets.append((fixture, row, detail))
            elif _already_applied(row, fixture):
                detail["status"] = "ALREADY_APPLIED"
                already.append(detail)
            else:
                raise BackfillError(
                    f"confirmed_row_not_pending_or_idempotent:"
                    f"{location}:{detail['row_id']}:{row.get('status')}"
                )

    if len(targets) + len(already) != EXPECTED_CONFIRMED_ROWS:
        raise BackfillError(
            f"expected_{EXPECTED_CONFIRMED_ROWS}_confirmed_rows:"
            f"found={len(targets) + len(already)}"
        )
    if len(protected) != EXPECTED_CONFLICT_ROWS:
        raise BackfillError(
            f"expected_{EXPECTED_CONFLICT_ROWS}_conflict_rows:found={len(protected)}"
        )
    # A transaction is either the initial 66-row backfill or an exact no-op
    # rerun.  A partially changed cohort cannot be guessed/repaired.
    if (len(targets), len(already)) not in {
        (EXPECTED_CONFIRMED_ROWS, 0), (0, EXPECTED_CONFIRMED_ROWS),
    }:
        raise BackfillError(
            f"confirmed_cohort_partially_applied:"
            f"pending={len(targets)}:already={len(already)}"
        )
    return {"targets": targets, "protected": protected, "already": already}


def _apply_to_copy(
    ledger: dict[str, Any], plan: dict[str, Any], fixture_sha256: str,
    config: Settings,
) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    output = copy.deepcopy(ledger)
    source = f"{SOURCE_PREFIX}{fixture_sha256[:16]}"
    targets_by_id = {
        detail["row_id"]: fixture for fixture, _row, detail in plan["targets"]
    }
    changes: list[dict[str, Any]] = []
    for location, row in _ledger_rows(output):
        row_id_candidates = [
            str(row.get(field) or "") for field in
            ("bet_id", "observation_id", "research_id") if row.get(field)
        ]
        row_id = row_id_candidates[0] if len(row_id_candidates) == 1 else ""
        fixture = targets_by_id.get(row_id)
        if fixture is None:
            continue
        expected = fixture["_identity"]
        before = {field: copy.deepcopy(row.get(field)) for field in SETTLEMENT_FIELDS}
        if not _settle(row, {
            "home_score": expected["home_score"],
            "away_score": expected["away_score"],
        }, source):
            raise BackfillError(f"existing_settlement_function_rejected:{location}:{row_id}")
        if row.get("result") != fixture["grade"]:
            raise BackfillError(
                f"post_settlement_grade_mismatch:{location}:{row_id}:"
                f"{row.get('result')}:{fixture['grade']}"
            )
        changes.append({
            "location": location,
            "row_id": row_id,
            "match_id": expected["match_id"],
            "before": before,
            "after": {
                field: copy.deepcopy(row.get(field)) for field in SETTLEMENT_FIELDS
            },
            "after_sha256": _row_digest(row),
        })
    if len(changes) != len(plan["targets"]):
        raise BackfillError(
            f"target_rebind_count_mismatch:{len(changes)}:{len(plan['targets'])}"
        )
    if changes:
        recompute_stats(output, config)
    return output, changes


def _write_immutable_backup(
    ledger_path: Path, raw: bytes, digest: str,
) -> Path:
    directory = ledger_path.parent / "backups"
    directory.mkdir(parents=True, exist_ok=True)
    backup = directory / f"{ledger_path.name}.before-confirmed-34.{digest}.json"
    try:
        descriptor = os.open(
            backup, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o400,
        )
    except FileExistsError:
        metadata = backup.lstat()
        if (
            not stat.S_ISREG(metadata.st_mode)
            or stat.S_IMODE(metadata.st_mode) != stat.S_IRUSR
            or backup.read_bytes() != raw
        ):
            raise BackfillError(f"backup_hash_collision:{backup}")
        return backup
    try:
        with os.fdopen(descriptor, "wb") as handle:
            handle.write(raw)
            handle.flush()
            os.fsync(handle.fileno())
        backup.chmod(0o400)
        directory_fd = os.open(directory, os.O_RDONLY)
        try:
            os.fsync(directory_fd)
        finally:
            os.close(directory_fd)
    except Exception:
        # Keep any created backup for forensic inspection; never remove it.
        raise
    return backup


def execute(
    config: Settings,
    fixture_path: Path,
    *,
    apply: bool = False,
    expected_ledger_sha256: str | None = None,
    manifest_path: Path | None = None,
    expected_fixture_sha256: str = EXPECTED_FIXTURE_SHA256,
) -> dict[str, Any]:
    try:
        fixture_raw = fixture_path.read_bytes()
    except OSError as exc:
        raise BackfillError(f"fixture_file_unreadable:{fixture_path}:{exc}") from exc
    fixture_sha256 = _sha256(fixture_raw)
    if fixture_sha256 != expected_fixture_sha256:
        raise BackfillError(
            f"fixture_file_sha256_mismatch:expected={expected_fixture_sha256}:"
            f"actual={fixture_sha256}"
        )
    fixtures = _validate_fixtures(_strict_json_bytes(fixture_raw, fixture_path))
    mode = "apply" if apply else "dry-run"
    if apply and not re.fullmatch(r"[0-9a-f]{64}", expected_ledger_sha256 or ""):
        raise BackfillError("apply_requires_exact_expected_ledger_sha256")

    lock = settlement_lock(config) if apply else state_lock(config)
    with lock as acquired:
        if not acquired:
            raise BackfillError(
                "settlement_lock_busy" if apply else "state_lock_busy"
            )
        # Apply takes the broad settlement lock first, then the short state
        # lock, matching the normal settlement lock order.
        state_context = state_lock(config) if apply else None
        if state_context is None:
            result = _execute_locked(
                config, fixture_path, fixture_sha256, fixtures, apply,
                expected_ledger_sha256,
            )
        else:
            with state_context as state_acquired:
                if not state_acquired:
                    raise BackfillError("state_lock_busy")
                result = _execute_locked(
                    config, fixture_path, fixture_sha256, fixtures, apply,
                    expected_ledger_sha256,
                )

    if manifest_path is None:
        stamp = datetime.now(HKT).strftime("%Y%m%dT%H%M%S%z")
        manifest_path = (
            config.state_dir / "backfill-manifests"
            / f"confirmed-34-{stamp}-{mode}.json"
        )
    result["manifest_path"] = str(manifest_path)
    write_json_atomic(manifest_path, result)
    return result


def _execute_locked(
    config: Settings,
    fixture_path: Path,
    fixture_sha256: str,
    fixtures: list[dict[str, Any]],
    apply: bool,
    expected_ledger_sha256: str | None,
) -> dict[str, Any]:
    ledger_path = paths(config)["ledger"]
    try:
        ledger_raw = ledger_path.read_bytes()
    except OSError as exc:
        raise BackfillError(f"ledger_unreadable:{ledger_path}:{exc}") from exc
    ledger_sha256 = _sha256(ledger_raw)
    if apply and ledger_sha256 != expected_ledger_sha256:
        raise BackfillError(
            f"ledger_cas_mismatch:expected={expected_ledger_sha256}:"
            f"actual={ledger_sha256}"
        )
    ledger = _strict_json_bytes(ledger_raw, ledger_path)
    if _canonical_bytes(ledger) != ledger_raw:
        raise BackfillError(
            "ledger_not_in_canonical_atomic_writer_format:"
            "refuse_backup_or_cas_on_reformatted_state"
        )
    plan = _plan(ledger, fixtures)
    protected_before = {
        item["location"]: item["before_sha256"] for item in plan["protected"]
    }
    target_locations = {
        detail["location"] for _fixture, _row, detail in plan["targets"]
    }
    before_non_target = {
        location: _row_digest(row)
        for location, row in _ledger_rows(ledger)
        if location not in target_locations
    }
    after, changes = _apply_to_copy(ledger, plan, fixture_sha256, config)
    after_rows = {location: row for location, row in _ledger_rows(after)}
    for location, digest in before_non_target.items():
        if location not in after_rows or _row_digest(after_rows[location]) != digest:
            raise BackfillError(f"unrelated_or_protected_row_changed:{location}")
    protected_after = {
        location: _row_digest(after_rows[location]) for location in protected_before
    }
    if protected_before != protected_after:
        raise BackfillError("conflict_rows_changed")

    after_raw = _canonical_bytes(after)
    after_sha256 = _sha256(after_raw)
    backup_path: Path | None = None
    applied = False
    if apply and changes:
        backup_path = _write_immutable_backup(
            ledger_path, ledger_raw, ledger_sha256,
        )
        # Recheck immediately before atomic replacement even though state_lock
        # is held: this also catches non-cooperating writers.
        if _sha256(ledger_path.read_bytes()) != ledger_sha256:
            raise BackfillError("ledger_cas_mismatch_immediately_before_write")
        write_json_atomic(ledger_path, after)
        if ledger_path.read_bytes() != after_raw:
            raise BackfillError("post_write_ledger_verification_failed")
        applied = True
    elif apply and ledger_path.read_bytes() != ledger_raw:
        raise BackfillError("idempotent_noop_ledger_changed")

    fixture_rows = []
    for fixture in fixtures:
        fixture_rows.append({
            key: fixture.get(key) for key in (
                "No.", "match_id", "kickoff_HKT", "home", "away", "bet",
                "home_score_90", "away_score_90", "grade", "verification",
            )
        })
    return {
        "schema_version": 1,
        "operation": "crown-confirmed-34-result-backfill",
        "mode": "apply" if apply else "dry-run",
        "safe_to_apply": True,
        "applied": applied,
        "idempotent_noop": not changes,
        "created_at_hkt": datetime.now(HKT).isoformat(timespec="seconds"),
        "fixture_file": str(fixture_path),
        "fixture_file_sha256": fixture_sha256,
        "fixture_identity_sha256": _identity_digest(fixtures),
        "ledger_path": str(ledger_path),
        "ledger_before_sha256": ledger_sha256,
        "ledger_after_sha256": after_sha256,
        "backup_path": str(backup_path) if backup_path else None,
        "counts": {
            "fixtures": len(fixtures),
            "confirmed_fixtures": EXPECTED_CONFIRMED_FIXTURES,
            "conflict_fixtures": EXPECTED_CONFLICT_FIXTURES,
            "confirmed_rows": len(plan["targets"]) + len(plan["already"]),
            "pending_rows_before": len(plan["targets"]),
            "already_applied_rows": len(plan["already"]),
            "changed_rows": len(changes),
            "conflict_rows_unchanged": len(plan["protected"]),
        },
        "fixtures": fixture_rows,
        "changes": changes,
        "already_applied": plan["already"],
        "protected_conflict_rows": plan["protected"],
        "apply_guard": {
            "required_argument": "--expected-ledger-sha256",
            "value": ledger_sha256,
        },
        "side_effects": {
            "provider_calls": 0,
            "telegram_calls": 0,
            "dashboard_publications": 0,
        },
    }


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Dry-run/apply the verified 34-fixture Crown result backfill",
    )
    parser.add_argument("--fixtures", required=True, type=Path)
    parser.add_argument("--state-dir", type=Path)
    parser.add_argument("--manifest", type=Path)
    parser.add_argument("--apply", action="store_true")
    parser.add_argument("--expected-ledger-sha256")
    arguments = parser.parse_args()
    config = settings()
    if arguments.state_dir is not None:
        config = replace(config, state_dir=arguments.state_dir)
    try:
        result = execute(
            config,
            arguments.fixtures,
            apply=arguments.apply,
            expected_ledger_sha256=arguments.expected_ledger_sha256,
            manifest_path=arguments.manifest,
        )
    except BackfillError as exc:
        raise SystemExit(f"REFUSED: {exc}") from exc
    print(json.dumps({
        key: result[key] for key in (
            "mode", "safe_to_apply", "applied", "idempotent_noop",
            "ledger_before_sha256", "ledger_after_sha256", "backup_path",
            "manifest_path", "counts", "side_effects",
        )
    }, ensure_ascii=False, sort_keys=True))


if __name__ == "__main__":
    main()
