from __future__ import annotations

import copy
import json
import tempfile
import unittest
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from analysis import crown_condition4_operator_merge as operator
from analysis.tests.test_crown_condition4_recovery import (
    ledger_fixture,
    rehash_candidate,
    replay_fixture,
    sync_replay_projections,
)
from crown.config import settings


def replay_with_unresolved_verified_results(ledger: dict) -> dict:
    replay = replay_fixture(ledger)
    rows = replay["matching_fixtures"]
    for index, spec in enumerate(operator._VERIFIED_RESULT_SPECS):
        row = rows[index]
        row["home"] = spec["home"][0]
        row["away"] = spec["away"][0]
        home_score, away_score = (
            int(value) for value in str(spec["score"]).split("-")
        )
        should_hit = index < 9
        if should_hit:
            side = "H" if home_score >= away_score else "A"
            line = 0.25 if home_score == away_score else -0.25
        else:
            side = "A" if home_score > away_score else "H"
            line = -0.25
        row["selected_side"] = side
        row["selected_line"] = line
        row["selected_line_path"][-1] = line
        row.update({
            "score": None,
            "hdc_grade": None,
            "result_known": False,
            "result_source": None,
            "result_status": None,
        })
        rehash_candidate(row)
    sync_replay_projections(replay)
    replay["summary"] = operator._replay_summary(rows)
    return replay


class CrownCondition4VerifiedResultOverlayTests(unittest.TestCase):
    def setUp(self) -> None:
        self.ledger = ledger_fixture()
        self.replay = replay_with_unresolved_verified_results(self.ledger)

    def test_all_fifteen_scores_overlay_and_leave_only_atlanta_pending(self) -> None:
        original = copy.deepcopy(self.replay)
        overlaid = operator._apply_operator_verified_result_overlay(self.replay)

        self.assertEqual(self.replay, original)
        verified = [
            row for row in overlaid["matching_fixtures"]
            if row.get("result_source") == operator.OPERATOR_RESULT_SOURCE
        ]
        self.assertEqual(len(verified), 15)
        self.assertEqual(len({row["match_id"] for row in verified}), 15)
        self.assertTrue(all(row["result_known"] is True for row in verified))
        self.assertTrue(all(row["result_status"] == "SETTLED" for row in verified))
        self.assertTrue(all(
            row["replay_candidate_hash"] == operator._candidate_hash(row)
            for row in verified
        ))
        self.assertTrue(all(
            row["hdc_grade"]["result"]
            == ("Won" if row["hdc_grade"]["hit"] else "Lost")
            for row in verified
        ))

        settled = [
            row for row in overlaid["matching_fixtures"]
            if row.get("result_known") is True
        ]
        self.assertEqual(len(settled), 39)
        self.assertEqual(
            sum(operator.recovery._score_hit(row) for row in settled), 21
        )
        self.assertEqual(overlaid["summary"]["unknown_result_fixture_count"], 1)
        self.assertEqual(len(overlaid["unknown_result_fixtures"]), 1)
        self.assertTrue(operator.recovery._pending_fixture(
            overlaid["unknown_result_fixtures"][0]
        ))

        report, _proposed, _signature, _binding = operator.plan_operator_merge(
            self.ledger, overlaid
        )
        self.assertEqual(report["changes"], {
            "added": 40,
            "settled": 39,
            "hits": 21,
            "non_hits": 18,
            "pending": 1,
            "deleted": 0,
        })
        self.assertEqual(report["final"], {
            "observations": 121,
            "decided": 120,
            "hits": 73,
            "pending": 1,
        })

    def test_overlay_does_not_change_unrelated_rows_or_replay_metadata(self) -> None:
        original = copy.deepcopy(self.replay)
        overlaid = operator._apply_operator_verified_result_overlay(self.replay)

        self.assertEqual(
            overlaid["matching_fixtures"][15:],
            original["matching_fixtures"][15:],
        )
        projected_keys = {
            "matching_fixtures", "missing_formal_fixtures",
            "unknown_result_fixtures", "summary",
        }
        self.assertEqual(
            {key: value for key, value in overlaid.items()
             if key not in projected_keys},
            {key: value for key, value in original.items()
             if key not in projected_keys},
        )

    def test_missing_verified_fixture_fails_closed(self) -> None:
        replay = copy.deepcopy(self.replay)
        replay["matching_fixtures"][0]["home"] = "Different Team"
        rehash_candidate(replay["matching_fixtures"][0])
        sync_replay_projections(replay)
        replay["summary"] = operator._replay_summary(replay["matching_fixtures"])
        with self.assertRaisesRegex(
            operator.OperatorMergeBlocked,
            "verified_result_overlay_spec_match_not_unique",
        ):
            operator._apply_operator_verified_result_overlay(replay)

    def test_ambiguous_verified_fixture_fails_closed(self) -> None:
        replay = copy.deepcopy(self.replay)
        replay["matching_fixtures"][1]["home"] = replay["matching_fixtures"][0]["home"]
        replay["matching_fixtures"][1]["away"] = replay["matching_fixtures"][0]["away"]
        rehash_candidate(replay["matching_fixtures"][1])
        sync_replay_projections(replay)
        replay["summary"] = operator._replay_summary(replay["matching_fixtures"])
        with self.assertRaisesRegex(
            operator.OperatorMergeBlocked,
            "verified_result_overlay_spec_match_not_unique",
        ):
            operator._apply_operator_verified_result_overlay(replay)

    def test_duplicate_candidate_identity_fails_closed(self) -> None:
        replay = copy.deepcopy(self.replay)
        replay["matching_fixtures"][1]["match_id"] = (
            replay["matching_fixtures"][0]["match_id"]
        )
        rehash_candidate(replay["matching_fixtures"][1])
        sync_replay_projections(replay)
        replay["summary"] = operator._replay_summary(replay["matching_fixtures"])
        with self.assertRaisesRegex(
            operator.OperatorMergeBlocked,
            "verified_result_overlay_duplicate_candidate",
        ):
            operator._apply_operator_verified_result_overlay(replay)


