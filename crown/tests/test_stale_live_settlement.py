"""Regression coverage for stale PinnAPI live-cache settlement fallback."""
from __future__ import annotations

import unittest
from datetime import datetime, timedelta
from dataclasses import replace
from pathlib import Path
import tempfile
from unittest.mock import patch

from crown import settle
from crown.common import HKT
from crown.config import settings


class StaleLiveSettlementTests(unittest.TestCase):
    def _config(self, directory: str):
        return replace(settings(), state_dir=Path(directory))

    def _bet(self) -> dict:
        return {
            "bet_id": "world-stale", "match_id": "titan-1", "titan_match_id": "titan-1",
            "hkjc_match_id": "hkjc-1", "pinnapi_event_id": "pin-1",
            "league": "League", "home": "Home", "away": "Away",
            "kickoff": "2026-08-01T12:00:00+08:00",
            "code": "HDC", "condition": "-0.5", "side": "H",
            "odds": 1.8, "stake": 1000, "status": "PENDING",
            "portfolio": "condition_simulation", "strategy": "granular-condition-v1",
        }

    def test_stale_seen_live_uses_strict_exact_official_result(self) -> None:
        ledger = {"bets": [self._bet()], "shadow_bets": [], "watch": {}, "stats": {}}
        stale = (datetime.now(HKT) - timedelta(seconds=settle.LIVE_CACHE_STALE_SECONDS + 1)).isoformat()
        with tempfile.TemporaryDirectory() as directory, \
             patch("crown.settle.load_ledger", return_value=ledger), \
             patch("crown.settle._refresh_live", return_value={
                 "pin-1": {"seen_live": True, "last_live_seen_at": stale},
             }), \
             patch("crown.settle.fetch_official_results", return_value={
                 "hkjc-1": {"home_score": 2, "away_score": 0},
             }), \
             patch("crown.settle.fetch_official_match_statuses", return_value={}), \
             patch("crown.settle.TitanClient.results", return_value=[]), \
             patch("crown.settle.save_ledger"):
            result = settle.settle_due(self._config(directory))
        self.assertEqual(result["settled"], 1)
        self.assertEqual(ledger["bets"][0]["settlement_source"], "hkjc_official_exact_id")
        self.assertNotIn("settlement_pending_reason", ledger["bets"][0])

    def test_fresh_seen_live_does_not_fallback_and_records_reason(self) -> None:
        ledger = {"bets": [self._bet()], "shadow_bets": [], "watch": {}, "stats": {}}
        fresh = datetime.now(HKT).isoformat()
        with tempfile.TemporaryDirectory() as directory, \
             patch("crown.settle.load_ledger", return_value=ledger), \
             patch("crown.settle._refresh_live", return_value={
                 "pin-1": {"seen_live": True, "last_live_seen_at": fresh},
             }), \
             patch("crown.settle.fetch_official_results") as official, \
             patch("crown.settle.fetch_official_match_statuses") as statuses, \
             patch("crown.settle.TitanClient.results") as titan_results, \
             patch("crown.settle.save_ledger"):
            result = settle.settle_due(self._config(directory))
        self.assertEqual(result["settled"], 0)
        self.assertEqual(ledger["bets"][0]["settlement_pending_reason"], "pinnapi_live_cache_fresh")
        official.assert_not_called()
        statuses.assert_not_called()
        titan_results.assert_not_called()

    def test_repeated_live_refresh_failure_releases_legacy_cache(self) -> None:
        ledger = {"bets": [self._bet()], "shadow_bets": [], "watch": {}, "stats": {}}
        cache = {"pin-1": {"seen_live": True, "live_refresh_failures": 1}}
        with tempfile.TemporaryDirectory() as directory, \
             patch("crown.settle.load_ledger", return_value=ledger), \
             patch("crown.settle._refresh_live", side_effect=OSError("temporary")), \
             patch("crown.settle.read_json", return_value=cache), \
             patch("crown.settle.write_json_atomic"), \
             patch("crown.settle.fetch_official_results", return_value={
                 "hkjc-1": {"home_score": 2, "away_score": 0},
             }), \
             patch("crown.settle.fetch_official_match_statuses", return_value={}), \
             patch("crown.settle.TitanClient.results", return_value=[]), \
             patch("crown.settle.save_ledger"):
            result = settle.settle_due(self._config(directory))
        self.assertEqual(result["settled"], 1)
        self.assertEqual(cache["pin-1"]["live_refresh_failures"], 2)


if __name__ == "__main__":
    unittest.main()
