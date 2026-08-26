"""Regression coverage for the isolated Footbreak × Crown simulation ledger."""
from __future__ import annotations

import copy
import inspect
import json
import os
import sys
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
if str(ROOT / "system") not in sys.path:
    sys.path.insert(0, str(ROOT / "system"))

import crown_execution_test as cross
import native_stage_state as stage_state
import notify
from analysis.wilson_validation import admission_arithmetic, freeze_condition
from crown import hkjc_execution_test as reciprocal
from crown.config import settings as crown_settings
from crown import state as crown_state
from crown.ledger import _snapshot as crown_stage_snapshot, sync_prediction
from crown.state import (
    paths as crown_paths, save_predictions, project_footbreak_execution_evidence,
)
import record_picks

HKT = timezone(timedelta(hours=8))


def stamp(minutes=0):
    return (datetime.now(HKT) + timedelta(minutes=minutes)).isoformat()


def crown_card(*, odds=1.9, line=2.5, side="H", observed=None, duplicate=False):
    kickoff = stamp(120)
    row = {"code": "HIL", "side": side, "line": line, "odds": odds,
           "source": "titan007-crown-id-3", "odds_status": "available",
           "observed_at": observed or stamp(-1)}
    card = {"match_id": "crown-fx", "hkjc_match_id": "fx", "kickoff_hkt": kickoff,
            "current_selected_odds_journal": [row]}
    return [card, copy.deepcopy(card)] if duplicate else [card]


def authoritative_betis_card(*, kickoff, observed=None, duplicate=False):
    row = {
        "code": "HIL", "side": "H", "line": 2.5, "odds": 1.91,
        "source": "titan007-crown-id-3", "odds_status": "available",
        "observed_at": observed or stamp(-1),
    }
    card = {
        "match_id": "crown-betis", "hkjc_match_id": "fx", "kickoff_hkt": kickoff,
        "league": "Spain - La Liga", "home": "皇家贝蒂斯", "away": "皇家社会",
        "native_stage_journals": {"T-30": [copy.deepcopy(row)]},
        "current_selected_odds_journal": [copy.deepcopy(row)],
    }
    return [card, copy.deepcopy(card)] if duplicate else [card]


def recovery_watch_and_ledger(*, kickoff, at):
    watch = {
        "match_id": "fx", "kickoff": kickoff, "league": "Spain - La Liga",
        "home": "貝迪斯", "away": "皇家蘇斯達", "stages": [],
    }
    stage_state.ensure_manifest(
        watch, origin="first_look", now=cross._time(at) - timedelta(minutes=1),
    )
    attempt = stage_state.start_attempt(
        {"native_stage_attempts": []}, watch, "T-30", now=cross._time(at),
    )
    ledger = {"watch": {"fx": watch}, "native_stage_attempts": [attempt]}
    snapshot = stage_state.enrich_snapshot({
        "stage": "T-30", "ts": at, "market_predictions": [
            {"code": "HIL", "side": "H", "line": 2.5},
        ],
        "native_snapshot_id": "attempt:" + attempt["attempt_id"],
    }, watch, "T-30")
    watch["stages"].append(snapshot)
    stage_state.finish_attempt(
        ledger, attempt, "COMMITTED", now=cross._time(at) + timedelta(seconds=1),
    )
    first = {
        "at": stamp(-60), "status": "UNAVAILABLE",
        "reason": "crown_fixture_not_listed",
        "counterpart_book": "crown",
        "hkjc_match_id": "fx",
        "footbreak_kickoff": kickoff,
        "footbreak_fixture": {
            "league": watch["league"], "home": watch["home"], "away": watch["away"],
        },
    }
    watch["counterpart_bridges"] = {"crown": {
        "schema_version": 3, "counterpart_book": "crown",
        "first_look": copy.deepcopy(first),
    }}
    return watch, ledger, first


