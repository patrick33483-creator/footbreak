"""Focused reciprocal 皇冠 × 馬會 execution simulation regressions."""
from __future__ import annotations

import json
import os
import tempfile
import unittest
from datetime import datetime, timedelta
from pathlib import Path
from unittest.mock import patch

from analysis.wilson_validation import admission_arithmetic
from crown import hkjc_execution_test as reciprocal
from crown import notify
from crown import settle
from crown.config import Settings


def now(minutes=0):
    return (datetime.now(reciprocal.timezone(timedelta(hours=8))) + timedelta(minutes=minutes)).isoformat()


def footbreak(*, line=2.5, side="H", observed=None):
    kickoff = now(120)
    return {"watch": {"hkjc-1": {"match_id": "hkjc-1", "kickoff": kickoff, "stages": [{
        "stage": "T-5", "ts": now(), "market_predictions": [{
            "code": "HIL", "side": side, "line": line, "odds": 1.9,
            "source": "hkjc_public_board", "observed_at": observed or now(-1),
        }],
    }]}}}


class ReciprocalEvidenceTests(unittest.TestCase):
    def _quote(self, source, *, side="H", line=2.5):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory, "footbreak.json"); path.write_text(json.dumps(source), encoding="utf-8")
            with patch.dict(os.environ, {"CROWN_HKJC_EXECUTION_EVIDENCE_PATH": str(path)}), \
                 patch.object(reciprocal, "_native_t5", return_value=True):
                return reciprocal._exact_hkjc_quote("hkjc-1", "HIL", side, line,
                                                    reciprocal._time(now()), reciprocal._time(now(120)))

    def test_exact_hkjc_execution_rejects_line_side_and_stale_evidence(self):
        quote, reason = self._quote(footbreak())
        self.assertEqual(reason, None); self.assertEqual(quote["odds"], 1.9)
        _, reason = self._quote(footbreak(), line=2.75)
        self.assertEqual(reason, "hkjc_exact_market_side_line_missing_or_ambiguous")
        _, reason = self._quote(footbreak(), side="L")
        self.assertEqual(reason, "hkjc_exact_market_side_line_missing_or_ambiguous")
        _, reason = self._quote(footbreak(observed=now(-10)))
        self.assertEqual(reason, "hkjc_execution_quote_stale_at_t5")
        missing_timestamp = footbreak()
        del missing_timestamp["watch"]["hkjc-1"]["stages"][0]["market_predictions"][0]["observed_at"]
        _, reason = self._quote(missing_timestamp)
        self.assertEqual(reason, "hkjc_execution_timestamp_missing")

    def test_execution_gate_is_hkjc_not_crown_signal_tier(self):
        self.assertFalse(admission_arithmetic(41, 59, 1.5)["passes"])
        self.assertTrue(admission_arithmetic(41, 59, 1.9)["passes"])

    def test_crown_signal_requires_native_fresh_quote(self):
        watch = {"kickoff": now(120)}
        stage = {"market_predictions": [{
            "code": "HIL", "side": "H", "line": 2.5, "odds": 1.66,
            "quote_source": "titan007-crown-id-3", "observed_at": now(-1),
        }]}
        with patch.object(reciprocal, "_selected", return_value=(stage["market_predictions"][0], None)):
            quote, reason = reciprocal._native_crown_signal(stage, "HIL", watch, reciprocal._time(now()))
        self.assertIsNone(reason)
        self.assertEqual(quote["odds"], 1.66)
        stage["market_predictions"][0]["quote_source"] = "titan007-crown-id-3-bulk-current"
        with patch.object(reciprocal, "_selected", return_value=(stage["market_predictions"][0], None)):
            _, reason = reciprocal._native_crown_signal(stage, "HIL", watch, reciprocal._time(now()))
        self.assertEqual(reason, "crown_signal_source_non_native_or_missing")

    def test_qualifying_reciprocal_entry_is_isolated_and_idempotent(self):
        kickoff, staged = now(120), now()
        watch = {
            "match_id": "crown-1", "hkjc_match_id": "hkjc-1", "kickoff": kickoff,
            "league": "英超", "home": "主", "away": "客",
            "stages": [{"stage": "T-5", "ts": staged, "market_predictions": []}],
        }
        signal = {
            "code": "HIL", "side": "H", "line": 2.5, "odds": 1.66,
            "quote_source": "titan007-crown-id-3", "observed_at": now(-1),
        }
        admission = {
            "signature": "sig", "history": {"hits": 41, "decided": 59},
            "arithmetic": admission_arithmetic(41, 59, 1.66),
        }
        ledger = {
            "bets": [{"bet_id": "crown-wilson-remains-untouched"}],
            "wilson_validation": {"conditions": {"sig": {
                "condition_number": 8,
                "active_evidence": {"version": 2, "evidence_hash": "frozen",
                                    "cumulative_hits": 41, "cumulative_decided": 59},
            }}},
        }
        source = footbreak()
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory, "footbreak.json"); path.write_text(json.dumps(source), encoding="utf-8")
            with patch.dict(os.environ, {"CROWN_HKJC_EXECUTION_EVIDENCE_PATH": str(path)}), \
                 patch.object(reciprocal, "_native_t5", return_value=True), \
                 patch.object(reciprocal, "_native_crown_signal", return_value=(signal, None)), \
                 patch.object(reciprocal, "match_upcoming", return_value={"crown-1": [{}]}), \
                 patch.object(reciprocal, "matching_admissions", return_value=([admission], "wilson_pass")):
                created, _ = reciprocal.evaluate_new_t5(ledger, watch, ranking=[{}])
                repeated, _ = reciprocal.evaluate_new_t5(ledger, watch, ranking=[{}])
        self.assertEqual(len(created), 1)
        self.assertEqual(repeated, [])
        self.assertEqual(len(ledger["bets"]), 1)
        bet = ledger[reciprocal.NAMESPACE]["bets"][0]
        self.assertEqual((bet["crown_signal_odds"], bet["hkjc_execution_odds"]), (1.66, 1.9))
        self.assertEqual(bet["condition_number"], 8)
        self.assertGreaterEqual(bet["hkjc_execution_odds"], bet["wilson_admission"]["minimum_acceptable_odds_raw"])
        self.assertEqual(bet["stake"], 500)
        self.assertLessEqual(sum(row["stake"] for row in ledger[reciprocal.NAMESPACE]["bets"]), 1500)
        self.assertEqual(bet["fixture_identity"], {"hkjc_match_id": "hkjc-1", "crown_match_id": "crown-1"})

    def test_post_kickoff_decision_never_backfills_a_reciprocal_entry(self):
        base = datetime.now(reciprocal.timezone(timedelta(hours=8)))
        watch = {
            "match_id": "crown-1", "hkjc_match_id": "hkjc-1",
            "kickoff": (base + timedelta(minutes=1)).isoformat(),
            "stages": [{"stage": "T-5", "ts": base.isoformat(), "market_predictions": []}],
        }
        with patch.object(reciprocal, "_native_t5", return_value=True), \
             patch.object(reciprocal, "iso_hkt", return_value=(base + timedelta(minutes=2)).isoformat()):
            created, audit = reciprocal.evaluate_new_t5({}, watch, ranking=[{}])
        self.assertEqual(created, [])
        self.assertEqual(audit[-1]["reason"], "not_first_native_pre_kickoff_t5_or_ranking_missing")

    def test_cross_book_urgent_adapter_has_no_provider_or_remote_import(self):
        source = Path(reciprocal.__file__).read_text(encoding="utf-8")
        self.assertNotIn("PinnapiClient", source)
        self.assertNotIn("TitanClient", source)
        self.assertNotIn("urllib", source)


