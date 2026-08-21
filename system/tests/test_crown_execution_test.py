"""Regression coverage for the isolated Footbreak × Crown simulation ledger."""
from __future__ import annotations

import copy
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
import notify
from analysis.wilson_validation import admission_arithmetic, freeze_condition
from crown import hkjc_execution_test as reciprocal
from crown.config import settings as crown_settings
from crown.state import paths as crown_paths, save_predictions
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
        self.assertEqual(late["reason"], "crown_bridge_post_kickoff_rejected")
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
