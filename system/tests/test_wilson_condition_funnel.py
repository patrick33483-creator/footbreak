"""Read-only contract tests for the Footbreak Wilson per-condition funnel."""
from __future__ import annotations

import copy
import unittest

from analysis.wilson_validation import project_condition_funnel


def marker(signature: str, fixture_hash: str, stage_at: str) -> dict:
    return {
        "schema_version": 1,
        "system": "footbreak",
        "condition_signature": signature,
        "native_pre_kickoff_t5": True,
        "stage_at": stage_at,
        "fixture_market_hash": fixture_hash,
    }


def formal_row(
    signature: str, identity: str, fixture_hash: str, *,
    result: str | None = None, status: str = "PENDING",
    observation: bool = False,
) -> dict:
    row = {
        "bet_id": None if observation else identity,
        "observation_id": identity if observation else None,
        "portfolio": (
            "footbreak_wilson_observations"
            if observation else "footbreak_wilson_test"
        ),
        "strategy": "wilson-test-strategy-v1",
        "formal_bet": False if observation else True,
        "frozen_condition_signature": signature,
        "stage": "T-5",
        "first_native_pre_kickoff_t5": True,
        "status": status,
        "result": result,
        "rollover_provenance": marker(
            signature, fixture_hash, "2026-08-22T10:00:00+08:00"
        ),
    }
    return row


