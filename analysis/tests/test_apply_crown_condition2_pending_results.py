from __future__ import annotations

import unittest

from analysis.apply_crown_condition2_pending_results import (
    _apply_manual_overrides, _assert_empty_prospective, _merge_history,
    _update_recovery_rows,
)


class ApplyPendingResultTest(unittest.TestCase):
    def test_refuses_to_rebind_nonempty_prospective(self) -> None:
        with self.assertRaisesRegex(ValueError, "prospective_not_empty"):
            _assert_empty_prospective({
                "prospective": {"fixture": {}},
                "prospective_observations": {},
                "pending_rollover_progress": {"eligible_decided": 0},
            })

    def test_updates_only_exact_pending_recovery_row(self) -> None:
        frozen = {
            "historical_recovery_rows": [{
                "match_id": "1", "kickoff": "2026-08-01T20:00:00+08:00",
                "stage_at": "2026-08-01T10:00:00+08:00",
                "side": "H", "line": 2.75, "odds": 1.8,
                "result": "PENDING",
            }],
        }
        counts = _update_recovery_rows(frozen, {"1": {
            "match_id": "1", "kickoff": "2026-08-01T20:00:00+08:00",
            "stage_at": "2026-08-01T10:00:00+08:00",
            "side": "H", "line": 2.75, "odds": 1.8,
            "result": "Half Won", "settled_at": "2026-08-01T22:00:00+08:00",
            "result_proof_hash": "proof", "result_source": "exact",
        }})
        self.assertEqual(counts["applied"], 1)
        self.assertEqual(counts["hits"], 1)
        self.assertEqual(counts["pending"], 0)
        self.assertEqual(
            frozen["historical_recovery_rows"][0]["normal_grade_source_hash"],
            "proof",
        )

    def test_merge_history_accepts_legacy_source_snapshot_timestamp(self) -> None:
        production = {"rows": [{
            "match_id": "1", "stage": "首預",
            "kickoff": "2026-08-01T20:00:00+08:00",
            "source_snapshot_at": "2026-08-01T10:00:00+08:00",
            "result_status": "待賽果",
        }]}
        refreshed = {"rows": [{
            "match_id": "1", "stage": "首預",
            "kickoff": "2026-08-01T20:00:00+08:00",
            "source_snapshot_at": "2026-08-01T10:00:00+08:00",
            "result_status": "已核對", "actual": "Won",
        }]}
        updated = _merge_history(production, refreshed, {"1"})
        self.assertEqual(updated, 1)
        self.assertEqual(production["rows"][0]["actual"], "Won")

    def test_manual_score_resolves_only_matching_unresolved_fixture(self) -> None:
        report = {
            "resolved": 0,
            "unresolved": 1,
            "rows": [{
                "match_id": "1",
                "kickoff": "2026-08-01T20:00:00+08:00",
                "stage_at": "2026-08-01T10:00:00+08:00",
                "league": "League", "home": "Home", "away": "Away",
                "side": "H", "line": 3.0, "odds": 1.89,
                "audit_status": "unresolved",
            }],
        }
        history = {"rows": [{
            "match_id": "1", "stage": "首預",
            "kickoff": "2026-08-01T20:00:00+08:00",
            "source_snapshot_at": "2026-08-01T10:00:00+08:00",
            "forecast": "主勝",
            "market_predictions": [{
                "code": "HIL", "side": "H", "line": 3.0,
                "probability": 0.6,
            }],
        }]}
        count = _apply_manual_overrides(report, history, {"1": {
            "home_score": 5, "away_score": 0,
            "attested_at": "2026-08-02T01:00:00+08:00",
            "source": "user_attested_manual_score",
            "attestation_reference": "test",
        }})
        self.assertEqual(count, 1)
        self.assertEqual(report["resolved"], 1)
        self.assertEqual(report["unresolved"], 0)
        self.assertEqual(report["rows"][0]["result"], "Won")
        self.assertEqual(history["rows"][0]["score"], "5-0")
        self.assertEqual(
            history["rows"][0]["market_grades"][0]["settlement"], "Won",
        )


if __name__ == "__main__":
    unittest.main()
