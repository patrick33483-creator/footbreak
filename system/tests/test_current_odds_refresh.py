"""Safety regression coverage for the Footbreak current-card quote refresh."""
from __future__ import annotations

import json
import tempfile
import unittest
from datetime import datetime, timedelta
from pathlib import Path

from system import refresh_current_odds as refresh
from system.hkjc_feed import HKT


def _board_match(match_id: str) -> dict:
    return {
        "id": match_id,
        "status": "PREEVENT",
        "foPools": [{
            "oddsType": "HDC",
            "status": "SELLING",
            "lines": [{
                "condition": "-0.5",
                "main": True,
                "combinations": [
                    {"selections": [{"str": "H"}], "currentOdds": "1.70"},
                    {"selections": [{"str": "A"}], "currentOdds": "1.85"},
                ],
            }],
        }],
    }


class FootbreakCurrentOddsRefreshTests(unittest.TestCase):
    def test_deploy_step_is_the_guarded_dashboard_only_command(self) -> None:
        root = Path(__file__).resolve().parents[2]
        workflow = (root / ".github" / "workflows" / "deploy.yml").read_text(
            encoding="utf-8"
        )
        wrapper = (root / "deploy" / "footbreak-refresh-current-odds.sh").read_text(
            encoding="utf-8"
        )
        self.assertIn("footbreak-refresh-current-odds.sh", workflow)
        self.assertIn("does not run prediction, ledger, settlement, bet,", workflow)
        self.assertIn("refresh_current_odds.py", wrapper)
        self.assertIn("--predictions \"$APP_DIR/system/predictions.json\"", wrapper)
        self.assertIn("--dashboard-data \"$WEB_ROOT/data.json\"", wrapper)

    def test_only_future_cards_receive_separate_current_quote_journal(self) -> None:
        now = datetime(2026, 8, 13, 14, 30, tzinfo=HKT)
        future = {
            "match_id": "future", "kickoff_hkt": (now + timedelta(minutes=20)).isoformat(),
            "stage": "T-30", "candidates": [
                {"code": "HDC", "line": "-0.5", "side": "A", "prob": .48,
                 "push": 0, "is_main": True},
                {"code": "HDC", "line": "-0.5", "side": "H", "prob": .61,
                 "push": 0, "is_main": True},
            ],
        }
        past = {
            "match_id": "past", "kickoff_hkt": (now - timedelta(minutes=1)).isoformat(),
            "stage": "T-5", "candidates": [future["candidates"][0]],
        }
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            predictions = root / "predictions.json"
            dashboard = root / "data.json"
            status = root / "private" / "status.json"
            predictions.write_text(json.dumps([future, past]), encoding="utf-8")
            dashboard.write_text(json.dumps({"matches": [future, past]}), encoding="utf-8")

            result = refresh.refresh(
                predictions, dashboard, status,
                fetcher=lambda: [_board_match("future")], now=now,
            )

            self.assertEqual(result["status"], "refreshed")
            self.assertEqual(result["updated"], 1)
            stored = json.loads(predictions.read_text(encoding="utf-8"))
            quote = stored[0]["current_selected_odds_journal"][0]
            self.assertEqual(quote["side"], "H")
            self.assertEqual(quote["line"], "-0.5")
            self.assertEqual(quote["odds"], 1.70)
            self.assertEqual(quote["source"], "hkjc_public_board")
            self.assertEqual(quote["provider"], "HKJC")
            self.assertEqual(quote["observed_at"], now.isoformat(timespec="seconds"))
            self.assertEqual(quote["observed_board_at"], now.isoformat(timespec="seconds"))
            self.assertNotIn("current_selected_odds_journal", stored[1])
            # Historical stage/candidate evidence is never overwritten.
            self.assertEqual(stored[0]["candidates"], future["candidates"])
            public = json.loads(dashboard.read_text(encoding="utf-8"))
            self.assertIn("current_selected_odds_journal", public["matches"][0])
            self.assertNotIn("current_selected_odds_journal", public["matches"][1])
            self.assertEqual(
                json.loads(status.read_text(encoding="utf-8"))["scope"],
                "future_current_cards_only",
            )

    def test_board_failure_is_fail_closed_without_card_or_dashboard_mutation(self) -> None:
        now = datetime(2026, 8, 13, 14, 30, tzinfo=HKT)
        card = {
            "match_id": "future", "kickoff_hkt": (now + timedelta(minutes=20)).isoformat(),
            "stage": "T-30", "candidates": [],
        }
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            predictions = root / "predictions.json"
            dashboard = root / "data.json"
            status = root / "private" / "status.json"
            predictions.write_text(json.dumps([card]), encoding="utf-8")
            dashboard.write_text(json.dumps({"matches": [card]}), encoding="utf-8")
            original_predictions = predictions.read_bytes()
            original_dashboard = dashboard.read_bytes()

            def broken_fetch():
                raise RuntimeError("offline")

            result = refresh.refresh(
                predictions, dashboard, status, fetcher=broken_fetch, now=now,
            )

            self.assertEqual(result["status"], "refresh_failed_closed")
            self.assertEqual(result["reason"], "board_fetch_RuntimeError")
            self.assertEqual(predictions.read_bytes(), original_predictions)
            self.assertEqual(dashboard.read_bytes(), original_dashboard)
            self.assertEqual(
                json.loads(status.read_text(encoding="utf-8"))["updated"], 0,
            )


if __name__ == "__main__":
    unittest.main()