class CrossEvidenceTests(unittest.TestCase):
    def _quote(self, cards, *, side="H", line=2.5):
        with tempfile.TemporaryDirectory() as directory:
            evidence = Path(directory, "predictions.json")
            evidence.write_text(json.dumps(cards), encoding="utf-8")
            with patch.dict(os.environ, {"FOOTBREAK_CROWN_EXECUTION_EVIDENCE_PATH": str(evidence)}):
                return cross._crown_quote_for_exact_fixture(
                    "fx", "HIL", side, line, cross._time(stamp()), cross._time(stamp(120))
                )

    def test_exact_cross_quote_accepts_only_same_fixture_market_side_line(self):
        quote, reason = self._quote(crown_card())
        self.assertEqual(reason, None)
        self.assertEqual(quote["odds"], 1.9)
        for side, line in (("L", 2.5), ("H", 2.75)):
            quote, reason = self._quote(crown_card(), side=side, line=line)
            self.assertIsNone(quote)
            self.assertEqual(reason, "crown_exact_market_side_line_missing_or_ambiguous")

    def test_ambiguous_stale_missing_and_postkickoff_evidence_fail_closed(self):
        _, reason = self._quote(crown_card(duplicate=True))
        self.assertEqual(reason, "crown_fixture_identity_missing_or_ambiguous")
        _, reason = self._quote(crown_card(observed=stamp(-10)))
        self.assertEqual(reason, "crown_execution_quote_stale_at_t5")
        cards = crown_card(); del cards[0]["current_selected_odds_journal"][0]["observed_at"]
        _, reason = self._quote(cards)
        self.assertEqual(reason, "crown_execution_timestamp_missing")
        _, reason = self._quote(crown_card(observed=stamp(121)))
        self.assertEqual(reason, "crown_execution_post_kickoff_or_post_decision")

    def test_cross_entry_uses_crown_gate_and_isolated_idempotent_cap(self):
        kickoff, stage = stamp(120), stamp()
        current = {"stage": "T-5", "ts": stage, "market_predictions": []}
        watch = {"match_id": "fx", "kickoff": kickoff, "league": "英超", "home": "主", "away": "客", "stages": [current],
                 "counterpart_bridges": {"crown": {
                     "first_look": {"status": "RESOLVED", "crown_match_id": "crown-fx"},
                     "t30": {"status": "RESOLVED", "crown_match_id": "crown-fx",
                             "market_mappings": {"HIL": {"status": "AVAILABLE", "side": "H", "line": 2.5}}},
                 }}}
        admission = {"signature": "sig", "history": {"hits": 41, "decided": 59},
                     "arithmetic": admission_arithmetic(41, 59, 1.9)}
        ledger = {"bets": [{"bet_id": "normal"}], "wilson_validation": {"conditions": {
            "sig": {"condition_number": 7, "active_evidence": {
                "version": 1, "evidence_hash": "frozen", "cumulative_hits": 41,
                "cumulative_decided": 59}}}}}
        selected = {"code": "HIL", "side": "H", "line": 2.5, "odds": 1.66,
                    "source": "hkjc_public_board", "observed_at": stamp(-1)}
        with tempfile.TemporaryDirectory() as directory:
            evidence = Path(directory, "predictions.json")
            evidence.write_text(json.dumps(crown_card(odds=1.9)), encoding="utf-8")
            with patch.dict(os.environ, {"FOOTBREAK_CROWN_EXECUTION_EVIDENCE_PATH": str(evidence)}), \
                 patch.object(cross, "_native_t5", return_value=True), \
                 patch.object(cross, "match_upcoming", return_value={"fx": [{}]}), \
                 patch.object(cross, "_hkjc_selected", return_value=(selected, None)), \
                 patch.object(cross, "matching_admissions", return_value=([admission], "wilson_pass")):
                created, _ = cross.evaluate_new_t5(ledger, watch, ranking=[{}], now=stamp(1))
                again, _ = cross.evaluate_new_t5(ledger, watch, ranking=[{}], now=stamp(1))
        self.assertEqual(len(created), 1)
        self.assertEqual(again, [])
        self.assertEqual(len(ledger["bets"]), 1)  # Normal Wilson never touched.
        bet = ledger[cross.NAMESPACE]["bets"][0]
        self.assertEqual(bet["hkjc_signal_odds"], 1.66)
        self.assertEqual(bet["crown_execution_odds"], 1.9)
        self.assertEqual(bet["condition_number"], 7)
        self.assertGreaterEqual(bet["crown_execution_odds"], bet["wilson_admission"]["minimum_acceptable_odds_raw"])
        # This namespace's own cap remains independent.
        self.assertLessEqual(sum(b["stake"] for b in ledger[cross.NAMESPACE]["bets"]), 1500)

    def test_t5_grace_accepts_late_quote_only_within_fixed_decision_cutoff(self):
        stage_at = datetime.now(HKT)
        decision_at = stage_at + timedelta(seconds=20)
        kickoff = stage_at + timedelta(minutes=5)

        def cards(*, board_at, observed_at):
            quote = {
                "code": "HIL", "side": "H", "line": 2.5, "odds": 1.91,
                "source": "titan007-crown-id-3", "odds_status": "available",
                "observed_at": observed_at.isoformat(),
            }
            return [{
                "match_id": "crown-fx", "hkjc_match_id": "fx",
                "kickoff_hkt": kickoff.isoformat(),
                "native_stage_quote_boards": {
                    "T-5": {
                        "stage": "T-5", "stage_at": board_at.isoformat(),
                        "coverage": "native_full_board", "quotes": [quote],
                    },
                },
            }]

        with tempfile.TemporaryDirectory() as directory:
            evidence = Path(directory, "cards.json")
            evidence.write_text(json.dumps(cards(
                board_at=stage_at + timedelta(seconds=9),
                observed_at=stage_at + timedelta(seconds=8),
            )), encoding="utf-8")
            with patch.dict(os.environ, {
                "FOOTBREAK_CROWN_EXECUTION_EVIDENCE_PATH": str(evidence),
            }):
                quote, reason = cross._crown_quote_for_exact_fixture(
                    "fx", "HIL", "H", 2.5, stage_at, kickoff,
                    expected_crown_match_id="crown-fx", decision_at=decision_at,
                )
        self.assertIsNone(reason)
        self.assertEqual(quote["odds"], 1.91)

    def test_t5_grace_rejects_board_or_quote_after_fixed_decision_cutoff(self):
        stage_at = datetime.now(HKT)
        decision_at = stage_at + timedelta(seconds=20)
        kickoff = stage_at + timedelta(minutes=5)

        def reason_for(*, board_seconds, quote_seconds):
            card = [{
                "match_id": "crown-fx", "hkjc_match_id": "fx",
                "kickoff_hkt": kickoff.isoformat(),
                "native_stage_quote_boards": {
                    "T-5": {
                        "stage": "T-5",
                        "stage_at": (stage_at + timedelta(seconds=board_seconds)).isoformat(),
                        "coverage": "native_full_board",
                        "quotes": [{
                            "code": "HIL", "side": "H", "line": 2.5, "odds": 1.91,
                            "source": "titan007-crown-id-3", "odds_status": "available",
                            "observed_at": (
                                stage_at + timedelta(seconds=quote_seconds)
                            ).isoformat(),
                        }],
                    },
                },
            }]
            with tempfile.TemporaryDirectory() as directory:
                evidence = Path(directory, "cards.json")
                evidence.write_text(json.dumps(card), encoding="utf-8")
                with patch.dict(os.environ, {
                    "FOOTBREAK_CROWN_EXECUTION_EVIDENCE_PATH": str(evidence),
                }):
                    _quote, reason = cross._crown_quote_for_exact_fixture(
                        "fx", "HIL", "H", 2.5, stage_at, kickoff,
                        expected_crown_match_id="crown-fx", decision_at=decision_at,
                    )
            return reason

        self.assertEqual(
            reason_for(board_seconds=21, quote_seconds=19),
            "crown_native_t5_post_decision_rejected",
        )
        self.assertEqual(
            reason_for(board_seconds=19, quote_seconds=21),
            "crown_execution_post_kickoff_or_post_decision",
        )

    def test_t5_grace_is_per_market_and_not_cancelled_by_unrelated_native_error(self):
        stage_at = datetime.now(HKT)
        kickoff = stage_at + timedelta(minutes=5)
        watch = {
            "match_id": "fx", "kickoff": kickoff.isoformat(),
            "stages": [{"stage": "T-5", "ts": stage_at.isoformat()}],
        }
        signal = {
            "side": "H", "line": 2.5, "observed_at": stage_at.isoformat(),
        }
        with patch.object(cross, "_attempt_t5_identity_recovery", return_value=None), \
             patch.object(cross, "_hkjc_selected", side_effect=[
                 (None, "hkjc_signal_missing"),
                 (signal, None),
                 (None, "hkjc_signal_missing"),
             ]), \
             patch.object(
                 cross, "_crown_quote_for_verified_bridge",
                 return_value=(None, "crown_native_t5_not_collected"),
             ):
            result = cross.capture_t5_counterparts(
                watch, now=(stage_at + timedelta(seconds=5)).isoformat(),
                grace_deadline_at=(stage_at + timedelta(seconds=20)).isoformat(),
            )
        self.assertEqual(result["HIL"]["status"], "PENDING")
        self.assertEqual(result["HDC"]["status"], "UNAVAILABLE")
        self.assertEqual(result["CHL"]["status"], "UNAVAILABLE")
        self.assertNotIn("t5", watch["counterpart_bridges"]["crown"])

    def test_low_execution_odd_does_not_commit_even_when_hkjc_tier_matches(self):
        arithmetic = admission_arithmetic(41, 59, 1.50)
        self.assertFalse(arithmetic["passes"])
        # The HKJC signal tier (1.66, i.e. <1.70) is not execution evidence.
        self.assertGreater(arithmetic["minimum_acceptable_odds_raw"], 1.50)

    def test_first_look_authoritative_id_accepts_translated_alias_and_t30_persists_exact_mapping(self):
        kickoff = stamp(120)
        cards = [{"match_id": "crown-betis", "hkjc_match_id": "fx",
                  "kickoff_hkt": stamp(121), "league": "Spain - La Liga",
                  "home": "皇家贝蒂斯", "away": "皇家社会",
                  "native_stage_journals": {"T-30": [{
                      "code": "HIL", "side": "H", "line": 2.5,
                      "source": "titan007-crown-id-3", "odds": 1.91,
                      "observed_at": stamp(-1), "odds_status": "available",
                  }]}}]
        watch = {"match_id": "fx", "kickoff": kickoff, "league": "Spain - La Liga",
                 "home": "貝迪斯", "away": "皇家蘇斯達",
                 "stages": [{"stage": "T-30", "market_predictions": [{
                     "code": "HIL", "side": "H", "line": 2.5}]}]}
        with tempfile.TemporaryDirectory() as directory:
            evidence = Path(directory, "cards.json")
            evidence.write_text(json.dumps(cards), encoding="utf-8")
            with patch.dict(os.environ, {"FOOTBREAK_CROWN_EXECUTION_EVIDENCE_PATH": str(evidence)}):
                first = cross.prefetch_bridge(watch, stage="首預", now=stamp())
                t30 = cross.prefetch_bridge(watch, stage="T-30", now=stamp())
        self.assertEqual(first["status"], "RESOLVED")
        self.assertEqual(first["crown_match_id"], "crown-betis")
        self.assertLessEqual(first["kickoff_delta_seconds"], 120)
        self.assertEqual(t30["market_mappings"]["HIL"]["status"], "AVAILABLE")

    def test_t30_bootstraps_existing_card_without_fabricating_first_look(self):
        kickoff, at = stamp(120), stamp()
        watch = {
            "match_id": "fx", "kickoff": kickoff, "league": "Spain - La Liga",
            "home": "貝迪斯", "away": "皇家蘇斯達",
            "stages": [{"stage": "T-30", "ts": at, "market_predictions": [
                {"code": "HIL", "side": "H", "line": 2.5},
            ]}],
        }
        with tempfile.TemporaryDirectory() as directory:
            evidence = Path(directory, "cards.json")
            evidence.write_text(json.dumps(authoritative_betis_card(kickoff=kickoff)), encoding="utf-8")
            with patch.dict(os.environ, {"FOOTBREAK_CROWN_EXECUTION_EVIDENCE_PATH": str(evidence)}):
                t30 = cross.prefetch_bridge(watch, stage="T-30", now=at)
                repeated = cross.prefetch_bridge(watch, stage="T-30", now=at)
        self.assertEqual(t30["status"], "RESOLVED")
        self.assertEqual(t30["origin"], "t30_bootstrap_existing_card")
        self.assertFalse(t30["first_look_recorded"])
        self.assertEqual(t30["market_mappings"]["HIL"]["status"], "AVAILABLE")
        self.assertEqual(repeated, t30)  # No duplicate stage or changing identity.
        bridge = watch["counterpart_bridges"]["crown"]
        self.assertNotIn("first_look", bridge)
        self.assertEqual(bridge["t30"], t30)
        self.assertEqual(watch["stages"][0]["stage"], "T-30")  # Native record remains immutable.

    def test_t30_bootstrap_persists_authoritative_failure_without_fake_first_look(self):
        kickoff, at = stamp(120), stamp()
        watch = {
            "match_id": "fx", "kickoff": kickoff, "league": "Spain - La Liga",
            "home": "貝迪斯", "away": "皇家蘇斯達",
            "stages": [{"stage": "T-30", "ts": at, "market_predictions": []}],
        }
        with tempfile.TemporaryDirectory() as directory:
            evidence = Path(directory, "cards.json")
            evidence.write_text(
                json.dumps(authoritative_betis_card(kickoff=kickoff, duplicate=True)),
                encoding="utf-8",
            )
            with patch.dict(os.environ, {"FOOTBREAK_CROWN_EXECUTION_EVIDENCE_PATH": str(evidence)}):
                t30 = cross.prefetch_bridge(watch, stage="T-30", now=at)
        self.assertEqual(t30["status"], "UNAVAILABLE")
        self.assertEqual(t30["reason"], "crown_fixture_identity_ambiguous")
        self.assertEqual(t30["origin"], "t30_bootstrap_existing_card")
        self.assertNotIn("first_look", watch["counterpart_bridges"]["crown"])

    def test_t30_recovers_after_unresolved_first_look_without_overwriting_history(self):
        kickoff, at = stamp(30), stamp()
        watch, ledger, first = recovery_watch_and_ledger(kickoff=kickoff, at=at)
        with tempfile.TemporaryDirectory() as directory:
            evidence = Path(directory, "cards.json")
            evidence.write_text(
                json.dumps(authoritative_betis_card(kickoff=kickoff, observed=stamp(-1))),
                encoding="utf-8",
            )
            with patch.dict(os.environ, {"FOOTBREAK_CROWN_EXECUTION_EVIDENCE_PATH": str(evidence)}):
                t30 = cross.prefetch_bridge(watch, stage="T-30", now=at, ledger=ledger)
                watch["stages"].append({
                    "stage": "T-5", "ts": at, "market_predictions": [{
                        "code": "HIL", "side": "H", "line": 2.5, "odds": 1.69,
                        "source": "hkjc_public_board", "observed_at": stamp(-1),
                    }],
                })
                captured = cross.capture_t5_counterparts(
                    watch, now=at, ledger=ledger,
                )
                quote, reason = cross._crown_quote_for_verified_bridge(
                    watch, "HIL", "H", 2.5, cross._time(at), cross._time(kickoff),
                    ledger=ledger,
                )
        self.assertEqual(watch["counterpart_bridges"]["crown"]["first_look"], first)
        self.assertEqual(t30["status"], "RESOLVED")
        self.assertEqual(t30["origin"], "t30_recovery_after_unresolved_first_look")
        self.assertEqual(t30["first_look_hash"], cross._record_hash(first))
        self.assertTrue(t30["native_t30_proof_hash"])
        self.assertEqual(captured["HIL"]["status"], "AVAILABLE")
        self.assertIsNone(reason)
        self.assertEqual(quote["odds"], 1.91)

    def test_t30_recovery_fails_closed_if_first_look_provenance_changes(self):
        kickoff, at = stamp(30), stamp()
        watch, ledger, _first = recovery_watch_and_ledger(kickoff=kickoff, at=at)
        with tempfile.TemporaryDirectory() as directory:
            evidence = Path(directory, "cards.json")
            evidence.write_text(
                json.dumps(authoritative_betis_card(kickoff=kickoff, observed=stamp(-1))),
                encoding="utf-8",
            )
            with patch.dict(os.environ, {"FOOTBREAK_CROWN_EXECUTION_EVIDENCE_PATH": str(evidence)}):
                cross.prefetch_bridge(watch, stage="T-30", now=at, ledger=ledger)
                watch["counterpart_bridges"]["crown"]["first_look"]["reason"] = "changed"
                quote, reason = cross._crown_quote_for_verified_bridge(
                    watch, "HIL", "H", 2.5, cross._time(at), cross._time(kickoff),
                    ledger=ledger,
                )
        self.assertIsNone(quote)
        self.assertEqual(reason, "crown_t30_bootstrap_unverified")

    def test_t30_recovery_rejects_tampered_time_identity_context_and_empty_first_look(self):
        kickoff, at = stamp(30), stamp()
        for label, tamper in (
            ("first look time", lambda watch: watch["counterpart_bridges"]["crown"]["first_look"].__setitem__("at", stamp(-30))),
            ("identity context", lambda watch: watch["counterpart_bridges"]["crown"]["t30"]["identity_context"]["crown"].__setitem__("home", "WRONG TEAM")),
        ):
            with self.subTest(label=label):
                watch, ledger, _first = recovery_watch_and_ledger(kickoff=kickoff, at=at)
                with tempfile.TemporaryDirectory() as directory:
                    evidence = Path(directory, "cards.json")
                    evidence.write_text(
                        json.dumps(authoritative_betis_card(kickoff=kickoff, observed=stamp(-1))),
                        encoding="utf-8",
                    )
                    with patch.dict(os.environ, {"FOOTBREAK_CROWN_EXECUTION_EVIDENCE_PATH": str(evidence)}):
                        cross.prefetch_bridge(watch, stage="T-30", now=at, ledger=ledger)
                        tamper(watch)
                        quote, reason = cross._crown_quote_for_verified_bridge(
                            watch, "HIL", "H", 2.5, cross._time(at), cross._time(kickoff),
                            ledger=ledger,
                        )
                self.assertIsNone(quote)
                self.assertEqual(reason, "crown_t30_bootstrap_unverified")

        watch, ledger, _first = recovery_watch_and_ledger(kickoff=kickoff, at=at)
        watch["counterpart_bridges"]["crown"]["first_look"] = {}
        with tempfile.TemporaryDirectory() as directory:
            evidence = Path(directory, "cards.json")
            evidence.write_text(
                json.dumps(authoritative_betis_card(kickoff=kickoff, observed=stamp(-1))),
                encoding="utf-8",
            )
            with patch.dict(os.environ, {"FOOTBREAK_CROWN_EXECUTION_EVIDENCE_PATH": str(evidence)}):
                result = cross.prefetch_bridge(watch, stage="T-30", now=at, ledger=ledger)
        self.assertEqual(result["status"], "UNAVAILABLE")
        self.assertNotEqual(result.get("origin"), cross.T30_RECOVERY_ORIGIN)

    def test_valid_t30_recovery_is_immutable_when_local_evidence_disappears(self):
        kickoff, at = stamp(30), stamp()
        watch, ledger, _first = recovery_watch_and_ledger(kickoff=kickoff, at=at)
        with tempfile.TemporaryDirectory() as directory:
            evidence = Path(directory, "cards.json")
            evidence.write_text(
                json.dumps(authoritative_betis_card(kickoff=kickoff, observed=stamp(-1))),
                encoding="utf-8",
            )
            with patch.dict(os.environ, {"FOOTBREAK_CROWN_EXECUTION_EVIDENCE_PATH": str(evidence)}):
                recovered = copy.deepcopy(cross.prefetch_bridge(
                    watch, stage="T-30", now=at, ledger=ledger,
                ))
                evidence.unlink()
                repeated = cross.prefetch_bridge(
                    watch, stage="T-30", now=stamp(1), ledger=ledger,
                )
        self.assertEqual(repeated, recovered)
        self.assertEqual(
            watch["counterpart_bridges"]["crown"]["t30"], recovered,
        )

    def test_t30_recovery_requires_committed_native_attempt(self):
        kickoff, at = stamp(30), stamp()
        watch, ledger, _first = recovery_watch_and_ledger(kickoff=kickoff, at=at)
        ledger["native_stage_attempts"] = [
            row for row in ledger["native_stage_attempts"]
            if row.get("status") != "COMMITTED"
        ]
        with tempfile.TemporaryDirectory() as directory:
            evidence = Path(directory, "cards.json")
            evidence.write_text(
                json.dumps(authoritative_betis_card(kickoff=kickoff, observed=stamp(-1))),
                encoding="utf-8",
            )
            with patch.dict(os.environ, {"FOOTBREAK_CROWN_EXECUTION_EVIDENCE_PATH": str(evidence)}):
                result = cross.prefetch_bridge(
                    watch, stage="T-30", now=at, ledger=ledger,
                )
        self.assertEqual(result["status"], "UNAVAILABLE")
        self.assertEqual(result["reason"], "crown_t30_recovery_native_stage_missing")

    def test_t30_recovery_rejects_invalid_manifest_due_and_postkickoff_first_look(self):
        kickoff, at = stamp(30), stamp()
        watch, ledger, _first = recovery_watch_and_ledger(kickoff=kickoff, at=at)
        watch["native_stage_manifest"]["jobs"]["T-30"]["due_at_utc"] = "garbage"
        watch["stages"][0]["due_at_utc"] = "garbage"
        for row in ledger["native_stage_attempts"]:
            row["due_at_utc"] = "garbage"
        self.assertIsNone(cross._native_t30_proof(ledger, watch))

        watch, ledger, _first = recovery_watch_and_ledger(kickoff=kickoff, at=at)
        watch["counterpart_bridges"]["crown"]["first_look"]["at"] = (
            cross._time(kickoff) + timedelta(seconds=1)
        ).isoformat()
        proof = cross._native_t30_proof(ledger, watch)
        self.assertIsNotNone(proof)
        self.assertFalse(cross._valid_unresolved_first_look(
            watch, watch["counterpart_bridges"]["crown"]["first_look"],
            native_t30_at=proof["snapshot_at"],
        ))

    def test_t30_recovery_rejects_unknown_reason_and_inconsistent_native_times(self):
        kickoff, at = stamp(30), stamp()
        watch, ledger, _first = recovery_watch_and_ledger(kickoff=kickoff, at=at)
        proof = cross._native_t30_proof(ledger, watch)
        self.assertIsNotNone(proof)
        watch["counterpart_bridges"]["crown"]["first_look"]["reason"] = "arbitrary_failure"
        self.assertFalse(cross._valid_unresolved_first_look(
            watch, watch["counterpart_bridges"]["crown"]["first_look"],
            native_t30_at=proof["snapshot_at"],
        ))

        for label, mutate in (
            ("manifest top kickoff", lambda w, _l: w["native_stage_manifest"].__setitem__(
                "kickoff_at_utc", stamp(31))),
            ("snapshot due hkt", lambda w, _l: w["stages"][0].__setitem__(
                "due_at_hkt", stamp(1))),
            ("attempt kickoff hkt", lambda _w, l: l["native_stage_attempts"][0].__setitem__(
                "kickoff_at_hkt", stamp(31))),
            ("manifest created after start", lambda w, _l: w["native_stage_manifest"].__setitem__(
                "created_at", stamp(1))),
        ):
            with self.subTest(label=label):
                candidate, candidate_ledger, _first = recovery_watch_and_ledger(
                    kickoff=kickoff, at=at,
                )
                mutate(candidate, candidate_ledger)
                self.assertIsNone(cross._native_t30_proof(candidate_ledger, candidate))

    def test_post_commit_t5_job_consumes_recovered_bridge_with_ledger(self):
        kickoff, at = stamp(30), stamp()
        watch, ledger, _first = recovery_watch_and_ledger(kickoff=kickoff, at=at)
        t5_snapshot = {
            "stage": "T-5", "ts": at, "native_snapshot_id": "snapshot:t5",
            "market_predictions": [{
                "code": "HIL", "side": "H", "line": 2.5, "odds": 1.69,
                "source": "hkjc_public_board", "observed_at": stamp(-1),
            }],
        }
        job = {
            "schema_version": 1, "job_id": "fx:snapshot:t5",
            "status": "PENDING", "at": at, "match_id": "fx", "stage": "T-5",
            "snapshot_id": "snapshot:t5", "t5_safe_to_evaluate": False,
        }
        ledger["wilson_validation"] = {"audit": []}
        with tempfile.TemporaryDirectory() as directory:
            evidence = Path(directory, "cards.json")
            evidence.write_text(
                json.dumps(authoritative_betis_card(
                    kickoff=kickoff, observed=stamp(-1),
                )),
                encoding="utf-8",
            )
            with patch.dict(os.environ, {
                "FOOTBREAK_CROWN_EXECUTION_EVIDENCE_PATH": str(evidence),
            }), patch.object(record_picks, "_record_learning_snapshot", return_value=None):
                recovered = cross.prefetch_bridge(
                    watch, stage="T-30", now=at, ledger=ledger,
                )
                watch["stages"].append(t5_snapshot)
                record_picks._process_optional_job(
                    ledger, job, now=at, changes=[],
                )
        self.assertEqual(recovered["status"], "RESOLVED")
        captured = watch["counterpart_bridges"]["crown"]["t5"]["markets"]["HIL"]
        self.assertEqual(captured["status"], "AVAILABLE")
        self.assertEqual(captured["crown_quote"]["odds"], 1.91)
        self.assertEqual(
            ledger["wilson_validation"]["audit"][-1]["reason"],
            "t5_safe_lead_not_met",
        )

    def test_existing_t30_history_is_never_overwritten(self):
        kickoff, at = stamp(30), stamp()
        watch, ledger, _first = recovery_watch_and_ledger(kickoff=kickoff, at=at)
        historical = {
            "at": "old", "status": "RESOLVED",
            "origin": "legacy_valid_history", "marker": "do-not-overwrite",
        }
        watch["counterpart_bridges"]["crown"]["t30"] = copy.deepcopy(historical)
        with patch.object(cross, "_load_local_crown_cards",
                          side_effect=AssertionError("immutable row must return before provider read")):
            repeated = cross.prefetch_bridge(
                watch, stage="T-30", now=at, ledger=ledger,
            )
        self.assertEqual(repeated, historical)
        self.assertEqual(watch["counterpart_bridges"]["crown"]["t30"], historical)

    def test_t30_bootstrap_requires_a_durable_native_t30_and_rejects_post_kickoff(self):
        kickoff, at = stamp(120), stamp()
        watch = {
            "match_id": "fx", "kickoff": kickoff, "league": "Spain - La Liga",
            "home": "貝迪斯", "away": "皇家蘇斯達", "stages": [],
        }
        with tempfile.TemporaryDirectory() as directory:
            evidence = Path(directory, "cards.json")
            evidence.write_text(json.dumps(authoritative_betis_card(kickoff=kickoff)), encoding="utf-8")
            with patch.dict(os.environ, {"FOOTBREAK_CROWN_EXECUTION_EVIDENCE_PATH": str(evidence)}):
                missing = cross.prefetch_bridge(watch, stage="T-30", now=at)
                watch["stages"].append({"stage": "T-30", "ts": at, "market_predictions": []})
                late = cross.prefetch_bridge(
                    watch, stage="T-30",
                    now=(cross._time(kickoff) + timedelta(seconds=1)).isoformat(),
                )
        self.assertEqual(missing["reason"], "crown_t30_bootstrap_native_stage_missing")
        self.assertEqual(late, missing)
        self.assertEqual(late["origin"], "t30_bootstrap_existing_card")
        self.assertNotIn("first_look", watch["counterpart_bridges"]["crown"])

    def test_t5_consumes_only_a_verified_t30_bootstrap(self):
        kickoff, stage_at = stamp(120), stamp()
        watch = {
            "match_id": "fx", "kickoff": kickoff, "league": "Spain - La Liga",
            "home": "貝迪斯", "away": "皇家蘇斯達",
            "stages": [
                {"stage": "T-30", "ts": stage_at, "market_predictions": [
                    {"code": "HIL", "side": "H", "line": 2.5},
                ]},
                {"stage": "T-5", "ts": stage_at, "market_predictions": []},
            ],
        }
        with tempfile.TemporaryDirectory() as directory:
            evidence = Path(directory, "cards.json")
            evidence.write_text(
                json.dumps(authoritative_betis_card(kickoff=kickoff, observed=stamp(-1))),
                encoding="utf-8",
            )
            with patch.dict(os.environ, {"FOOTBREAK_CROWN_EXECUTION_EVIDENCE_PATH": str(evidence)}):
                t30 = cross.prefetch_bridge(watch, stage="T-30", now=stage_at)
                quote, reason = cross._crown_quote_for_verified_bridge(
                    watch, "HIL", "H", 2.5, cross._time(stage_at), cross._time(kickoff),
                )
        self.assertEqual(t30["origin"], "t30_bootstrap_existing_card")
        self.assertIsNone(reason)
        self.assertEqual(quote["odds"], 1.91)
        # An unverified marker alone cannot bypass the absent first-look guard.
        watch["counterpart_bridges"]["crown"]["t30"]["bridge_id"] = "forged"
        quote, reason = cross._crown_quote_for_verified_bridge(
            watch, "HIL", "H", 2.5, cross._time(stage_at), cross._time(kickoff),
        )
        self.assertIsNone(quote)
        self.assertEqual(reason, "crown_t30_bootstrap_unverified")

    def test_t5_preserves_the_exact_failed_t30_bootstrap_reason(self):
        watch = {
            "match_id": "fx", "kickoff": stamp(120), "stages": [],
            "counterpart_bridges": {"crown": {"t30": {
                "status": "UNAVAILABLE", "origin": "t30_bootstrap_existing_card",
                "reason": "crown_fixture_not_listed",
            }}},
        }
        quote, reason = cross._crown_quote_for_verified_bridge(
            watch, "HIL", "L", 2.5, cross._time(stamp()), cross._time(watch["kickoff"]),
        )
        self.assertIsNone(quote)
        self.assertEqual(reason, "crown_fixture_not_listed")

    def test_native_t30_continues_and_persists_bootstrap_for_existing_card(self):
        kickoff = stamp(120)
        result = {
            "match_id": "fx", "stage": "T-30", "kickoff_hkt": kickoff,
            "league": "Spain - La Liga", "home": "貝迪斯", "away": "皇家蘇斯達",
            "candidates": [], "selected_odds_observed_at": stamp(-1),
        }
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "predictions.json").write_text(json.dumps([result]), encoding="utf-8")
            evidence = root / "cards.json"
            evidence.write_text(json.dumps(authoritative_betis_card(kickoff=kickoff)), encoding="utf-8")
            ledger_path = root / "sim_ledger.json"
            with patch.dict(os.environ, {"FOOTBREAK_CROWN_EXECUTION_EVIDENCE_PATH": str(evidence)}), \
                 patch.object(record_picks, "HERE", str(root)), \
                 patch.object(record_picks, "LEDGER", str(ledger_path)), \
                 patch.object(record_picks, "_record_learning_snapshot", return_value=None), \
                 patch.object(record_picks, "recompute", return_value=None), \
                 patch.object(record_picks, "recompute_crown_execution", return_value=None):
                _, notes, ledger = record_picks.sync(send_notifications=False)
        stored = ledger["watch"]["fx"]
        self.assertEqual(stored["stages"][0]["stage"], "T-30")
        self.assertEqual(
            stored["counterpart_bridges"]["crown"]["t30"]["origin"],
            "t30_bootstrap_existing_card",
        )
        self.assertNotIn("first_look", stored["counterpart_bridges"]["crown"])
        self.assertTrue(notes)  # Native stage completed; counterpart was optional.

    def test_first_look_distinguishes_collector_no_fixture_and_ambiguous_identity(self):
        watch = {
            "match_id": "fx", "kickoff": stamp(120), "league": "Spain - La Liga",
            "home": "貝迪斯", "away": "皇家蘇斯達", "stages": [],
        }
        with tempfile.TemporaryDirectory() as directory:
            evidence = Path(directory, "cards.json")
            with patch.dict(os.environ, {"FOOTBREAK_CROWN_EXECUTION_EVIDENCE_PATH": str(evidence)}):
                evidence.write_text("[]", encoding="utf-8")
                self.assertEqual(
                    cross.prefetch_bridge(watch, stage="首預", now=stamp())["reason"],
                    "crown_collector_unavailable",
                )
                evidence.write_text(json.dumps([{
                    "match_id": "other", "hkjc_match_id": "not-fx",
                    "kickoff_hkt": stamp(120),
                }]), encoding="utf-8")
                self.assertEqual(
                    cross.prefetch_bridge(watch, stage="首預", now=stamp())["reason"],
                    "crown_fixture_not_listed",
                )
                evidence.write_text(json.dumps(crown_card(duplicate=True)), encoding="utf-8")
                self.assertEqual(
                    cross.prefetch_bridge(watch, stage="首預", now=stamp())["reason"],
                    "crown_fixture_identity_ambiguous",
                )

    def test_first_look_requires_complete_team_league_context_after_authoritative_id(self):
        watch = {
            "match_id": "fx", "kickoff": stamp(120), "league": "Spain - La Liga",
            "home": "貝迪斯", "away": "皇家蘇斯達", "stages": [],
        }
        card = {
            "match_id": "crown-betis", "hkjc_match_id": "fx",
            "kickoff_hkt": stamp(120),
        }
        with tempfile.TemporaryDirectory() as directory:
            evidence = Path(directory, "cards.json")
            with patch.dict(os.environ, {"FOOTBREAK_CROWN_EXECUTION_EVIDENCE_PATH": str(evidence)}):
                evidence.write_text(json.dumps([card]), encoding="utf-8")
                self.assertEqual(
                    cross.prefetch_bridge(watch, stage="首預", now=stamp())["reason"],
                    "crown_fixture_identity_incomplete",
                )
                evidence.write_text(json.dumps([card | {
                    "league": "Spain - La Liga", "home": "錯隊", "away": "皇家蘇斯達",
                }]), encoding="utf-8")
                self.assertEqual(
                    cross.prefetch_bridge(watch, stage="首預", now=stamp())["reason"],
                    "crown_fixture_team_or_league_identity_mismatch",
                )

    def test_t30_persists_market_unavailable_and_exact_line_unavailable_separately(self):
        kickoff = stamp(120)
        watch = {
            "match_id": "fx", "kickoff": kickoff, "stages": [{
                "stage": "T-30", "market_predictions": [
                    {"code": "HIL", "side": "H", "line": 2.5},
                    {"code": "CHL", "side": "H", "line": 9.5},
                ],
            }],
        }
        card = crown_card(line=2.75)[0]
        watch["counterpart_bridges"] = {"crown": {
            "first_look": {
                "status": "RESOLVED", "crown_match_id": "crown-fx",
                "bridge_id": "old", "crown_kickoff": kickoff,
            },
        }}
        # Call the mapping component directly: market availability is T-30
        # structural evidence and deliberately contains no odds gate.
        mapping = cross._t30_market_mappings(watch, {
            **card,
            "native_stage_journals": {"T-30": [{
                "code": "HIL", "side": "H", "line": 2.75,
            }]},
        })
        self.assertEqual(mapping["HIL"]["reason"], "crown_t30_exact_line_unavailable")
        self.assertEqual(mapping["CHL"]["reason"], "crown_t30_market_unavailable")

    def test_t5_rejects_a_changed_crown_id_or_post_kickoff_capture_without_native_replay(self):
        kickoff, stage = stamp(120), stamp()
        watch = {
            "match_id": "fx", "kickoff": kickoff,
            "stages": [{"stage": "T-5", "ts": stage, "market_predictions": []}],
            "counterpart_bridges": {"crown": {
                "first_look": {"status": "RESOLVED", "crown_match_id": "crown-old"},
                "t30": {"status": "RESOLVED", "crown_match_id": "crown-old",
                        "market_mappings": {"HIL": {"status": "AVAILABLE", "side": "H", "line": 2.5}}},
            }},
        }
        with tempfile.TemporaryDirectory() as directory:
            evidence = Path(directory, "cards.json")
            evidence.write_text(json.dumps(crown_card()), encoding="utf-8")
            with patch.dict(os.environ, {"FOOTBREAK_CROWN_EXECUTION_EVIDENCE_PATH": str(evidence)}):
                quote, reason = cross._crown_quote_for_verified_bridge(
                    watch, "HIL", "H", 2.5, cross._time(stage), cross._time(kickoff),
                )
        self.assertIsNone(quote)
        self.assertEqual(reason, "crown_t5_bridge_changed")
        with patch.object(cross, "_hkjc_selected", return_value=(
            {"code": "HIL", "side": "H", "line": 2.5, "observed_at": stage}, None,
        )):
            captured = cross.capture_t5_counterparts(
                watch, now=(cross._time(kickoff) + timedelta(seconds=1)).isoformat(),
            )
        self.assertEqual(captured["HIL"]["reason"], "crown_t5_post_kickoff_capture_rejected")
        self.assertEqual(watch["stages"][0]["stage"], "T-5")

    def test_post_kickoff_prefetch_never_backfills_a_bridge(self):
        kickoff = stamp(1)
        watch = {"match_id": "fx", "kickoff": kickoff, "stages": []}
        with tempfile.TemporaryDirectory() as directory:
            evidence = Path(directory, "cards.json")
            evidence.write_text(json.dumps(crown_card()), encoding="utf-8")
            with patch.dict(os.environ, {"FOOTBREAK_CROWN_EXECUTION_EVIDENCE_PATH": str(evidence)}):
                result = cross.prefetch_bridge(
                    watch, stage="首預",
                    now=(cross._time(kickoff) + timedelta(seconds=1)).isoformat(),
                )
        self.assertEqual(result["reason"], "crown_bridge_post_kickoff_rejected")

    def test_t5_requires_durable_first_and_t30_bridge(self):
        watch = {"match_id": "fx", "kickoff": stamp(120), "stages": []}
        quote, reason = cross._crown_quote_for_verified_bridge(
            watch, "HIL", "H", 2.5, cross._time(stamp()), cross._time(stamp(120)),
        )
        self.assertIsNone(quote)
        self.assertEqual(reason, "crown_first_look_bridge_missing_or_unresolved")

    def test_native_crown_t5_persistence_produces_bounded_consumer_evidence(self):
        kickoff, observed, stage_at = stamp(120), stamp(-1), stamp()
        card = {
            "match_id": "crown-fx", "hkjc_match_id": "fx", "kickoff_hkt": kickoff,
            "raw_provider_payload": {"token": "never-export"},
            "stages": [{"stage": "T-5", "selected_odds_journal": [{
                "code": "HIL", "line": 2.5, "side": "H", "odds": 1.9,
                "odds_status": "available", "source": "titan007-crown-id-3",
                "provider": "Crown", "observed_at": observed,
            }]}],
        }
        with tempfile.TemporaryDirectory() as directory:
            base = crown_settings()
            config = type(base)(**{**base.__dict__, "state_dir": Path(directory)})
            save_predictions(config, [card])
            project_footbreak_execution_evidence(config)
            sidecar = crown_paths(config)["footbreak_execution_evidence"]
            projected = json.loads(sidecar.read_text(encoding="utf-8"))
            self.assertEqual(projected[0]["hkjc_match_id"], "fx")
            self.assertEqual(projected[0]["current_selected_odds_journal"][0]["odds"], 1.9)
            self.assertNotIn("raw_provider_payload", json.dumps(projected))
            with patch.dict(os.environ, {"CROWN_STATE_DIR": directory}, clear=False):
                quote, reason = cross._crown_quote_for_exact_fixture(
                    "fx", "HIL", "H", 2.5, cross._time(stage_at), cross._time(kickoff),
                )
            self.assertIsNone(reason)
            self.assertEqual(quote["odds"], 1.9)

    def test_native_crown_stage_board_exports_both_sides_from_persisted_snapshot_only(self):
        kickoff, observed = stamp(120), stamp(-1)
        native = {
            "match_id": "crown-board", "hkjc_match_id": "fx", "kickoff_hkt": kickoff,
            "league": "Spain - La Liga", "home": "皇家贝蒂斯", "away": "皇家社会",
            "source_snapshot_at": observed,
            "crown_quote_source": "titan007-crown-id-3-bulk-current",
            "book_odds": {"crown": [
                {"market": "HDC", "line": 0.25, "selection": "H", "odds": 1.91, "source_at": observed},
                {"market": "HDC", "line": 0.25, "selection": "A", "odds": 1.97, "source_at": observed},
                {"market": "HIL", "line": 2.5, "selection": "H", "odds": 1.88, "source_at": observed},
                {"market": "HIL", "line": 2.5, "selection": "L", "odds": 2.01, "source_at": observed},
            ]},
            "raw_provider_payload": {"token": "never-export"},
            "forecast_candidates": [],
        }
        card = {
            key: native[key] for key in (
                "match_id", "hkjc_match_id", "kickoff_hkt", "league", "home", "away",
            )
        }
        card["stages"] = [
            crown_stage_snapshot(native, "T-30"),
            crown_stage_snapshot(native, "T-5"),
        ]
        with tempfile.TemporaryDirectory() as directory:
            base = crown_settings()
            config = type(base)(**{**base.__dict__, "state_dir": Path(directory)})
            save_predictions(config, [card])
            project_footbreak_execution_evidence(config)
            projected = json.loads(
                crown_paths(config)["footbreak_execution_evidence"].read_text(encoding="utf-8")
            )
        boards = projected[0]["native_stage_quote_boards"]
        self.assertEqual(boards["T-30"]["coverage"], "native_full_board")
        hil_sides = {
            row["side"]: row["odds"] for row in boards["T-5"]["quotes"]
            if row["code"] == "HIL" and row["line"] == 2.5
        }
        self.assertEqual(hil_sides, {"H": 1.88, "L": 2.01})
        encoded = json.dumps(projected)
        self.assertNotIn("raw_provider_payload", encoded)
        self.assertNotIn("never-export", encoded)

    def test_native_board_marks_missing_opposite_side_without_inversion(self):
        kickoff, observed = stamp(120), stamp(-1)
        native = {
            "match_id": "crown-one-side", "hkjc_match_id": "fx", "kickoff_hkt": kickoff,
            "source_snapshot_at": observed, "crown_quote_source": "titan007-crown-id-3",
            "book_odds": {"crown": [
                {"market": "HIL", "line": 2.5, "selection": "H", "odds": 1.91, "source_at": observed},
            ]},
            "forecast_candidates": [],
        }
        card = {key: native[key] for key in ("match_id", "hkjc_match_id", "kickoff_hkt")}
        card["stages"] = [crown_stage_snapshot(native, "T-5")]
        with tempfile.TemporaryDirectory() as directory:
            base = crown_settings()
            config = type(base)(**{**base.__dict__, "state_dir": Path(directory)})
            save_predictions(config, [card])
            project_footbreak_execution_evidence(config)
            with patch.dict(os.environ, {"CROWN_STATE_DIR": directory}, clear=False):
                quote, reason = cross._crown_quote_for_exact_fixture(
                    "fx", "HIL", "L", 2.5, cross._time(stamp()), cross._time(kickoff),
                )
        self.assertIsNone(quote)
        self.assertEqual(reason, "crown_native_quote_side_unavailable")

    def test_sidecar_projection_is_not_a_save_or_notification_dependency(self):
        kickoff = stamp(120)
        card = {"match_id": "independent", "hkjc_match_id": "fx", "kickoff_hkt": kickoff}
        with tempfile.TemporaryDirectory() as directory:
            base = crown_settings()
            config = type(base)(**{**base.__dict__, "state_dir": Path(directory)})
            save_predictions(config, [card])
            self.assertTrue(crown_paths(config)["predictions"].exists())
            self.assertFalse(crown_paths(config)["footbreak_execution_evidence"].exists())
            with patch("crown.state.write_json_atomic", side_effect=OSError("sidecar full")):
                with self.assertRaises(OSError):
                    project_footbreak_execution_evidence(config)
            # The durable native card remains untouched when optional
            # publication fails; no Telegram function is imported/called.
            self.assertEqual(json.loads(crown_paths(config)["predictions"].read_text()), [card])

    def test_stage_timestamp_is_immutable_and_projection_launch_never_imports_telegram(self):
        kickoff, observed = stamp(120), stamp(-1)
        prediction = {
            "match_id": "immutable-t30", "stage": "T-30", "kickoff_hkt": kickoff,
            "source_snapshot_at": observed, "forecast_candidates": [],
            "book_odds": {"crown": [
                {"market": "HIL", "line": 2.5, "selection": "H", "odds": 1.91, "source_at": observed},
                {"market": "HIL", "line": 2.5, "selection": "L", "odds": 1.91, "source_at": observed},
            ]},
        }
        with tempfile.TemporaryDirectory() as directory:
            base = crown_settings()
            config = type(base)(**{**base.__dict__, "state_dir": Path(directory)})
            ledger = {"watch": {}, "bets": [], "log": [], "stats": {}}
            sync_prediction(ledger, prediction, config)
            original = ledger["watch"]["immutable-t30"]["stages"][0]["ts"]
            sync_prediction(ledger, prediction, config)
        self.assertEqual(ledger["watch"]["immutable-t30"]["stages"][0]["ts"], original)
        self.assertNotIn("from .notify", inspect.getsource(crown_state))
        self.assertNotIn("notify_new", inspect.getsource(crown_state))
        fake = type("Process", (), {"daemon": False, "start": lambda self: setattr(self, "started", True)})()
        with patch("crown.state.multiprocessing.Process", return_value=fake):
            self.assertTrue(crown_state.schedule_footbreak_execution_evidence_projection(
                config, ["T-30"],
            ))
        self.assertTrue(fake.started)

    def test_native_footbreak_t5_persistence_carries_reciprocal_hkjc_evidence(self):
        kickoff = datetime.now(HKT) + timedelta(minutes=20)
        observed = datetime.now(HKT) - timedelta(seconds=30)
        result = {
            "match_id": "hkjc-native", "stage": "T-5", "kickoff_hkt": kickoff.isoformat(),
            "league": "英超", "home": "主", "away": "客",
            "selected_odds_observed_at": observed.isoformat(), "candidates": [{
                "code": "HIL", "market": "入球大細", "line": 2.5, "condition": 2.5,
                "side": "H", "label": "大", "odds": 1.9, "prob": .56, "push": .04,
                "is_main": True, "source": "hkjc_public_board",
                "observed_at": observed.isoformat(),
            }],
        }
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "predictions.json").write_text(json.dumps([result]), encoding="utf-8")
            ledger_path = root / "sim_ledger.json"
            with patch.object(record_picks, "HERE", str(root)), \
                 patch.object(record_picks, "LEDGER", str(ledger_path)), \
                 patch.object(record_picks, "_record_learning_snapshot", return_value=None), \
                 patch.object(record_picks, "evaluate_new_t5", return_value=([], [])), \
                 patch.object(record_picks, "evaluate_probability_research", return_value=([], [])), \
                 patch.object(record_picks, "evaluate_crown_execution_t5", return_value=([], [])), \
                 patch.object(record_picks, "recompute", return_value=None), \
                 patch.object(record_picks, "recompute_crown_execution", return_value=None):
                _, _, ledger = record_picks.sync(send_notifications=False)
            stored = ledger["watch"]["hkjc-native"]["stages"][0]["market_predictions"][0]
            self.assertEqual(stored["odds"], 1.9)
            self.assertEqual(stored["source"], "hkjc_public_board")
            self.assertEqual(stored["observed_at"], observed.isoformat())
            self.assertTrue(ledger_path.exists())
            with patch.dict(os.environ, {"CROWN_HKJC_EXECUTION_EVIDENCE_PATH": str(ledger_path)}), \
                 patch.object(reciprocal, "_native_t5", return_value=True):
                quote, reason = reciprocal._exact_hkjc_quote(
                    "hkjc-native", "HIL", "H", 2.5,
                    reciprocal._time(datetime.now(HKT).isoformat()),
                    reciprocal._time(kickoff.isoformat()),
                )
            self.assertIsNone(reason)
            self.assertEqual(quote["odds"], 1.9)

    def test_crown_sidecar_failure_cannot_block_native_footbreak_t5_commit(self):
        kickoff = datetime.now(HKT) + timedelta(minutes=20)
        result = {
            "match_id": "native-survives", "stage": "T-5", "kickoff_hkt": kickoff.isoformat(),
            "league": "英超", "home": "主", "away": "客", "candidates": [],
        }
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "predictions.json").write_text(json.dumps([result]), encoding="utf-8")
            ledger_path = root / "sim_ledger.json"
            with patch.object(record_picks, "HERE", str(root)), \
                 patch.object(record_picks, "LEDGER", str(ledger_path)), \
                 patch.object(record_picks, "_record_learning_snapshot", return_value=None), \
                 patch.object(record_picks, "evaluate_new_t5", return_value=([], [])), \
                 patch.object(record_picks, "evaluate_probability_research", return_value=([], [])), \
                 patch.object(record_picks, "capture_t5_counterparts", side_effect=RuntimeError("malformed")), \
                 patch.object(record_picks, "evaluate_crown_execution_t5", side_effect=RuntimeError("malformed")), \
                 patch.object(record_picks, "recompute", return_value=None), \
                 patch.object(record_picks, "recompute_crown_execution", return_value=None):
                _, _, ledger = record_picks.sync(send_notifications=False)
            self.assertEqual(ledger["watch"]["native-survives"]["stages"][0]["stage"], "T-5")
            self.assertTrue(ledger_path.exists())


