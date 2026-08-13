"""Regression coverage for Crown selected quote evidence and safe refresh."""
from __future__ import annotations

import unittest
from datetime import datetime, timedelta
from unittest.mock import Mock, patch

from crown.common import HKT
from crown.engine import refresh_current_quotes
from crown.ledger import _snapshot


class CrownOddsEvidenceTests(unittest.TestCase):
    def test_snapshot_has_compact_selected_quote_journal(self) -> None:
        prediction = {
            "stage": "T-30", "match_id": "t1",
            "forecast_candidates": [{
                "code": "HIL", "market": "入球大細", "condition": "2.5",
                "line": 2.5, "side": "L", "label": "細", "odds": 1.70,
                "prob": .54, "provider": "Crown", "source": "titan007-crown-id-3",
                "observed_at": 1_786_000_000,
            }],
        }
        snapshot = _snapshot(prediction, "T-30")
        selected = snapshot["market_predictions"][0]
        self.assertEqual(selected["odds_status"], "available")
        self.assertEqual(selected["line"], 2.5)
        self.assertEqual(selected["quote_source"], "titan007-crown-id-3")
        self.assertEqual(snapshot["selected_odds_journal"][0]["source"], "titan007-crown-id-3")
        self.assertEqual(snapshot["selected_odds_journal"][0]["observed_at"], 1_786_000_000)

    def test_snapshot_is_missing_when_any_selected_market_quote_is_missing(self) -> None:
        prediction = {
            "stage": "T-5", "match_id": "mixed",
            "forecast_candidates": [
                {
                    "code": "HDC", "market": "讓球", "condition": "-0.5",
                    "line": -0.5, "side": "H", "odds": 1.70, "prob": .55,
                    "observed_at": 1_786_000_000,
                },
                {
                    "code": "HIL", "market": "入球大細", "condition": "2.5",
                    "line": 2.5, "side": "L", "odds": None, "prob": .54,
                    "observed_at": 1_786_000_000,
                },
            ],
        }
        snapshot = _snapshot(prediction, "T-5")
        self.assertEqual(snapshot["odds_status"], "missing")
        self.assertEqual(
            snapshot["odds_reason"],
            "one_or_more_selected_quotes_unavailable",
        )
        self.assertEqual(
            [row["odds_status"] for row in snapshot["selected_odds_journal"]],
            ["available", "missing"],
        )

    def test_snapshot_marks_an_untimestamped_quote_incomplete(self) -> None:
        snapshot = _snapshot({
            "match_id": "no-timestamp",
            "forecast_candidates": [{
                "code": "HDC", "condition": "-0.5", "line": -0.5,
                "side": "H", "odds": 1.70, "prob": .55,
            }],
        }, "首預")
        selected = snapshot["selected_odds_journal"][0]
        self.assertEqual(selected["odds_status"], "missing")
        self.assertEqual(
            selected["reason"], "selected_quote_timestamp_unavailable"
        )

    def test_refresh_only_touches_not_yet_started_dashboard_cards(self) -> None:
        now = datetime.now(HKT)
        future = {
            "match_id": "future", "titan_match_id": "future", "league": "L",
            "home": "H", "away": "A",
            "kickoff_hkt": (now + timedelta(minutes=20)).isoformat(),
            "book_odds": {"crown": []}, "stage": "T-30",
        }
        past = {
            "match_id": "past", "titan_match_id": "past", "league": "L",
            "home": "H", "away": "A",
            "kickoff_hkt": (now - timedelta(minutes=2)).isoformat(),
            "book_odds": {"crown": []}, "stage": "T-5",
        }
        config = Mock()
        merged = []
        with patch("crown.engine.load_predictions", return_value=[future, past]), \
             patch("crown.engine.TitanClient") as client, \
             patch("crown.engine._refresh_crown_quote", side_effect=lambda old, *_: {**old, "_quote_refresh_only": True, "book_odds": {"crown": [{"odds": 1.8}]}}), \
             patch("crown.engine.state_lock") as lock, \
             patch("crown.engine.merge_predictions", side_effect=lambda _c, rows, now=None: merged.extend(rows) or rows):
            lock.return_value.__enter__.return_value = None
            result = refresh_current_quotes(config)
        self.assertTrue(result["safe_quote_refresh_only"])
        self.assertEqual(result["predictions"], 1)
        self.assertEqual(merged[0]["match_id"], "future")
        self.assertEqual(client.return_value.crown_price_snapshot.call_count, 1)

    def test_current_refresh_journal_is_separate_from_stage_evidence(self) -> None:
        from crown.engine import _refresh_crown_quote
        kickoff = datetime.now(HKT) + timedelta(minutes=20)
        previous = {
            "match_id": "t1", "league": "L", "home": "H", "away": "A",
            "kickoff_hkt": kickoff.isoformat(),
            "forecast_candidates": [{
                "code": "HDC", "line": -0.5, "condition": "-0.5", "side": "H",
            }],
            "market_predictions": [{"code": "HDC", "odds": 1.70}],
            "book_odds": {"crown": []},
        }
        refreshed = _refresh_crown_quote(
            previous,
            {"id": "t1", "league": "L", "home": "H", "away": "A", "kickoff": kickoff},
            Mock(),
            {"prices": [{"market": "HDC", "line": -0.5, "selection": "H",
                         "odds": 1.70, "source_at": 123.0}],
             "asian_ok": True, "total_ok": True},
        )
        self.assertEqual(refreshed["current_selected_odds_journal"][0]["odds"], 1.70)
        self.assertEqual(
            refreshed["current_selected_odds_journal"][0]["observed_board_at"],
            refreshed["current_odds_refreshed_at"],
        )
        self.assertEqual(
            refreshed["current_odds_refresh_source"], "titan007-crown-id-3"
        )
        self.assertEqual(refreshed["market_predictions"], previous["market_predictions"])
