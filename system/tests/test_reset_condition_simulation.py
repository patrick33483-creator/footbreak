from __future__ import annotations

import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

SYSTEM = Path(__file__).resolve().parents[1]
if str(SYSTEM) not in sys.path:
    sys.path.insert(0, str(SYSTEM))

import reset_condition_simulation as reset


class ResetConditionSimulationTests(unittest.TestCase):
    def test_confirmation_is_exact_and_migration_preserves_historical_archive(self):
        with tempfile.TemporaryDirectory() as directory:
            ledger_path = Path(directory, "ledger.json")
            ledger_path.write_text(json.dumps({
                "bankroll": 321, "bets": [{"bet_id": "old"}], "stats": {"pnl": 9}, "log": [],
                "watch": {"preserved": {"stages": []}}, "shadow_bets": [{"bet_id": "old-shadow"}],
                "shadow_stats": {"n": 1}, "shadow_comparison": {"n": 1},
                "condition_simulation_audit": [{"x": 1}], "provider_data": {"keep": True},
            }), encoding="utf-8")
            with patch.object(reset, "LEDGER", ledger_path), patch.object(reset, "LOCK", Path(directory, "ledger.lock")), patch("gen_app_data.main") as dashboard:
                with self.assertRaisesRegex(ValueError, "exact_confirmation_required"):
                    reset.reset("RESET_FOOTBREAK_CONDITION_SIMULATION_5000")
                self.assertEqual(json.loads(ledger_path.read_text(encoding="utf-8"))["bankroll"], 321)
                with self.assertRaisesRegex(ValueError, "post_deploy_confirmation_required"):
                    reset.reset(reset.CONFIRMATION)
                outcome = reset.reset(
                    reset.CONFIRMATION,
                    reset.POST_DEPLOY_CONFIRMATION,
                )
            dashboard.assert_called_once()
            ledger = json.loads(ledger_path.read_text(encoding="utf-8"))
        self.assertEqual(outcome["bankroll"], 50000.0)
        self.assertEqual(outcome["cleared_main_bets"], 0)
        self.assertFalse(outcome["retired_shadow_state_removed"])
        self.assertTrue(outcome["migration_only"])
        self.assertEqual(ledger["bets"], [{"bet_id": "old"}])
        self.assertEqual(ledger["stats"], {"pnl": 9})
        self.assertEqual(ledger["watch"], {"preserved": {"stages": []}})
        self.assertEqual(ledger["provider_data"], {"keep": True})
        self.assertIn("shadow_bets", ledger)
        self.assertIn("shadow_stats", ledger)
        self.assertIn("shadow_comparison", ledger)
        self.assertIn("condition_simulation_audit", ledger)
        namespace = ledger["independent_validation"]
        self.assertEqual(namespace["system"], "footbreak")
        self.assertTrue(namespace["historical_discovery_archive"]["read_only"])
        self.assertEqual(namespace["historical_discovery_archive"]["legacy_bet_count"], 1)


if __name__ == "__main__":
    unittest.main()