class WilsonConditionFunnelTests(unittest.TestCase):
    def setUp(self) -> None:
        self.signature = "a" * 24
        self.other_signature = "b" * 24
        self.valid_hash = "1" * 64
        duplicate_hash = "2" * 64
        self.ledger = {
            "bets": [
                formal_row(
                    self.signature, "valid-settlement", self.valid_hash,
                    result="Won", status="SETTLED",
                ),
                formal_row(
                    self.signature, "pending-bet", "3" * 64,
                    status="PENDING",
                ),
                formal_row(
                    self.signature, "duplicate-a", duplicate_hash,
                    result="Lost", status="SETTLED",
                ),
                formal_row(
                    self.signature, "duplicate-b", duplicate_hash,
                    result="Won", status="SETTLED",
                ),
                # Retired/legacy rows and another condition must never leak in.
                {
                    "bet_id": "legacy",
                    "portfolio": "footbreak_independent_validation",
                    "strategy": "independent-validation-v1",
                    "frozen_condition_signature": self.signature,
                    "status": "SETTLED",
                    "result": "Won",
                },
            ],
            "wilson_validation": {
                "schema_version": 2,
                "system": "footbreak",
                "activation_at": "2026-08-20T00:00:00+08:00",
                # Deliberately not signature sort order.
                "condition_order": [self.other_signature, self.signature],
                "conditions": {
                    self.signature: {
                        "condition_number": 7,
                        "frozen_at": "2026-08-20T01:00:00+08:00",
                        "definition": {
                            "version": "granular-condition-v1",
                            "system": "footbreak",
                            "market": "HDC",
                            "stage": "T-5",
                            "path": "首預→T-30→T-5",
                            "role": "主讓",
                            "miner_key": ["system=footbreak", "market=HDC"],
                        },
                        "evidence_versions": [{
                            "version": 3,
                            "condition_signature": self.signature,
                            "evidence_hash": "e" * 64,
                            "cumulative_hits": 51,
                            "cumulative_decided": 80,
                            "activation_boundary_at": "2026-08-20T01:00:00+08:00",
                        }],
                        "pending_rollover_progress": {
                            "eligible_decided": 7,
                            "eligible_hits": 5,
                            "required": 20,
                            "display": "7/20",
                            "excluded": {},
                        },
                    },
                    self.other_signature: {
                        "condition_number": 2,
                        "definition": {
                            "version": "granular-condition-v1",
                            "system": "footbreak",
                            "market": "HIL",
                            "stage": "T-5",
                        },
                        # Missing baseline and inconsistent persisted progress
                        # must remain unavailable rather than be repaired.
                        "evidence_versions": [],
                        "pending_rollover_progress": {
                            "eligible_decided": 4,
                            "required": 20,
                            "display": "5/20",
                        },
                    },
                },
                "observations": [
                    formal_row(
                        self.signature, "refunded-observation", "4" * 64,
                        result="Refunded", status="SETTLED", observation=True,
                    ),
                ],
                "audit": [
                    {
                        "ts": "2026-08-22T10:00:00+08:00",
                        "match_id": "fixture-1",
                        "market": "HDC",
                        "status": "CREATED",
                        "reason": "wilson_candidate_frozen",
                        "frozen_condition_signature": self.signature,
                    },
                    # Replay is a rejection but not a second exact match.
                    {
                        "ts": "2026-08-22T10:01:00+08:00",
                        "match_id": "fixture-1",
                        "market": "HDC",
                        "status": "SKIPPED",
                        "reason": "idempotent_existing_market",
                        "frozen_condition_signature": self.signature,
                    },
                    {
                        "ts": "2026-08-23T10:00:00+08:00",
                        "match_id": "fixture-2",
                        "market": "HDC",
                        "status": "MATCHED_NO_BET",
                        "reason": "wilson_gate_not_passed",
                        "frozen_condition_signature": self.signature,
                    },
                    # A global, unattributed T-5 rejection cannot be assigned
                    # to either frozen condition.
                    {
                        "ts": "2026-08-23T11:00:00+08:00",
                        "match_id": "fixture-3",
                        "market": "*",
                        "status": "SKIPPED",
                        "reason": "not_first_native_pre_kickoff_t5",
                    },
                ],
            },
        }

    def test_projection_is_pure_and_preserves_persisted_identity_order(self) -> None:
        before = copy.deepcopy(self.ledger)
        payload = project_condition_funnel(self.ledger, "footbreak")

        self.assertEqual(self.ledger, before)
        self.assertTrue(payload["read_only"])
        self.assertEqual(payload["condition_count"], 2)
        self.assertEqual(
            [row["condition_signature"] for row in payload["conditions"]],
            [self.other_signature, self.signature],
        )
        row = payload["conditions"][1]
        self.assertEqual(row["condition_number"], 7)
        self.assertEqual(row["condition_version"], "granular-condition-v1")
        self.assertEqual(row["definition"], before["wilson_validation"]["conditions"][self.signature]["definition"])
        self.assertEqual(row["active_evidence"]["version"], 3)
        self.assertEqual(row["active_evidence"]["evidence_hash"], "e" * 64)

    def test_counts_only_signature_bound_persisted_evidence(self) -> None:
        row = project_condition_funnel(
            self.ledger, "footbreak"
        )["conditions"][1]
        stages = row["stages"]

        self.assertFalse(stages["eligible_post_activation_t5_observations"]["available"])
        self.assertEqual(
            stages["eligible_post_activation_t5_observations"]["reason"],
            "condition_attribution_not_persisted_before_exact_match",
        )
        self.assertEqual(stages["exact_condition_matches"]["availability"], "bounded")
        self.assertEqual(stages["exact_condition_matches"]["count"], 2)
        self.assertEqual(stages["recorded_formal_evidence"]["count"], 5)
        self.assertEqual(stages["recorded_formal_evidence"]["formal_bets"], 4)
        self.assertEqual(stages["recorded_formal_evidence"]["formal_observations"], 1)
        self.assertEqual(stages["settled_valid_evidence"]["count"], 1)
        self.assertEqual(stages["settled_valid_evidence"]["hits"], 1)
        self.assertEqual(stages["current_rollover_progress"]["display"], "7/20")
        self.assertEqual(
            stages["current_rollover_progress"]["source"],
            "persisted_pending_rollover_progress",
        )

    def test_rejections_are_allowlisted_categorized_and_bounded(self) -> None:
        row = project_condition_funnel(
            self.ledger, "footbreak"
        )["conditions"][1]
        rejection = row["rejections"]
        by_code = {item["code"]: item for item in rejection["items"]}

        self.assertTrue(rejection["bounded"])
        self.assertLessEqual(len(rejection["items"]), rejection["visible_limit"])
        self.assertEqual(by_code["wilson_gate_not_passed"]["category"], "execution_gate")
        self.assertEqual(by_code["idempotent_existing_market"]["count"], 1)
        self.assertEqual(by_code["not_binary_decided"]["count"], 1)
        self.assertEqual(
            by_code["duplicate_or_conflicting_fixture_market"]["count"], 2
        )
        self.assertNotIn("not_first_native_pre_kickoff_t5", by_code)

    def test_malformed_persisted_stages_remain_unavailable(self) -> None:
        row = project_condition_funnel(
            self.ledger, "footbreak"
        )["conditions"][0]
        stages = row["stages"]

        self.assertIsNone(row["active_evidence"]["version"])
        self.assertFalse(stages["settled_valid_evidence"]["available"])
        self.assertIsNone(stages["settled_valid_evidence"]["count"])
        self.assertFalse(stages["current_rollover_progress"]["available"])
        self.assertIsNone(stages["current_rollover_progress"]["display"])

    def test_missing_namespace_is_an_explicit_empty_unavailable_projection(self) -> None:
        payload = project_condition_funnel({"bets": []}, "footbreak")
        self.assertEqual(payload["conditions"], [])
        self.assertEqual(payload["unavailable_reason"], "wilson_namespace_unavailable")


if __name__ == "__main__":
    unittest.main()
