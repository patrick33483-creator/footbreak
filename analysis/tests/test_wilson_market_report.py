from __future__ import annotations

import unittest

from analysis.wilson_market_report import aggregate, wilson_lower


class WilsonMarketReportTests(unittest.TestCase):
    def test_footbreak_native_t5_dedupes_and_excludes_conflicts(self):
        payload = {"matches": [
            {"match_id": "a", "stages": [
                {"stage": "T-5", "market_grades": [
                    {"code": "HDC", "grade_status": "GRADED",
                     "settlement": "Won", "hit": True},
                ]},
                {"stage": "T-5", "market_grades": [
                    {"code": "HDC", "grade_status": "GRADED",
                     "settlement": "Won", "hit": True},
                ]},
            ]},
            {"match_id": "b", "stages": [{"stage": "T-5", "market_grades": [
                {"code": "HDC", "grade_status": "GRADED",
                 "settlement": "Lost", "hit": False},
            ]}]},
            {"match_id": "c", "stages": [
                {"stage": "T-5", "market_grades": [
                    {"code": "HIL", "grade_status": "GRADED",
                     "settlement": "Won", "hit": True},
                ]},
                {"stage": "T-5", "market_grades": [
                    {"code": "HIL", "grade_status": "GRADED",
                     "settlement": "Lost", "hit": False},
                ]},
            ]},
        ]}
        report = aggregate(payload, "footbreak")
        self.assertEqual(report["markets"]["HDC"]["hits"], 1)
        self.assertEqual(report["markets"]["HDC"]["losses"], 1)
        self.assertEqual(
            report["markets"]["HIL"]["conflicting_fixture_markets_excluded"], 1
        )

    def test_crown_refunds_do_not_enter_decided_denominator(self):
        payload = {"rows": [
            {"match_id": "a", "stage": "T-5", "market_grades": [
                {"code": "CHL", "grade_status": "GRADED",
                 "settlement": "Refunded", "hit": None},
            ]},
            {"match_id": "b", "stage": "T-5", "market_grades": [
                {"code": "CHL", "grade_status": "GRADED",
                 "settlement": "Won", "hit": True},
            ]},
            {"match_id": "x", "stage": "T-5", "recovery_mode": "backfill",
             "market_grades": [{"code": "CHL", "grade_status": "GRADED",
                                "settlement": "Won", "hit": True}]},
        ]}
        report = aggregate(payload, "crown")
        self.assertEqual(report["markets"]["CHL"]["refunded"], 1)
        self.assertEqual(report["markets"]["CHL"]["decided"], 1)
        self.assertEqual(report["markets"]["CHL"]["hits"], 1)

    def test_known_wilson_value(self):
        self.assertAlmostEqual(wilson_lower(41, 59), 0.5685367347)


if __name__ == "__main__":
    unittest.main()