class CrossNotificationTests(unittest.TestCase):
    def _bet(self):
        return {"bet_id": "cross-1", "portfolio": cross.NAMESPACE,
                "strategy": "footbreak-crown-execution-test-v1", "status": "PENDING",
                "simulation_only": True, "real_betting_enabled": False, "league": "英超",
                "home": "主", "away": "客", "kickoff": stamp(120), "market_label": "入球大細",
                "selected_role": "大", "selected_line": 2.5, "stake": 500,
                "condition_number": 7, "hkjc_signal_odds": 1.66,
                "hkjc_signal_source": "hkjc_public_board", "hkjc_signal_observed_at": stamp(-1),
                "crown_execution_odds": 1.9, "crown_execution_source": "titan007-crown-id-3",
                "crown_execution_observed_at": stamp(-1),
                "decision_at": stamp(),
                "wilson_admission": admission_arithmetic(41, 59, 1.9)}

    def test_message_dedupe_and_retry_has_required_venue_and_fields(self):
        bet = self._bet()
        text = notify._crown_execution_message(bet)
        for expected in ("足破×皇冠", "合符 足破 Wilson 條件 #7", "投注：入球大細 · 大 2.5",
                         "投注平台：皇冠", "馬會訊號賠率：1.66",
                         "皇冠執行賠率：1.90", "最低賠率要求："):
            self.assertIn(expected, text)
        self.assertNotIn("模擬投注 HK$", text)
        ledger = {cross.NAMESPACE: {"bets": [bet]}}
        with tempfile.TemporaryDirectory() as directory, patch.object(notify, "STATE", str(Path(directory, "n.json"))):
            with patch.object(notify, "send", side_effect=RuntimeError("retry")):
                with self.assertRaises(RuntimeError):
                    notify.notify_pending_crown_execution_bets(ledger)
            with patch.object(notify, "send") as sender:
                self.assertEqual(notify.notify_pending_crown_execution_bets(ledger), 1)
                self.assertEqual(notify.notify_pending_crown_execution_bets(ledger), 0)
            sender.assert_called_once()


