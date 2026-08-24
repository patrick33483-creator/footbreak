from __future__ import annotations

import os
import tempfile
import unittest
from dataclasses import replace
from datetime import datetime, timedelta
from pathlib import Path
from unittest.mock import patch

from crown.common import HKT
from crown.config import settings
from crown.footbreak_identity_reconciliation import (
    _HEALTH_FILE,
    _record_health,
    reconcile_persisted_hkjc_identities,
    schedule_hkjc_identity_reconciliation,
)
from crown.common import read_json
from crown.state import load_ledger, load_predictions, save_ledger, save_predictions


class FootbreakIdentityReconciliationTests(unittest.TestCase):
    def setUp(self) -> None:
        self.now = datetime.now(HKT)

    def _config(self, directory: str):
        return replace(settings(), state_dir=Path(directory))

    def _seed(self, config) -> None:
        kickoff = self.now + timedelta(hours=2)
        card = {
            "match_id": "crown-1",
            "titan_match_id": "crown-1",
            "native_fixture_id": "crown-1",
            "kickoff_hkt": kickoff.isoformat(),
            "league": "英格蘭超級聯賽",
            "home": "曼城",
            "away": "阿仙奴",
            "stage": "首預",
        }
        save_predictions(config, [card])
        save_ledger(config, {
            "watch": {"crown-1": {**card, "stages": []}},
            "bets": [],
            "log": [],
            "stats": {},
        })

    def test_worker_enriches_persisted_identity_from_strict_hkjc_event(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            config = self._config(directory)
            self._seed(config)
            kickoff = load_predictions(config)[0]["kickoff_hkt"]
            row = {
                "id": "hkjc-1",
                "kickOffTime": kickoff,
                "homeTeam": {
                    "id": "home-1", "name_ch": "曼城", "name_en": "Manchester City",
                },
                "awayTeam": {
                    "id": "away-1", "name_ch": "阿仙奴", "name_en": "Arsenal",
                },
                "tournament": {
                    "name_ch": "英格蘭超級聯賽", "name_en": "Premier League",
                },
            }
            with patch(
                "crown.hkjc.fetch_matches", return_value=[row],
            ), patch(
                "crown.state.project_footbreak_execution_evidence",
            ) as project:
                self.assertEqual(reconcile_persisted_hkjc_identities(config), 1)
                project.assert_not_called()
            self.assertEqual(
                load_predictions(config)[0]["hkjc_match_id"], "hkjc-1",
            )
            self.assertEqual(
                load_ledger(config)["watch"]["crown-1"]["hkjc_match_id"],
                "hkjc-1",
            )

    def test_worker_does_not_import_execution_or_notification_paths(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            config = self._config(directory)
            self._seed(config)
            with patch("crown.hkjc.fetch_matches", return_value=[]):
                self.assertEqual(reconcile_persisted_hkjc_identities(config), 0)

    def test_scheduler_is_disabled_without_launching(self) -> None:
        with tempfile.TemporaryDirectory() as directory, patch.dict(
            os.environ, {"CROWN_HKJC_IDENTITY_RECONCILE_ENABLED": "0"},
        ), patch("multiprocessing.get_context") as context:
            self.assertFalse(
                schedule_hkjc_identity_reconciliation(
                    self._config(directory), ["首預"],
                ),
            )
            context.assert_not_called()

    def test_health_is_isolated_from_prediction_and_ledger_state(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            config = self._config(directory)
            self._seed(config)
            predictions_before = load_predictions(config)
            ledger_before = load_ledger(config)
            _record_health(
                config, "FAILED", detail="TimeoutError", stages=("首預",),
            )
            self.assertEqual(load_predictions(config), predictions_before)
            self.assertEqual(load_ledger(config), ledger_before)
            health = read_json(Path(directory) / _HEALTH_FILE, {})
            self.assertEqual(health["status"], "FAILED")
            self.assertEqual(health["detail"], "TimeoutError")


if __name__ == "__main__":
    unittest.main()
