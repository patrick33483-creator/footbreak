"""Focused regressions for the post-commit Crown -> HKJC reverse T-5 bridge."""
from __future__ import annotations

import copy
import tempfile
import unittest
from dataclasses import replace
from datetime import datetime, timedelta
from pathlib import Path
from unittest.mock import patch

from analysis.wilson_validation import admission_arithmetic
from crown import hkjc_execution_test as reciprocal
from crown import reverse_t5_bridge as bridge
from crown import run as crown_run
from crown.common import HKT
from crown.config import settings
from crown.state import load_ledger, save_ledger


def stamp(minutes: int = 0) -> str:
    return (datetime.now(HKT) + timedelta(minutes=minutes)).isoformat()


def watch(*, complete: bool = True) -> dict:
    stages = [
        {"stage": "T-5", "ts": stamp(), "market_predictions": []},
    ]
    if complete:
        stages = [
            {"stage": "首預", "ts": stamp(-60), "market_predictions": []},
            {"stage": "T-30", "ts": stamp(-30), "market_predictions": []},
            *stages,
        ]
    return {
        "match_id": "crown-1", "kickoff": stamp(120), "league": "英格蘭超級聯賽",
        "home": "曼城", "away": "阿仙奴", "stages": stages,
    }


def signal(*, odds: float = 1.65) -> dict:
    return {
        "code": "HIL", "side": "H", "line": 2.5, "odds": odds,
        "quote_source": "titan007-crown-id-3", "observed_at": stamp(-1),
    }


def quote(*, odds: float = 1.90, line: float = 2.5, side: str = "H", observed: str | None = None) -> dict:
    return {
        "code": "HIL", "side": side, "line": line, "odds": odds,
        "source": "hkjc_public_board", "observed_at": observed or stamp(),
        "fixture_identity": {"hkjc_match_id": "hkjc-1"},
    }


def ledger_for(value: dict) -> dict:
    return {
        "watch": {"crown-1": value}, "bets": [{"bet_id": "native-unchanged"}],
        "log": [], "wilson_validation": {"conditions": {"sig": {
            "condition_number": 8,
            "active_evidence": {
                "version": 2, "evidence_hash": "frozen",
                "cumulative_hits": 41, "cumulative_decided": 59,
            },
        }}},
    }


class ReverseT5QuoteValidationTests(unittest.TestCase):
    def test_exact_line_orientation_and_freshness_cover_hdc_hil_chl(self) -> None:
        captured = reciprocal._time(stamp())
        kickoff = reciprocal._time(stamp(60))
        assert captured is not None and kickoff is not None
        rows = [
            {"code": "HDC", "side": "H", "line": -0.25, "odds": 1.91,
             "source": "hkjc_public_board", "observed_at": stamp(-1)},
            quote(observed=stamp(-1)),
            {"code": "CHL", "side": "L", "line": 10.5, "odds": 1.92,
             "source": "hkjc_public_board", "observed_at": stamp(-1)},
        ]
        for market, side, line in (("HDC", "H", -0.25), ("HIL", "H", 2.5), ("CHL", "L", 10.5)):
            result, reason = reciprocal._provided_hkjc_quote(
                rows, market, side, line, captured, kickoff,
            )
            self.assertIsNone(reason)
            self.assertIsNotNone(result)
        for side, line, expected in (
            ("A", -0.25, "hkjc_exact_market_side_line_missing_or_ambiguous"),
            ("H", -0.5, "hkjc_exact_market_side_line_missing_or_ambiguous"),
        ):
            _, reason = reciprocal._provided_hkjc_quote(rows, "HDC", side, line, captured, kickoff)
            self.assertEqual(reason, expected)
        stale = [dict(rows[1], observed_at=stamp(-3))]
        _, reason = reciprocal._provided_hkjc_quote(stale, "HIL", "H", 2.5, captured, kickoff)
        self.assertEqual(reason, "hkjc_execution_quote_stale_at_capture")


