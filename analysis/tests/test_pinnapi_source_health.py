"""Regression tests for the read-only 14-day PinnAPI source-health report."""
from __future__ import annotations

import json
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

from analysis.learning_store import LearningStore
from analysis.pinnapi_source_health import build_report, public_view

NOW = datetime(2026, 8, 13, 0, 0, tzinfo=timezone.utc)


def payload(*, league: str, source: str = "", status: str = "", predictions=None, live=False,
            age=None, identity=None, pinnapi_event_id=None, sharp_reference_available=None, edge_reference_status=None):
    return {
        "league": league,
        "source": source,
        "source_status": status,
        "provider_live": live,
        "data_age_seconds": age,
        "pinnapi_fixture_identity": identity,
        "market_predictions": predictions or [],
        "pinnapi_event_id": pinnapi_event_id,
        "sharp_reference_available": sharp_reference_available,
        "edge_reference_status": edge_reference_status,
    }


def prediction(code="HIL", side="H", condition="2.5"):
    return {"code": code, "side": side, "condition": condition}


class PinnapiSourceHealthTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.db = Path(self.temporary.name) / "learning.sqlite"

    def snapshot(self, store, fixture, stage, data, *, hours_before=1, system="footbreak"):
        kickoff = NOW - timedelta(days=2)
        return store.record_snapshot(
            system, fixture, stage, kickoff - timedelta(hours=hours_before), kickoff, data,
        )

    def grade(self, store, snapshot, *, hit, target=1.0):
        return store.record_grade(
            snapshot["snapshot_id"], "HIL", "2.5|H", "GRADED",
            {"hit": hit, "target": target},
        )

    def test_latest_fixture_market_stage_is_deduplicated_and_push_excluded(self):
        with LearningStore(self.db) as store:
            early = self.snapshot(store, "live", "T-30", payload(
                league="甲組", source="pinnapi_live", status="pinnapi_live", live=True,
                identity={"fixture_id": "p-live"}, predictions=[prediction()],
            ), hours_before=2)
            latest = self.snapshot(store, "live", "T-5", payload(
                league="甲組", source="pinnapi_live", status="pinnapi_live", live=True,
                identity={"fixture_id": "p-live"}, predictions=[prediction()],
            ))
            self.grade(store, early, hit=False)
            self.grade(store, latest, hit=True)
            unmatched = self.snapshot(store, "unmatched", "T-5", payload(
                league="乙組", source="hkjc_full_market", status="pinnapi_fixture_unmatched",
                predictions=[prediction()],
            ))
            self.grade(store, unmatched, hit=None, target=.5)
            self.snapshot(store, "missing", "T-5", payload(
                league="乙組", source="unavailable", status="no_prediction_due_to_source_or_hkjc_model",
            ))
            self.snapshot(store, "fallback", "T-5", payload(
                league="丙組", source="fallback", status="pinnapi_fallback_diagnostic_only",
                age=30, identity={"fixture_id": "p-fallback"}, predictions=[],
            ))

        report = build_report(self.db, now=NOW)
        footbreak = report["systems"]["footbreak"]
        self.assertEqual(footbreak["fixture_categories"], {
            "no_prediction_due_to_source": 1,
            "predicted_but_unmatched": 1,
            "with_pinnapi": 1,
            "without_pinnapi": 1,
        })
        all_metrics = footbreak["primary_metrics"]["all"]
        self.assertEqual(all_metrics["fixture_market_latest_stage_rows"], 2)
        self.assertEqual(all_metrics["settled_decisions"], 1)
        self.assertEqual(all_metrics["pushes_excluded"], 1)
        self.assertEqual(all_metrics["hit_rate"], 1.0)
        self.assertEqual(footbreak["primary_metrics"]["with_pinnapi"]["hit_rate"], 1.0)
        self.assertIsNone(footbreak["primary_metrics"]["without_pinnapi"]["hit_rate"])
        self.assertTrue(all(row["sample_status"] == "insufficient"
                            for row in footbreak["league_health_candidates"]))
        self.assertTrue(all(row["candidate_only"] for row in footbreak["league_health_candidates"]))

    def test_public_view_has_only_bounded_aggregate_data(self):
        with LearningStore(self.db) as store:
            self.snapshot(store, "id-secret", "T-5", payload(
                league="公開聯賽", source="pinnapi_live", status="pinnapi_live", live=True,
                identity={"fixture_id": "SECRET_PROVIDER_ID"}, predictions=[prediction()],
            ))
        rendered = json.dumps(public_view(build_report(self.db, now=NOW)), ensure_ascii=False)
        self.assertNotIn("id-secret", rendered)
        self.assertNotIn("SECRET_PROVIDER_ID", rendered)
        self.assertIn("公開聯賽", rendered)

    def test_outside_window_is_not_counted(self):
        with LearningStore(self.db) as store:
            kickoff = NOW - timedelta(days=30)
            store.record_snapshot(
                "footbreak", "old", "T-5", kickoff - timedelta(hours=1), kickoff,
                payload(league="舊聯賽", source="pinnapi_live", status="pinnapi_live",
                        live=True, predictions=[prediction()]),
            )
        report = build_report(self.db, now=NOW)
        self.assertEqual(sum(report["systems"]["footbreak"]["fixture_categories"].values()), 0)


