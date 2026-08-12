"""The production audit must expose only safe, compact summaries.

``deploy/audit-result-state.py`` is a script, not a package module, so it is
loaded by path here.  These tests cover the new data-health summary and the
extended Crown CHL prospective whitelist.
"""
from __future__ import annotations

import importlib.util
import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[2]


def load_audit():
    sys.path.insert(0, str(ROOT))
    spec = importlib.util.spec_from_file_location(
        "footbreak_audit_result_state", ROOT / "deploy" / "audit-result-state.py"
    )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


REPORT = {
    "schema_version": 1,
    "report": "data_health",
    "system": "crown",
    "generated_at": "2026-08-12T04:00:00+00:00",
    "status": "degraded",
    "policy": {"minimum_unique_fixtures": 30},
    "completeness": {
        "overall": {
            "unique_fixtures": 120,
            "stage_rows": 340,
            "prediction_rows": 690,
            "graded_rows": 600,
            "pending_rows": 60,
            "excluded_rows": 30,
            "duplicate_stage_keys": 3,
            "quarantined_post_kickoff_rows": 2,
            "result": {"coverage": 0.93, "stale_unresolved_fixtures": 8},
            "corner_result": {"coverage": 0.69, "stale_beyond_retry_fixtures": 9},
            "structural_issues": {"nonfinite_prediction_values": 1},
        },
        "by_market": {"CHL": {"unique_fixtures": 90}},
    },
    "issues": [
        {"code": f"issue{index}", "severity": "high", "scope": "overall", "count": index}
        for index in range(9)
    ],
    "issue_counts": {"high": 9, "warn": 0, "info": 0, "total": 9},
    "baseline": {
        "unique_fixtures": 120, "accuracy": 0.53, "brier": 0.244,
        "log_loss": 0.68, "sample_status": "sufficient",
    },
    "error_slices": {"league": [{"key": "SECRET_LEAGUE_ROWS"}]},
    "hil_v4_diagnostics": {
        "recommendations": [
            {"id": f"r{index}", "kind": "weak_slice", "priority": "medium",
             "title": f"t{index}", "evidence": {"raw": "PRIVATE_EVIDENCE_BLOB"}}
            for index in range(8)
        ],
    },
}


class AuditDataHealthSummaryTests(unittest.TestCase):
    def setUp(self) -> None:
        self.audit = load_audit()
        self._directory = tempfile.TemporaryDirectory()
        self.directory = Path(self._directory.name)
        self.addCleanup(self._directory.cleanup)

    def test_summary_is_compact_and_carries_no_private_rows(self) -> None:
        summary = self.audit.data_health_summary(REPORT)
        self.assertEqual(summary["system"], "crown")
        self.assertEqual(summary["status"], "degraded")
        self.assertEqual(summary["generated_at"], "2026-08-12T04:00:00+00:00")
        self.assertEqual(summary["counts"]["unique_fixtures"], 120)
        self.assertEqual(summary["counts"]["duplicate_stage_keys"], 3)
        self.assertEqual(summary["coverage"]["result"], 0.93)
        self.assertEqual(summary["coverage"]["corner_result"], 0.69)
        self.assertEqual(summary["coverage"]["stale_missing_corner_fixtures"], 9)
        self.assertEqual(summary["issue_counts"]["total"], 9)
        self.assertEqual(summary["minimum_unique_fixtures"], 30)
        self.assertLessEqual(len(summary["top_issues"]), 5)
        self.assertLessEqual(len(summary["top_recommendations"]), 5)
        text = json.dumps(summary, ensure_ascii=False)
        self.assertNotIn("PRIVATE_EVIDENCE_BLOB", text)
        self.assertNotIn("SECRET_LEAGUE_ROWS", text)
        self.assertNotIn("error_slices", text)
        self.assertLess(len(text), 3000)

    def test_state_reads_both_artifacts_and_never_fails_on_bad_input(self) -> None:
        good = self.directory / "crown" / "data-health.json"
        good.parent.mkdir(parents=True)
        good.write_text(json.dumps(REPORT), encoding="utf-8")
        broken = self.directory / "footbreak" / "data-health.json"
        broken.parent.mkdir(parents=True)
        broken.write_text("{not json", encoding="utf-8")
        with patch.dict(
            self.audit.DATA_HEALTH_REPORTS,
            {"crown": good, "footbreak": broken},
            clear=True,
        ):
            state = self.audit.data_health_state()
        self.assertTrue(state["read_only"])
        self.assertFalse(state["auto_apply"])
        self.assertEqual(state["primary_sample"], "unique_fixtures")
        self.assertTrue(state["systems"]["crown"]["available"])
        self.assertFalse(state["systems"]["footbreak"]["available"])
        self.assertIn("reason", state["systems"]["footbreak"])

    def test_missing_artifact_is_reported_not_raised(self) -> None:
        with patch.dict(
            self.audit.DATA_HEALTH_REPORTS,
            {"crown": self.directory / "absent.json"},
            clear=True,
        ):
            state = self.audit.data_health_state()
        self.assertEqual(state["systems"]["crown"]["reason"], "artifact_missing")

    def test_unexpected_report_shape_is_rejected(self) -> None:
        path = self.directory / "other.json"
        path.write_text(json.dumps({"report": "something_else"}), encoding="utf-8")
        with patch.dict(
            self.audit.DATA_HEALTH_REPORTS, {"crown": path}, clear=True
        ):
            state = self.audit.data_health_state()
        self.assertEqual(state["systems"]["crown"]["reason"], "unexpected_report")


