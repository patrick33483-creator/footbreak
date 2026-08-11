from __future__ import annotations

import json
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import patch

from analysis.rolling_backtest import automatic_upgrade_test, run, system_status
from analysis.learning_store import LearningStore
from analysis.time_order_backtest import crown_rows, footbreak_rows


class RollingBacktestTest(unittest.TestCase):
    def test_automatic_upgrade_test_waits_for_300_valid_matches(self) -> None:
        start = datetime(2026, 1, 1, tzinfo=timezone.utc)
        rows = [{
            "match_id": f"m{index:03d}",
            "kickoff": start,
            "stage": "T-5",
            "predicted_at": f"{index:03d}",
            "conf": 60,
            "hit": 1,
            "brier": 0.2,
            "log_loss": 0.3,
        } for index in range(299)]
        result = automatic_upgrade_test("footbreak", rows)
        self.assertEqual(result["status"], "waiting_for_300_matches")
        self.assertEqual(result["remaining_matches"], 1)
        self.assertFalse(result["auto_promote"])

    @patch("analysis.rolling_backtest.evaluate")
    def test_automatic_upgrade_test_runs_but_does_not_auto_promote(self, evaluate) -> None:
        start = datetime(2026, 1, 1, tzinfo=timezone.utc)
        rows = [{
            "match_id": f"m{index:03d}",
            "kickoff": start,
            "stage": "T-5",
            "predicted_at": f"{index:03d}",
            "conf": 60,
            "hit": 1,
            "brier": 0.2,
            "log_loss": 0.3,
        } for index in range(300)]
        evaluate.return_value = {
            "holdout_start": "2026-01-01T00:00:00+00:00",
            "baseline_latest": {
                "holdout": {"n": 90, "accuracy": .50, "brier": .60, "log_loss": 1.0},
            },
            "stage_candidate": {
                "selected_on_train": "T-5",
                "holdout": {"n": 80, "accuracy": .51, "brier": .58, "log_loss": .98},
                "holdout_coverage": .89,
            },
            "confidence_candidate": {
                "selected_on_train": 55,
                "holdout": {"n": 70, "accuracy": .52, "brier": .57, "log_loss": .97},
                "holdout_coverage": .78,
            },
        }
        result = automatic_upgrade_test("footbreak", rows)
        self.assertEqual(result["status"], "candidate_passed")
        self.assertEqual(result["recommended_candidate"], "stage:T-5")
        self.assertFalse(result["auto_promote"])
        self.assertTrue(all(test["passed"] for test in result["tests"]))

    def test_forward_gate_uses_new_unique_ids_and_t5_coverage(self) -> None:
        start = datetime(2026, 1, 1, tzinfo=timezone.utc)
        rows = []
        for index in range(150):
            rows.append({
                "match_id": f"m{index:03d}",
                "kickoff": start,
                "stage": "T-5" if index < 120 else "首預",
                "predicted_at": f"{index:03d}",
                "conf": 60,
                "hit": 1,
                "brier": 0.2,
                "log_loss": 0.3,
            })
        state = {
            "baseline_match_ids": [f"m{index:03d}" for index in range(50)],
            "seen_match_ids": [f"m{index:03d}" for index in range(50)],
            "locked_selection": {
                "stage": "T-5",
                "confidence_threshold": None,
                "sample_sufficient": True,
            },
            "baseline_report": {},
        }
        ready = system_status(rows, state)
        self.assertEqual(ready["new_matches"], 100)
        self.assertEqual(ready["t5_holdout_coverage"], 0.7)
        self.assertEqual(ready["status"], "ready_for_human_review")

        rows[119]["stage"] = "首預"
        state["seen_match_ids"] = [f"m{index:03d}" for index in range(50)]
        low_coverage = system_status(rows, state)
        self.assertEqual(low_coverage["status"], "accumulating")

    def test_direct_state_payloads_are_supported(self) -> None:
        kickoff = datetime(2026, 8, 9, tzinfo=timezone.utc).isoformat()
        crown = {
            "rows": [{
                "match_id": "c1",
                "kickoff": kickoff,
                "stage": "首預",
                "forecast": "主勝",
                "actual": "主勝",
                "outcome": {"home": 0.5, "draw": 0.3, "away": 0.2},
            }]
        }
        footbreak = {
            "matches": [{
                "match_id": "f1",
                "kickoff": kickoff,
                "stages": [{
                    "stage": "首預",
                    "wdl_hit": 1,
                    "wdl_brier": 0.4,
                    "wdl_ll": 0.7,
                }],
            }]
        }
        self.assertEqual(len(crown_rows(crown)), 1)
        self.assertEqual(len(footbreak_rows(footbreak)), 1)

    @patch("analysis.rolling_backtest.evaluate")
    @patch("analysis.rolling_backtest.footbreak_rows", return_value=[])
    @patch("analysis.rolling_backtest.crown_rows", return_value=[])
    def test_first_run_establishes_baseline_without_auto_apply(
        self, _crown_rows, _footbreak_rows, evaluate
    ) -> None:
        base_report = {
            "train_matches": 100,
            "stage_candidate": {
                "selected_on_train": "T-5",
                "train": {"n": 80},
            },
            "confidence_candidate": {
                "selected_on_train": None,
                "train": {"n": 100},
            },
        }
        evaluate.side_effect = [base_report, base_report]
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            crown_path = root / "crown.json"
            footbreak_path = root / "footbreak.json"
            crown_path.write_text("{}", encoding="utf-8")
            footbreak_path.write_text("{}", encoding="utf-8")
            result = run(
                crown_path,
                footbreak_path,
                root / "state.json",
                root / "latest.json",
                [root / "public.json"],
            )
            self.assertEqual(result["overall_status"], "accumulating")
            self.assertFalse(result["policy"]["auto_apply"])
            self.assertEqual(
                result["policy"]["notifications"], "passed_threshold_only"
            )
            self.assertEqual(result["systems"]["crown"]["new_matches"], 0)
            self.assertEqual(result["systems"]["footbreak"]["new_matches"], 0)
            self.assertTrue((root / "public.json").exists())

    def test_production_run_uses_immutable_database_not_mutable_json(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            database = root / "learning.sqlite"
            with LearningStore(database) as store:
                snapshot = store.record_snapshot(
                    "footbreak", "f-immutable", "T-5",
                    "2026-08-10T11:55:00+08:00",
                    "2026-08-10T12:00:00+08:00",
                    {"conviction": 61},
                )
                result = store.record_result(
                    "footbreak", "f-immutable",
                    home_score=1, away_score=0, source="verified",
                )
                store.record_grade(
                    snapshot, "WDL", "0", "GRADED",
                    {"hit": 1, "brier": .16, "log_loss": .51},
                    result_id=result["result_id"],
                )
            crown_path = root / "crown.json"
            footbreak_path = root / "footbreak.json"
            crown_path.write_text("not valid json", encoding="utf-8")
            footbreak_path.write_text("not valid json", encoding="utf-8")
            output = run(
                crown_path,
                footbreak_path,
                root / "state.json",
                root / "latest.json",
                learning_db=database,
            )
            self.assertEqual(
                output["systems"]["footbreak"]["source_available_matches"], 1
            )
            self.assertEqual(
                output["systems"]["crown"]["source_available_matches"], 0
            )


if __name__ == "__main__":
    unittest.main()
