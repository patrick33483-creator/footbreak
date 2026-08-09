from __future__ import annotations

import json
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import patch

from analysis.rolling_backtest import run, system_status
from analysis.time_order_backtest import crown_rows, footbreak_rows


class RollingBacktestTest(unittest.TestCase):
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
            self.assertEqual(result["systems"]["crown"]["new_matches"], 0)
            self.assertEqual(result["systems"]["footbreak"]["new_matches"], 0)
            self.assertTrue((root / "public.json").exists())


if __name__ == "__main__":
    unittest.main()
