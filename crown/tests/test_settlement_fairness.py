"""Network-free regression coverage for durable Crown settlement fairness."""
from __future__ import annotations

import tempfile
import unittest
from dataclasses import replace
from pathlib import Path
from unittest.mock import patch

from crown import settle
from crown.config import settings
from crown.state import load_ledger, save_ledger


class SettlementFairnessTests(unittest.TestCase):
    def setUp(self) -> None:
        self.directory = tempfile.TemporaryDirectory()
        self.config = replace(settings(), state_dir=Path(self.directory.name))

    def tearDown(self) -> None:
        self.directory.cleanup()

    @staticmethod
    def _bet(index: int) -> dict:
        titan_id = str(3_000_000 + index)
        return {
            "bet_id": f"fair-{index:02d}",
            "match_id": titan_id,
            "titan_match_id": titan_id,
            "hkjc_match_id": f"hkjc-{index:02d}",
            "league": "Fair League",
            "home": f"Home {index:02d}",
            "away": f"Away {index:02d}",
            "kickoff": "2020-01-01T12:00:00+08:00",
            "code": "HDC",
            "condition": "-0.5",
            "side": "H",
            "odds": 1.9,
            "stake": 500,
            "status": "PENDING",
            "portfolio": "crown_independent_validation",
            "strategy": "independent-validation-v1",
        }

    def _save(self, count: int) -> None:
        save_ledger(self.config, {
            "bankroll": 50_000,
            "bets": [self._bet(index) for index in range(count)],
            "watch": {},
            "log": [],
            "stats": {},
        })

    def _provider_patches(self, detail_side_effect):
        return (
            patch("crown.settle._refresh_live", return_value={}),
            patch(
                "crown.settle.fetch_official_settlement_bundle",
                return_value=({}, {}),
            ),
            patch("crown.settle.TitanClient.results", return_value=[]),
            patch(
                "crown.settle.TitanClient.result_detail",
                side_effect=detail_side_effect,
            ),
        )

    def test_42_unresolved_rows_all_get_detail_attempt_within_14_passes(self) -> None:
        self._save(42)
        attempted: list[str] = []

        def unresolved(titan_id: str, **_kwargs):
            attempted.append(titan_id)
            return None

        refresh, official, results, detail = self._provider_patches(unresolved)
        with refresh, official, results, detail:
            for _ in range(14):
                outcome = settle.settle_due(self.config)
                self.assertEqual(outcome["settled"], 0)

        self.assertEqual(
            attempted,
            [str(3_000_000 + index) for index in range(42)],
        )
        self.assertTrue(all(
            row["status"] == "PENDING" for row in load_ledger(self.config)["bets"]
        ))

    def test_failures_advance_cursor_so_later_rows_are_not_starved(self) -> None:
        self._save(8)
        attempted: list[str] = []

        def failing(titan_id: str, **_kwargs):
            attempted.append(titan_id)
            raise OSError("provider failure")

        refresh, official, results, detail = self._provider_patches(failing)
        with refresh, official, results, detail:
            settle.settle_due(self.config)
            settle.settle_due(self.config)

        self.assertEqual(
            attempted,
            [str(3_000_000 + index) for index in range(6)],
        )

    def test_deadline_consumed_by_early_row_resumes_at_next_row(self) -> None:
        self._save(5)
        attempted: list[str] = []
        clock = [100.0]

        def consumes_pass(titan_id: str, **_kwargs):
            attempted.append(titan_id)
            clock[0] += 91.0
            return None

        refresh, official, results, detail = self._provider_patches(consumes_pass)
        with patch(
            "crown.settle.time.monotonic", side_effect=lambda: clock[0]
        ), refresh, official, results, detail:
            for _ in range(4):
                settle.settle_due(self.config)

        self.assertEqual(
            attempted,
            [str(3_000_000 + index) for index in range(4)],
        )

    def test_restart_reads_durable_cursor_deterministically(self) -> None:
        self._save(7)
        first_attempts: list[str] = []
        refresh, official, results, detail = self._provider_patches(
            lambda titan_id, **_kwargs: first_attempts.append(titan_id) or None
        )
        with refresh, official, results, detail:
            settle.settle_due(self.config)

        persisted = load_ledger(self.config)["settlement_state"]
        self.assertEqual(
            persisted["titan_detail_cursor"]["row_key"],
            "bet:fair-02",
        )

        # A new settle_due call creates a new TitanClient and reloads the
        # ledger, matching process-restart behavior without any network call.
        restarted_attempts: list[str] = []
        refresh, official, results, detail = self._provider_patches(
            lambda titan_id, **_kwargs: restarted_attempts.append(titan_id) or None
        )
        with refresh, official, results, detail:
            settle.settle_due(self.config)

        self.assertEqual(
            restarted_attempts,
            [str(3_000_000 + index) for index in range(3, 6)],
        )

    def test_successful_detail_settlement_remains_idempotent(self) -> None:
        self._save(1)
        row = self._bet(0)
        verified = {
            "id": row["titan_match_id"],
            "league": row["league"],
            "home": row["home"],
            "away": row["away"],
            "kickoff": row["kickoff"],
            "home_score": 2,
            "away_score": 0,
        }
        refresh, official, results, detail = self._provider_patches(
            lambda _titan_id, **_kwargs: verified
        )
        with refresh, official, results, detail as detail_lookup:
            first = settle.settle_due(self.config)
            second = settle.settle_due(self.config)

        settled = load_ledger(self.config)["bets"][0]
        self.assertEqual(first["settled"], 1)
        self.assertEqual(second["settled"], 0)
        self.assertEqual(settled["status"], "SETTLED")
        self.assertEqual(settled["score"], {"goals": "2-0", "goals_total": 2})
        self.assertEqual(len(settled["history"]), 1)
        self.assertEqual(detail_lookup.call_count, 1)


if __name__ == "__main__":
    unittest.main()