class ReverseT5DecisionTests(unittest.TestCase):
    def _evaluate(
        self, value: dict, provided: list[dict], *, existing: dict | None = None,
    ) -> tuple[dict, list[dict]]:
        ledger = existing if existing is not None else ledger_for(value)
        admission = {
            "signature": "sig", "history": {"hits": 41, "decided": 59},
            "definition": {"system": "crown", "market": "HIL", "stage": "T-5"},
            "arithmetic": admission_arithmetic(41, 59, signal()["odds"]),
        }
        native_rows = [
            {
                "match_id": "crown-1", "stage": row["stage"], "kickoff": value["kickoff"],
                "predicted_at": row["ts"], "market_predictions": [],
            }
            for row in value["stages"]
        ]
        with patch.object(reciprocal, "_native_t5", return_value=True), \
             patch.object(reciprocal, "_native_match_rows", return_value=native_rows), \
             patch.object(
                 reciprocal, "_native_crown_signal",
                 side_effect=lambda _current, market, _watch, _stage_at: (
                     (signal(), None) if market == "HIL" else (None, "crown_signal_missing")
                 ),
             ), \
             patch.object(reciprocal, "formal_registry_candidates", return_value=[{}]), \
             patch.object(reciprocal, "match_formal_registry", return_value={"crown-1": [{}]}), \
             patch.object(reciprocal, "matching_admissions", return_value=([admission], "wilson_pass")):
            created, _ = reciprocal.evaluate_new_t5(
                ledger, value, ranking=[],
                counterpart_quotes=provided, counterpart_captured_at=stamp(),
                require_complete_history=True, record_native_observation=False,
            )
        return ledger, created

    def test_incomplete_history_is_research_only_without_decision_or_stage_effect(self) -> None:
        value = watch(complete=False)
        before = copy.deepcopy(value)
        ledger, created = self._evaluate(value, [quote()])
        ns = ledger[reciprocal.NAMESPACE]
        self.assertEqual(created, [])
        self.assertEqual(ns["decisions"], [])
        self.assertEqual(ns["decision_outbox"], [])
        self.assertEqual(ns["bets"], [])
        self.assertEqual(ns["research_observations"][0]["reason"], "footbreak_three_stage_history_incomplete")
        self.assertEqual(value, before)
        self.assertEqual(ledger["bets"], [{"bet_id": "native-unchanged"}])
        self.assertNotIn("observations", ledger["wilson_validation"])

    def test_low_odds_is_not_missing_counterpart_and_uses_existing_outbox(self) -> None:
        ledger, created = self._evaluate(watch(), [quote(odds=1.50)])
        ns = ledger[reciprocal.NAMESPACE]
        self.assertEqual(created, [])
        self.assertEqual(len(ns["decisions"]), 1)
        self.assertEqual(len(ns["decision_outbox"]), 1)
        row = ns["decisions"][0]
        self.assertEqual(row["decision"], "NO_BET_LOW_ODDS")
        self.assertEqual(row["counterpart_quote"], 1.50)
        self.assertEqual(row["signal_quote"], 1.65)
        self.assertGreater(row["minimum_odds"], row["counterpart_quote"])
        from crown.notify import _bilateral_decision_message
        self.assertIn("馬會對照 @1.50", _bilateral_decision_message(row) or "")
        self.assertIn("不投注：賠率不足", _bilateral_decision_message(row) or "")
        self.assertEqual(ledger["bets"], [{"bet_id": "native-unchanged"}])
        self.assertNotIn("observations", ledger["wilson_validation"])

    def test_sufficient_quote_creates_only_isolated_simulation_and_is_restart_safe(self) -> None:
        value = watch()
        ledger, created = self._evaluate(value, [quote(odds=1.95)])
        self.assertEqual(len(created), 1)
        ns = ledger[reciprocal.NAMESPACE]
        self.assertEqual(len(ns["bets"]), 1)
        self.assertTrue(ns["bets"][0]["simulation_only"])
        self.assertFalse(ns["bets"][0]["real_betting_enabled"])
        self.assertEqual(ledger["bets"], [{"bet_id": "native-unchanged"}])
        self.assertNotIn("observations", ledger["wilson_validation"])
        # A persisted bridge replay retains its first immutable decision/bet.
        again, repeated = self._evaluate(value, [quote(odds=1.95)], existing=ledger)
        self.assertEqual(repeated, [])
        self.assertEqual(len(again[reciprocal.NAMESPACE]["bets"]), 1)
        self.assertEqual(len(again[reciprocal.NAMESPACE]["decisions"]), 1)
        self.assertEqual(len(again[reciprocal.NAMESPACE]["decision_outbox"]), 1)
        self.assertEqual(again["bets"], [{"bet_id": "native-unchanged"}])

    def test_missing_mismatched_and_stale_are_research_not_low_odds(self) -> None:
        cases = (
            ([], "hkjc_exact_market_side_line_missing_or_ambiguous"),
            ([quote(line=2.75)], "hkjc_exact_market_side_line_missing_or_ambiguous"),
            ([quote(observed=stamp(-3))], "hkjc_execution_quote_stale_at_capture"),
        )
        for provided, expected in cases:
            with self.subTest(expected=expected):
                ledger, created = self._evaluate(watch(), provided)
                ns = ledger[reciprocal.NAMESPACE]
                self.assertEqual(created, [])
                self.assertEqual(ns["decisions"], [])
                reasons = [row["reason"] for row in ns["research_observations"]]
                self.assertIn(expected, reasons)
                self.assertNotIn("NO_BET_LOW_ODDS", reasons)