class AuditCrownChlWhitelistTests(unittest.TestCase):
    def setUp(self) -> None:
        self.audit = load_audit()
        self._directory = tempfile.TemporaryDirectory()
        self.directory = Path(self._directory.name)
        self.addCleanup(self._directory.cleanup)

    def test_crown_chl_prospective_is_whitelisted_without_private_state(self) -> None:
        path = self.directory / "latest.json"
        path.write_text(json.dumps({
            "generated_at": "2026-08-12T04:20:00+00:00",
            "policy": {"auto_apply": False},
            "review_required": False,
            "systems": {
                "crown": {
                    "review_required": False,
                    "tests": {
                        "CHL": {
                            "status": "insufficient_data",
                            "eligible_fixtures": 42,
                            "coefficient_importance": [{"feature": "PRIVATE_COEF"}],
                            "prospective_chl": {
                                "market": "CHL",
                                "status": "prospective_shadow_collecting",
                                "state_version_hash": "hash123",
                                "freeze_cutoff": "2026-06-01T00:00:00+00:00",
                                "primary_unit": "one_row_per_unique_fixture",
                                "primary_stage_rule": ["T-5", "T-30", "首預"],
                                "selected_strategy": "always_under",
                                "prospective_fixtures": 12,
                                "prospective_rows": 33,
                                "remaining_fixtures": 18,
                                "stage_diagnostics": [{"stage": "T-5"}],
                                "closing_reference": {"available": False},
                                "feature_coverage": {"eligible": False},
                                "shadow_returns": {"roi": None},
                                "auto_apply": False,
                                "retraining": False,
                                "live_integration": "none",
                                "private_model": {"coefficients": [1.0, 2.0]},
                            },
                        }
                    },
                }
            },
        }), encoding="utf-8")
        with patch.object(self.audit, "CHALLENGER_STATUS", path):
            state = self.audit.challenger_state()
        chl = state["systems"]["crown"]["tests"]["CHL"]["prospective_chl"]
        self.assertEqual(chl["status"], "prospective_shadow_collecting")
        self.assertEqual(chl["primary_unit"], "one_row_per_unique_fixture")
        self.assertEqual(chl["primary_stage_rule"], ["T-5", "T-30", "首預"])
        self.assertEqual(chl["prospective_fixtures"], 12)
        self.assertFalse(chl["auto_apply"])
        self.assertNotIn("private_model", chl)
        self.assertNotIn("PRIVATE_COEF", json.dumps(chl, ensure_ascii=False))


