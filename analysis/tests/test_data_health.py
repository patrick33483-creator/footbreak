"""Tests for the read-only 資料健康 (data health) report generator.

Every check here exists because a real production defect was observed at least
once: wrong result statistics, missing corners, NaN, duplicated stage counts,
and small-sample slices being read as signal.
"""
from __future__ import annotations

import json
import math
import os
import sqlite3
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

from analysis import data_health
from analysis.data_health import (
    MIN_UNIQUE_FIXTURES,
    ReadOnlyLearningSource,
    aggregate_metrics,
    audit_summary,
    build_reports,
    public_view,
    run,
    wilson_interval,
)
from analysis.learning_store import LearningStore

NOW = datetime(2026, 8, 12, 6, 0, tzinfo=timezone.utc)


def market_prediction(code: str, *, side="H", condition="0.5", odds=1.9, probability=0.55, **extra):
    return {
        "code": code,
        "market": code,
        "condition": condition,
        "line": condition,
        "side": side,
        "label": f"{code} {condition} {side}",
        "odds": odds,
        "probability": probability,
        "source": "test_source",
        "provider": "test_provider",
        **extra,
    }


class Fixtures:
    """Builds a small immutable learning database on disk."""

    def __init__(self, directory: Path) -> None:
        self.path = directory / "learning.sqlite"

    def build(
        self,
        *,
        system: str = "footbreak",
        fixtures: int = 40,
        stages: tuple[str, ...] = ("首預", "T-30", "T-5"),
        markets: tuple[str, ...] = ("HDC", "HIL"),
        grade_corners: bool = True,
        hit_pattern=lambda index: index % 2 == 0,
        league=lambda index: "英超" if index % 2 == 0 else "德甲",
        conviction=lambda index: 60.0,
        with_results: bool = True,
    ) -> Path:
        with LearningStore(self.path) as store:
            for index in range(fixtures):
                kickoff = NOW - timedelta(days=3, hours=index)
                fixture_id = f"m{index:04d}"
                result = None
                if with_results:
                    result = store.record_result(
                        system,
                        fixture_id,
                        home_score=2,
                        away_score=1,
                        source="verified_result",
                        provenance={"corners_total": 9 if grade_corners else None},
                    )
                for stage_index, stage in enumerate(stages):
                    generated = kickoff - timedelta(hours=len(stages) - stage_index)
                    payload = {
                        "stage": stage,
                        "league": league(index),
                        "conviction": conviction(index),
                        "market_predictions": [
                            market_prediction(code, side="H" if index % 2 == 0 else "A")
                            for code in markets
                        ],
                        "info": {"weather": True, "news": False, "hk_lines": 4},
                        "movement": {"d_total": 0.1},
                    }
                    snapshot = store.record_snapshot(
                        system, fixture_id, stage, generated, kickoff, payload,
                    )
                    if result is None:
                        continue
                    for code in markets:
                        hit = bool(hit_pattern(index))
                        store.record_grade(
                            snapshot["snapshot_id"],
                            code,
                            "0.5|H" if index % 2 == 0 else "0.5|A",
                            "GRADED",
                            {
                                "probability": 0.55,
                                "target": 1.0 if hit else 0.0,
                                "hit": hit,
                                "brier": 0.2,
                                "log_loss": 0.6,
                            },
                            result_id=result["result_id"],
                        )
        return self.path