class CrownCondition4OperatorPlanTests(unittest.TestCase):
    def setUp(self) -> None:
        self.ledger = ledger_fixture()
        self.replay = replay_fixture(self.ledger)

    def test_exact_approved_cohort_reaches_only_the_confirmed_totals(self) -> None:
        report, proposed, signature, binding = operator.plan_operator_merge(
            self.ledger, self.replay
        )
        self.assertEqual(
            report["changes"],
            {
                "added": 40,
                "settled": 39,
                "hits": 21,
                "non_hits": 18,
                "pending": 1,
                "deleted": 0,
            },
        )
        self.assertEqual(report["final"], operator.EXPECTED_FINAL)
        operator.verify_final_ledger(
            proposed, signature=signature, binding=binding
        )
        pending = [
            row
            for row in proposed["wilson_validation"]["observations"]
            if row.get("status") == "PENDING"
        ]
        self.assertEqual(len(pending), 1)
        self.assertTrue(operator.recovery._pending_fixture(pending[0]))
        self.assertNotIn("authority_payload_sha256",
                         pending[0]["recovered_missing_observation"])

    def test_plan_is_deterministic_for_identical_ledger_and_replay(self) -> None:
        first = operator.plan_operator_merge(self.ledger, self.replay)
        second = operator.plan_operator_merge(self.ledger, self.replay)
        self.assertEqual(first, second)

    def test_any_candidate_identity_already_in_ledger_fails_closed(self) -> None:
        _report, proposed, _signature, _binding = operator.plan_operator_merge(
            self.ledger, self.replay
        )
        with self.assertRaisesRegex(
            operator.OperatorMergeBlocked,
            "candidate_fixture_market_already_exists",
        ):
            operator.plan_operator_merge(proposed, self.replay)

    def test_approved_hit_count_mismatch_fails_closed(self) -> None:
        replay = copy.deepcopy(self.replay)
        row = replay["matching_fixtures"][0]
        row["score"] = "0-1"
        row["hdc_grade"] = {
            "grade_status": "GRADED", "hit": False, "result": "Lost"
        }
        rehash_candidate(row)
        sync_replay_projections(replay)
        with self.assertRaisesRegex(
            operator.OperatorMergeBlocked, "approved_replay_count_mismatch"
        ):
            operator.plan_operator_merge(self.ledger, replay)

    def test_replay_manifest_mismatch_fails_closed(self) -> None:
        replay = copy.deepcopy(self.replay)
        replay["summary"]["recorded_expected_fixture_count"] = 1
        with self.assertRaisesRegex(
            operator.OperatorMergeBlocked, "approved_replay_manifest_mismatch"
        ):
            operator.plan_operator_merge(self.ledger, replay)

    def test_known_production_legacy_rejections_are_preserved_not_repaired(
        self,
    ) -> None:
        ledger = copy.deepcopy(self.ledger)
        namespace = ledger["wilson_validation"]
        condition_one = next(
            row for row in namespace["conditions"].values()
            if row["condition_number"] == 1
        )
        existing = {
            "bet_id": "legacy-condition-one-row",
            "match_id": "legacy-fixture",
            "market": "HDC",
            "score": "2-1",
            "result": "Won",
            "status": "SETTLED",
            "frozen_condition_signature": condition_one["signature"],
        }
        ledger["bets"].append(copy.deepcopy(existing))
        replay = replay_fixture(ledger)
        original = copy.deepcopy(ledger)
        real_build_manifest = operator.build_manifest

        def production_reason_shaped_manifest(
            value: dict, system: str,
        ) -> dict:
            manifest = real_build_manifest(value, system)
            by_number = {
                row["condition_number"]: row
                for row in manifest["conditions"]
                if isinstance(row, dict)
                and isinstance(row.get("condition_number"), int)
            }
            for number, reasons in operator.LEGACY_REJECTION_FINGERPRINT[
                "conditions"
            ].items():
                row = by_number.get(number)
                if row is None:
                    row = {"condition_number": number}
                    manifest["conditions"].append(row)
                row["valid"] = False
                row["rejection_reasons"] = list(reasons)
            condition_four = by_number[4]
            condition_four["valid"] = True
            condition_four["rejection_reasons"] = []
            manifest["valid"] = False
            manifest["rejection_reasons"] = copy.deepcopy(
                operator.LEGACY_REJECTION_FINGERPRINT["rejection_reasons"]
            )
            return manifest

        unrelated_before = {
            key: copy.deepcopy(value)
            for key, value in namespace["conditions"].items()
            if value["condition_number"] != 4
        }
        with patch.object(
            operator,
            "build_manifest",
            side_effect=production_reason_shaped_manifest,
        ):
            report, proposed, signature, binding = operator.plan_operator_merge(
                ledger, replay
            )
            operator.verify_final_ledger(
                proposed, signature=signature, binding=binding
            )

        self.assertEqual(ledger, original)
        self.assertEqual(proposed["bets"], original["bets"])
        self.assertEqual(proposed["bets"][0], existing)
        self.assertEqual(
            proposed["wilson_validation"]["observations"][
                :len(namespace.get("observations") or [])
            ],
            original["wilson_validation"].get("observations") or [],
        )
        self.assertEqual(
            {
                key: value
                for key, value in proposed["wilson_validation"][
                    "conditions"
                ].items()
                if key != signature
            },
            unrelated_before,
        )
        self.assertEqual(
            len(proposed["wilson_validation"]["observations"]),
            len(original["wilson_validation"].get("observations") or []) + 40,
        )
        self.assertEqual(report["final"], {
            "observations": 121,
            "decided": 120,
            "hits": 73,
            "pending": 1,
        })
        self.assertTrue(
            report["safety"]["legacy_rejection_fingerprint_preserved"]
        )

    def test_any_change_to_legacy_rejection_fingerprint_fails_closed(self) -> None:
        manifest = operator.build_manifest(self.ledger, "crown")
        manifest["valid"] = False
        manifest["rejection_reasons"] = {
            **operator.LEGACY_REJECTION_FINGERPRINT["rejection_reasons"],
            "evidence_hash_drift": 1,
        }
        for number, reasons in operator.LEGACY_REJECTION_FINGERPRINT[
            "conditions"
        ].items():
            manifest["conditions"].append({
                "condition_number": number,
                "valid": False,
                "rejection_reasons": list(reasons),
            })
        with (
            patch.object(operator, "build_manifest", return_value=manifest),
            self.assertRaisesRegex(
                operator.OperatorMergeBlocked,
                "input_ledger_strict_manifest_invalid",
            ),
        ):
            operator.plan_operator_merge(self.ledger, self.replay)


