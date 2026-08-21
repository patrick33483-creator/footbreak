from __future__ import annotations

import json
import sqlite3
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from analysis.learning_store import LATEST_SCHEMA_VERSION, LearningStore


KICKOFF = "2026-08-10T12:00:00+08:00"
EARLY = "2026-08-10T11:30:00+08:00"


class LearningStoreTest(unittest.TestCase):
    def _store(self) -> tuple[tempfile.TemporaryDirectory[str], LearningStore]:
        directory = tempfile.TemporaryDirectory()
        store = LearningStore(Path(directory.name) / "learning.sqlite3")
        self.addCleanup(store.close)
        self.addCleanup(directory.cleanup)
        return directory, store

    def test_reopen_existing_wal_store_does_not_reissue_journal_mode_write(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "learning.sqlite3"
            with LearningStore(path):
                pass

            original_connect = sqlite3.connect
            journal_writes: list[str] = []

            class ConnectionProxy:
                def __init__(self, connection):
                    object.__setattr__(self, "_connection", connection)

                def __getattr__(self, name):
                    return getattr(self._connection, name)

                def __setattr__(self, name, value):
                    setattr(self._connection, name, value)

                def execute(self, sql, *args, **kwargs):
                    normalized = " ".join(str(sql).upper().split())
                    if normalized.startswith("PRAGMA JOURNAL_MODE ="):
                        journal_writes.append(normalized)
                        raise sqlite3.OperationalError("database is locked")
                    return self._connection.execute(sql, *args, **kwargs)

            def guarded_connect(*args, **kwargs):
                return ConnectionProxy(original_connect(*args, **kwargs))

            with mock.patch(
                "analysis.learning_store.sqlite3.connect",
                side_effect=guarded_connect,
            ):
                with LearningStore(path) as reopened:
                    self.assertEqual(
                        reopened._connection.execute(
                            "PRAGMA journal_mode"
                        ).fetchone()[0].lower(),
                        "wal",
                    )

            self.assertEqual(journal_writes, [])

    def test_snapshot_changed_pre_kickoff_replay_is_suppressed_and_audited(self) -> None:
        _, store = self._store()
        first = store.record_snapshot(
            "footbreak",
            "match-101",
            "首預",
            EARLY,
            KICKOFF,
            {"odds": {"away": 4.2, "home": 1.8}, "market": "1X2"},
            model_version="fb-3",
            schema_version="prediction-v2",
        )
        duplicate = store.record_snapshot(
            "footbreak",
            "match-101",
            "首預",
            EARLY,
            KICKOFF,
            {"market": "1X2", "odds": {"home": 1.8, "away": 4.2}},
            model_version="different-metadata-does-not-change-payload",
        )
        retry = store.record_snapshot(
            "footbreak",
            "match-101",
            "首預",
            "2026-08-10T11:35:00+08:00",
            KICKOFF,
            {"market": "1X2", "odds": {"home": 1.7, "away": 4.4}},
        )

        self.assertEqual(first["attempt"], 1)
        self.assertFalse(first["idempotent"])
        self.assertEqual(duplicate["snapshot_id"], first["snapshot_id"])
        self.assertTrue(duplicate["idempotent"])
        self.assertEqual(retry["attempt"], 1)
        self.assertEqual(retry["snapshot_id"], first["snapshot_id"])
        self.assertTrue(retry["suppressed_duplicate_stage"])
        self.assertEqual(
            store._connection.execute(  # noqa: SLF001 - verifies database invariant
                "SELECT COUNT(*) FROM prediction_snapshots"
            ).fetchone()[0],
            1,
        )
        self.assertEqual(
            store._connection.execute(  # noqa: SLF001 - verifies audit retention
                "SELECT COUNT(*) FROM snapshot_suppression_audit"
            ).fetchone()[0],
            1,
        )
        with self.assertRaises(sqlite3.IntegrityError):
            store._connection.execute(
                "UPDATE prediction_snapshots SET model_version = 'tampered' WHERE snapshot_id = ?",
                (first["snapshot_id"],),
            )

    def test_post_kickoff_snapshot_is_retained_and_quarantined(self) -> None:
        _, store = self._store()
        payload = {
            "forecast": "主勝",
            "outcome": {"home": 0.5, "draw": 0.3, "away": 0.2},
        }
        valid = store.record_snapshot(
            "crown",
            "fixture-9",
            "T-5",
            EARLY,
            KICKOFF,
            payload,
        )
        quarantined = store.record_snapshot(
            "crown",
            "fixture-9",
            "T-5",
            KICKOFF,
            KICKOFF,
            payload,
        )

        self.assertNotEqual(quarantined["snapshot_id"], valid["snapshot_id"])
        self.assertFalse(quarantined["pre_kickoff"])
        self.assertTrue(quarantined["quarantined"])
        stored = store._connection.execute(  # noqa: SLF001 - validates retained row
            """
            SELECT generated_at, kickoff, pre_kickoff
            FROM prediction_snapshots WHERE snapshot_id = ?
            """,
            (quarantined["snapshot_id"],),
        ).fetchone()
        self.assertEqual(stored["pre_kickoff"], 0)
        self.assertEqual(stored["generated_at"], stored["kickoff"])
        summary = store.summary()
        self.assertEqual(summary["quarantined_snapshots"], 1)
        self.assertEqual(summary["systems"]["crown"]["pre_kickoff_snapshots"], 1)

    def test_legacy_duplicate_reconciliation_keeps_most_complete_row_with_audit(self) -> None:
        _, store = self._store()
        first = store.record_snapshot(
            "crown", "legacy-1", "T-30", EARLY, KICKOFF,
            {
                "league": "League",
                "market_predictions": [{
                    "code": "HIL", "condition": "2.5", "side": "H",
                    "probability": .55, "odds": 1.9,
                }],
            },
        )
        payload = {
            "league": "League", "home": "Alpha", "away": "Beta",
            "market_predictions": [
                {
                    "code": "HIL", "condition": "2.5", "side": "H",
                    "probability": .55, "odds": 1.9,
                },
                {
                    "code": "HDC", "condition": "-0.5", "side": "A",
                    "probability": .51, "odds": 1.95,
                },
            ],
        }
        payload_json, payload_hash = store._json_with_hash(payload, "payload")  # noqa: SLF001
        store._connection.execute(  # noqa: SLF001 - synthesizes a pre-v2 legacy row
            """
            INSERT INTO prediction_snapshots (
                system, fixture_id, stage, attempt, generated_at, kickoff,
                pre_kickoff, payload_json, payload_sha256, model_version,
                schema_version, model_run_id, recorded_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                "crown", "legacy-1", "T-30", 2,
                "2026-08-10T03:40:00.000000+00:00",
                "2026-08-10T04:00:00.000000+00:00", 1,
                payload_json, payload_hash, "legacy", "1", None,
                "2026-08-10T03:40:00.000000+00:00",
            ),
        )

        first_pass = store.reconcile_stage_duplicates("crown")
        second_pass = store.reconcile_stage_duplicates("crown")
        resolution = store._connection.execute(  # noqa: SLF001
            """
            SELECT canonical_snapshot_id, candidates_json, selection_rule
            FROM stage_snapshot_reconciliations
            WHERE system = 'crown' AND fixture_id = 'legacy-1' AND stage = 'T-30'
            """
        ).fetchone()

        self.assertEqual(first_pass["reconciled"], 1)
        self.assertEqual(second_pass["reconciled"], 0)
        self.assertEqual(second_pass["already_reconciled"], 1)
        self.assertNotEqual(int(resolution["canonical_snapshot_id"]), first["snapshot_id"])
        self.assertEqual(len(json.loads(resolution["candidates_json"])), 2)
        self.assertIn("max(valid_complete_market_selections", resolution["selection_rule"])
        self.assertEqual(
            store._connection.execute(  # noqa: SLF001 - raw audit evidence remains
                "SELECT COUNT(*) FROM prediction_snapshots WHERE fixture_id = 'legacy-1'"
            ).fetchone()[0],
            2,
        )

    def test_backtest_export_uses_canonical_pre_kickoff_snapshot_and_excludes_late_attempt(self) -> None:
        _, store = self._store()
        earlier = store.record_snapshot(
            "footbreak", "match-303", "T-5", EARLY, KICKOFF,
            {"conviction": 55, "market_predictions": []},
        )
        latest = store.record_snapshot(
            "footbreak", "match-303", "T-5",
            "2026-08-10T11:55:00+08:00", KICKOFF,
            {"conviction": 62, "market_predictions": []},
        )
        late = store.record_snapshot(
            "footbreak", "match-303", "T-5",
            "2026-08-10T12:01:00+08:00", KICKOFF,
            {"conviction": 99, "market_predictions": []},
        )
        result = store.record_result(
            "footbreak", "match-303", home_score=2, away_score=1,
            source="verified",
        )
        for snapshot, hit, brier in (
            (earlier, 0, .36),
            (latest, 1, .16),
            (late, 0, .81),
        ):
            store.record_grade(
                snapshot, "WDL", "0", "GRADED",
                {"hit": hit, "brier": brier, "log_loss": .5},
                result_id=result["result_id"],
            )
            store.record_grade(
                snapshot, "HIL", "2.5|H", "GRADED",
                {
                    "probability": .6, "target": float(hit), "hit": bool(hit),
                    "brier": brier, "log_loss": .5,
                },
                result_id=result["result_id"],
            )

        wdl_rows, market_rows = store.backtest_rows("footbreak")
        self.assertEqual(len(wdl_rows), 1)
        self.assertEqual(wdl_rows[0]["conf"], 55)
        self.assertEqual(wdl_rows[0]["hit"], 1)
        self.assertEqual(len(market_rows), 1)
        self.assertEqual(market_rows[0]["brier"], .16)

    def test_results_and_grades_are_versioned_and_linked(self) -> None:
        _, store = self._store()
        snapshot = store.record_snapshot(
            "footbreak",
            "match-202",
            "T-30",
            EARLY,
            KICKOFF,
            {"pick": "home", "probability": 0.61},
        )
        provisional = store.record_result(
            "footbreak",
            "match-202",
            home_score=1,
            away_score=0,
            home_corners=4,
            away_corners=3,
            terminal_status="provisional",
            source="feed-a",
            provenance={"endpoint": "scores/202"},
            observed_at="2026-08-10T13:00:00+08:00",
        )
        duplicate_result = store.record_result(
            "footbreak",
            "match-202",
            home_score=1,
            away_score=0,
            home_corners=4,
            away_corners=3,
            terminal_status="provisional",
            source="feed-a",
            provenance={"endpoint": "scores/202"},
            observed_at="2026-08-10T13:05:00+08:00",
        )
        final = store.record_result(
            "footbreak",
            "match-202",
            home_score=2,
            away_score=0,
            home_corners=5,
            away_corners=3,
            terminal_status="finished",
            source="feed-a",
            provenance={"endpoint": "scores/202", "revision": 2},
        )
        grade = store.record_grade(
            snapshot,
            "1X2",
            "home",
            "settled",
            {"hit": 1, "brier": 0.1521},
            result_id=final["result_id"],
        )
        duplicate_grade = store.record_grade(
            snapshot["snapshot_id"],
            "1X2",
            "home",
            "settled",
            {"brier": 0.1521, "hit": 1},
            result_id=final["result_id"],
        )
        regrade = store.record_grade(
            snapshot["snapshot_id"],
            "1X2",
            "home",
            "settled",
            {"hit": 1, "brier": 0.15, "audited": True},
            result_id=final["result_id"],
        )

        self.assertEqual(provisional["attempt"], 1)
        self.assertTrue(duplicate_result["idempotent"])
        self.assertEqual(duplicate_result["result_id"], provisional["result_id"])
        self.assertEqual(final["attempt"], 2)
        self.assertEqual(grade["attempt"], 1)
        self.assertTrue(duplicate_grade["idempotent"])
        self.assertEqual(regrade["attempt"], 2)
        row = store._connection.execute(  # noqa: SLF001 - verifies stored provenance
            "SELECT provenance_json FROM results WHERE result_id = ?", (final["result_id"],)
        ).fetchone()
        self.assertEqual(json.loads(row["provenance_json"])["source"], "feed-a")
        self.assertEqual(store.summary()["systems"]["footbreak"]["results"], 2)
        self.assertEqual(store.summary()["systems"]["footbreak"]["grades"], 2)

    def test_migrations_enable_wal_and_model_manifest_can_link_snapshot(self) -> None:
        _, store = self._store()
        run = store.record_model_run(
            "crown",
            run_key="crown-20260810-001",
            model_version="crown-7",
            schema_version="forecast-v3",
            manifest={"git_sha": "abc123", "features": ["form", "injuries"]},
            started_at="2026-08-10T10:00:00+08:00",
            ended_at="2026-08-10T10:01:00+08:00",
        )
        snapshot = store.record_snapshot(
            "crown",
            "fixture-33",
            "首預",
            EARLY,
            KICKOFF,
            {"forecast": "和局"},
            model_version="crown-7",
            schema_version="forecast-v3",
            model_run_id=run["model_run_id"],
        )

        self.assertFalse(run["idempotent"])
        self.assertEqual(snapshot["attempt"], 1)
        self.assertEqual(
            store._connection.execute("SELECT COUNT(*) FROM schema_migrations").fetchone()[0],
            LATEST_SCHEMA_VERSION,
        )
        self.assertEqual(
            store._connection.execute("PRAGMA journal_mode").fetchone()[0].lower(),
            "wal",
        )
        self.assertEqual(store.summary()["model_runs"], 1)


if __name__ == "__main__":
    unittest.main()