class DataHealthTest(unittest.TestCase):
    def setUp(self) -> None:
        self._directory = tempfile.TemporaryDirectory()
        self.directory = Path(self._directory.name)
        self.addCleanup(self._directory.cleanup)

    # ── 1. unique-fixture dedup across stages ─────────────────────────────
    def test_unique_fixtures_never_count_stage_rows_as_fixtures(self) -> None:
        database = Fixtures(self.directory).build(fixtures=40)
        report = build_reports(database, now=NOW)["footbreak"]
        overall = report["completeness"]["overall"]
        self.assertEqual(overall["unique_fixtures"], 40)
        self.assertEqual(overall["stage_rows"], 120)
        self.assertEqual(overall["prediction_rows"], 240)
        market_slice = {item["key"]: item for item in report["error_slices"]["market"]}
        # 3 stages × 40 fixtures produce 120 rows but still only 40 fixtures.
        self.assertEqual(market_slice["HDC"]["unique_fixtures"], 40)
        self.assertEqual(market_slice["HDC"]["rows"], 120)
        stage_slice = {item["key"]: item for item in report["error_slices"]["stage"]}
        self.assertEqual(sum(item["unique_fixtures"] for item in stage_slice.values()), 120)
        self.assertEqual(report["baseline"]["unique_fixtures"], 40)

    def test_definitions_state_stage_rows_are_reference_only(self) -> None:
        report = build_reports(Fixtures(self.directory).build(fixtures=3), now=NOW)["footbreak"]
        self.assertTrue(report["policy"]["stage_rows_are_reference_only"])
        self.assertEqual(report["policy"]["primary_sample"], "unique_fixtures")
        self.assertIn("次要參考", report["definitions"]["stage_rows"])

    # ── 2. no mean of means ───────────────────────────────────────────────
    def test_aggregate_metrics_uses_raw_rows_not_mean_of_means(self) -> None:
        def rows(count: int, hit: bool):
            return [{
                "grade_state": "GRADED",
                "probability": 0.6,
                "grade_metrics": {"hit": hit, "target": 1.0 if hit else 0.0},
            } for _ in range(count)]

        big = rows(90, True)
        small = rows(10, False)
        combined = aggregate_metrics(big + small)
        mean_of_means = (
            aggregate_metrics(big)["accuracy"] + aggregate_metrics(small)["accuracy"]
        ) / 2
        self.assertEqual(combined["accuracy"], 0.9)
        self.assertNotEqual(combined["accuracy"], mean_of_means)
        self.assertEqual(combined["decided_rows"], 100)
        self.assertEqual(combined["hits"], 90)

    def test_pushes_are_excluded_from_accuracy_denominator(self) -> None:
        rows = [
            {"grade_state": "GRADED", "probability": 0.5,
             "grade_metrics": {"hit": True, "target": 1.0}},
            {"grade_state": "GRADED", "probability": 0.5,
             "grade_metrics": {"hit": None, "target": 0.5}},
        ]
        metrics = aggregate_metrics(rows)
        self.assertEqual(metrics["decided_rows"], 1)
        self.assertEqual(metrics["pushes"], 1)
        self.assertEqual(metrics["accuracy"], 1.0)

    # ── 3. finite metrics ─────────────────────────────────────────────────
    def test_every_published_number_is_finite(self) -> None:
        database = Fixtures(self.directory).build(fixtures=35)
        payload = public_view(build_reports(database, now=NOW)["footbreak"])

        def walk(value):
            if isinstance(value, dict):
                for item in value.values():
                    walk(item)
            elif isinstance(value, list):
                for item in value:
                    walk(item)
            elif isinstance(value, float):
                self.assertTrue(math.isfinite(value), f"non-finite value: {value}")

        walk(payload)
        # NaN / Infinity must not even be representable in the artifact.
        json.dumps(payload, allow_nan=False)

    def test_nonfinite_and_missing_source_values_are_reported_not_propagated(self) -> None:
        database = self.directory / "learning.sqlite"
        kickoff = NOW - timedelta(days=2)
        with LearningStore(database) as store:
            snapshot = store.record_snapshot(
                "footbreak", "bad1", "T-5", kickoff - timedelta(hours=1), kickoff,
                {
                    "league": "英超",
                    "conviction": 61,
                    "market_predictions": [
                        market_prediction("HIL", probability="NaN", odds="Infinity", condition="abc"),
                        {"code": "HDC", "condition": None, "side": None,
                         "odds": None, "probability": None},
                    ],
                },
            )
            self.assertTrue(snapshot["pre_kickoff"])
        report = build_reports(database, now=NOW)["footbreak"]
        overall = report["completeness"]["overall"]
        self.assertGreaterEqual(overall["structural_issues"]["nonfinite_prediction_values"], 2)
        self.assertEqual(overall["missing_or_invalid"]["probability"], 2)
        self.assertEqual(overall["missing_or_invalid"]["line"], 2)
        self.assertEqual(overall["missing_or_invalid"]["selection_side"], 1)
        codes = {issue["code"] for issue in report["issues"]}
        self.assertIn("nonfinite_prediction_values", codes)
        self.assertIn("missing_probability", codes)
        json.dumps(public_view(report), allow_nan=False)

    def test_wilson_interval_is_bounded_and_defined(self) -> None:
        self.assertIsNone(wilson_interval(0, 0))
        low, high = wilson_interval(5, 10)
        self.assertLess(low, 0.5)
        self.assertGreater(high, 0.5)
        self.assertGreaterEqual(low, 0.0)
        self.assertLessEqual(high, 1.0)

    # ── 4. missing / corner coverage ──────────────────────────────────────
    def test_missing_corner_results_are_counted_and_flagged(self) -> None:
        database = self.directory / "learning.sqlite"
        with LearningStore(database) as store:
            for index in range(4):
                fixture_id = f"c{index}"
                kickoff = NOW - timedelta(days=10 if index < 2 else 1)
                result = store.record_result(
                    "crown", fixture_id, home_score=1, away_score=1, source="titan",
                )
                snapshot = store.record_snapshot(
                    "crown", fixture_id, "T-5", kickoff - timedelta(hours=1), kickoff,
                    {"league": "西甲", "conviction": 60,
                     "market_predictions": [market_prediction("CHL", side="L", condition="9.5")]},
                )
                store.record_grade(
                    snapshot["snapshot_id"], "CHL", "9.5|L", "NOT_APPLICABLE",
                    {"reason": "corners_result_missing"}, result_id=result["result_id"],
                )
        report = build_reports(database, now=NOW)["crown"]
        corner = report["completeness"]["overall"]["corner_result"]
        self.assertEqual(corner["corner_prediction_fixtures"], 4)
        self.assertEqual(corner["settle_due_fixtures"], 4)
        self.assertEqual(corner["fixtures_with_corner_result"], 0)
        self.assertEqual(corner["coverage"], 0.0)
        self.assertEqual(corner["missing_fixtures"], 4)
        self.assertEqual(corner["stale_beyond_retry_fixtures"], 2)
        codes = {issue["code"] for issue in report["issues"]}
        self.assertIn("stale_missing_corner_results", codes)
        self.assertIn("missing_corner_results", codes)
        reasons = report["completeness"]["by_market"]["CHL"]["exclusion_reasons"]
        self.assertEqual(reasons["corners_result_missing"], 4)

    def test_stale_unresolved_results_respect_the_existing_grace_policy(self) -> None:
        database = self.directory / "learning.sqlite"
        with LearningStore(database) as store:
            # inside the 105-minute grace window: not yet stale
            fresh_kickoff = NOW - timedelta(minutes=30)
            store.record_snapshot(
                "footbreak", "fresh", "T-5", fresh_kickoff - timedelta(hours=1), fresh_kickoff,
                {"league": "英超", "conviction": 60,
                 "market_predictions": [market_prediction("HDC")]},
            )
            old_kickoff = NOW - timedelta(days=1)
            store.record_snapshot(
                "footbreak", "old", "T-5", old_kickoff - timedelta(hours=1), old_kickoff,
                {"league": "英超", "conviction": 60,
                 "market_predictions": [market_prediction("HDC")]},
            )
        report = build_reports(database, now=NOW)["footbreak"]
        result = report["completeness"]["overall"]["result"]
        self.assertEqual(result["grace_minutes"], 105)
        self.assertEqual(result["settle_due_fixtures"], 1)
        self.assertEqual(result["stale_unresolved_fixtures"], 1)
        self.assertEqual(result["coverage"], 0.0)

    def test_verified_terminal_no_contest_counts_as_result_coverage(self) -> None:
        """A postponed/cancelled exact-ID decision is terminal, not stale."""
        database = self.directory / "learning.sqlite"
        kickoff = NOW - timedelta(days=1)
        with LearningStore(database) as store:
            store.record_snapshot(
                "crown", "postponed", "T-5", kickoff - timedelta(hours=1), kickoff,
                {"league": "英超", "conviction": 60,
                 "market_predictions": [market_prediction("HDC")]},
            )
            store.record_result(
                "crown", "postponed", terminal_status="MATCHPOSTPONED",
                source="hkjc_official_exact_id_terminal_status",
                provenance={"terminal_reason": "fixture_not_played"},
            )
        report = build_reports(database, now=NOW)["crown"]
        result = report["completeness"]["overall"]["result"]
        self.assertEqual(result["settle_due_fixtures"], 1)
        self.assertEqual(result["fixtures_with_result"], 1)
        self.assertEqual(result["stale_unresolved_fixtures"], 0)
        self.assertEqual(result["coverage"], 1.0)

    def test_result_coverage_and_sources_come_from_raw_counts(self) -> None:
        database = Fixtures(self.directory).build(fixtures=12)
        report = build_reports(database, now=NOW)["footbreak"]
        result = report["completeness"]["overall"]["result"]
        self.assertEqual(result["settle_due_fixtures"], 12)
        self.assertEqual(result["fixtures_with_result"], 12)
        self.assertEqual(result["coverage"], 1.0)
        self.assertEqual(
            report["completeness"]["overall"]["result_sources"], {"verified_result": 12}
        )

    # ── 5. small-sample labels ────────────────────────────────────────────
    def test_small_slices_are_labelled_insufficient_and_never_recommended(self) -> None:
        database = Fixtures(self.directory).build(
            fixtures=40,
            markets=("HIL",),
            league=lambda index: "英超" if index < 35 else f"小聯賽{index}",
            hit_pattern=lambda index: index < 35,
        )
        report = build_reports(database, now=NOW)["footbreak"]
        leagues = {item["key"]: item for item in report["error_slices"]["league"]}
        self.assertEqual(leagues["英超"]["sample_status"], "sufficient")
        self.assertFalse(leagues["英超"]["small_sample"])
        tiny = [item for key, item in leagues.items() if key.startswith("小聯賽")]
        self.assertTrue(tiny)
        for item in tiny:
            self.assertEqual(item["sample_status"], "insufficient")
            self.assertTrue(item["small_sample"])
            self.assertEqual(item["minimum_unique_fixtures"], MIN_UNIQUE_FIXTURES)
        recommended = {
            item["id"] for item in report["hil_v4_diagnostics"]["recommendations"]
        }
        for item in tiny:
            self.assertNotIn(f"slice:league:{item['key']}", recommended)
        for entry in report["hil_v4_diagnostics"]["worst_stable_slices"]:
            self.assertGreaterEqual(entry["unique_fixtures"], MIN_UNIQUE_FIXTURES)

    def test_diagnostics_are_advice_only_and_never_claim_causation(self) -> None:
        database = Fixtures(self.directory).build(fixtures=40, markets=("HIL",))
        diagnostics = build_reports(database, now=NOW)["footbreak"]["hil_v4_diagnostics"]
        self.assertFalse(diagnostics["auto_apply"])
        self.assertFalse(diagnostics["retraining"])
        self.assertFalse(diagnostics["is_model"])
        joined = json.dumps(diagnostics, ensure_ascii=False)
        self.assertIn("唔會自動套用", joined)
        self.assertIn("唔會重訓", joined)
        self.assertIn("非因果", joined)
        self.assertIn("corner_independent_source", diagnostics["missing_feature_families"])

    # ── 6. deterministic ordering ─────────────────────────────────────────
    def test_report_is_deterministic_for_the_same_inputs(self) -> None:
        database = Fixtures(self.directory).build(fixtures=33)
        first = build_reports(database, now=NOW)
        second = build_reports(database, now=NOW)
        self.assertEqual(
            json.dumps(first, ensure_ascii=False, sort_keys=False),
            json.dumps(second, ensure_ascii=False, sort_keys=False),
        )
        markets = [item["key"] for item in first["footbreak"]["error_slices"]["market"]]
        self.assertEqual(markets, ["HDC", "HIL", "CHL"])
        stages = [item["key"] for item in first["footbreak"]["error_slices"]["stage"]]
        self.assertEqual(stages, ["首預", "T-30", "T-5"])
        confidences = [item["key"] for item in first["footbreak"]["error_slices"]["confidence"]]
        self.assertEqual(confidences, ["58-64"])

    # ── 7. no raw / private leak ──────────────────────────────────────────
    def test_public_artifact_contains_no_raw_rows_ids_or_secrets(self) -> None:
        database = self.directory / "learning.sqlite"
        kickoff = NOW - timedelta(days=1)
        with LearningStore(database) as store:
            store.record_snapshot(
                "crown", "SECRET-FIXTURE-9001", "T-5", kickoff - timedelta(hours=1), kickoff,
                {
                    "league": "西甲",
                    "conviction": 60,
                    "home": "SECRET_HOME_TEAM",
                    "away": "SECRET_AWAY_TEAM",
                    "api_token": "tok_do_not_leak",
                    "market_predictions": [market_prediction("HIL")],
                },
            )
        payload = json.dumps(
            public_view(build_reports(database, now=NOW)["crown"]), ensure_ascii=False
        )
        for secret in (
            "SECRET-FIXTURE-9001", "SECRET_HOME_TEAM", "SECRET_AWAY_TEAM",
            "tok_do_not_leak", "api_token", "payload_json", "coefficient",
        ):
            self.assertNotIn(secret, payload)
        self.assertNotIn("fixture_id", payload)
        self.assertNotIn("match_id", payload)

    def test_public_view_drops_nothing_the_dashboard_needs(self) -> None:
        report = build_reports(Fixtures(self.directory).build(fixtures=31), now=NOW)["footbreak"]
        payload = public_view(report)
        for key in (
            "generated_at", "status", "policy", "definitions", "completeness",
            "issues", "issue_counts", "baseline", "error_slices", "hil_v4_diagnostics",
        ):
            self.assertIn(key, payload)

    # ── 8. atomic report behaviour + read-only source ─────────────────────
    def test_public_write_is_atomic_and_never_leaves_a_partial_file(self) -> None:
        database = Fixtures(self.directory).build(fixtures=31)
        public = self.directory / "www" / "data-health.json"
        run(database, self.directory / "private.json", {"footbreak": public}, now=NOW)
        first = json.loads(public.read_text(encoding="utf-8"))
        self.assertEqual(first["system"], "footbreak")
        self.assertEqual(oct(public.stat().st_mode & 0o777), "0o644")
        run(database, self.directory / "private.json", {"footbreak": public}, now=NOW)
        self.assertEqual(json.loads(public.read_text(encoding="utf-8")), first)
        self.assertEqual(
            [name for name in os.listdir(public.parent) if name.startswith(".")], []
        )
        self.assertEqual(
            oct((self.directory / "private.json").stat().st_mode & 0o777), "0o600"
        )

    def test_source_database_is_opened_read_only_and_is_unchanged(self) -> None:
        database = Fixtures(self.directory).build(fixtures=5)
        before = database.read_bytes()
        with ReadOnlyLearningSource(database) as source:
            with self.assertRaises(sqlite3.OperationalError):
                source._connection.execute("DELETE FROM prediction_snapshots")
            with self.assertRaises(sqlite3.OperationalError):
                source._connection.execute(
                    "INSERT INTO results (system, fixture_id, result_attempt,"
                    " terminal_status, provenance_json, result_sha256, observed_at,"
                    " recorded_at) VALUES ('crown','x',1,'finished','{}',"
                    " '0000000000000000000000000000000000000000000000000000000000000000',"
                    " 'now','now')"
                )
        build_reports(database, now=NOW)
        self.assertEqual(database.read_bytes(), before)

    def test_missing_database_produces_a_safe_unavailable_report(self) -> None:
        reports = build_reports(self.directory / "absent.sqlite", now=NOW)
        for system in ("footbreak", "crown"):
            self.assertEqual(reports[system]["status"], "unavailable")
            self.assertEqual(reports[system]["status_reason"], "learning_database_missing")
            self.assertFalse(reports[system]["hil_v4_diagnostics"]["recommendations"])
            json.dumps(public_view(reports[system]), allow_nan=False)

    # ── 9. duplicate stage keys and quarantined rows ──────────────────────
    def test_changed_stage_replays_are_suppressed_before_they_reach_health(self) -> None:
        database = self.directory / "learning.sqlite"
        kickoff = NOW - timedelta(days=1)
        with LearningStore(database) as store:
            for probability in (0.51, 0.52, 0.53):
                store.record_snapshot(
                    "footbreak", "dup1", "T-5", kickoff - timedelta(hours=2), kickoff,
                    {"league": "英超", "conviction": 60,
                     "market_predictions": [market_prediction("HDC", probability=probability)]},
                )
            store.record_snapshot(
                "footbreak", "dup1", "T-5", kickoff + timedelta(minutes=5), kickoff,
                {"league": "英超", "conviction": 60,
                 "market_predictions": [market_prediction("HDC", probability=0.9)]},
            )
        report = build_reports(database, now=NOW)["footbreak"]
        overall = report["completeness"]["overall"]
        self.assertEqual(overall["unique_fixtures"], 1)
        self.assertEqual(overall["stage_rows"], 1)
        self.assertEqual(overall["prediction_rows"], 1)
        self.assertEqual(overall["duplicate_stage_keys"], 0)
        self.assertEqual(overall["quarantined_post_kickoff_rows"], 1)
        codes = {issue["code"] for issue in report["issues"]}
        self.assertIn("post_kickoff_quarantined_rows", codes)

    def test_duplicate_market_keys_inside_one_stage_are_deduplicated(self) -> None:
        database = self.directory / "learning.sqlite"
        kickoff = NOW - timedelta(days=1)
        with LearningStore(database) as store:
            store.record_snapshot(
                "crown", "dupmk", "T-30", kickoff - timedelta(hours=1), kickoff,
                {"league": "意甲", "conviction": 60, "market_predictions": [
                    market_prediction("HIL", side="L", condition="2.5"),
                    market_prediction("HIL", side="L", condition="2.5", odds=1.95),
                ]},
            )
        report = build_reports(database, now=NOW)["crown"]
        overall = report["completeness"]["overall"]
        self.assertEqual(overall["prediction_rows"], 1)
        self.assertEqual(overall["structural_issues"]["duplicate_market_keys_in_stage"], 1)

    def test_post_kickoff_rows_never_enter_metrics(self) -> None:
        database = self.directory / "learning.sqlite"
        kickoff = NOW - timedelta(days=1)
        with LearningStore(database) as store:
            late = store.record_snapshot(
                "crown", "late1", "T-5", kickoff + timedelta(minutes=10), kickoff,
                {"league": "法甲", "conviction": 90,
                 "market_predictions": [market_prediction("HIL")]},
            )
            self.assertTrue(late["quarantined"])
            result = store.record_result("crown", "late1", home_score=3, away_score=0, source="t")
            store.record_grade(
                late["snapshot_id"], "HIL", "0.5|H", "GRADED",
                {"probability": 0.99, "target": 1.0, "hit": True},
                result_id=result["result_id"],
            )
        report = build_reports(database, now=NOW)["crown"]
        self.assertEqual(report["completeness"]["overall"]["prediction_rows"], 0)
        self.assertEqual(report["baseline"]["graded_rows"], 0)
        self.assertIsNone(report["baseline"]["accuracy"])

    # ── 10. status and audit summary ──────────────────────────────────────
    def test_status_reflects_sample_and_issue_state(self) -> None:
        small = build_reports(Fixtures(self.directory).build(fixtures=3), now=NOW)["footbreak"]
        self.assertEqual(small["status"], "insufficient_data")
        empty = build_reports(Fixtures(self.directory).build(fixtures=3), now=NOW)["crown"]
        self.assertEqual(empty["status"], "no_data")

    def test_audit_summary_is_compact_and_row_free(self) -> None:
        database = Fixtures(self.directory).build(fixtures=31)
        report = build_reports(database, now=NOW)["footbreak"]
        summary = audit_summary(report)
        self.assertEqual(summary["system"], "footbreak")
        self.assertEqual(summary["status"], report["status"])
        self.assertIn("unique_fixtures", summary["counts"])
        self.assertIn("result", summary["coverage"])
        self.assertFalse(summary["auto_apply"])
        self.assertLessEqual(len(summary["top_recommendations"]), 5)
        self.assertLessEqual(len(summary["top_issues"]), 5)
        text = json.dumps(summary, ensure_ascii=False)
        self.assertNotIn("m0000", text)
        self.assertNotIn("payload", text)
        self.assertLess(len(text), 4000)

    def test_both_systems_are_reported_independently(self) -> None:
        Fixtures(self.directory).build(fixtures=5, system="footbreak")
        database = self.directory / "learning.sqlite"
        with LearningStore(database) as store:
            kickoff = NOW - timedelta(days=1)
            store.record_snapshot(
                "crown", "x1", "T-5", kickoff - timedelta(hours=1), kickoff,
                {"league": "英超", "conviction": 60,
                 "market_predictions": [market_prediction("HIL")]},
            )
        reports = build_reports(database, now=NOW)
        self.assertEqual(reports["footbreak"]["completeness"]["overall"]["unique_fixtures"], 5)
        self.assertEqual(reports["crown"]["completeness"]["overall"]["unique_fixtures"], 1)
        self.assertEqual(reports["footbreak"]["system"], "footbreak")
        self.assertEqual(reports["crown"]["system"], "crown")


