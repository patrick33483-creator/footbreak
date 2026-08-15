"""Offline guardrail tests for missed-Crown-T-5 recovery."""
from __future__ import annotations

import copy
import json
import tempfile
import unittest
from dataclasses import replace
from pathlib import Path

from crown.config import settings
from crown.ledger import RECOVERED_T5_STAGE
from crown.prediction_history import calculate_stats
from crown.state import load_ledger, save_ledger
from crown.t5_recovery import apply_plan, build_plan

KICKOFF = "2026-08-10T13:00:00+08:00"


def candidate(observed_at: str = "2026-08-10T12:54:30+08:00", **changes):
    value = {
        "code": "HDC", "line": -0.5, "side": "H", "odds": 1.82,
        "probability": 0.56, "provider": "Crown",
        "source": "titan007-crown-id-3", "observed_at": observed_at,
    }
    value.update(changes)
    return value


def source_stage(**changes):
    value = {
        "stage": "T-30", "ts": "2026-08-10T12:30:00+08:00",
        "match_id": "local-1", "titan_match_id": "9001", "kickoff_hkt": KICKOFF,
        "forecast": "主勝", "probability": 0.52, "market_predictions": [candidate()],
    }
    value.update(changes)
    return value


def ledger(stages=None, **watch_changes):
    watch = {
        "match_id": "local-1", "titan_match_id": "9001", "kickoff": KICKOFF,
        "home": "Home", "away": "Away", "league": "League",
        "stages": stages if stages is not None else [source_stage()],
    }
    watch.update(watch_changes)
    return {"bankroll": 50000, "bets": [], "watch": {"local-1": watch}, "log": [], "stats": {}}