class ReciprocalNotificationTests(unittest.TestCase):
    def test_message_has_hkjc_venue_and_dedupes(self):
        bet = {
            "bet_id": "r1", "portfolio": reciprocal.NAMESPACE, "strategy": reciprocal.STRATEGY,
            "status": "PENDING", "simulation_only": True, "real_betting_enabled": False,
            "league": "英超", "home": "主", "away": "客", "kickoff": now(120),
            "market_label": "入球大細", "selected_role": "大", "selected_line": 2.5, "stake": 500,
            "condition_number": 2, "crown_signal_odds": 1.66, "crown_signal_observed_at": now(-1),
            "crown_signal_source": "titan007-crown-id-3",
            "hkjc_execution_odds": 1.9, "hkjc_execution_observed_at": now(-1),
            "hkjc_execution_source": "hkjc_public_board",
            "decision_at": now(),
            "wilson_admission": admission_arithmetic(41, 59, 1.9),
        }
        text = notify._hkjc_execution_message(bet)
        for field in ("皇冠×馬會", "合符 皇冠 Wilson 條件 #2", "投注：入球大細 · 大 2.5",
                      "投注平台：馬會", "皇冠訊號賠率：1.66",
                      "馬會執行賠率：1.90", "最低賠率要求："):
            self.assertIn(field, text)
        self.assertNotIn("模擬投注 HK$", text)
        with tempfile.TemporaryDirectory() as directory:
            config = Settings(
                state_dir=Path(directory), app_dir=Path(directory), web_root=Path(directory),
                enabled=True, pinnapi_key=None, pinnapi_base_url="https://example.test",
                source_max_age_seconds=90, allow_inferred_pinnapi_timestamp=False,
                titan_bf_base="https://example.test", titan_vip_base="https://example.test",
                titan_company_id="3", telegram_enabled=True, telegram_bot_token="token",
                telegram_chat_id="chat", confidence_floor=58, min_edge=.02, bankroll=50000,
            )
            ledger = {reciprocal.NAMESPACE: {"bets": [bet]}}
            with patch("crown.notify._send", side_effect=[False, True]) as sender:
                self.assertEqual(notify.notify_hkjc_execution_pending(ledger, config), 0)
                self.assertEqual(notify.notify_hkjc_execution_pending(ledger, config), 1)
                self.assertEqual(notify.notify_hkjc_execution_pending(ledger, config), 0)
            self.assertEqual(sender.call_count, 2)

    def test_message_selects_higher_qualifying_crown_price(self):
        bet = {
            "bet_id": "r-higher-crown", "portfolio": reciprocal.NAMESPACE, "strategy": reciprocal.STRATEGY,
            "status": "PENDING", "simulation_only": True, "real_betting_enabled": False,
            "league": "英超", "home": "主", "away": "客", "kickoff": now(120),
            "market_label": "入球大細", "selected_role": "大", "selected_line": 2.5,
            "condition_number": 2, "crown_signal_odds": 1.95, "crown_signal_observed_at": now(-1),
            "crown_signal_source": "titan007-crown-id-3",
            "hkjc_execution_odds": 1.90, "hkjc_execution_observed_at": now(-1),
            "hkjc_execution_source": "hkjc_public_board", "decision_at": now(),
            "wilson_admission": admission_arithmetic(41, 59, 1.90),
        }
        text = notify._hkjc_execution_message(bet)
        self.assertIn("皇冠訊號賠率：1.95", text)
        self.assertIn("馬會執行賠率：1.90", text)
        self.assertIn("投注平台：皇冠", text)