class DataHealthJobIsolationTest(unittest.TestCase):
    """Health generation stays read-only; alert-delivery failures are visible."""

    def test_health_generation_is_guarded_but_reconcile_alert_delivery_can_fail(self) -> None:
        root = Path(__file__).resolve().parents[2]
        backtest = (root / "deploy" / "backtest-run.sh").read_text(encoding="utf-8")
        reconcile = (root / "deploy" / "reconcile-results.sh").read_text(encoding="utf-8")
        for script, name in ((backtest, "backtest-run.sh"), (reconcile, "reconcile-results.sh")):
            with self.subTest(script=name):
                self.assertIn("analysis.data_health", script)
                self.assertIn("資料健康", script)
                # The invocation is inside a conditional and its failure is
                # only logged: no `exit`, no `set -e` abort, no failure flag.
                before = script.split("analysis.data_health", 1)[0]
                self.assertTrue(
                    before.rstrip().endswith("-m"),
                    "data-health must be invoked as a module",
                )
                guard = before.rsplit("\n", 1)[-1]
                self.assertTrue(
                    guard.lstrip().startswith(("if ", "if! ", "if !")),
                    f"data-health invocation must be guarded, got: {guard!r}",
                )
                after = script.split("analysis.data_health", 1)[1]
                block = after.split("\nfi\n", 1)[0]
                self.assertNotIn("exit 1", block)
                self.assertNotIn("\n    failed=1", block)
                self.assertIn("資料健康報告生成失敗", block)
        # The generation itself stays isolated.  A separate direct-Telegram
        # alert checks both public reports after generation; only delivery
        # failure marks the reconciliation service failed for retry/visibility.
        self.assertIn("analysis.health_alert", reconcile)
        self.assertIn("--footbreak-report /var/www/footbreak/data-health.json", reconcile)
        self.assertIn("--crown-report /var/www/crown/data-health.json", reconcile)
        self.assertIn("--env-file /etc/footbreak.env", reconcile)
        self.assertIn("--generation-failed", reconcile)
        alert_block = reconcile.split("analysis.health_alert", 1)[1].split("\nfi\n", 1)[0]
        self.assertIn("failed=1", alert_block)

    def test_nginx_serves_the_artifact_with_no_store(self) -> None:
        root = Path(__file__).resolve().parents[2]
        for name in ("deploy/nginx-footbreak.conf", "deploy/nginx-crown.conf"):
            conf = (root / name).read_text(encoding="utf-8")
            with self.subTest(conf=name):
                self.assertIn("location = /data-health.json {", conf)
                block = conf.split("location = /data-health.json {", 1)[1].split("}", 1)[0]
                self.assertIn("no-store", block)


