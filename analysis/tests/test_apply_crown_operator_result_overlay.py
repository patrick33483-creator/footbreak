from __future__ import annotations

import unittest

from analysis.apply_crown_operator_result_overlay import apply_overlay


def manifest() -> dict:
    return {
        "batch_id": "test-batch",
        "verified_at": "2026-08-31T18:15:00+08:00",
        "score_scope": "90_minutes_including_stoppage_time_excluding_extra_time",
        "results": [{
            "match_id": "1", "league": "L", "home": "H", "away": "A",
            "kickoff": "2026-08-31T01:00:00+08:00",
            "home_score": 2, "away_score": 1, "provider_event_id": "p1",
            "provider_home": "Home", "provider_away": "Away",
            "provider_start": "2026-08-30T17:00:00Z", "orientation": "direct",
        }],
        "excluded": [],
    }


def history() -> dict:
    return {"rows": [{
        "match_id": "1", "stage": "T-5", "league": "L", "home": "H", "away": "A",
        "kickoff": "2026-08-31T01:00:00+08:00", "result_status": "待賽果",
        "market_predictions": [{
            "code": "HDC", "side": "H", "line": -0.5, "probability": 0.6,
        }],
    }]}


class OperatorOverlayTest(unittest.TestCase):
    def test_grades_exact_pending_fixture(self) -> None:
        proposed, report = apply_overlay(history(), manifest(), apply=False)
        row = proposed["rows"][0]
        self.assertEqual(report["changed_rows"], 1)
        self.assertEqual(row["score"], "2-1")
        self.assertEqual(row["result_status"], "已核對")
        self.assertEqual(row["market_grades"][0]["settlement"], "Won")

    def test_refuses_identity_mismatch(self) -> None:
        value = history()
        value["rows"][0]["away"] = "Wrong"
        with self.assertRaisesRegex(ValueError, "history away mismatch"):
            apply_overlay(value, manifest(), apply=False)

    def test_refuses_conflicting_existing_score(self) -> None:
        value = history()
        value["rows"][0].update({"result_status": "已核對", "score": "0-4"})
        with self.assertRaisesRegex(ValueError, "existing result conflict"):
            apply_overlay(value, manifest(), apply=False)

    def test_same_existing_score_is_idempotent(self) -> None:
        value = history()
        value["rows"][0].update({"result_status": "已核對", "score": "2-1"})
        _, report = apply_overlay(value, manifest(), apply=False)
        self.assertEqual(report["changed_rows"], 0)
        self.assertEqual(report["already_rows"], 1)

    def test_syncs_missing_history_row_from_ledger(self) -> None:
        ledger = {"watch": {"1": {
            "match_id": "1", "league": "L", "home": "H", "away": "A",
            "kickoff": "2026-08-31T01:00:00+08:00",
            "stages": [{
                "stage": "T-5",
                "market_predictions": [{
                    "code": "HDC", "side": "H", "line": -0.5,
                    "probability": 0.6,
                }],
            }],
        }}}
        proposed, report = apply_overlay(
            {"rows": []}, manifest(), apply=False, ledger=ledger,
        )
        self.assertEqual(report["synced_rows"], 1)
        self.assertEqual(report["changed_rows"], 1)
        self.assertEqual(proposed["rows"][0]["score"], "2-1")

    def test_materializes_only_target_rows_from_dashboard_history(self) -> None:
        target = history()["rows"][0]
        unrelated = {**target, "match_id": "2", "home": "Other"}
        source = {"prediction_history": {"rows": [target, unrelated]}}
        proposed, report = apply_overlay(
            {"rows": []}, manifest(), apply=False, source_history=source,
        )
        self.assertEqual(report["materialized_rows"], 1)
        self.assertEqual(report["changed_rows"], 1)
        self.assertEqual(len(proposed["rows"]), 1)
        self.assertTrue(
            proposed["rows"][0]["operator_materialized_from_dashboard_history"]
        )


if __name__ == "__main__":
    unittest.main()
