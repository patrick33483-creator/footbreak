"""Safety coverage for the one-time verified Crown result backfill."""
from __future__ import annotations

import copy
import hashlib
import json
import stat
import tempfile
import unittest
from dataclasses import replace
from pathlib import Path

from crown.backfill_confirmed_results import BackfillError, execute
from crown.config import settings
from crown.state import load_ledger, save_ledger


class ConfirmedResultBackfillTests(unittest.TestCase):
    def setUp(self) -> None:
        self.directory = tempfile.TemporaryDirectory()
        self.root = Path(self.directory.name)
        self.config = replace(settings(), state_dir=self.root / "state")
        self.fixtures_path = self.root / "fixtures_42_verified.json"
        self.manifest_path = self.root / "manifest.json"
        self.fixtures = self._fixtures()
        self.fixtures_path.write_text(
            json.dumps(self.fixtures, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )
        self.ledger = self._ledger(self.fixtures)
        save_ledger(self.config, self.ledger)

    def tearDown(self) -> None:
        self.directory.cleanup()

    @staticmethod
    def _fixtures() -> list[dict]:
        fixtures = []
        for index in range(42):
            confirmed = index < 34
            fixtures.append({
                "No.": index + 1,
                "match_id": str(3_000_000 + index),
                "kickoff_HKT": f"2026-08-{23 + index // 24:02d} "
                               f"{index % 24:02d}:00",
                "league": "測試聯賽",
                "home": f"主隊 {index:02d}",
                "away": f"客隊 {index:02d}",
                "bet": "Over 2.5",
                "final_status": "Finished",
                "home_score_90": 2,
                "away_score_90": 1,
                "grade": "Won",
                "verification": "CONFIRMED" if confirmed else "CONFLICT",
                "primary_source": "https://example.test/primary",
                "secondary_source": "https://example.test/secondary",
                "notes": "test",
            })
        return fixtures

    @staticmethod
    def _row(fixture: dict, serial: int) -> dict:
        return {
            "bet_id": f"row-{serial:03d}",
            "match_id": fixture["match_id"],
            "titan_match_id": fixture["match_id"],
            "league": fixture["league"],
            "home": fixture["home"],
            "away": fixture["away"],
            "kickoff": fixture["kickoff_HKT"] + ":00+08:00",
            "code": "HIL",
            "condition": "2.5",
            "side": "H",
            "odds": 1.9,
            "stake": 500,
            "status": "PENDING",
            "portfolio": "crown_wilson_test",
            "strategy": "wilson-test-strategy-v1",
            "history": [{"ts": "2026-08-20T00:00:00+08:00", "action": "created"}],
        }

    @classmethod
    def _ledger(cls, fixtures: list[dict]) -> dict:
        rows = []
        serial = 0
        for index, fixture in enumerate(fixtures):
            copies = (
                2 if index < 32 else
                1 if index < 34 else
                3 if index == 34 else 2
            )
            for _ in range(copies):
                rows.append(cls._row(fixture, serial))
                serial += 1
        rows.append({
            "bet_id": "unrelated",
            "match_id": "unrelated",
            "status": "PENDING",
            "opaque": {"must": ["remain", "byte-equivalent"]},
        })
        return {
            "bankroll": 50_000,
            "bets": rows,
            "watch": {"unrelated": {"stage": "T-5"}},
            "log": [{"kind": "unrelated"}],
            "stats": {"old": True},
        }

    def _run(self, **kwargs):
        fixture_sha256 = hashlib.sha256(self.fixtures_path.read_bytes()).hexdigest()
        return execute(
            self.config,
            self.fixtures_path,
            manifest_path=self.manifest_path,
            expected_fixture_sha256=fixture_sha256,
            **kwargs,
        )

    def test_default_dry_run_is_read_only_and_reports_exact_cohorts(self) -> None:
        ledger_path = self.config.state_dir / "ledger.json"
        before = ledger_path.read_bytes()
        report = self._run()
        self.assertEqual(ledger_path.read_bytes(), before)
        self.assertEqual(report["mode"], "dry-run")
        self.assertFalse(report["applied"])
        self.assertEqual(report["counts"]["pending_rows_before"], 66)
        self.assertEqual(report["counts"]["changed_rows"], 66)
        self.assertEqual(report["counts"]["conflict_rows_unchanged"], 17)
        self.assertEqual(report["side_effects"], {
            "provider_calls": 0, "telegram_calls": 0, "dashboard_publications": 0,
        })
        self.assertIsNone(report["backup_path"])
        self.assertTrue(self.manifest_path.exists())
        self.assertFalse((self.config.state_dir / "backups").exists())

    def test_apply_uses_cas_backup_existing_settlement_and_recompute(self) -> None:
        before = copy.deepcopy(load_ledger(self.config))
        dry_run = self._run()
        report = self._run(
            apply=True,
            expected_ledger_sha256=dry_run["ledger_before_sha256"],
        )
        after = load_ledger(self.config)
        self.assertTrue(report["applied"])
        self.assertEqual(report["counts"]["changed_rows"], 66)
        self.assertEqual(report["counts"]["conflict_rows_unchanged"], 17)
        target_ids = {fixture["match_id"] for fixture in self.fixtures[:34]}
        conflict_ids = {fixture["match_id"] for fixture in self.fixtures[34:]}
        targets = [row for row in after["bets"] if row.get("match_id") in target_ids]
        self.assertEqual(len(targets), 66)
        self.assertTrue(all(row["status"] == "SETTLED" for row in targets))
        self.assertTrue(all(row["result"] == "Won" for row in targets))
        self.assertTrue(all(row["score"] == {
            "goals": "2-1", "goals_total": 3,
        } for row in targets))
        self.assertTrue(all(
            row["settlement_source"].startswith("manual_verified_backfill:")
            for row in targets
        ))
        before_conflicts = [
            row for row in before["bets"] if row.get("match_id") in conflict_ids
        ]
        after_conflicts = [
            row for row in after["bets"] if row.get("match_id") in conflict_ids
        ]
        self.assertEqual(after_conflicts, before_conflicts)
        self.assertEqual(
            next(row for row in after["bets"] if row.get("bet_id") == "unrelated"),
            next(row for row in before["bets"] if row.get("bet_id") == "unrelated"),
        )
        self.assertEqual(after["stats"]["n_settled"], 66)
        self.assertEqual(after["wilson_validation"]["stats"]["settled"], 66)
        backup = Path(report["backup_path"])
        self.assertEqual(
            stat.S_IMODE(backup.stat().st_mode), stat.S_IRUSR,
        )
        self.assertEqual(
            hashlib.sha256(backup.read_bytes()).hexdigest(),
            dry_run["ledger_before_sha256"],
        )

    def test_apply_is_idempotent_and_second_run_is_byte_exact_noop(self) -> None:
        first_dry_run = self._run()
        self._run(
            apply=True,
            expected_ledger_sha256=first_dry_run["ledger_before_sha256"],
        )
        ledger_path = self.config.state_dir / "ledger.json"
        after_first = ledger_path.read_bytes()
        second_dry_run = self._run()
        self.assertTrue(second_dry_run["idempotent_noop"])
        self.assertEqual(second_dry_run["counts"]["already_applied_rows"], 66)
        second = self._run(
            apply=True,
            expected_ledger_sha256=second_dry_run["ledger_before_sha256"],
        )
        self.assertTrue(second["idempotent_noop"])
        self.assertFalse(second["applied"])
        self.assertEqual(ledger_path.read_bytes(), after_first)

    def test_stale_cas_preserves_concurrently_added_unrelated_row(self) -> None:
        dry_run = self._run()
        ledger = load_ledger(self.config)
        ledger["bets"].append({
            "bet_id": "concurrent", "match_id": "other", "status": "PENDING",
        })
        save_ledger(self.config, ledger)
        with self.assertRaisesRegex(BackfillError, "ledger_cas_mismatch"):
            self._run(
                apply=True,
                expected_ledger_sha256=dry_run["ledger_before_sha256"],
            )
        self.assertIn(
            "concurrent",
            {row.get("bet_id") for row in load_ledger(self.config)["bets"]},
        )

    def test_identity_mismatch_fails_closed_without_backup_or_write(self) -> None:
        ledger_path = self.config.state_dir / "ledger.json"
        ledger = load_ledger(self.config)
        ledger["bets"][0]["home"] = "wrong exact home"
        save_ledger(self.config, ledger)
        before = ledger_path.read_bytes()
        with self.assertRaisesRegex(BackfillError, "row_identity_mismatch"):
            self._run()
        self.assertEqual(ledger_path.read_bytes(), before)
        self.assertFalse((self.config.state_dir / "backups").exists())

    def test_conflict_cannot_be_relabelled_confirmed(self) -> None:
        fixtures = copy.deepcopy(self.fixtures)
        fixtures[34]["verification"] = "CONFIRMED"
        fixtures[0]["verification"] = "CONFLICT"
        self.fixtures_path.write_text(
            json.dumps(fixtures, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )
        with self.assertRaisesRegex(BackfillError, "confirmed_row"):
            self._run()

    def test_conflict_rows_are_hash_protected_without_trusting_disputed_identity(
        self,
    ) -> None:
        ledger = load_ledger(self.config)
        conflict_ids = {fixture["match_id"] for fixture in self.fixtures[34:]}
        for row in ledger["bets"]:
            if row.get("match_id") in conflict_ids:
                row["home"] = f"provider alias {row['match_id']}"
                row["away"] = "disputed opponent"
                row["kickoff"] = "2026-08-30T00:00:00+08:00"
                row["portfolio"] = "quarantined-conflict"
        save_ledger(self.config, ledger)
        ledger_path = self.config.state_dir / "ledger.json"
        before = load_ledger(self.config)
        before_conflicts = [
            copy.deepcopy(row) for row in before["bets"]
            if row.get("match_id") in conflict_ids
        ]

        dry_run = self._run()
        self.assertEqual(dry_run["counts"]["conflict_rows_unchanged"], 17)
        report = self._run(
            apply=True,
            expected_ledger_sha256=dry_run["ledger_before_sha256"],
        )
        self.assertEqual(report["counts"]["changed_rows"], 66)
        after_conflicts = [
            copy.deepcopy(row) for row in load_ledger(self.config)["bets"]
            if row.get("match_id") in conflict_ids
        ]
        self.assertEqual(after_conflicts, before_conflicts)
        self.assertNotEqual(ledger_path.read_bytes(), b"")

    def test_partial_prior_application_is_rejected(self) -> None:
        dry_run = self._run()
        self._run(
            apply=True,
            expected_ledger_sha256=dry_run["ledger_before_sha256"],
        )
        ledger = load_ledger(self.config)
        ledger["bets"][0].update({
            "status": "PENDING", "result": None, "score": None,
        })
        ledger["bets"][0].pop("settlement_source", None)
        save_ledger(self.config, ledger)
        with self.assertRaisesRegex(BackfillError, "partially_applied"):
            self._run()

    def test_apply_requires_explicit_cas_hash(self) -> None:
        with self.assertRaisesRegex(
            BackfillError, "apply_requires_exact_expected_ledger_sha256",
        ):
            self._run(apply=True)

    def test_authorized_fixture_artifact_is_hash_pinned(self) -> None:
        with self.assertRaisesRegex(BackfillError, "fixture_file_sha256_mismatch"):
            execute(
                self.config,
                self.fixtures_path,
                manifest_path=self.manifest_path,
            )


if __name__ == "__main__":
    unittest.main()
