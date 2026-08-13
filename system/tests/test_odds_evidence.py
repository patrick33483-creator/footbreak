"""Regression coverage for visible, immutable selected-odds evidence."""
from __future__ import annotations

import sys
import unittest
from pathlib import Path

SYSTEM_DIR = Path(__file__).resolve().parents[1]
if str(SYSTEM_DIR) not in sys.path:
    sys.path.insert(0, str(SYSTEM_DIR))

import record_picks


class FootbreakOddsEvidenceTests(unittest.TestCase):
    def test_stage_snapshot_keeps_selected_quote_evidence(self) -> None:
        result = {
            "stage": "T-30", "can_bet": False, "candidates": [{
                "code": "HDC", "market": "讓球", "condition": "-0.5",
                "line": "-0.5", "side": "H", "label": "主 -0.5",
                "odds": 1.70, "prob": .61, "push": 0, "ev": .03,
                "kelly_used": .01, "is_main": True,
            }],
            "selected_odds_observed_at": "2026-08-13T12:30:00+08:00",
            "league": "L", "home": "H", "away": "A", "mins_to_ko": 30,
            "conviction": 60, "weather": {}, "source": "live",
        }
        snapshot = record_picks._snap(result, "2026-08-13T12:30:01+08:00")
        selected = snapshot["market_predictions"][0]
        self.assertEqual(selected["odds"], 1.70)
        self.assertEqual(selected["line"], "-0.5")
        self.assertEqual(selected["side"], "H")
        self.assertEqual(selected["odds_status"], "available")
        self.assertEqual(selected["observed_at"], "2026-08-13T12:30:00+08:00")
        self.assertEqual(selected["observed_board_at"], "2026-08-13T12:30:00+08:00")
        self.assertEqual(selected["source"], "hkjc_public_board")
        self.assertEqual(snapshot["odds_status"], "available")
        self.assertEqual(snapshot["selected_odds_journal"][0]["odds"], 1.70)
        self.assertEqual(
            snapshot["selected_odds_journal"][0]["observed_board_at"],
            "2026-08-13T12:30:00+08:00",
        )

    def test_no_selected_quote_is_explicit_not_silently_complete(self) -> None:
        snapshot = record_picks._snap({
            "stage": "首預", "can_bet": False, "candidates": [],
            "league": "L", "home": "H", "away": "A", "weather": {},
        }, "2026-08-13T12:30:01+08:00")
        self.assertEqual(snapshot["odds_status"], "missing")
        self.assertEqual(snapshot["odds_reason"], "no_selected_market_quote")
        self.assertEqual(snapshot["selected_odds_journal"], [])

    def test_untimestamped_selected_price_is_explicitly_incomplete(self) -> None:
        snapshot = record_picks._snap({
            "stage": "首預", "can_bet": False, "candidates": [{
                "code": "HIL", "market": "入球大細", "condition": "2.5",
                "line": 2.5, "side": "L", "odds": 1.70, "prob": .55,
                "push": 0, "is_main": True,
            }],
            "league": "L", "home": "H", "away": "A", "weather": {},
        }, "2026-08-13T12:30:01+08:00")
        self.assertEqual(snapshot["odds_status"], "missing")
        self.assertEqual(
            snapshot["selected_odds_journal"][0]["reason"],
            "selected_quote_timestamp_unavailable",
        )