class CrossDashboardContractTests(unittest.TestCase):
    def test_dashboard_has_isolated_projection_and_no_provider_ids(self):
        app = (ROOT / "hkjc-dashboard" / "app.js").read_text(encoding="utf-8")
        projection = (ROOT / "system" / "gen_app_data.py").read_text(encoding="utf-8")
        self.assertIn("足破×皇冠執行測試倉（模擬）", app)
        self.assertIn("訊號賠率層", app)
        self.assertIn("執行最低賠率", app)
        self.assertIn("_public_crown_execution_bet", projection)
        self.assertNotIn('"crown_execution_source"', projection.split("def _public_crown_execution_bet", 1)[1].split("def _wilson", 1)[0])

    def test_runtime_contract_uses_authoritative_paths_and_read_only_cross_access(self):
        deploy = ROOT / "deploy"
        crown_tick = (deploy / "systemd" / "crown-tick.service").read_text(encoding="utf-8")
        footbreak_tick = (deploy / "systemd" / "footbreak-tick.service").read_text(encoding="utf-8")
        self.assertIn("CROWN_HKJC_EXECUTION_EVIDENCE_PATH=/opt/footbreak/system/sim_ledger.json", crown_tick)
        self.assertIn("ReadOnlyPaths=/opt/footbreak/system/sim_ledger.json", crown_tick)
        self.assertIn("FOOTBREAK_CROWN_EXECUTION_EVIDENCE_PATH=/var/lib/footbreak/crown/footbreak-execution-evidence.json", footbreak_tick)
        self.assertIn("ReadOnlyPaths=/var/lib/footbreak/crown/footbreak-execution-evidence.json", footbreak_tick)
        self.assertIn('chmod 0600 "$APP_DIR/system/sim_ledger.json"', (deploy / "setup.sh").read_text(encoding="utf-8"))
        self.assertIn('chmod 0600 "$APP_DIR/system/sim_ledger.json"', (deploy / "update.sh").read_text(encoding="utf-8"))


