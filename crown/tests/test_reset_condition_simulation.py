from __future__ import annotations

import tempfile
import unittest
from dataclasses import replace
from pathlib import Path
from unittest.mock import patch

from crown.config import settings
from crown.reset_condition_simulation import CONFIRMATION, reset
from crown.state import load_ledger, save_ledger


class ResetConditionSimulationTests(unittest.TestCase):
    def test_confirmation_is_exact_and_migration_preserves_historical_archive(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            config = replace(settings(), state_dir=root / "state", web_root=root / "web")
            config.web_root.mkdir()
            save_ledger(config, {
                "bankroll": 123, "bets": [{"bet_id": "old"}], "watch": {}, "log": [],
                "stats": {"pnl": 9}, "shadow_bets": [{"bet_id": "shadow"}],
                "shadow_stats": {"n": 1}, "shadow_comparison": {"n": 1},
                "handicap_world": {"bets": [{"bet_id": "world"}]},
                "handicap_world_audit": [{"x": 1}], "handicap_world_stats": {"n": 1},
                "condition_simulation_audit": [{"x": 1}],
            })
            with patch("crown.reset_condition_simulation.settings", return_value=config), patch(
                "crown.reset_condition_simulation.write_dashboard_data"
            ) as dashboard:
                with self.assertRaisesRegex(ValueError, "exact_confirmation_required"):
                    reset("RESET_CROWN_CONDITION_SIMULATION_5000")
                self.assertEqual(load_ledger(config)["bankroll"], 123)
                result = reset(CONFIRMATION)
            dashboard.assert_called_once_with(config)
            ledger = load_ledger(config)
        self.assertEqual(result["ok"], True)
        self.assertEqual(result["bankroll"], 50_000.0)
        self.assertEqual(result["cleared_main_bets"], 0)
        self.assertFalse(result["legacy_keys_removed"])
        self.assertTrue(result["migration_only"])
        self.assertEqual(ledger["bankroll"], 123)
        self.assertEqual(ledger["bets"], [{"bet_id": "old"}])
        self.assertEqual(ledger["stats"], {"pnl": 9})
        namespace = ledger["independent_validation"]
        self.assertEqual(namespace["system"], "crown")
        self.assertTrue(namespace["historical_discovery_archive"]["read_only"])
        self.assertEqual(namespace["historical_discovery_archive"]["legacy_bet_count"], 1)
        for key in (
            "shadow_bets", "shadow_stats", "shadow_comparison", "handicap_world",
            "handicap_world_audit", "handicap_world_stats", "condition_simulation_audit",
        ):
            self.assertIn(key, ledger)


if __name__ == "__main__":
    unittest.main()
