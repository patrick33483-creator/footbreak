from __future__ import annotations

import copy
import unittest
from unittest.mock import patch

from analysis.correct_crown_condition2_dedup import correct
from analysis.wilson_validation import _evidence_values, _version_hash


class CrownCondition2DedupCorrectionTest(unittest.TestCase):
    def test_correction_preserves_prospective_state(self) -> None:
        v1_values = _evidence_values(141, 231)
        v1 = {
            "version": 1,
            "condition_signature": "sig",
            "prior_version": None,
            "prior_evidence_hash": None,
            "batch_fixture_market_hashes": [],
            "batch_hits": 141,
            "batch_decided": 231,
            "cumulative_hits": 141,
            "cumulative_decided": 231,
            "wilson95_lower_raw": v1_values["wilson95_lower_raw"],
            "minimum_acceptable_odds_raw":
                v1_values["minimum_acceptable_odds_raw"],
            "minimum_acceptable_odds_display":
                v1_values["display"]["minimum_acceptable_odds"],
            "activation_boundary_at": "2026-08-01T00:00:00+08:00",
            "created_at": "2026-08-01T00:00:00+08:00",
            "migration_baseline": True,
        }
        v1["evidence_hash"] = _version_hash(v1)
        v2_values = _evidence_values(317, 530)
        v2 = {
            "version": 2,
            "condition_signature": "sig",
            "prior_version": 1,
            "prior_evidence_hash": v1["evidence_hash"],
            "batch_fixture_market_hashes": [],
            "batch_fixture_market_ids_unavailable_from_legacy_aggregate": True,
            "batch_hits": 176,
            "batch_decided": 299,
            "cumulative_hits": 317,
            "cumulative_decided": 530,
            "wilson95_lower_raw": v2_values["wilson95_lower_raw"],
            "minimum_acceptable_odds_raw":
                v2_values["minimum_acceptable_odds_raw"],
            "minimum_acceptable_odds_display":
                v2_values["display"]["minimum_acceptable_odds"],
            "activation_boundary_at": "2026-08-02T00:00:00+08:00",
            "created_at": "2026-08-02T00:00:00+08:00",
            "initial_migration_full_cohort": True,
            "legacy_prospective_cohort": {
                "hits": 176, "decided": 299, "pushes": 9,
            },
        }
        v2["evidence_hash"] = _version_hash(v2)
        prospective = {"fixture-new": {"result": "PENDING"}}
        progress = {
            "eligible_hits": 1,
            "eligible_decided": 2,
            "required": 20,
            "display": "2/20",
        }
        frozen = {
            "signature": "sig",
            "evidence_versions": [v1, v2],
            "active_evidence_version": 2,
            "active_evidence_hash": v2["evidence_hash"],
            "prospective": {"hits": 1, "decided": 2},
            "prospective_observations": copy.deepcopy(prospective),
            "pending_rollover_progress": copy.deepcopy(progress),
            "rollover_status": "active",
            "historical_recovery_rows": (
                [{"result": "Won"}] * 132
                + [{"result": "Lost"}] * 96
                + [{"result": "Refunded"}] * 5
            ),
        }
        namespace = {"conditions": {"sig": frozen}}
        ledger = {"wilson_validation": namespace}

        with (
            patch(
                "analysis.correct_crown_condition2_dedup._condition",
                return_value=(namespace, frozen),
            ),
            patch(
                "analysis.correct_crown_condition2_dedup."
                "_validate_frozen_identity_and_chain",
                side_effect=lambda row, *_args: (
                    {},
                    row["evidence_versions"],
                    None,
                ),
            ),
        ):
            report = correct(ledger, apply=True)

        self.assertEqual(report["status"], "applied")
        self.assertTrue(report["prospective_preserved"])
        self.assertEqual(
            (
                frozen["active_evidence"]["cumulative_hits"],
                frozen["active_evidence"]["cumulative_decided"],
            ),
            (273, 459),
        )
        self.assertEqual(
            (
                frozen["evidence_versions"][1]["batch_hits"],
                frozen["evidence_versions"][1]["batch_decided"],
            ),
            (132, 228),
        )
        self.assertEqual(frozen["prospective_observations"], prospective)
        self.assertEqual(frozen["pending_rollover_progress"], progress)


if __name__ == "__main__":
    unittest.main()
