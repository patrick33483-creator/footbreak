"""Exact-contract adversarial rereview for three-stage capture repair."""
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch

from crown.dashboard_data import _dashboard_watch_card
from crown.period import in_current_period
from deploy.check_dashboard_stage_projection import (
    _should_be_public,
    projection_is_current,
)
from deploy.crown_tick_preempt import urgent_stage_due

UTC = timezone.utc
HKT = timezone(timedelta(hours=8))
NOW = datetime(2026, 8, 28, 12, 0, tzinfo=UTC)
ROOT = Path(__file__).resolve().parents[2]


class ProjectionAdversarialTests(unittest.TestCase):
    def test_attempt_only_never_requires_projection(self):
        ledger = {
            "watch": {"m": {"match_id": "m", "stages": []}},
            "native_stage_attempts": [
                {"hkjc_match_id": "m", "stage": "T-5", "status": "COMMITTED"},
            ],
        }
        self.assertTrue(projection_is_current(ledger, {"matches": []}, now=NOW))

    def test_historical_committed_card_is_not_required(self):
        ledger = {"watch": {"old": {
            "match_id": "old",
            "kickoff": "2020-01-01T00:00:00+00:00",
            "stages": [{"stage": "T-30"}],
        }}}
        self.assertTrue(projection_is_current(
            ledger, {"matches": []}, system="footbreak", now=NOW,
        ))

    def test_duplicate_ledger_stage_is_rejected(self):
        ledger = {"watch": {"m": {
            "match_id": "m",
            "stages": [{"stage": "T-5"}, {"stage": "T-5"}],
        }}}
        dashboard = {
            "matches": [{"match_id": "m", "stages": [{"stage": "T-5"}]}],
        }
        self.assertFalse(projection_is_current(ledger, dashboard, now=NOW))

    def test_data_missing_stage_does_not_require_projection(self):
        ledger = {"watch": {"m": {
            "match_id": "m",
            "stages": [{"stage": "T-5", "status": "DATA_MISSING"}],
        }}}
        self.assertTrue(projection_is_current(ledger, {"matches": []}, now=NOW))

    def test_crown_fractional_final_second_matches_builder_exactly(self):
        kickoff = datetime(2026, 8, 29, 11, 58, 59, 500000, tzinfo=HKT)
        self.assertEqual(
            _should_be_public(
                {"kickoff": kickoff.isoformat()}, system="crown", now=NOW,
            ),
            in_current_period(kickoff, NOW),
        )

    def test_unrecoverable_crown_watch_does_not_request_impossible_republish(self):
        kickoff = NOW + timedelta(minutes=20)
        watch = {
            "match_id": "m", "home": "H", "away": "A", "league": "L",
            "kickoff": kickoff.isoformat(),
            "stages": [
                {"stage": "首預", "match_id": "m"},
                {
                    "stage": "T-30", "status": "OK",
                    "ts": (NOW - timedelta(minutes=1)).isoformat(),
                },
            ],
        }
        with patch("crown.dashboard_data.in_current_period", return_value=True):
            self.assertIsNone(_dashboard_watch_card(watch))
        self.assertTrue(projection_is_current(
            {"watch": {"m": watch}}, {"matches": []},
            system="crown", now=NOW,
        ))

    def test_valid_crown_post_republish_succeeds(self):
        kickoff = NOW + timedelta(minutes=20)
        watch = {
            "match_id": "good", "home": "H", "away": "A", "league": "L",
            "kickoff": kickoff.isoformat(),
            "stages": [
                {"stage": "首預", "match_id": "good", "status": "READY"},
                {
                    "stage": "T-30", "status": "OK",
                    "ts": (NOW - timedelta(minutes=1)).isoformat(),
                },
            ],
        }
        with patch("crown.dashboard_data.in_current_period", return_value=True):
            rebuilt = _dashboard_watch_card(watch)
        self.assertIsNotNone(rebuilt)
        self.assertTrue(projection_is_current(
            {"watch": {"good": watch}}, {"matches": [rebuilt]},
            system="crown", now=NOW,
        ))

    def test_crown_post_republish_succeeds_with_invalid_timed_row(self):
        kickoff = NOW + timedelta(minutes=20)
        watch = {
            "match_id": "m2", "home": "H", "away": "A", "league": "L",
            "kickoff": kickoff.isoformat(),
            "stages": [
                {"stage": "首預", "match_id": "m2", "status": "READY"},
                {"stage": "T-30", "status": "OK"},
            ],
        }
        with patch("crown.dashboard_data.in_current_period", return_value=True):
            rebuilt = _dashboard_watch_card(watch)
        self.assertIsNotNone(rebuilt)
        self.assertEqual(
            [row["stage"] for row in rebuilt["stages"]],
            ["首預"],
        )
        self.assertTrue(projection_is_current(
            {"watch": {"m2": watch}}, {"matches": [rebuilt]},
            system="crown", now=NOW,
        ))

    def test_gen_app_data_uses_only_read_only_ranking_projection(self):
        source = (ROOT / "system" / "gen_app_data.py").read_text(encoding="utf-8")
        self.assertNotIn("project_granular_ranking_evidence(", source)
        self.assertIn("project_frozen_ranking_evidence(", source)


class CrownPreemptAdversarialTests(unittest.TestCase):
    def ledger(self, state="PENDING", due_offset=0):
        kickoff = NOW + timedelta(minutes=5)
        return {"watch": {"m": {
            "kickoff_utc": kickoff.isoformat(),
            "stage_jobs": {"T-5": {
                "due_at_utc": (NOW + timedelta(seconds=due_offset)).isoformat(),
                "kickoff_utc": kickoff.isoformat(),
                "state": state,
            }},
        }}}

    def test_one_second_before_persisted_due_is_not_urgent(self):
        self.assertFalse(urgent_stage_due(self.ledger(due_offset=1), NOW))

    def test_exact_persisted_due_is_urgent(self):
        self.assertTrue(urgent_stage_due(self.ledger(), NOW))

    def test_failed_noncommitted_job_is_urgent(self):
        self.assertTrue(urgent_stage_due(self.ledger(state="FAILED"), NOW))

    def test_committed_job_is_not_urgent(self):
        self.assertFalse(urgent_stage_due(self.ledger(state="COMMITTED"), NOW))

    def test_active_due_job_with_missing_state_fails_closed(self):
        ledger = self.ledger()
        del ledger["watch"]["m"]["stage_jobs"]["T-5"]["state"]
        with self.assertRaises(ValueError):
            urgent_stage_due(ledger, NOW)

    def test_unrelated_legacy_job_does_not_block_valid_urgent_job(self):
        ledger = self.ledger()
        ledger["watch"]["legacy"] = {
            "stage_jobs": {"T-30": {"state": "FAILED"}},
        }
        self.assertTrue(urgent_stage_due(ledger, NOW))


if __name__ == "__main__":
    unittest.main(verbosity=2)
