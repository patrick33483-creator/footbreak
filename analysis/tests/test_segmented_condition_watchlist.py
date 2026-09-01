from __future__ import annotations

import unittest

from analysis.segmented_condition_watchlist import (
    ALERT_ROI_FLOOR,
    MIN_DECIDED_FOR_ALERT,
    ROLLING_WINDOW,
    WATCHED_IDS,
    _breach,
    _pnl_from_settlement,
    _rolling,
    _validate_watched_ids,
)


class SegmentedConditionWatchlistTests(unittest.TestCase):
    def test_watched_ids_all_exist_in_conditions(self) -> None:
        # Startup guard: adding an id to WATCHED_IDS without a matching
        # CONDITIONS entry must fail loud.
        _validate_watched_ids()

    def test_pnl_from_settlement_covers_all_labels(self) -> None:
        self.assertAlmostEqual(_pnl_from_settlement("Won", 1.9), 0.9, places=6)
        self.assertAlmostEqual(_pnl_from_settlement("Half Won", 1.9), 0.45, places=6)
        self.assertAlmostEqual(_pnl_from_settlement("Refunded", 1.9), 0.0)
        self.assertAlmostEqual(_pnl_from_settlement("Half Lost", 1.9), -0.5)
        self.assertAlmostEqual(_pnl_from_settlement("Lost", 1.9), -1.0)
        # Undecided / unknown labels return None
        self.assertIsNone(_pnl_from_settlement("待賽果", 1.9))
        self.assertIsNone(_pnl_from_settlement("", 1.9))
        # No odds → None
        self.assertIsNone(_pnl_from_settlement("Won", None))

    def test_rolling_takes_at_most_window_decided_newest_first(self) -> None:
        # 35 decided observations, all Won @ 1.9 → ROI should be 0.9 flat.
        obs = [
            {"settlement": "Won", "odds": 1.9, "kickoff": f"2026-08-{i:02d}T20:00:00+08:00"}
            for i in range(1, 36)
        ]
        rolling = _rolling(obs)
        self.assertEqual(rolling["decided"], ROLLING_WINDOW)
        self.assertAlmostEqual(rolling["hit_rate"], 1.0)
        self.assertAlmostEqual(rolling["roi"], 0.9, places=6)

    def test_rolling_skips_undecided(self) -> None:
        obs = [
            {"settlement": "待賽果", "odds": 1.9, "kickoff": "2026-08-30T20:00:00+08:00"},
            {"settlement": "Won", "odds": 1.9, "kickoff": "2026-08-29T20:00:00+08:00"},
            {"settlement": "Lost", "odds": 1.9, "kickoff": "2026-08-28T20:00:00+08:00"},
        ]
        rolling = _rolling(obs)
        self.assertEqual(rolling["decided"], 2)
        self.assertAlmostEqual(rolling["profit"], -0.1, places=6)

    def test_breach_needs_min_decided_and_negative_roi(self) -> None:
        # Below min_decided → no breach even with terrible ROI
        self.assertFalse(_breach({"decided": 10, "roi": -0.20}))
        # Above min_decided but ROI above floor → no breach
        self.assertFalse(_breach({"decided": MIN_DECIDED_FOR_ALERT, "roi": -0.03}))
        # Above min_decided AND ROI below floor → breach
        self.assertTrue(_breach({"decided": MIN_DECIDED_FOR_ALERT, "roi": ALERT_ROI_FLOOR - 0.001}))
        # None ROI → no breach
        self.assertFalse(_breach({"decided": 25, "roi": None}))

    def test_watched_ids_are_two_expected_conditions(self) -> None:
        # Guard against accidental removal / renaming.
        self.assertIn("A-HIL-OPEN-T5-OVER-180", WATCHED_IDS)
        self.assertIn("A-HDC-OPEN-AWAY-MINUS-050", WATCHED_IDS)


if __name__ == "__main__":
    unittest.main()