class ReciprocalSettlementTests(unittest.TestCase):
    def test_push_settlement_updates_only_reciprocal_stats(self):
        bet = {
            "bet_id": "r-settle", "portfolio": reciprocal.NAMESPACE, "strategy": reciprocal.STRATEGY,
            "status": "PENDING", "code": "HIL", "side": "H", "condition": 2.0,
            "odds": 1.90, "stake": 500,
        }
        self.assertTrue(settle._settle(bet, {"home_score": 1, "away_score": 1}, "official_exact_id"))
        self.assertEqual((bet["status"], bet["result"], bet["pnl"]), ("SETTLED", "Refunded", 0.0))
        ledger = {
            "bets": [{"bet_id": "normal-wilson", "status": "PENDING"}],
            reciprocal.NAMESPACE: {"bets": [bet]},
        }
        stats = reciprocal.recompute(ledger)
        self.assertEqual(stats["res_counts"]["Refunded"], 1)
        self.assertEqual(stats["n_decided"], 0)
        self.assertEqual(ledger["bets"][0]["status"], "PENDING")


class ReciprocalDashboardTest(unittest.TestCase):
    def test_dashboard_has_read_only_reciprocal_section(self):
        app = Path(__file__).resolve().parents[1] / "dashboard" / "app.js"
        self.assertIn("皇冠×馬會執行測試倉（模擬）", app.read_text(encoding="utf-8"))

    def test_dashboard_projection_drops_raw_provider_evidence(self):
        from crown.dashboard_data import _public_ledger

        dashboard, _ = _public_ledger({
            "watch": {"fixture-1": {"raw_board": "x" * 1_000_000}},
            "predictions": [{"raw_provider_payload": "secret"}],
            "log": [{"index": index} for index in range(250)],
            "crown_hkjc_execution_test": {
                "bets": [{
                    "bet_id": "r1", "portfolio": reciprocal.NAMESPACE,
                    "strategy": reciprocal.STRATEGY, "crown_signal_source": "titan007-crown-id-3",
                    "hkjc_execution_source": "hkjc_public_board",
                }],
                "stats": {},
            },
        })
        self.assertNotIn("crown_hkjc_execution_test", dashboard)
        self.assertNotIn("watch", dashboard)
        self.assertNotIn("predictions", dashboard)
        self.assertEqual(len(dashboard["log"]), 200)
        self.assertEqual(dashboard["log"][0]["index"], 50)
        self.assertNotIn("crown_signal_source", dashboard["hkjc_execution_test"]["bets"][0])
