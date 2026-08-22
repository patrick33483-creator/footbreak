"""Footbreak-native durable T-30/T-5 manifest and attempt regressions."""
from __future__ import annotations

import copy
import json
import subprocess
import sys
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

from system import native_stage_state as state

HKT = timezone(timedelta(hours=8))
UTC = timezone.utc


class NativeStageStateTests(unittest.TestCase):
    def setUp(self) -> None:
        self.kickoff = datetime(2026, 8, 23, 20, 0, tzinfo=HKT)
        self.now = datetime(2026, 8, 23, 19, 30, tzinfo=HKT)

    def _watch(self, match_id: str = "50073037", kickoff: datetime | None = None) -> dict:
        value = kickoff or self.kickoff
        return {
            "match_id": match_id,
            "league": "測試聯賽",
            "home": "主隊",
            "away": "客隊",
            "kickoff": value.isoformat(),
            "stages": [],
        }

    def test_first_look_creates_exact_utc_manifest_without_fabricating_timed_stage(self) -> None:
        watch = self._watch()
        created = state.ensure_first_look_manifest(watch, now=self.now)
        self.assertTrue(created)
        manifest = watch["native_stage_manifest"]
        self.assertEqual(manifest["identity"], {
            "hkjc_match_id": "50073037",
            "kickoff_at_utc": "2026-08-23T12:00:00+00:00",
        })
        self.assertEqual(manifest["origin"], "first_look")
        self.assertEqual(manifest["kickoff_at_hkt"], "2026-08-23T20:00:00+08:00")
        self.assertEqual(manifest["jobs"]["T-30"]["due_at_utc"], "2026-08-23T11:30:00+00:00")
        self.assertEqual(manifest["jobs"]["T-5"]["due_at_hkt"], "2026-08-23T19:55:00+08:00")
        self.assertEqual(watch["stages"], [])
        self.assertFalse(state.ensure_first_look_manifest(watch, now=self.now))

    def test_legacy_migration_creates_jobs_only_for_future_cards_and_never_fakes_stage(self) -> None:
        future = self._watch("future", self.now + timedelta(hours=2))
        past = self._watch("past", self.now - timedelta(seconds=1))
        ledger = {"watch": {"future": future, "past": past}}
        self.assertEqual(state.migrate_future_manifests(ledger, now=self.now), 1)
        self.assertEqual(future["native_stage_manifest"]["origin"], "migration_existing_future_card")
        self.assertNotIn("native_stage_manifest", past)
        self.assertEqual(future["stages"], [])
        self.assertNotIn("native_stage_attempts", ledger)

    def test_post_kickoff_first_look_is_rejected_without_manifest_or_timed_stage(self) -> None:
        watch = self._watch(kickoff=self.now - timedelta(seconds=1))
        self.assertFalse(state.ensure_first_look_manifest(watch, now=self.now))
        self.assertNotIn("native_stage_manifest", watch)
        self.assertEqual(watch["stages"], [])

    def test_due_is_utc_based_restart_safe_and_only_before_kickoff(self) -> None:
        watch = self._watch()
        state.ensure_first_look_manifest(watch, now=self.now)
        ledger = {"watch": {"50073037": watch}}
        self.assertEqual(
            [(row["hkjc_match_id"], row["stage"]) for row in state.due_stage_work(ledger, now=self.now)],
            [("50073037", "T-30")],
        )
        started = state.start_attempt(ledger, watch, "T-30", now=self.now)
        self.assertEqual(started["status"], "STARTED")
        # A restart may resume a non-terminal write-ahead attempt.
        self.assertEqual(
            [(row["hkjc_match_id"], row["stage"]) for row in state.due_stage_work(ledger, now=self.now + timedelta(seconds=5))],
            [("50073037", "T-30")],
        )
        state.finish_attempt(ledger, started, "DATA_MISSING", now=self.now + timedelta(seconds=6), reason="provider_fixture_missing")
        self.assertEqual(state.due_stage_work(ledger, now=self.now + timedelta(seconds=7)), [])
        self.assertEqual([item["status"] for item in ledger["native_stage_attempts"]], ["STARTED", "DATA_MISSING"])
        self.assertEqual(state.due_stage_work(ledger, now=self.kickoff), [])

    def test_post_kickoff_only_expires_pending_attempt_without_quote_or_stage_backfill(self) -> None:
        watch = self._watch()
        state.ensure_first_look_manifest(watch, now=self.now)
        ledger = {"watch": {"50073037": watch}}
        started = state.start_attempt(ledger, watch, "T-5", now=self.kickoff - timedelta(minutes=4))
        expired = state.expire_lapsed_work(ledger, now=self.kickoff)
        self.assertEqual(expired, 2)
        self.assertEqual(ledger["native_stage_attempts"][-1]["status"], "EXPIRED")
        self.assertEqual(ledger["native_stage_attempts"][-1]["attempt_id"], started["attempt_id"])
        self.assertEqual(watch["stages"], [])
        self.assertEqual(state.due_stage_work(ledger, now=self.kickoff + timedelta(minutes=1)), [])

    def test_snapshot_redundancy_keeps_identity_schedule_and_selected_quote_evidence(self) -> None:
        watch = self._watch()
        state.ensure_first_look_manifest(watch, now=self.now)
        base = {
            "stage": "T-30",
            "market_predictions": [{
                "code": "HIL", "side": "H", "line": 2.5, "odds": 1.75,
                "observed_at": "2026-08-23T11:29:30+00:00", "source": "hkjc_public_board",
            }],
        }
        enriched = state.enrich_snapshot(copy.deepcopy(base), watch, "T-30")
        self.assertEqual(enriched["match_id"], "50073037")
        self.assertEqual(enriched["kickoff_at_utc"], "2026-08-23T12:00:00+00:00")
        self.assertEqual(enriched["due_at_hkt"], "2026-08-23T19:30:00+08:00")
        self.assertEqual(enriched["market_predictions"][0]["line"], 2.5)
        self.assertEqual(enriched["market_predictions"][0]["odds"], 1.75)

    def test_completeness_projection_reports_per_fixture_status_without_mutation(self) -> None:
        good = self._watch("good")
        missing = self._watch("missing")
        state.ensure_first_look_manifest(good, now=self.now)
        state.ensure_first_look_manifest(missing, now=self.now)
        ledger = {"watch": {"good": good, "missing": missing}}
        t30 = state.start_attempt(ledger, good, "T-30", now=self.now)
        state.finish_attempt(ledger, t30, "COMMITTED", now=self.now + timedelta(seconds=1))
        t5 = state.start_attempt(ledger, missing, "T-5", now=self.now)
        state.finish_attempt(ledger, t5, "FAILED", now=self.now + timedelta(seconds=1), reason="analysis_timeout")
        before = copy.deepcopy(ledger)
        report = state.completeness_projection(ledger, now=self.now + timedelta(minutes=1))
        by_id = {row["hkjc_match_id"]: row for row in report["fixtures"]}
        self.assertEqual(by_id["good"]["stages"]["T-30"]["status"], "COMMITTED")
        self.assertEqual(by_id["missing"]["stages"]["T-5"]["status"], "FAILED")
        self.assertEqual(report["counts"]["FAILED"], 1)
        self.assertEqual(ledger, before)

    def test_audit_command_is_provider_free_and_does_not_change_ledger(self) -> None:
        watch = self._watch()
        state.ensure_first_look_manifest(watch, now=self.now)
        ledger = {"watch": {"50073037": watch}}
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory, "ledger.json")
            original = json.dumps(ledger, ensure_ascii=False, sort_keys=True)
            path.write_text(original, encoding="utf-8")
            command = [
                sys.executable,
                str(Path(__file__).resolve().parents[2] / "deploy" / "audit-footbreak-native-stage-state.py"),
                "--ledger-path",
                str(path),
            ]
            completed = subprocess.run(command, text=True, capture_output=True, check=False)
            self.assertEqual(completed.returncode, 0, completed.stderr)
            self.assertTrue(json.loads(completed.stdout)["ok"])
            self.assertEqual(path.read_text(encoding="utf-8"), original)


if __name__ == "__main__":
    unittest.main()