if __name__ == "__main__":
    unittest.main()

class PinnapiSourceHealthCrownTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.db = Path(self.temporary.name) / "learning.sqlite"

    def test_crown_uses_event_reference_and_status_fields_and_combines(self):
        kickoff = NOW - timedelta(days=1)
        with LearningStore(self.db) as store:
            live = store.record_snapshot(
                "crown", "c-live", "T-30", kickoff - timedelta(hours=2), kickoff,
                payload(league="皇冠甲", pinnapi_event_id="pin-1", sharp_reference_available=True,
                        edge_reference_status="available", predictions=[prediction()]),
            )
            latest = store.record_snapshot(
                "crown", "c-live", "T-5", kickoff - timedelta(hours=1), kickoff,
                payload(league="皇冠甲", pinnapi_event_id="pin-1", sharp_reference_available=True,
                        edge_reference_status="available", predictions=[prediction()]),
            )
            store.record_grade(live["snapshot_id"], "HIL", "2.5|H", "GRADED", {"hit": False, "target": 0})
            store.record_grade(latest["snapshot_id"], "HIL", "2.5|H", "GRADED", {"hit": True, "target": 1})
            store.record_snapshot(
                "crown", "c-unmatched", "T-5", kickoff - timedelta(hours=1), kickoff,
                payload(league="皇冠乙", pinnapi_event_id=None, sharp_reference_available=False,
                        edge_reference_status="unavailable", predictions=[prediction()]),
            )
            store.record_snapshot(
                "crown", "c-missing", "T-5", kickoff - timedelta(hours=1), kickoff,
                payload(league="皇冠乙", pinnapi_event_id="pin-2", sharp_reference_available=False,
                        edge_reference_status="unavailable", predictions=[]),
            )
        report = build_report(self.db, now=NOW)
        crown = report["systems"]["crown"]
        self.assertEqual(crown["fixture_categories"]["with_pinnapi"], 1)
        self.assertEqual(crown["fixture_categories"]["predicted_but_unmatched"], 1)
        self.assertEqual(crown["fixture_categories"]["no_prediction_due_to_source"], 1)
        self.assertEqual(crown["primary_metrics"]["all"]["settled_decisions"], 1)
        self.assertEqual(crown["primary_metrics"]["all"]["hit_rate"], 1.0)
        coverage = crown["coverage"]["historical_no_prediction_due_to_pinnapi"]
        self.assertEqual(coverage["observed_count"], 1)
        self.assertEqual(coverage["count_status"], "lower_bound")
        self.assertEqual(report["combined_summary"]["fixture_categories"]["with_pinnapi"], 1)

    def test_empty_system_marks_historical_omissions_unavailable(self):
        with LearningStore(self.db):
            pass
        report = build_report(self.db, now=NOW)
        self.assertEqual(
            report["systems"]["crown"]["coverage"]["historical_no_prediction_due_to_pinnapi"]["count_status"],
            "unavailable",
        )