class CrossSettlementTests(unittest.TestCase):
    def test_settlement_rules_and_namespace_isolation(self):
        import settle

        bet = {
            "portfolio": cross.NAMESPACE, "strategy": "footbreak-crown-execution-test-v1",
            "code": "HIL", "side": "H", "condition": 2.0, "odds": 1.9, "stake": 500,
        }
        result, pnl = settle.settle_bet(bet, {
            "goals_home": 1, "goals_away": 1, "goals_total": 2, "corners_total": None,
        })
        self.assertEqual((result, pnl), ("Refunded", 0.0))
        ledger = {
            "bets": [{"bet_id": "wilson", "portfolio": "footbreak_wilson_test"}],
            cross.NAMESPACE: {"bets": [{**bet, "status": "SETTLED", "result": result, "pnl": pnl}]},
        }
        cross.recompute(ledger)
        stats = ledger[cross.NAMESPACE]["stats"]
        self.assertEqual(stats["res_counts"]["Refunded"], 1)
        self.assertEqual(stats["n_decided"], 0)
        self.assertEqual(len(settle.settlement_bets(ledger)), 1)

    def test_cross_book_adapter_has_no_remote_client_or_radar_dependency(self):
        source = Path(cross.__file__).read_text(encoding="utf-8")
        for forbidden in ("PinnapiClient", "TitanClient", "urllib", "radar"):
            self.assertNotIn(forbidden, source.lower() if forbidden == "radar" else source)