if __name__ == "__main__":
    unittest.main()


class DataHealthMetricUnitTests(unittest.TestCase):
    """Accuracy/Brier/log loss aggregate graded prediction rows, not fixtures.

    Several stage rows of the same fixture are correlated repeated measures.
    Unique fixtures are only the sample-size basis, so nothing in the report
    may read as one-per-fixture, and no recommendation may treat repeated
    stage rows as independent evidence.
    """

    def setUp(self) -> None:
        self._directory = tempfile.TemporaryDirectory()
        self.directory = Path(self._directory.name)
        self.addCleanup(self._directory.cleanup)

    def report(self, **kwargs) -> dict:
        database = Fixtures(self.directory).build(**kwargs)
        return build_reports(database, now=NOW)["footbreak"]

    def test_metrics_policy_states_the_unit_explicitly(self) -> None:
        policy = self.report(fixtures=40)["metrics_policy"]
        self.assertEqual(policy["metric_unit"], "graded_prediction_rows")
        self.assertEqual(policy["sample_basis"], "unique_fixtures")
        self.assertTrue(policy["correlated_stage_rows"])
        self.assertFalse(policy["metrics_are_one_per_fixture"])
        self.assertEqual(
            policy["recommendation_evidence_unit"],
            "graded_prediction_rows_latest_stage_per_fixture_market",
        )

    def test_all_stage_slices_are_flagged_as_correlated(self) -> None:
        report = self.report(fixtures=40)
        market = {item["key"]: item for item in report["error_slices"]["market"]}
        hdc = market["HDC"]
        self.assertEqual(hdc["metric_unit"], "graded_prediction_rows")
        self.assertEqual(hdc["sample_basis"], "unique_fixtures")
        # 40 fixtures × 3 stages of graded rows: repeated, correlated measures.
        self.assertEqual(hdc["unique_fixtures"], 40)
        self.assertEqual(hdc["graded_rows"], 120)
        self.assertTrue(hdc["correlated_stage_rows"])
        self.assertTrue(report["baseline"]["correlated_stage_rows"])
        # A single-stage slice repeats nothing, so it is honestly not flagged.
        stage = {item["key"]: item for item in report["error_slices"]["stage"]}
        self.assertFalse(stage["T-5"]["correlated_stage_rows"])
        self.assertEqual(stage["T-5"]["metric_unit"], "graded_prediction_rows")

    def test_primary_diagnostic_keeps_one_row_per_fixture_and_market(self) -> None:
        report = self.report(fixtures=40, markets=("HDC", "HIL"))
        primary = report["primary_diagnostic"]
        self.assertEqual(
            primary["unit"], "graded_prediction_rows_latest_stage_per_fixture_market"
        )
        self.assertEqual(primary["stage_priority"], ["T-5", "T-30", "首預"])
        # Two markets × 40 fixtures, one stage each — never 3 stages.
        self.assertEqual(primary["baseline"]["rows"], 80)
        self.assertEqual(primary["baseline"]["graded_rows"], 80)
        self.assertEqual(primary["baseline"]["unique_fixtures"], 40)
        market = {item["key"]: item for item in primary["error_slices"]["market"]}
        self.assertEqual(market["HDC"]["graded_rows"], 40)
        self.assertEqual(market["HDC"]["unique_fixtures"], 40)
        self.assertFalse(market["HDC"]["correlated_stage_rows"])

    def test_primary_diagnostic_selects_the_latest_available_stage(self) -> None:
        rows = [
            {
                "fixture_id": "f1", "market": "HIL", "target_key": "2.5|L",
                "stage": stage, "kickoff": NOW,
                "generated_at": NOW - timedelta(hours=hours),
                "grade_state": "GRADED", "grade_metrics": {}, "probability": 0.5,
            }
            for stage, hours in (("首預", 30), ("T-30", 3), ("T-5", 1))
        ]
        kept = data_health.latest_stage_rows(rows)
        self.assertEqual([row["stage"] for row in kept], ["T-5"])
        # Without a T-5 snapshot the rule falls back, never forward.
        kept = data_health.latest_stage_rows(rows[:2])
        self.assertEqual([row["stage"] for row in kept], ["T-30"])
        kept = data_health.latest_stage_rows(rows[:1])
        self.assertEqual([row["stage"] for row in kept], ["首預"])

    def test_primary_diagnostic_is_deterministic(self) -> None:
        database = Fixtures(self.directory).build(fixtures=35)
        first = build_reports(database, now=NOW)["footbreak"]["primary_diagnostic"]
        second = build_reports(database, now=NOW)["footbreak"]["primary_diagnostic"]
        self.assertEqual(json.dumps(first, sort_keys=True), json.dumps(second, sort_keys=True))

    def test_recommendations_never_use_repeated_stage_rows_as_evidence(self) -> None:
        report = self.report(fixtures=40)
        diagnostics = report["hil_v4_diagnostics"]
        self.assertEqual(
            diagnostics["evidence_unit"],
            "graded_prediction_rows_latest_stage_per_fixture_market",
        )
        self.assertFalse(diagnostics["evidence_uses_repeated_stage_rows"])
        self.assertEqual(diagnostics["evidence_sample_basis"], "unique_fixtures")
        self.assertEqual(
            diagnostics["baseline"]["metric_unit"],
            "graded_prediction_rows_latest_stage_per_fixture_market",
        )
        for item in diagnostics["worst_stable_slices"]:
            self.assertFalse(item["correlated_stage_rows"], item)
            self.assertEqual(
                item["metric_unit"],
                "graded_prediction_rows_latest_stage_per_fixture_market",
            )
            # The threshold still counts unique fixtures, not rows.
            self.assertGreaterEqual(item["unique_fixtures"], MIN_UNIQUE_FIXTURES)

    def test_recommendation_evidence_rows_never_exceed_the_all_stage_rows(self) -> None:
        """A guard against the evidence silently reverting to stage rows."""
        report = self.report(fixtures=40)
        primary_rows = report["primary_diagnostic"]["baseline"]["graded_rows"]
        all_rows = report["baseline"]["graded_rows"]
        self.assertLess(primary_rows, all_rows)
        for item in report["hil_v4_diagnostics"]["worst_stable_slices"]:
            self.assertLessEqual(item["graded_rows"], primary_rows)

    def test_definitions_and_notes_never_imply_one_row_per_fixture(self) -> None:
        report = self.report(fixtures=40)
        definitions = report["definitions"]
        self.assertIn("已結算預測列", definitions["metric_unit"])
        self.assertIn("樣本量基礎", definitions["sample_basis"])
        self.assertIn("唔係每場一行", definitions["accuracy"])
        self.assertIn("最新賽前階段", definitions["primary_diagnostic"])
        notes = " ".join(report["hil_v4_diagnostics"]["notes"])
        self.assertIn("最新階段", notes)
        self.assertIn("獨立證據", notes)

    def test_audit_summary_carries_both_units_without_private_rows(self) -> None:
        report = self.report(fixtures=40)
        summary = audit_summary(report)
        self.assertEqual(summary["baseline"]["metric_unit"], "graded_prediction_rows")
        self.assertTrue(summary["baseline"]["correlated_stage_rows"])
        primary = summary["primary_diagnostic_baseline"]
        self.assertEqual(
            primary["metric_unit"],
            "graded_prediction_rows_latest_stage_per_fixture_market",
        )
        self.assertFalse(primary["correlated_stage_rows"])
        self.assertEqual(
            summary["recommendation_evidence_unit"],
            "graded_prediction_rows_latest_stage_per_fixture_market",
        )
        self.assertFalse(summary["metrics_policy"]["metrics_are_one_per_fixture"])
        blob = json.dumps(summary, ensure_ascii=False)
        self.assertNotIn("fixture_id", blob)
        self.assertNotIn("m0000", blob)
        self.assertNotIn("payload", blob)

    def test_public_artifact_exposes_the_primary_diagnostic(self) -> None:
        report = self.report(fixtures=40)
        public = public_view(report)
        self.assertIn("primary_diagnostic", public)
        self.assertIn("metrics_policy", public)
        blob = json.dumps(public, ensure_ascii=False)
        self.assertNotIn("m0000", blob)
        self.assertNotIn("payload", blob)

    def test_unavailable_report_still_declares_the_units(self) -> None:
        report = data_health.unavailable_report("crown", NOW, "database_missing")
        self.assertEqual(report["metrics_policy"]["metric_unit"], "graded_prediction_rows")
        self.assertFalse(report["hil_v4_diagnostics"]["evidence_uses_repeated_stage_rows"])
        self.assertEqual(
            report["primary_diagnostic"]["unit"],
            "graded_prediction_rows_latest_stage_per_fixture_market",
        )

    def test_primary_diagnostic_metrics_stay_finite(self) -> None:
        report = self.report(fixtures=40)
        for dimension, items in report["primary_diagnostic"]["error_slices"].items():
            for item in items:
                for key in ("accuracy", "brier", "log_loss"):
                    value = item[key]
                    if value is not None:
                        self.assertTrue(math.isfinite(value), (dimension, item["key"], key))