if __name__ == "__main__":
    unittest.main()


class AuditMetricUnitTests(unittest.TestCase):
    """The production audit must carry the metric unit, not just the numbers."""

    def _module(self):
        return load_audit()

    def test_audit_summary_states_both_units_and_the_evidence_unit(self) -> None:
        module = self._module()
        report = {
            "schema_version": 1,
            "system": "crown",
            "status": "watch",
            "policy": {"minimum_unique_fixtures": 30},
            "completeness": {"overall": {"unique_fixtures": 120, "graded_rows": 600}},
            "metrics_policy": {
                "sample_basis": "unique_fixtures",
                "metric_unit": "graded_prediction_rows",
                "correlated_stage_rows": True,
                "primary_diagnostic_metric_unit":
                    "graded_prediction_rows_latest_stage_per_fixture_market",
                "metrics_are_one_per_fixture": False,
                "recommendation_evidence_unit":
                    "graded_prediction_rows_latest_stage_per_fixture_market",
            },
            "baseline": {
                "unique_fixtures": 120, "graded_rows": 600, "accuracy": 0.53,
                "brier": 0.24, "log_loss": 0.68, "sample_status": "sufficient",
                "sample_basis": "unique_fixtures",
                "metric_unit": "graded_prediction_rows",
                "correlated_stage_rows": True,
            },
            "primary_diagnostic": {
                "unit": "graded_prediction_rows_latest_stage_per_fixture_market",
                "stage_priority": ["T-5", "T-30", "首預"],
                "baseline": {
                    "unique_fixtures": 120, "graded_rows": 220, "accuracy": 0.52,
                    "brier": 0.246, "log_loss": 0.686, "sample_status": "sufficient",
                    "sample_basis": "unique_fixtures",
                    "correlated_stage_rows": False,
                },
            },
            "issues": [],
            "issue_counts": {"high": 0, "warn": 1, "info": 0, "total": 1},
            "hil_v4_diagnostics": {
                "evidence_unit":
                    "graded_prediction_rows_latest_stage_per_fixture_market",
                "evidence_uses_repeated_stage_rows": False,
                "recommendations": [],
            },
        }
        summary = module.data_health_summary(report)
        self.assertFalse(summary["metrics_policy"]["metrics_are_one_per_fixture"])
        self.assertEqual(summary["baseline"]["metric_unit"], "graded_prediction_rows")
        self.assertTrue(summary["baseline"]["correlated_stage_rows"])
        self.assertEqual(summary["baseline"]["graded_rows"], 600)
        primary = summary["primary_diagnostic_baseline"]
        self.assertEqual(
            primary["metric_unit"],
            "graded_prediction_rows_latest_stage_per_fixture_market",
        )
        self.assertFalse(primary["correlated_stage_rows"])
        self.assertEqual(primary["graded_rows"], 220)
        self.assertFalse(summary["recommendation_uses_repeated_stage_rows"])
        self.assertEqual(
            summary["recommendation_evidence_unit"],
            "graded_prediction_rows_latest_stage_per_fixture_market",
        )

    def test_audit_summary_degrades_safely_on_a_legacy_report(self) -> None:
        module = self._module()
        summary = module.data_health_summary({"system": "footbreak", "status": "ok"})
        self.assertIsNone(summary["baseline"]["metric_unit"])
        self.assertIsNone(summary["primary_diagnostic_baseline"]["metric_unit"])
        self.assertIsNone(summary["recommendation_evidence_unit"])

    def test_chl_champion_shadow_returns_are_whitelisted(self) -> None:
        source = (ROOT / "deploy" / "audit-result-state.py").read_text(encoding="utf-8")
        self.assertIn('"champion_shadow_returns"', source)
        self.assertIn('"shadow_returns"', source)
