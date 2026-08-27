from __future__ import annotations

import unittest

from analysis.apply_crown_condition2_pending_results import (
    _assert_empty_prospective, _merge_history, _update_recovery_rows,
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


if __name__ == "__main__":
    unittest.main()
