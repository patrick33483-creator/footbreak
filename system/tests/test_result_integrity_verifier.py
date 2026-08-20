import copy
import importlib.util
import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from crown.ledger import PREDICTION_ERA
from crown.prediction_history import calculate_stats


MODULE_PATH = Path(__file__).parents[2] / "deploy" / "verify-result-integrity.py"
SPEC = importlib.util.spec_from_file_location("verify_result_integrity", MODULE_PATH)
verify = importlib.util.module_from_spec(SPEC)
assert SPEC.loader is not None
SPEC.loader.exec_module(verify)


class ResultIntegrityVerifierTests(unittest.TestCase):
    def test_main_reads_footbreak_rows_from_versioned_history_sidecar(self):
        row = {
            "match_id": "foot-sidecar",
            "stage": "T-5",
            "kickoff": "2026-08-20T01:00:00+08:00",
            "predicted_at": "2026-08-20T00:55:00+08:00",
            "market_predictions": [
                {"code": "HDC", "side": "H", "line": -0.25}
            ],
        }
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            paths = {
                "foot_data": root / "foot-data.json",
                "foot_history": root / "foot-history.json",
                "crown_history": root / "crown-raw-history.json",
                "crown_data": root / "crown-data.json",
                "crown_public_history": root / "history-crown-v1.json",
            }
            paths["foot_data"].write_text(json.dumps({
                "prediction_history": {"stats": {"graded": 999}},
                "history_data_url": "history.json",
                "history_data_version": "foot-v1",
            }), encoding="utf-8")
            paths["foot_history"].write_text(json.dumps({
                "schema_version": "footbreak-history-v1",
                "history_data_version": "foot-v1",
                "prediction_history": {"stats": {}, "rows": [row]},
            }), encoding="utf-8")
            paths["crown_history"].write_text(
                json.dumps({"rows": [], "stats": {}}), encoding="utf-8"
            )
            paths["crown_data"].write_text(json.dumps({
                "prediction_history": {"stats": {}},
                "history_data_url": "history-crown-v1.json",
                "history_data_version": "crown-v1",
            }), encoding="utf-8")
            paths["crown_public_history"].write_text(json.dumps({
                "schema_version": "crown-history-v1",
                "history_data_version": "crown-v1",
                "prediction_history": {"stats": {}, "rows": []},
            }), encoding="utf-8")
            calls = []

            def capture(category, check, *args):
                calls.append((category, args))

            with patch.object(verify, "FOOTBREAK_DATA", paths["foot_data"]), \
                 patch.object(
                     verify, "FOOTBREAK_PUBLIC_HISTORY", paths["foot_history"]
                 ), \
                 patch.object(verify, "CROWN_HISTORY", paths["crown_history"]), \
                 patch.object(verify, "CROWN_DATA", paths["crown_data"]), \
                 patch.object(verify, "run_integrity_check", side_effect=capture), \
                 patch.object(verify, "report_result_gaps"), \
                 patch.object(verify, "verify_known_crown_incident"):
                verify.main()

        foot_shape = next(args for category, args in calls
                          if category == "footbreak_history_shape")
        self.assertEqual(foot_shape[1][0]["match_id"], "foot-sidecar")
        foot_stats = next(args for category, args in calls
                          if category == "footbreak_market_stats")
        self.assertEqual(foot_stats[2], {})

    def test_published_history_rejects_mixed_generation(self):
        public = {
            "history_data_url": "history.json",
            "history_data_version": "expected",
        }
        sidecar = {
            "schema_version": "footbreak-history-v1",
            "history_data_version": "other",
            "prediction_history": {"rows": []},
        }
        with self.assertRaisesRegex(
            AssertionError, "Footbreak boot/history sidecar version mismatch"
        ):
            verify.published_history(
                "Footbreak", public, sidecar, "footbreak-history-v1"
            )

    def test_top_level_check_failure_exposes_only_stable_category(self):
        def fail_with_sensitive_payload():
            raise AssertionError({"fixture_id": "secret-fixture"})

        with self.assertRaisesRegex(
            AssertionError,
            r"^integrity_check=crown_market_stats$",
        ) as error:
            verify.run_integrity_check(
                "crown_market_stats",
                fail_with_sensitive_payload,
            )
        self.assertNotIn("secret-fixture", str(error.exception))

    def test_crown_publication_accepts_recovery_overlay_without_mutating_raw(self):
        raw_row = {
            "match_id": "3031468",
            "kickoff": "2026-08-12T20:00:00+08:00",
            "predicted_at": "2026-08-12T19:55:00+08:00",
            "stage": "T-5",
            "prediction_era": PREDICTION_ERA,
            "market_predictions": [
                {"code": "HIL", "line": 2.5, "side": "L"},
            ],
            "market_grades": [
                {
                    "code": "HIL",
                    "line": 2.5,
                    "side": "L",
                    "grade_status": "GRADED",
                    "hit": True,
                },
            ],
        }
        raw = {
            "rows": [raw_row],
            "stats": calculate_stats([raw_row], comparable_era=PREDICTION_ERA),
        }
        with tempfile.TemporaryDirectory() as directory:
            sidecar = Path(directory) / "odds.json"
            from analysis.odds_recovery import (
                SCHEMA_VERSION,
                _sha,
                overlay_rows,
                snapshot_identity,
            )

            entry = {
                "system": "crown",
                "snapshot_identity": snapshot_identity(raw_row, "crown"),
                "market_code": "HIL",
                "line": "2.5",
                "side": "L",
                "selected_odds": "1.88",
                "observed_at": "2026-08-12T11:54:00+00:00",
                "evidence_source_kind": "provider",
                "evidence_source_hash": "test-evidence",
                "evidence_age_seconds": 60.0,
            }
            entry["entry_hash"] = _sha(entry)
            sidecar.write_text(
                json.dumps(
                    {
                        "schema_version": SCHEMA_VERSION,
                        "entries": [entry],
                        "audit": [],
                    }
                ),
                encoding="utf-8",
            )
            with patch.dict(
                os.environ,
                {
                    "ODDS_RECOVERY_ENABLED": "1",
                    "ODDS_RECOVERY_SIDECAR": str(sidecar),
                },
                clear=False,
            ):
                projected_rows = overlay_rows(raw["rows"], "crown")
                public = {
                    "prediction_history": {
                        "rows": projected_rows,
                        "stats": calculate_stats(
                            projected_rows,
                            comparable_era=PREDICTION_ERA,
                        ),
                    }
                }
                verify.assert_crown_publication_matches(raw, public)

                self.assertNotIn("odds", raw_row["market_predictions"][0])
                self.assertEqual(
                    public["prediction_history"]["rows"][0][
                        "market_predictions"
                    ][0]["odds"],
                    1.88,
                )

                public["prediction_history"]["rows"][0][
                    "market_predictions"
                ][0]["odds"] = 1.89
                with self.assertRaises(AssertionError):
                    verify.assert_crown_publication_matches(raw, public)

    def test_crown_publication_accepts_persisted_ledger_display_projection(self):
        raw = {"rows": [], "stats": {}}
        ledger = {"watch": {"future": {
            "match_id": "future",
            "league": "League",
            "home": "Home",
            "away": "Away",
            "kickoff": "2026-08-14T01:00:00+08:00",
            "stages": [{
                "match_id": "future",
                "stage": "首預",
                "ts": "2026-08-13T18:00:00+08:00",
                "market_predictions": [{
                    "code": "HDC", "line": -0.5, "side": "H",
                }],
            }],
        }}}
        from crown.prediction_history import project_watch_rows

        projected = project_watch_rows([], ledger)
        public = {
            "ledger": ledger,
            "prediction_history": {
                "rows": projected,
                "stats": calculate_stats(
                    projected, comparable_era=PREDICTION_ERA,
                ),
            },
        }
        verify.assert_crown_publication_matches(raw, public)

    def test_crown_publication_compares_the_active_wilson_ranking_projection(self):
        raw = {"rows": [], "stats": {}}
        calculated = calculate_stats([], comparable_era=PREDICTION_ERA)
        self.assertIsInstance(calculated.get("granular_conditions"), dict)
        projected_ranking = [{
            "condition_signature": "frozen-signature",
            "condition_number": 1,
            "total": {"hits": 185, "decided": 302},
            "active_evidence": {"version": 2, "evidence_hash": "frozen-hash"},
        }]
        public_stats = copy.deepcopy(calculated)
        public_stats["granular_conditions"]["ranking"] = projected_ranking
        public = {
            "generated_at": "2026-08-21T03:00:00+08:00",
            "ledger": {"wilson_validation": {"conditions": {}}},
            "prediction_history": {"rows": [], "stats": public_stats},
        }
        with patch(
            "analysis.wilson_validation.project_granular_ranking_evidence",
            return_value=projected_ranking,
        ) as projection:
            verify.assert_crown_publication_matches(raw, public)
            projection.assert_called_once()

        tampered_ranking = copy.deepcopy(public)
        tampered_ranking["prediction_history"]["stats"]["granular_conditions"][
            "ranking"
        ][0]["active_evidence"]["evidence_hash"] = "tampered"
        with patch(
            "analysis.wilson_validation.project_granular_ranking_evidence",
            return_value=projected_ranking,
        ), self.assertRaises(AssertionError):
            verify.assert_crown_publication_matches(raw, tampered_ranking)

        tampered_other_stat = copy.deepcopy(public)
        tampered_other_stat["prediction_history"]["stats"]["by_stage"] = {
            "T-5": {"accuracy": 1.0}
        }
        with patch(
            "analysis.wilson_validation.project_granular_ranking_evidence",
            return_value=projected_ranking,
        ), self.assertRaises(AssertionError):
            verify.assert_crown_publication_matches(raw, tampered_other_stat)

    def test_market_stats_respects_reported_model_version_scope(self):
        current = {
            "match_id": "current",
            "stage": "T-5",
            "prediction_era": PREDICTION_ERA,
            "market_grades": [{
                "code": "HDC", "grade_status": "GRADED", "hit": True,
            }],
        }
        legacy = {
            "match_id": "legacy",
            "stage": "T-5",
            "prediction_era": "legacy",
            "market_grades": [{
                "code": "HDC", "grade_status": "GRADED", "hit": False,
            }],
        }
        stats = calculate_stats([current, legacy], comparable_era=PREDICTION_ERA)
        verify.assert_market_stats_consistent("test", [current, legacy], stats)

    def test_accepts_push_and_checks_exact_stage_cell(self):
        rows = [
            {
                "match_id": "new",
                "kickoff": "2026-08-12T20:00:00+08:00",
                "predicted_at": "2026-08-12T19:55:00+08:00",
                "stage": "T-5",
                "market_grades": [
                    {
                        "code": "HDC", "grade_status": "GRADED",
                        "hit": True, "odds": 1.80,
                    },
                    {
                        "code": "HIL", "grade_status": "GRADED",
                        "hit": None, "odds": 1.80,
                    },
                ],
            }
        ]
        stats = calculate_stats(rows)
        verify.assert_market_stats_consistent("test", rows, stats)

        stats["by_stage_market"]["首預"]["HDC"]["all_odds"]["graded"] = 1
        with self.assertRaises(AssertionError):
            verify.assert_market_stats_consistent("test", rows, stats)

    def test_rejects_duplicate_or_non_descending_history(self):
        latest = {
            "match_id": "latest",
            "stage": "T-5",
            "kickoff": "2026-08-12T20:00:00+08:00",
            "predicted_at": "2026-08-12T19:55:00+08:00",
        }
        older = {
            "match_id": "older",
            "stage": "T-5",
            "kickoff": "2026-08-12T18:00:00+08:00",
            "predicted_at": "2026-08-12T17:55:00+08:00",
        }
        verify.assert_unique_and_sorted("test", [latest, older])
        with self.assertRaises(AssertionError):
            verify.assert_unique_and_sorted("test", [older, latest])
        with self.assertRaises(AssertionError):
            verify.assert_unique_and_sorted("test", [latest, dict(latest)])

    def test_crown_normalizer_uses_the_verifier_order_for_recovered_rows(self):
        from crown.prediction_history import normalize_history

        native = {
            "match_id": "fixture", "stage": "T-5",
            "kickoff": "2026-08-12T20:00:00+08:00",
            "predicted_at": "2026-08-12T19:55:00+08:00",
            "market_predictions": [{"code": "HDC", "line": -0.25, "side": "H"}],
        }
        recovered = {
            "match_id": "fixture", "stage": "T-5（事後回補）",
            "kickoff": "2026-08-12T20:00:00+08:00",
            "predicted_at": "2026-08-12T19:55:00+08:00",
            "post_hoc_backfill": True,
            "market_predictions": [{"code": "HIL", "line": 2.5, "side": "L"}],
        }
        history = normalize_history({"rows": [recovered, native], "stats": {}})
        self.assertEqual(
            [row["stage"] for row in history["rows"]],
            ["T-5", "T-5（事後回補）"],
        )
        verify.assert_unique_and_sorted("Crown", history["rows"])

    def test_accuracy_accepts_only_six_decimal_rounding(self):
        self.assertTrue(verify.same_accuracy(0.321429, 9 / 28))
        self.assertFalse(verify.same_accuracy(0.32143, 9 / 28))

    def test_accepts_odds_tier_stats_and_audits_all_odds(self):
        rows = [
            {
                "match_id": "priced",
                "stage": "T-5",
                "market_grades": [
                    {
                        "code": "HIL", "grade_status": "GRADED",
                        "hit": True, "odds": 1.80,
                    },
                ],
            },
            {
                "match_id": "missing",
                "stage": "T-5",
                "market_grades": [
                    {"code": "HIL", "grade_status": "GRADED", "hit": False},
                ],
            },
        ]
        empty = {"graded": 0, "decided": 0, "hits": 0, "pushes": 0, "accuracy": None}

        def tiered(all_odds, high=None, low=None, excluded_missing_odds=0):
            high = high or dict(empty)
            low = low or dict(empty)
            return {
                **high,
                "all_odds": all_odds,
                "odds_groups": {
                    "at_or_above_1_70": high,
                    "below_1_70": low,
                },
                "excluded_missing_odds": excluded_missing_odds,
            }

        hil_all = {"graded": 1, "decided": 1, "hits": 1, "pushes": 0, "accuracy": 1.0}
        hil_high = {"graded": 1, "decided": 1, "hits": 1, "pushes": 0, "accuracy": 1.0}
        stats = {
            "by_market": {
                "HDC": tiered(dict(empty)),
                "HIL": tiered(hil_all, high=hil_high, excluded_missing_odds=1),
                "CHL": tiered(dict(empty)),
            },
            "by_stage_market": {
                stage: {
                    code: (
                        tiered(hil_all, high=hil_high, excluded_missing_odds=1)
                        if stage == "T-5" and code == "HIL"
                        else tiered(dict(empty))
                    )
                    for code in verify.MARKETS
                }
                for stage in verify.STAGES
            },
        }
        verify.assert_market_stats_consistent("test", rows, stats)

        stats["by_market"]["HIL"]["excluded_missing_odds"] = 0
        with self.assertRaises(AssertionError):
            verify.assert_market_stats_consistent("test", rows, stats)


if __name__ == "__main__":
    unittest.main()