class T5RecoveryTests(unittest.TestCase):
    def test_saved_native_payload_recovery_uses_exact_t5_evidence_and_marks_every_exclusion(self):
        payload = source_stage(
            stage="saved T-5 model payload",
            ts="2026-08-10T12:54:50+08:00",
            market_predictions=[candidate("2026-08-10T12:54:30+08:00")],
        )
        original = ledger(t5_model_payload=payload)
        planned, audit = build_plan(copy.deepcopy(original), recovered_at="2026-08-11T01:00:00+08:00")

        self.assertEqual(audit["aggregate"]["total_recovered_records"], 1)
        self.assertEqual(audit["aggregate"]["total_native_payload_recovery_records"], 1)
        self.assertEqual(audit["aggregate"]["total_carry_forward_recovery_records"], 0)
        self.assertEqual(len(planned), 1)
        stage = planned[0]["stage"]
        self.assertEqual(stage["stage"], RECOVERED_T5_STAGE)
        self.assertTrue(stage["post_hoc_backfill"])
        self.assertTrue(stage["exclude_from_telegram"])
        self.assertTrue(stage["exclude_from_simulation"])
        self.assertTrue(stage["exclude_from_learning"])
        self.assertTrue(stage["exclude_from_settlement"])
        self.assertTrue(stage["exclude_from_primary_statistics"])
        self.assertEqual(stage["recovery"]["recovery_kind"], "native_payload_recovery")
        self.assertEqual(stage["recovery"]["source_stage"], "saved T-5 model payload")
        self.assertEqual(stage["market_predictions"][0]["recovery_evidence_type"], "t5_exact")
        self.assertFalse(stage["market_predictions"][0]["closing_odds_substitution"])
        self.assertEqual(original["bets"], [])

    def test_closing_quote_is_only_used_after_t5_locf_is_unavailable(self):
        state = ledger(stages=[source_stage(market_predictions=[candidate("2026-08-10T12:57:00+08:00")])])
        planned, audit = build_plan(state)

        self.assertEqual(len(planned), 1)
        self.assertEqual(
            planned[0]["stage"]["recovery"]["recovery_kind"],
            "last_pre_t5_prediction_carry_forward",
        )
        market = planned[0]["stage"]["market_predictions"][0]
        self.assertEqual(market["recovery_evidence_type"], "closing_substitution")
        self.assertTrue(market["closing_odds_substitution"])
        self.assertEqual(
            audit["aggregate"]["by_kickoff_stage_market_evidence"]["2026-08-10"]["T-30"]["HDC"],
            {"closing_substitution": 1},
        )

    def test_carry_forward_prefers_t30_then_first_look_and_never_claims_native_t5(self):
        first_look = source_stage(
            stage="首預", ts="2026-08-10T12:00:00+08:00",
            market_predictions=[candidate("2026-08-10T12:00:00+08:00", line=-0.25)],
        )
        t30 = source_stage(
            stage="T-30", ts="2026-08-10T12:30:00+08:00",
            market_predictions=[candidate("2026-08-10T12:54:30+08:00", line=-0.5)],
        )
        planned, audit = build_plan(ledger(stages=[first_look, t30]))

        self.assertEqual(len(planned), 1)
        stage = planned[0]["stage"]
        self.assertEqual(stage["recovery"]["recovery_kind"], "last_pre_t5_prediction_carry_forward")
        self.assertEqual(stage["recovery"]["source_stage"], "T-30")
        self.assertTrue(stage["recovery"]["prediction_stage_substitution"])
        self.assertIn("NOT AN ACTUAL T-5", stage["recovery"]["label"])
        self.assertTrue(stage["market_predictions"][0]["prediction_stage_substitution"])
        self.assertEqual(audit["aggregate"]["total_carry_forward_recovery_records"], 1)
        self.assertEqual(audit["aggregate"]["carry_forward_recovery"]["records"], 1)

        first_only, _ = build_plan(ledger(stages=[first_look]))
        self.assertEqual(first_only[0]["stage"]["recovery"]["source_stage"], "首預")

    def test_carry_forward_requires_exact_line_and_side_and_rejects_opposite_direction(self):
        direction_only = source_stage(
            market_predictions=[candidate(source="model-only", observed_at="2026-08-10T12:30:00+08:00")],
        )
        opposite_quote = source_stage(
            stage="首預", ts="2026-08-10T12:00:00+08:00",
            market_predictions=[candidate(side="A", observed_at="2026-08-10T12:54:30+08:00")],
        )
        planned, audit = build_plan(ledger(stages=[opposite_quote, direction_only]))
        self.assertEqual(planned, [])
        self.assertEqual(
            audit["aggregate"]["unresolved_reasons"],
            {"no_valid_pre_kickoff_crown_quote": 1},
        )

        wrong_line_quote = source_stage(
            stage="首預", ts="2026-08-10T12:00:00+08:00",
            market_predictions=[candidate(line=-0.25, observed_at="2026-08-10T12:54:30+08:00")],
        )
        planned, _ = build_plan(ledger(stages=[wrong_line_quote, direction_only]))
        self.assertEqual(planned, [])

        wrong_fixture_quote = source_stage(
            stage="首預", ts="2026-08-10T12:00:00+08:00",
            market_predictions=[candidate(
                observed_at="2026-08-10T12:54:30+08:00",
                titan_match_id="another-fixture",
            )],
        )
        planned, _ = build_plan(ledger(stages=[wrong_fixture_quote, direction_only]))
        self.assertEqual(planned, [])

    def test_carry_forward_rejects_post_kickoff_stage_and_quote(self):
        post_kickoff = source_stage(
            ts="2026-08-10T13:00:01+08:00",
            market_predictions=[candidate(observed_at="2026-08-10T13:00:01+08:00")],
        )
        planned, audit = build_plan(ledger(stages=[post_kickoff]))
        self.assertEqual(planned, [])
        self.assertEqual(
            audit["aggregate"]["unresolved_reasons"],
            {"no_valid_last_pre_t5_prediction": 1},
        )

    def test_post_kickoff_and_wrong_company_quotes_fail_closed(self):
        state = ledger(stages=[source_stage(market_predictions=[
            candidate("2026-08-10T13:00:01+08:00"),
            candidate("2026-08-10T12:54:00+08:00", source="other-book"),
        ])])
        planned, audit = build_plan(state)

        self.assertEqual(planned, [])
        self.assertEqual(audit["aggregate"]["total_unresolved_records"], 1)
        self.assertEqual(
            audit["aggregate"]["unresolved_reasons"],
            {"no_valid_pre_kickoff_crown_quote": 1},
        )

    def test_native_t5_is_never_represented_as_a_recovery(self):
        state = ledger(stages=[source_stage(), {"stage": "T-5", "ts": "2026-08-10T12:55:00+08:00"}])
        planned, audit = build_plan(state)
        self.assertEqual(planned, [])
        self.assertEqual(audit["aggregate"]["skipped"], {"native_t5_already_exists": 1})

    def test_strict_fixture_identity_rejects_populated_stage_mismatch(self):
        state = ledger(stages=[source_stage(home="Different home")])
        planned, audit = build_plan(state)
        self.assertEqual(planned, [])
        self.assertEqual(
            audit["aggregate"]["unresolved_reasons"],
            {"fixture_identity_mismatch": 1},
        )

    def test_apply_is_idempotent_backs_up_and_does_not_mutate_bets(self):
        with tempfile.TemporaryDirectory() as directory:
            config = replace(settings(), state_dir=Path(directory))
            original = ledger()
            save_ledger(config, original)
            planned, _ = build_plan(original, recovered_at="2026-08-11T01:00:00+08:00")

            self.assertEqual(apply_plan(config, planned), {"added": 1, "already_present": 0})
            saved = load_ledger(config)
            stages = saved["watch"]["local-1"]["stages"]
            recovered = [row for row in stages if row.get("stage") == RECOVERED_T5_STAGE]
            self.assertEqual(len(recovered), 1)
            self.assertEqual(
                recovered[0]["recovery"]["recovery_kind"],
                "last_pre_t5_prediction_carry_forward",
            )
            self.assertEqual(saved["bets"], [])
            self.assertTrue((Path(directory) / "t5-recovery-backups").exists())
            history = json.loads((Path(directory) / "prediction_history.json").read_text())
            self.assertEqual(history["rows"][0]["stage"], RECOVERED_T5_STAGE)
            self.assertTrue(history["rows"][0]["post_hoc_backfill"])
            self.assertTrue(history["rows"][0]["exclude_from_settlement"])
            self.assertEqual(apply_plan(config, planned), {"added": 0, "already_present": 1})
            self.assertEqual(len(load_ledger(config)["watch"]["local-1"]["stages"]), 2)

    def test_recovered_rows_are_excluded_from_primary_statistics(self):
        native = {
            "match_id": "native", "stage": "T-5", "prediction_era": "era",
            "actual": "主勝", "forecast": "主勝", "correct": True,
            "result_status": "已核對", "market_predictions": [],
        }
        recovered = {
            "match_id": "backfill", "stage": RECOVERED_T5_STAGE, "prediction_era": "era",
            "post_hoc_backfill": True, "exclude_from_primary_statistics": True,
            "actual": "客勝", "forecast": "主勝", "correct": False,
            "result_status": "已核對",
            "recovery": {"source_stage": "T-30"},
            "kickoff": KICKOFF,
            "market_predictions": [{"code": "HDC", "line": -0.5, "side": "H", "odds": 1.82,
                                    "recovery_evidence_type": "t5_locf"}],
        }
        stats = calculate_stats([native, recovered], comparable_era="era")
        self.assertEqual(stats["predictions"], 1)
        self.assertEqual(stats["hits"], 1)
        self.assertEqual(stats["recovery_audit"]["records"], 1)
        self.assertEqual(stats["recovery_audit"]["markets"], 1)

    def test_carry_forward_ui_labels_are_prominent_and_non_native(self):
        app = (Path(__file__).resolve().parents[1] / "dashboard" / "app.js").read_text(encoding="utf-8")
        self.assertIn("last_pre_t5_prediction_carry_forward", app)
        self.assertIn("預測階段替代：承接最後 T-5 前方向（非真實 T-5）", app)
        self.assertIn("含收市賠率替代", app)


if __name__ == "__main__":
    unittest.main()
