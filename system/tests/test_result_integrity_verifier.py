import importlib.util
import unittest
from pathlib import Path


MODULE_PATH = Path(__file__).parents[2] / "deploy" / "verify-result-integrity.py"
SPEC = importlib.util.spec_from_file_location("verify_result_integrity", MODULE_PATH)
verify = importlib.util.module_from_spec(SPEC)
assert SPEC.loader is not None
SPEC.loader.exec_module(verify)


class ResultIntegrityVerifierTests(unittest.TestCase):
    def test_accepts_push_and_checks_exact_stage_cell(self):
        rows = [
            {
                "match_id": "new",
                "kickoff": "2026-08-12T20:00:00+08:00",
                "predicted_at": "2026-08-12T19:55:00+08:00",
                "stage": "T-5",
                "market_grades": [
                    {"code": "HDC", "grade_status": "GRADED", "hit": True},
                    {"code": "HIL", "grade_status": "GRADED", "hit": None},
                ],
            }
        ]
        empty = {"graded": 0, "decided": 0, "hits": 0, "accuracy": None}
        stats = {
            "by_market": {
                "HDC": {"graded": 1, "decided": 1, "hits": 1, "accuracy": 1.0},
                "HIL": {"graded": 1, "decided": 0, "hits": 0, "accuracy": None},
                "CHL": dict(empty),
            },
            "by_stage_market": {
                stage: {
                    code: (
                        {"graded": 1, "decided": 1, "hits": 1, "accuracy": 1.0}
                        if stage == "T-5" and code == "HDC"
                        else {"graded": 1, "decided": 0, "hits": 0, "accuracy": None}
                        if stage == "T-5" and code == "HIL"
                        else dict(empty)
                    )
                    for code in verify.MARKETS
                }
                for stage in verify.STAGES
            },
        }
        verify.assert_market_stats_consistent("test", rows, stats)

        stats["by_stage_market"]["首預"]["HDC"]["graded"] = 1
        with self.assertRaises(AssertionError):
            verify.assert_market_stats_consistent("test", rows, stats)

    def test_rejects_duplicate_or_non_descending_history(self):
        latest = {
            "match_id": "latest",
            "stage": "T-5",
            "kickoff": "2026-08-12T20:00:00+08:00",
            "predicted_at": "2026-08-12T19:55:00+08:00",
        }
        older = {
            "match_id": "older",
            "stage": "T-5",
            "kickoff": "2026-08-12T18:00:00+08:00",
            "predicted_at": "2026-08-12T17:55:00+08:00",
        }
        verify.assert_unique_and_sorted("test", [latest, older])
        with self.assertRaises(AssertionError):
            verify.assert_unique_and_sorted("test", [older, latest])
        with self.assertRaises(AssertionError):
            verify.assert_unique_and_sorted("test", [latest, dict(latest)])

    def test_accuracy_accepts_only_six_decimal_rounding(self):
        self.assertTrue(verify.same_accuracy(0.321429, 9 / 28))
        self.assertFalse(verify.same_accuracy(0.32143, 9 / 28))

    def test_accepts_odds_tier_stats_and_audits_all_odds(self):
        rows = [
            {
                "match_id": "priced",
                "stage": "T-5",
                "market_grades": [
                    {"code": "HIL", "grade_status": "GRADED", "hit": True},
                ],
            },
            {
                "match_id": "missing",
                "stage": "T-5",
                "market_grades": [
                    {"code": "HIL", "grade_status": "GRADED", "hit": False},
                ],
            },
        ]
        empty = {"graded": 0, "decided": 0, "hits": 0, "pushes": 0, "accuracy": None}

        def tiered(all_odds, high=None, low=None, missing=None):
            high = high or dict(empty)
            low = low or dict(empty)
            missing = missing or dict(empty)
            return {
                **high,
                "all_odds": all_odds,
                "odds_groups": {
                    "at_or_above_1_70": high,
                    "below_1_70": low,
                    "missing": missing,
                },
            }

        hil_all = {"graded": 2, "decided": 2, "hits": 1, "pushes": 0, "accuracy": 0.5}
        hil_high = {"graded": 1, "decided": 1, "hits": 1, "pushes": 0, "accuracy": 1.0}
        hil_missing = {"graded": 1, "decided": 1, "hits": 0, "pushes": 0, "accuracy": 0.0}
        stats = {
            "by_market": {
                "HDC": tiered(dict(empty)),
                "HIL": tiered(hil_all, high=hil_high, missing=hil_missing),
                "CHL": tiered(dict(empty)),
            },
            "by_stage_market": {
                stage: {
                    code: (
                        tiered(hil_all, high=hil_high, missing=hil_missing)
                        if stage == "T-5" and code == "HIL"
                        else tiered(dict(empty))
                    )
                    for code in verify.MARKETS
                }
                for stage in verify.STAGES
            },
        }
        verify.assert_market_stats_consistent("test", rows, stats)

        stats["by_market"]["HIL"]["odds_groups"]["missing"]["hits"] = 1
        with self.assertRaises(AssertionError):
            verify.assert_market_stats_consistent("test", rows, stats)


if __name__ == "__main__":
    unittest.main()
