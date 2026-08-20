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

HKT = timezone(timedelta(hours=8))


def stamp(minutes=0):
    return (datetime.now(HKT) + timedelta(minutes=minutes)).isoformat()


def crown_card(*, odds=1.9, line=2.5, side="H", observed=None, duplicate=False):
    kickoff = stamp(120)
    row = {"code": "HIL", "side": side, "line": line, "odds": odds,
           "source": "titan007-crown-id-3", "odds_status": "available",
           "observed_at": observed or stamp(-1)}
    card = {"hkjc_match_id": "fx", "kickoff_hkt": kickoff,
            "current_selected_odds_journal": [row]}
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
        watch = {"match_id": "fx", "kickoff": kickoff, "league": "英超", "home": "主", "away": "客", "stages": [current]}
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
        for expected in ("足破×皇冠", "投注平台：皇冠", "合符條件 #7", "馬會訊號", "皇冠模擬",
                         "最低要求賠率", "模擬投注 HK$500"):
            self.assertIn(expected, text)
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