class BilateralSidecarFailureIsolationTests(unittest.TestCase):
    """Gap #2 from bilateral_wilson_audit_report.md: both directions' sidecars
    raising RuntimeError at the same time must not block either native
    pipeline's durable stage/observation commit, and must not cross-pollute
    the two namespaces (``footbreak_crown_execution_test`` vs the native
    Footbreak/Crown ledgers themselves).
    """

    def test_both_sidecars_raising_simultaneously_does_not_block_either_native_pipeline(self):
        # --- Footbreak-native direction: record_picks.sync with its Crown
        # sidecar (capture_t5_counterparts / evaluate_crown_execution_t5)
        # both raising RuntimeError at once, mirroring
        # test_crown_sidecar_failure_cannot_block_native_footbreak_t5_commit
        # but asserting the *other* direction fails at the same wall-clock
        # moment too. ---
        kickoff = datetime.now(HKT) + timedelta(minutes=20)
        footbreak_result = {
            "match_id": "bilateral-native-survives", "stage": "T-5",
            "kickoff_hkt": kickoff.isoformat(), "league": "英超",
            "home": "主", "away": "客", "candidates": [],
        }
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "predictions.json").write_text(json.dumps([footbreak_result]), encoding="utf-8")
            ledger_path = root / "sim_ledger.json"
            with patch.object(record_picks, "HERE", str(root)), \
                 patch.object(record_picks, "LEDGER", str(ledger_path)), \
                 patch.object(record_picks, "_record_learning_snapshot", return_value=None), \
                 patch.object(record_picks, "evaluate_new_t5", return_value=([], [])), \
                 patch.object(record_picks, "evaluate_probability_research", return_value=([], [])), \
                 patch.object(record_picks, "capture_t5_counterparts",
                               side_effect=RuntimeError("both-sides-down")), \
                 patch.object(record_picks, "evaluate_crown_execution_t5",
                               side_effect=RuntimeError("both-sides-down")), \
                 patch.object(record_picks, "recompute", return_value=None), \
                 patch.object(record_picks, "recompute_crown_execution", return_value=None), \
                 patch(
                     "crown.state.multiprocessing.Process",
                     side_effect=RuntimeError("both-sides-down"),
                 ):
                # --- Crown-native direction: sync_prediction commits its own
                # durable stage first (exactly like production order: native
                # commit happens, then the optional Footbreak-evidence sidecar
                # is scheduled and may fail independently). Both RuntimeErrors
                # are therefore live at the same instant as the footbreak sync
                # above runs. ---
                crown_prediction = {
                    "match_id": "bilateral-crown-native-survives", "stage": "T-5",
                    "kickoff_hkt": kickoff.isoformat(), "league": "英超",
                    "home": "主", "away": "客",
                    "source_snapshot_at": stamp(-1),
                    "book_odds": {"crown": [
                        {"market": "HIL", "line": 2.5, "selection": "H",
                         "odds": 1.91, "source_at": stamp(-1)},
                    ]},
                }
                crown_ledger_doc = {"watch": {}, "bets": [], "log": [], "stats": {}}
                base = crown_settings()
                config = type(base)(**{**base.__dict__, "state_dir": root})
                sync_prediction(crown_ledger_doc, crown_prediction, config)
                # The optional sidecar launch failing must not raise out of
                # this call and must not touch the native watch/stage.
                scheduled_ok = crown_state.schedule_footbreak_execution_evidence_projection(
                    config, ["T-5"],
                )
                _, _, footbreak_ledger = record_picks.sync(send_notifications=False)

            # Footbreak native pipeline: the durable T-5 stage commits regardless.
            self.assertEqual(
                footbreak_ledger["watch"]["bilateral-native-survives"]["stages"][0]["stage"],
                "T-5",
            )
            self.assertTrue(ledger_path.exists())
            # Crown native pipeline: the durable T-5 stage commits regardless,
            # even though the reciprocal projection launch raised.
            self.assertFalse(scheduled_ok)
            crown_watch = crown_ledger_doc["watch"]["bilateral-crown-native-survives"]
            self.assertEqual(crown_watch["stages"][0]["stage"], "T-5")
            # Namespace isolation: neither native watch dict leaked into the
            # other book's ledger, and the cross-book simulation namespace was
            # not fabricated by either sidecar failure.
            self.assertNotIn("bilateral-crown-native-survives", footbreak_ledger["watch"])
            self.assertNotIn("bilateral-native-survives", crown_ledger_doc["watch"])
            self.assertNotIn(cross.NAMESPACE, crown_ledger_doc)
            crown_t5_bridge = (
                footbreak_ledger["watch"]["bilateral-native-survives"]
                .get("counterpart_bridges", {})
                .get("crown", {})
                .get("t5", {})
            )
            if crown_t5_bridge:
                self.assertIn("crown_sidecar_local_error", str(crown_t5_bridge.get("reason", "")))