class CrownCondition4OperatorTransactionTests(unittest.TestCase):
    def test_post_write_verification_failure_restores_exact_original_bytes(
        self,
    ) -> None:
        ledger = ledger_fixture()
        replay = replay_with_unresolved_verified_results(ledger)
        with tempfile.TemporaryDirectory() as directory:
            state_dir = Path(directory)
            ledger_path = state_dir / "ledger.json"
            original = (
                json.dumps(ledger, ensure_ascii=False, indent=2, sort_keys=True)
                + "\n"
            ).encode()
            ledger_path.write_bytes(original)
            (state_dir / operator.HISTORY_NAME).write_text(
                '{"rows":[]}\n', encoding="utf-8"
            )
            config = replace(settings(), state_dir=state_dir)
            replay_module = SimpleNamespace(
                replay=lambda *_args, **_kwargs: copy.deepcopy(replay)
            )
            real_verify = operator.verify_final_ledger
            calls = 0

            def fail_second(
                value: dict, *, signature: str, binding: str
            ) -> None:
                nonlocal calls
                calls += 1
                if calls == 2:
                    raise operator.OperatorMergeBlocked("injected")
                real_verify(value, signature=signature, binding=binding)

            def test_backup(path: Path, raw: bytes) -> Path:
                backup = path.with_name(
                    "ledger.json.condition4.20260828T022800.000000Z.bak"
                )
                backup.write_bytes(raw)
                return backup

            with (
                patch.object(operator, "LEDGER_PATH", ledger_path),
                patch.object(operator, "_load_replay_module",
                             return_value=replay_module),
                patch.object(operator.os, "geteuid", return_value=0),
                patch.object(operator, "_create_root_backup",
                             side_effect=test_backup),
                patch.object(operator, "verify_final_ledger",
                             side_effect=fail_second),
            ):
                with self.assertRaisesRegex(
                    operator.PostWriteVerificationFailure,
                    "rollback=verified",
                ):
                    operator.apply_operator_merge(
                        config, state_dir / "replay.py"
                    )
            self.assertEqual(ledger_path.read_bytes(), original)
            backups = list(state_dir.glob("ledger.json.condition4.*.bak"))
            self.assertEqual(len(backups), 1)
            self.assertEqual(backups[0].read_bytes(), original)


if __name__ == "__main__":
    unittest.main()
