from __future__ import annotations

import json
import sqlite3
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

from analysis.learning_store import LearningStore
from crown.opening_model import (
    INPUT_POLICY,
    MODEL_VERSION,
    apply_opening_model,
    opening_cutoff,
)


class OpeningModelTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.db = Path(self.temp.name) / "learning.sqlite"
        self.cutoff = datetime(2026, 8, 29, 3, 0, tzinfo=timezone.utc)
        self.fixture = {
            "id": "target",
            "league": "測試聯賽",
            "home": "主隊",
            "away": "客隊",
        }
        epoch = self.cutoff.timestamp()
        self.prices = [
            {"market": "HDC", "line": -0.25, "selection": "H", "odds": 1.90, "source_at": epoch},
            {"market": "HDC", "line": -0.25, "selection": "A", "odds": 1.96, "source_at": epoch},
            {"market": "HIL", "line": 2.75, "selection": "H", "odds": 1.92, "source_at": epoch},
            {"market": "HIL", "line": 2.75, "selection": "L", "odds": 1.94, "source_at": epoch},
        ]
        self.forecasts = [
            {
                "code": "HDC", "market": "讓球", "line": -0.25,
                "side": "H", "odds": 1.90, "prob": 0.507,
                "conviction": 50.7, "label": "old",
            },
            {
                "code": "HIL", "market": "入球大細", "line": 2.75,
                "side": "H", "odds": 1.92, "prob": 0.503,
                "conviction": 50.3, "label": "old",
            },
        ]

    def tearDown(self) -> None:
        self.temp.cleanup()

    def _result(
        self, fixture_id: str, home: str, away: str, home_score: int,
        away_score: int, *, observed_offset_days: int = -1,
    ) -> None:
        kickoff = self.cutoff - timedelta(days=10 + int(fixture_id[-1]))
        with LearningStore(self.db) as store:
            store.record_snapshot(
                "crown", fixture_id, "首預",
                kickoff - timedelta(hours=2), kickoff,
                {
                    "home": home, "away": away, "league": "測試聯賽",
                    "kickoff_hkt": kickoff.isoformat(),
                    "market_predictions": [],
                },
            )
            store.record_result(
                "crown", fixture_id, home_score=home_score,
                away_score=away_score, terminal_status="finished",
                source="test",
                observed_at=self.cutoff + timedelta(days=observed_offset_days),
            )

    def test_missing_history_fails_closed_to_opening_market(self) -> None:
        rows, metadata = apply_opening_model(
            fixture=self.fixture, prices=self.prices,
            forecasts=self.forecasts, learning_db_path=self.db,
        )
        self.assertEqual(len(rows), 2)
        self.assertEqual(metadata["prediction_model"], MODEL_VERSION)
        self.assertEqual(metadata["input_policy"], INPUT_POLICY)
        self.assertEqual(metadata["opening_model_status"], "market_only_history_insufficient")
        self.assertEqual(metadata["late_inputs_used"], [])
        self.assertEqual(metadata["blend"]["team_history"], 0.0)

    def test_uses_only_results_observed_before_opening_cutoff(self) -> None:
        for index in range(3):
            self._result(f"h{index}", "主隊", f"對手甲{index}", 3, 0)
            self._result(f"a{index}", f"對手乙{index}", "客隊", 0, 1)
        self._result(
            "late9", "主隊", "未來對手", 9, 0, observed_offset_days=1,
        )
        rows, metadata = apply_opening_model(
            fixture=self.fixture, prices=self.prices,
            forecasts=self.forecasts, learning_db_path=self.db,
        )
        self.assertEqual(metadata["opening_model_status"], "market_plus_team_history")
        self.assertEqual(metadata["team_history_sample"]["home"], 3)
        self.assertEqual(metadata["team_history_sample"]["away"], 3)
        self.assertEqual(metadata["blend"], {"opening_market": 0.70, "team_history": 0.30})
        self.assertTrue(all(row["reference"] == "opening_market_70_team_history_30" for row in rows))

    def test_opening_hash_and_output_are_deterministic(self) -> None:
        first = apply_opening_model(
            fixture=self.fixture, prices=self.prices,
            forecasts=self.forecasts, learning_db_path=None,
        )
        second = apply_opening_model(
            fixture=self.fixture, prices=list(reversed(self.prices)),
            forecasts=self.forecasts, learning_db_path=None,
        )
        self.assertEqual(first, second)
        self.assertEqual(opening_cutoff(self.prices), self.cutoff)

    def test_result_cache_refreshes_after_database_change(self) -> None:
        self._result("cache0", "主隊", "對手甲", 2, 0)
        _, before = apply_opening_model(
            fixture=self.fixture, prices=self.prices,
            forecasts=self.forecasts, learning_db_path=self.db,
        )
        self.assertEqual(
            before["team_history_sample"]["available_prior_results"], 1,
        )
        self._result("cache1", "對手乙", "客隊", 0, 1)
        _, after = apply_opening_model(
            fixture=self.fixture, prices=self.prices,
            forecasts=self.forecasts, learning_db_path=self.db,
        )
        self.assertEqual(
            after["team_history_sample"]["available_prior_results"], 2,
        )

    def test_database_is_opened_read_only(self) -> None:
        with LearningStore(self.db):
            pass
        before = self.db.read_bytes()
        apply_opening_model(
            fixture=self.fixture, prices=self.prices,
            forecasts=self.forecasts, learning_db_path=self.db,
        )
        self.assertEqual(self.db.read_bytes(), before)


if __name__ == "__main__":
    unittest.main()
