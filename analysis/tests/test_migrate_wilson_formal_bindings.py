from __future__ import annotations

import copy
import fcntl
import json
import os
import tempfile
import unittest
from datetime import datetime, timedelta
from pathlib import Path
from unittest.mock import patch

from analysis.migrate_wilson_formal_bindings import (
    CONFIRMATION,
    MIGRATION_AUDIT_KEY,
    migrate_file,
    migrate_legacy_formal_bindings,
)
from analysis.tests.test_wilson_validation import candidate, selected
from analysis.wilson_validation import (
    _project_pending_rollover_rows,
    _prove_explicit_rollover_batch,
    _version_hash,
    apply_active_evidence,
    choose_admission,
    commit_bet,
    recompute_namespace,
    validate_formal_row,
)

NOW = "2026-08-27T09:37:00+08:00"


def _settled_row(
    case: unittest.TestCase, ledger: dict, index: int, *,
    system: str = "footbreak", result: str = "Won",
) -> dict:
    stage_at = f"2026-08-20T00:{index:02d}:00+08:00"
    seed, reason = choose_admission(
        system, "HDC", selected(), [candidate(system=system)],
        stage_at=stage_at,
    )
    case.assertEqual(reason, "wilson_pass")
    assert seed is not None
    admission, reason = apply_active_evidence(
        ledger, system, seed, stage_at=stage_at, now=stage_at,
    )
    case.assertIsNone(reason)
    assert admission is not None
    row = commit_bet(
        ledger, system,
        {
            "match_id": f"migration-fixture-{index}", "league": "測試",
            "home": "主", "away": "客",
            "kickoff": (
                datetime.fromisoformat(stage_at) + timedelta(hours=1)
            ).isoformat(),
        },
        "HDC", selected(), admission, now=stage_at,
        market_label="讓球", selected_label="讓球", selected_role="主讓",
        selected_line=-.25,
    )
    assert row is not None
    row.update({
        "status": "SETTLED", "result": result,
        "pnl": 450 if result == "Won" else -500,
        "settled_at": (
            datetime.fromisoformat(stage_at) + timedelta(hours=2)
        ).isoformat(),
    })
    ledger["bets"].append(row)
    return row


def _legacy_ledger(case: unittest.TestCase, system: str = "footbreak") -> dict:
    ledger: dict = {"bets": []}
    rows = [
        _settled_row(
            case, ledger, index, system=system,
            result="Won" if index <= 4 else "Lost",
        )
        for index in range(1, 8)
    ]
    recompute_namespace(ledger, system)
    rows[0]["frozen_condition_definition"] = {}
    rows[1].pop("native_stage_at")
    rows[2]["frozen_condition_definition"] = {}
    rows[2].pop("native_stage_at")
    return ledger


