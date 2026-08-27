from __future__ import annotations

import unittest

from analysis.quarter_line import validate
from analysis.recover_crown_condition2_history import (
    _merge_recovered_into_v2, _with_quarter_line_profile,
)
from analysis.wilson_validation import _evidence_values, _version_hash


class CrownCondition2HistoryRecoveryTest(unittest.TestCase):
    def test_authorized_history_merge_rebuilds_active_v2_and_resets_rollover(self) -> None:
        v1_values = _evidence_values(141, 231)
        v1 = {
            "version": 1, "condition_signature": "sig",
            "prior_version": None, "prior_evidence_hash": None,
            "batch_fixture_market_hashes": [],
            "batch_hits": 141, "batch_decided": 231,
            "cumulative_hits": 141, "cumulative_decided": 231,
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
        v2_values = _evidence_values(185, 302)
        v2 = {
            "version": 2, "condition_signature": "sig",
            "prior_version": 1, "prior_evidence_hash": v1["evidence_hash"],
            "batch_fixture_market_hashes": [],
            "batch_hits": 44, "batch_decided": 71,
            "cumulative_hits": 185, "cumulative_decided": 302,
            "wilson95_lower_raw": v2_values["wilson95_lower_raw"],
            "minimum_acceptable_odds_raw":
                v2_values["minimum_acceptable_odds_raw"],
            "minimum_acceptable_odds_display":
                v2_values["display"]["minimum_acceptable_odds"],
            "activation_boundary_at": "2026-08-02T00:00:00+08:00",
            "created_at": "2026-08-02T00:00:00+08:00",
            "initial_migration_full_cohort": True,
            "batch_fixture_market_ids_unavailable_from_legacy_aggregate": True,
            "legacy_prospective_cohort": {
                "hits": 44, "decided": 71, "pushes": 4,
            },
        }
        v2["evidence_hash"] = _version_hash(v2)
        frozen = {"evidence_versions": [v1, v2]}
        proof = {"frozen": frozen, "v1": v1, "v2": v2}
        recovered = [
            {"match_id": "won", "result": "Won"},
            {"match_id": "half-won", "result": "Half Won"},
            {"match_id": "lost", "result": "Lost"},
            {"match_id": "push", "result": "Refunded"},
            {"match_id": "pending", "result": "PENDING"},
        ]

        counts = _merge_recovered_into_v2(
            proof, recovered, "2026-08-28T01:00:00+08:00",
        )

        active = frozen["evidence_versions"][1]
        self.assertEqual(counts, {
            "hits": 2, "losses": 1, "decided": 3,
            "pushes": 1, "pending": 1, "settled": 4,
        })
        self.assertEqual(
            (active["version"], active["cumulative_hits"],
             active["cumulative_decided"]),
            (2, 187, 305),
        )
        self.assertEqual(frozen["pending_rollover_progress"]["display"], "0/20")
        self.assertEqual(
            active["condition2_history_recovery"]["starting_active"],
            {"hits": 185, "decided": 302},
        )
        self.assertEqual(active["evidence_hash"], _version_hash(active))

    def test_reconstructs_quarter_profile_from_same_stage_two_sided_quote(self) -> None:
        selected = {"code": "HIL", "side": "H", "line": 2.75, "odds": 1.81}
        source = {
            "market_predictions": [
                {"code": "HIL", "side": "H", "line": 2.75, "odds": 1.81},
                {"code": "HIL", "side": "L", "line": 2.75, "odds": 2.05},
            ],
        }

        recovered = _with_quarter_line_profile(selected, source)

        self.assertNotIn("quarter_line_settlement", selected)
        self.assertEqual(
            validate(
                recovered["quarter_line_settlement"],
                market="HIL", side="H", line=2.75,
            ),
            recovered["quarter_line_settlement"],
        )

    def test_never_guesses_profile_without_one_exact_quote_per_side(self) -> None:
        selected = {"code": "HIL", "side": "H", "line": 2.75, "odds": 1.81}
        source = {
            "market_predictions": [
                {"code": "HIL", "side": "H", "line": 2.75, "odds": 1.81},
            ],
        }

        recovered = _with_quarter_line_profile(selected, source)

        self.assertNotIn("quarter_line_settlement", recovered)

    def test_uses_persisted_same_stage_no_vig_probability_for_legacy_row(self) -> None:
        selected = {
            "code": "HIL", "side": "H", "line": 2.75, "odds": 1.81,
            "probability": 0.53142,
        }

        recovered = _with_quarter_line_profile(selected, {})

        profile = recovered["quarter_line_settlement"]
        self.assertEqual(profile["method"], "native_market_no_vig_probability")
        self.assertEqual(
            profile["source"]["selected_probability"], selected["probability"],
        )
        self.assertEqual(
            validate(profile, market="HIL", side="H", line=2.75), profile,
        )

    def test_integer_line_does_not_require_profile(self) -> None:
        selected = {"code": "HIL", "side": "H", "line": 3.0, "odds": 1.88}

        recovered = _with_quarter_line_profile(selected, {})

        self.assertEqual(recovered, selected)


if __name__ == "__main__":
    unittest.main()
