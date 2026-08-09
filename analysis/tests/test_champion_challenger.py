from __future__ import annotations

import unittest
from datetime import datetime, timedelta, timezone

from analysis.champion_challenger import market_test
from analysis.time_order_backtest import crown_market_rows, footbreak_market_rows


class ChampionChallengerTests(unittest.TestCase):
    def test_market_rows_preserve_exact_line_probability_and_time(self) -> None:
        grade = {
            "code": "HIL", "condition": "2.5/3", "side": "H",
            "probability": .61, "target": .75, "hit": True,
            "brier": .0196, "log_loss": .57, "grade_status": "GRADED",
        }
        crown = crown_market_rows({"rows": [{
            "match_id": "c1", "kickoff": "2026-08-10T12:00:00+08:00",
            "stage": "T-30", "predicted_at": "2026-08-10T11:30:00+08:00",
            "market_grades": [grade],
        }]})
        footbreak = footbreak_market_rows({"matches": [{
            "match_id": "f1", "kickoff": "2026-08-10T12:00:00+08:00",
            "stages": [{"stage": "T-5", "predicted_at": "2026-08-10T11:55:00+08:00",
                        "market_grades": [grade]}],
        }]})
        self.assertEqual(crown[0]["target_key"], "2.5/3|H")
        self.assertEqual(crown[0]["probability"], .61)
        self.assertEqual(footbreak[0]["predicted_at"], "2026-08-10T11:55:00+08:00")

    def test_upgrade_waits_for_100_verified_matches(self) -> None:
        now = datetime(2026, 8, 10, tzinfo=timezone.utc)
        rows = [{
            "match_id": str(i), "market": "HDC", "kickoff": now + timedelta(hours=i),
            "predicted_at": (now + timedelta(hours=i - 1)).isoformat(),
            "probability": .60, "target": 1.0, "hit": 1,
            "brier": .16, "log_loss": .51,
        } for i in range(99)]
        report = market_test(rows, "HDC")
        self.assertEqual(report["status"], "waiting_for_100_verified_matches")
        self.assertEqual(report["remaining_matches"], 1)
        self.assertFalse(report["auto_promote"])

    def test_refund_is_kept_for_probability_metrics_not_accuracy(self) -> None:
        rows = crown_market_rows({"rows": [{
            "match_id": "push-1",
            "kickoff": "2026-08-10T12:00:00+08:00",
            "stage": "T-5",
            "predicted_at": "2026-08-10T11:55:00+08:00",
            "market_grades": [{
                "code": "HIL", "condition": "3", "side": "H",
                "probability": .50, "target": .50, "hit": None,
                "brier": 0.0, "log_loss": .693147,
                "grade_status": "GRADED",
            }],
        }]})
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["target"], .50)
        self.assertIsNone(rows[0]["hit"])

    def test_challenger_is_time_split_and_never_auto_promoted(self) -> None:
        now = datetime(2026, 8, 10, tzinfo=timezone.utc)
        rows = []
        for i in range(100):
            probability = .82
            target = 1.0 if i % 2 == 0 else 0.0
            rows.append({
                "match_id": str(i), "market": "HIL",
                "kickoff": now + timedelta(hours=i),
                "predicted_at": (now + timedelta(hours=i - 1)).isoformat(),
                "probability": probability, "target": target,
                "hit": int(target == 1.0), "brier": (probability - target) ** 2,
                "log_loss": .0,
            })
        report = market_test(rows, "HIL")
        self.assertEqual(report["train_matches"], 70)
        self.assertEqual(report["holdout_matches"], 30)
        self.assertFalse(report["auto_promote"])
        self.assertIn(report["status"], {
            "candidate_passed_human_review_required", "tested_no_safe_upgrade",
        })


if __name__ == "__main__":
    unittest.main()