class ReverseT5WorkerTests(unittest.TestCase):
    def _raw_match(self) -> dict:
        return {
            "id": "hkjc-1", "kickOffTime": watch()["kickoff"], "status": "PREEVENT",
            "homeTeam": {"id": "home-1", "name_ch": "曼城", "name_en": "Manchester City"},
            "awayTeam": {"id": "away-1", "name_ch": "阿仙奴", "name_en": "Arsenal"},
            "tournament": {"name_ch": "英格蘭超級聯賽", "name_en": "Premier League"},
            "foPools": [{"oddsType": "HIL", "status": "SELLING", "lines": [{
                "condition": "2.5", "combinations": [
                    {"selections": [{"str": "H"}], "currentOdds": "1.95"},
                    {"selections": [{"str": "L"}], "currentOdds": "1.90"},
                ],
            }]}],
        }

    def test_worker_uses_strict_board_identity_and_preserves_native_ledgers(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            config = replace(settings(), state_dir=Path(directory))
            value = watch()
            saved = ledger_for(value)
            save_ledger(config, saved)
            with patch.object(bridge, "fetch_matches", return_value=[self._raw_match()]), \
                 patch.object(reciprocal, "_native_t5", return_value=True), \
                 patch.object(
                     reciprocal, "_native_match_rows",
                     return_value=[
                         {
                             "match_id": "crown-1", "stage": row["stage"],
                             "kickoff": value["kickoff"], "predicted_at": row["ts"],
                             "market_predictions": [],
                         }
                         for row in value["stages"]
                     ],
                 ), \
                 patch.object(
                     reciprocal, "_native_crown_signal",
                     side_effect=lambda _current, market, _watch, _stage_at: (
                         (signal(), None) if market == "HIL" else (None, "crown_signal_missing")
                     ),
                 ), \
                 patch.object(reciprocal, "formal_registry_candidates", return_value=[{}]), \
                 patch.object(reciprocal, "match_formal_registry", return_value={"crown-1": [{}]}), \
                 patch.object(reciprocal, "matching_admissions", return_value=([{
                     "signature": "sig", "history": {"hits": 41, "decided": 59},
                     "definition": {}, "arithmetic": admission_arithmetic(41, 59, 1.65),
                 }], "wilson_pass")):
                result = bridge.collect_and_evaluate(config, ["crown-1"])
                replay = bridge.collect_and_evaluate(config, ["crown-1"])
            self.assertEqual(result["fixtures"], 1)
            self.assertEqual(replay["decisions"], 0)
            durable = load_ledger(config)
            self.assertEqual(durable["bets"], [{"bet_id": "native-unchanged"}])
            self.assertNotIn("observations", durable["wilson_validation"])
            self.assertEqual(len(durable[reciprocal.NAMESPACE]["bets"]), 1)
            self.assertEqual(len(durable[reciprocal.NAMESPACE]["decisions"]), 1)
            self.assertEqual(len(durable[reciprocal.NAMESPACE]["decision_outbox"]), 1)
            self.assertEqual(durable[reciprocal.NAMESPACE]["bets"][0]["hkjc_match_id"], "hkjc-1")
            self.assertNotIn("hkjc_match_id", durable["watch"]["crown-1"])

    def test_scheduler_accepts_only_new_t5_and_can_be_disabled(self) -> None:
        with tempfile.TemporaryDirectory() as directory, patch.dict(
            "os.environ", {"CROWN_REVERSE_T5_BRIDGE_ENABLED": "0"},
        ), patch("multiprocessing.get_context") as context:
            config = replace(settings(), state_dir=Path(directory))
            self.assertFalse(bridge.schedule_reverse_t5_bridge(config, [{"match_id": "crown-1", "stage": "T-30"}]))
            self.assertFalse(bridge.schedule_reverse_t5_bridge(config, [{"match_id": "crown-1", "stage": "T-5"}]))
            context.assert_not_called()

    @unittest.skipUnless(__import__("os").name == "posix", "fork sidecar is POSIX-only")
    def test_scheduler_launches_only_durable_t5_fixture_ids(self) -> None:
        with tempfile.TemporaryDirectory() as directory, patch.dict(
            "os.environ", {"CROWN_REVERSE_T5_BRIDGE_ENABLED": "1"},
        ), patch.object(bridge, "_record_health"), patch(
            "crown.reverse_t5_bridge.multiprocessing.get_context",
        ) as get_context:
            config = replace(settings(), state_dir=Path(directory))
            child = get_context.return_value.Process.return_value
            self.assertTrue(bridge.schedule_reverse_t5_bridge(config, [
                {"match_id": "not-t5", "stage": "T-30"},
                {"match_id": "crown-1", "stage": "T-5"},
                {"match_id": "crown-1", "stage": "T-5"},
            ]))
            get_context.assert_called_once_with("fork")
            _args, kwargs = get_context.return_value.Process.call_args
            self.assertIs(kwargs["target"], bridge._worker)
            self.assertEqual(kwargs["args"], (config, ("crown-1",)))
            self.assertFalse(child.daemon)
            child.start.assert_called_once_with()

    def test_run_schedules_reverse_only_after_native_engine_and_notification(self) -> None:
        source = Path(crown_run.__file__).read_text(encoding="utf-8")
        engine_call = source.index("_run_tick_engine(config, tick_deadline)")
        notification_call = source.index("_run_tick_notification(", source.index("# The durable outbox"))
        reverse_call = source.index("schedule_reverse_t5_bridge(", notification_call)
        self.assertLess(engine_call, notification_call)
        self.assertLess(notification_call, reverse_call)