class BilateralFirstLookVsT30BootstrapPriorityTests(unittest.TestCase):
    """Gap #3 from bilateral_wilson_audit_report.md: when an authoritative
    首預 (first-look) bridge already exists, a later T-30 call must always
    re-verify against it (never silently take the T30_BOOTSTRAP_ORIGIN
    narrow-path), and the bootstrap path must never overwrite or fabricate a
    first-look record. This directly locks in the priority rule implemented
    by ``bootstrap = not isinstance(root.get("first_look"), dict)`` in
    ``crown_execution_test.prefetch_bridge``.
    """

    def test_authoritative_first_look_takes_priority_over_t30_bootstrap_and_is_never_overwritten(self):
        kickoff, at = stamp(120), stamp()
        watch = {
            "match_id": "fx", "kickoff": kickoff, "league": "Spain - La Liga",
            "home": "貝迪斯", "away": "皇家蘇斯達",
            "stages": [{"stage": "T-30", "ts": at, "market_predictions": [
                {"code": "HIL", "side": "H", "line": 2.5},
            ]}],
        }
        with tempfile.TemporaryDirectory() as directory:
            evidence = Path(directory, "cards.json")
            evidence.write_text(
                json.dumps(authoritative_betis_card(kickoff=kickoff)), encoding="utf-8",
            )
            with patch.dict(os.environ, {"FOOTBREAK_CROWN_EXECUTION_EVIDENCE_PATH": str(evidence)}):
                # A genuine 首預 is recorded first (authoritative), *then* T-30
                # runs on the same watch that would otherwise have qualified
                # for the T30_BOOTSTRAP narrow path (no prior first_look).
                first = cross.prefetch_bridge(watch, stage="首預", now=at)
                t30 = cross.prefetch_bridge(watch, stage="T-30", now=at)
        self.assertEqual(first["status"], "RESOLVED")
        # Because a real first_look now exists, T-30 must take the normal
        # re-verification branch, never the bootstrap origin marker.
        self.assertNotEqual(t30.get("origin"), cross.T30_BOOTSTRAP_ORIGIN)
        self.assertNotIn("first_look_recorded", t30)
        bridge = watch["counterpart_bridges"]["crown"]
        # The first_look object itself must be exactly what 首預 produced;
        # T-30 (bootstrap or not) must never mutate it.
        self.assertEqual(bridge["first_look"], first)
        self.assertEqual(bridge["first_look"]["crown_match_id"], "crown-betis")

    def test_bootstrap_path_cannot_retroactively_fabricate_a_first_look_after_it_runs_first(self):
        kickoff, at = stamp(120), stamp()
        watch = {
            "match_id": "fx", "kickoff": kickoff, "league": "Spain - La Liga",
            "home": "貝迪斯", "away": "皇家蘇斯達",
            "stages": [{"stage": "T-30", "ts": at, "market_predictions": [
                {"code": "HIL", "side": "H", "line": 2.5},
            ]}],
        }
        with tempfile.TemporaryDirectory() as directory:
            evidence = Path(directory, "cards.json")
            evidence.write_text(
                json.dumps(authoritative_betis_card(kickoff=kickoff)), encoding="utf-8",
            )
            with patch.dict(os.environ, {"FOOTBREAK_CROWN_EXECUTION_EVIDENCE_PATH": str(evidence)}):
                # Bootstrap runs first (no first_look exists yet) -- this is
                # the documented narrow path.
                bootstrapped = cross.prefetch_bridge(watch, stage="T-30", now=at)
                self.assertEqual(bootstrapped["origin"], cross.T30_BOOTSTRAP_ORIGIN)
                self.assertNotIn("first_look", watch["counterpart_bridges"]["crown"])
                # A subsequent authoritative 首預 call is still free to record
                # the real first_look afterwards; the earlier bootstrap must
                # not have pre-populated or forged one.
                first = cross.prefetch_bridge(watch, stage="首預", now=at)
        self.assertEqual(first["status"], "RESOLVED")
        self.assertNotEqual(first.get("origin"), cross.T30_BOOTSTRAP_ORIGIN)
        bridge = watch["counterpart_bridges"]["crown"]
        self.assertEqual(bridge["first_look"], first)
        # The prior bootstrap t30 record remains as historical fact, distinct
        # from (and not silently merged into) the newly authoritative first_look.
        self.assertEqual(bridge["t30"]["origin"], cross.T30_BOOTSTRAP_ORIGIN)