class FormalBindingMigrationTest(unittest.TestCase):
    def test_missing_condition_projection_preserves_durable_progress(self):
        detail = _project_pending_rollover_rows(
            {
                "bets": [],
                "wilson_validation": {
                    "system": "footbreak", "conditions": {},
                },
            },
            "footbreak", "missing", {"evidence_hash": "x"},
            {
                "eligible_decided": 7, "eligible_hits": 4, "required": 20,
                "excluded": {},
            },
        )
        self.assertEqual(
            (
                detail["expected_decided"], detail["expected_hits"],
                detail["required"],
            ),
            (7, 4, 20),
        )
        self.assertFalse(detail["complete"])

    def test_dry_run_apply_and_idempotency(self):
        ledger = _legacy_ledger(self)
        original = copy.deepcopy(ledger)
        dry = migrate_legacy_formal_bindings(
            ledger, "footbreak", now=NOW, apply=False,
        )
        self.assertEqual(dry["status"], "ready")
        self.assertEqual(dry["repair_count"], 3)
        self.assertEqual(ledger, original)

        applied = migrate_legacy_formal_bindings(
            ledger, "footbreak", now=NOW, apply=True,
        )
        self.assertEqual(applied["status"], "applied")
        self.assertEqual(applied["repair_count"], 3)
        frozen = next(iter(ledger["wilson_validation"]["conditions"].values()))
        for row in ledger["bets"]:
            admitted, reason = validate_formal_row(
                row, system="footbreak",
                signature=row["frozen_condition_signature"], frozen=frozen,
                projection_time=datetime.fromisoformat(NOW),
                require_settled=True, ledger=ledger,
            )
            self.assertIsNotNone(admitted, reason)
        audit = ledger["wilson_validation"][MIGRATION_AUDIT_KEY]
        self.assertEqual(audit["repair_count"], 3)
        self.assertEqual(
            audit["pending_proofs"][frozen["signature"]]["expected_decided"], 7,
        )
        after = json.dumps(ledger, ensure_ascii=False, sort_keys=True)
        second = migrate_legacy_formal_bindings(
            ledger, "footbreak", now="2026-08-28T00:00:00+08:00", apply=True,
        )
        self.assertEqual(second["status"], "already_applied")
        self.assertEqual(json.dumps(ledger, ensure_ascii=False, sort_keys=True), after)

    def test_explicit_null_and_second_defect_fail_without_mutation(self):
        for mutate in (
            lambda row: row.__setitem__("native_stage_at", None),
            lambda row: row.__setitem__("odds", 9.99),
        ):
            ledger = _legacy_ledger(self)
            mutate(ledger["bets"][1])
            before = copy.deepcopy(ledger)
            with self.assertRaises(ValueError):
                migrate_legacy_formal_bindings(
                    ledger, "footbreak", now=NOW, apply=True,
                )
            self.assertEqual(ledger, before)

    def test_pending_tamper_fails_closed(self):
        ledger = _legacy_ledger(self)
        frozen = next(iter(ledger["wilson_validation"]["conditions"].values()))
        frozen["pending_rollover_progress"]["eligible_hits"] = 3
        before = copy.deepcopy(ledger)
        with self.assertRaisesRegex(ValueError, "pending_preproof_failed"):
            migrate_legacy_formal_bindings(
                ledger, "footbreak", now=NOW, apply=True,
            )
        self.assertEqual(ledger, before)

    def test_identity_bearing_merged_batch_is_proved_without_rewrite(self):
        ledger: dict = {"bets": []}
        rows = [
            _settled_row(
                self, ledger, index,
                result="Won" if index <= 13 else "Lost",
            )
            for index in range(1, 21)
        ]
        recompute_namespace(ledger, "footbreak")
        frozen = next(iter(ledger["wilson_validation"]["conditions"].values()))
        versions_before = copy.deepcopy(frozen["evidence_versions"])
        rows[0]["frozen_condition_definition"] = {}
        rows[1].pop("native_stage_at")

        result = migrate_legacy_formal_bindings(
            ledger, "footbreak", now=NOW, apply=True,
        )

        self.assertEqual(result["status"], "applied")
        migrated_frozen = next(
            iter(ledger["wilson_validation"]["conditions"].values()),
        )
        self.assertEqual(migrated_frozen["evidence_versions"], versions_before)
        proof = result["audit"]["merged_batch_proofs"]
        self.assertEqual(len(proof), 1)
        self.assertEqual(
            (proof[0]["batch_decided"], proof[0]["batch_hits"]), (20, 13),
        )

    def test_cross_version_batch_identity_reuse_is_rejected(self):
        ledger: dict = {"bets": []}
        rows = [
            _settled_row(self, ledger, index, result="Lost")
            for index in range(1, 41)
        ]
        recompute_namespace(ledger, "footbreak")
        frozen = next(iter(ledger["wilson_validation"]["conditions"].values()))
        version_two, version_three = frozen["evidence_versions"][1:3]
        original_three = list(version_three["batch_fixture_market_hashes"])
        version_three["batch_fixture_market_hashes"] = [
            *version_two["batch_fixture_market_hashes"][:19],
            original_three[-1],
        ]
        version_three["evidence_hash"] = _version_hash(version_three)
        frozen["active_evidence_hash"] = version_three["evidence_hash"]
        frozen["active_evidence"]["evidence_hash"] = version_three["evidence_hash"]
        rows[19]["frozen_condition_definition"] = {}
        before = copy.deepcopy(ledger)

        with self.assertRaisesRegex(
            ValueError, "cross_version_batch_identity_reuse",
        ):
            migrate_legacy_formal_bindings(
                ledger, "footbreak", now=NOW, apply=False,
            )
        self.assertEqual(ledger, before)

    def test_batch_rows_must_follow_predecessor_activation_boundary(self):
        ledger: dict = {"bets": []}
        rows = [
            _settled_row(self, ledger, index, result="Lost")
            for index in range(1, 41)
        ]
        recompute_namespace(ledger, "footbreak")
        frozen = next(iter(ledger["wilson_validation"]["conditions"].values()))
        version_two, version_three = frozen["evidence_versions"][1:3]
        version_two["activation_boundary_at"] = rows[29][
            "rollover_provenance"
        ]["stage_at"]
        version_two["evidence_hash"] = _version_hash(version_two)
        version_three["prior_evidence_hash"] = version_two["evidence_hash"]
        version_three["evidence_hash"] = _version_hash(version_three)
        frozen["active_evidence_hash"] = version_three["evidence_hash"]
        frozen["active_evidence"]["evidence_hash"] = version_three["evidence_hash"]

        proof = _prove_explicit_rollover_batch(
            ledger, "footbreak", frozen["signature"], frozen, version_three,
            projection_time=datetime.fromisoformat(NOW),
        )

        self.assertFalse(proof["complete"])
        self.assertEqual(
            proof["reason"], "batch_row_not_after_predecessor_boundary",
        )

    def test_malformed_unclaimed_same_signature_row_blocks_migration(self):
        ledger = _legacy_ledger(self)
        malformed = copy.deepcopy(ledger["bets"][3])
        malformed["portfolio"] = "unclaimed-but-same-signature"
        malformed["bet_id"] = "malformed-unclaimed"
        ledger["bets"].append(malformed)
        before = copy.deepcopy(ledger)

        with self.assertRaisesRegex(
            ValueError, "unrepairable_same_signature_activity",
        ):
            migrate_legacy_formal_bindings(
                ledger, "footbreak", now=NOW, apply=False,
            )
        self.assertEqual(ledger, before)

    def test_explicit_rollover_success_is_required_after_recompute(self):
        ledger = _legacy_ledger(self)
        before = copy.deepcopy(ledger)
        with patch(
            "analysis.migrate_wilson_formal_bindings._rollover_condition",
            return_value=False,
        ):
            with self.assertRaisesRegex(
                ValueError, "ordinary_rollover_validation_failed",
            ):
                migrate_legacy_formal_bindings(
                    ledger, "footbreak", now=NOW, apply=False,
                )
        self.assertEqual(ledger, before)

    def test_apply_requires_exact_confirmation_and_is_idempotent_on_disk(self):
        ledger = _legacy_ledger(self)
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "ledger.json"
            lock = Path(directory) / "ledger.lock"
            path.write_text(json.dumps(ledger), encoding="utf-8")
            original = path.read_bytes()
            with self.assertRaisesRegex(ValueError, "confirmation"):
                migrate_file(
                    path, "footbreak", lock_path=lock, now=NOW,
                    apply=True, confirmation="wrong",
                )
            self.assertEqual(path.read_bytes(), original)
            result = migrate_file(
                path, "footbreak", lock_path=lock, now=NOW,
                apply=True, confirmation=CONFIRMATION,
            )
            self.assertEqual(result["status"], "applied")
            applied = path.read_bytes()
            result = migrate_file(
                path, "footbreak", lock_path=lock, now=NOW,
                apply=True, confirmation=CONFIRMATION,
            )
            self.assertEqual(result["status"], "already_applied")
            self.assertEqual(path.read_bytes(), applied)

    def test_replace_failure_leaves_original_file(self):
        ledger = _legacy_ledger(self)
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "ledger.json"
            lock = Path(directory) / "ledger.lock"
            path.write_text(json.dumps(ledger), encoding="utf-8")
            original = path.read_bytes()
            with patch(
                "analysis.migrate_wilson_formal_bindings.os.replace",
                side_effect=OSError("simulated replace failure"),
            ):
                with self.assertRaises(OSError):
                    migrate_file(
                        path, "footbreak", lock_path=lock, now=NOW,
                        apply=True, confirmation=CONFIRMATION,
                    )
            self.assertEqual(path.read_bytes(), original)

    def test_directory_fsync_failure_returns_committed_warning(self):
        ledger = _legacy_ledger(self)
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "ledger.json"
            lock = Path(directory) / "ledger.lock"
            path.write_text(json.dumps(ledger), encoding="utf-8")
            calls = {"count": 0}
            real_fsync = os.fsync

            def fail_directory_fsync(descriptor):
                calls["count"] += 1
                if calls["count"] == 2:
                    raise OSError("simulated directory fsync failure")
                return real_fsync(descriptor)

            with patch(
                "analysis.migrate_wilson_formal_bindings.os.fsync",
                side_effect=fail_directory_fsync,
            ):
                result = migrate_file(
                    path, "footbreak", lock_path=lock, now=NOW,
                    apply=True, confirmation=CONFIRMATION,
                )

            self.assertEqual(result["status"], "applied")
            self.assertRegex(
                result["durability_warnings"][0],
                "parent_directory_fsync_failed",
            )
            persisted = json.loads(path.read_text(encoding="utf-8"))
            self.assertIn(
                MIGRATION_AUDIT_KEY, persisted["wilson_validation"],
            )

    def test_lock_contention_prevents_reload_and_write(self):
        ledger = _legacy_ledger(self)
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "ledger.json"
            lock_path = Path(directory) / "ledger.lock"
            path.write_text(json.dumps(ledger), encoding="utf-8")
            original = path.read_bytes()
            with lock_path.open("a+") as held:
                fcntl.flock(held.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                try:
                    with self.assertRaisesRegex(
                        RuntimeError, "migration_lock_unavailable",
                    ):
                        migrate_file(
                            path, "footbreak", lock_path=lock_path, now=NOW,
                            apply=True, confirmation=CONFIRMATION,
                        )
                finally:
                    fcntl.flock(held.fileno(), fcntl.LOCK_UN)
            self.assertEqual(path.read_bytes(), original)


if __name__ == "__main__":
    unittest.main()
