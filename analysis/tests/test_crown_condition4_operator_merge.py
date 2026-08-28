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


class CrownCondition4OperatorTransactionTests(unittest.TestCase):
    def test_post_write_verification_failure_restores_exact_original_bytes(
        self,
    ) -> None:
        ledger = ledger_fixture()
        replay = replay_fixture(ledger)
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