class WilsonExecutionOddsSourceOrderInvarianceTests(unittest.TestCase):
    """Gap #4 from bilateral_wilson_audit_report.md: for a fixed hits/decided,
    the Wilson gate threshold must not depend on whether the caller first
    computed arithmetic against the HKJC quote and then replaced it with a
    higher qualifying Crown quote, versus feeding the same higher Crown quote
    directly. ``admission_arithmetic`` is a pure function of
    (hits, decided, odds); this test locks that invariant in explicitly.

    Per the audit report: if this were NOT the case, the correct action would
    be to leave a red/failing test and explain, never to change production
    code to make the test pass. Reading ``admission_arithmetic`` shows
    ``minimum_acceptable_odds_raw`` derives solely from ``wilson95(hits,
    decided)`` and ``EDGE_BUFFER`` -- it is odds-independent by construction,
    so this invariant is expected to hold (green), while ``passes`` legitimately
    tracks whichever odds value is actually used as the final chosen quote.
    """

    def test_hkjc_then_higher_crown_matches_direct_higher_crown_minimum_and_verdict(self):
        hits, decided = 41, 59
        hkjc_odds, higher_crown_odds = 1.66, 1.90

        # Sequence A: compute with the native HKJC quote first (as the T-5
        # evaluator does via _active_existing_admission), then the caller
        # substitutes the higher qualifying Crown quote as chosen_odds,
        # mirroring `chosen_odds = max(native_odds, counterpart_odds or 0.0)`.
        hkjc_first_arithmetic = admission_arithmetic(hits, decided, hkjc_odds)
        chosen_odds_after_hkjc_first = max(hkjc_odds, higher_crown_odds)
        sequence_a_final = admission_arithmetic(hits, decided, chosen_odds_after_hkjc_first)

        # Sequence B: the caller is handed the higher Crown quote directly,
        # with no prior HKJC computation at all.
        sequence_b_direct = admission_arithmetic(hits, decided, higher_crown_odds)

        self.assertIsNotNone(hkjc_first_arithmetic)
        self.assertIsNotNone(sequence_a_final)
        self.assertIsNotNone(sequence_b_direct)
        self.assertEqual(
            sequence_a_final["minimum_acceptable_odds_raw"],
            sequence_b_direct["minimum_acceptable_odds_raw"],
        )
        self.assertEqual(sequence_a_final["passes"], sequence_b_direct["passes"])
        self.assertEqual(
            sequence_a_final["wilson95_lower_raw"], sequence_b_direct["wilson95_lower_raw"],
        )
        # The threshold is also identical to the (unused) HKJC-only
        # computation, proving minimum_acceptable_odds_raw never moves with
        # the odds argument at all -- only hits/decided determine it.
        self.assertEqual(
            hkjc_first_arithmetic["minimum_acceptable_odds_raw"],
            sequence_b_direct["minimum_acceptable_odds_raw"],
        )


class RoleEqualsSideCharacterizationTest(unittest.TestCase):
    """Gap #1 from bilateral_wilson_audit_report.md Q3 / uncertainty #2:
    characterization test only -- locks in the CURRENT behavior that there is
    no independent "role" dimension in the codebase; what the audit calls
    "role" is exactly ``side`` (H/A or H/L) plus ``line``. This test adds NO
    production field. If the business later requires an independent role
    axis distinct from side, that is a separate design/feature-flagged change
    (see audit report shadow-mode plan phase 3) and must get its own test --
    this test must then be revisited, not silently reinterpreted.
    """

    def test_current_codebase_has_no_independent_role_field_role_is_side_plus_line(self):
        # `selected_role` in bet/observation rows is a free-text display label
        # (e.g. "主讓", "大") derived from side+line for notification text; it
        # is never used as an independent identity/matching key anywhere the
        # exact Crown fixture/market/side/line comparison happens.
        source = Path(cross.__file__).read_text(encoding="utf-8")
        self.assertNotIn('"role"', source)
        self.assertNotIn("selected_role ==", source)
        self.assertNotIn("row.get(\"role\")", source)
        # The exact T-5 identity gate compares side + line only (see
        # _crown_quote_for_exact_fixture / _exact_board_rows); it never reads
        # or requires a `role` key on either the HKJC or Crown side.
        self.assertIn("side", source)
        self.assertIn("line", source)
        kickoff = stamp(120)
        # A row that only carries side+line (no `role` key at all, and no
        # `selected_role` on the quote/journal row) is fully sufficient for
        # a successful exact quote match -- proving side+line alone is the
        # complete current identity contract.
        card = crown_card(side="H", line=2.5)
        self.assertNotIn("role", card[0]["current_selected_odds_journal"][0])
        with tempfile.TemporaryDirectory() as directory:
            evidence = Path(directory, "cards.json")
            evidence.write_text(json.dumps(card), encoding="utf-8")
            with patch.dict(os.environ, {"FOOTBREAK_CROWN_EXECUTION_EVIDENCE_PATH": str(evidence)}):
                quote, reason = cross._crown_quote_for_exact_fixture(
                    "fx", "HIL", "H", 2.5, cross._time(stamp()), cross._time(kickoff),
                )
        self.assertIsNone(reason)
        self.assertIsNotNone(quote)
