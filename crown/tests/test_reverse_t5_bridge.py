"""Regressions for the durable, lock-isolated reverse Crown T-5 bridge."""
from __future__ import annotations

import copy
import inspect
import os
import tempfile
import time
import unittest
from dataclasses import replace
from datetime import datetime, timedelta
from pathlib import Path
from unittest.mock import patch

from analysis.wilson_validation import admission_arithmetic
from crown import engine as crown_engine
from crown import hkjc_execution_test as reciprocal
from crown import reverse_t5_bridge as bridge
from crown import run as crown_run
from crown.common import HKT
from crown.config import settings
from crown.matching import Match
from crown.state import load_ledger, save_ledger, state_lock


def stamp(minutes: int = 0) -> str:
    return (datetime.now(HKT) + timedelta(minutes=minutes)).isoformat()


def watch(fixture: str = "crown-1", *, complete: bool = True) -> dict:
    stages = [{"stage": "T-5", "ts": stamp(), "market_predictions": []}]
    if complete:
        stages = [
            {"stage": "首預", "ts": stamp(-60), "market_predictions": []},
            {"stage": "T-30", "ts": stamp(-30), "market_predictions": []},
            *stages,
        ]
    return {
        "match_id": fixture, "kickoff": stamp(120), "league": "英格蘭超級聯賽",
        "home": "曼城", "away": "阿仙奴", "stages": stages,
    }


def signal() -> dict:
    return {
        "code": "HIL", "side": "H", "line": 2.5, "odds": 1.65,
        "quote_source": "titan007-crown-id-3", "observed_at": stamp(-1),
    }


def quote(*, odds: float = 1.90, line: float = 2.5, side: str = "H", fixture: str = "hkjc-1", observed: str | None = None) -> dict:
    return {
        "code": "HIL", "side": side, "line": line, "odds": odds,
        "source": "hkjc_public_board", "observed_at": observed or stamp(),
        "fixture_identity": {"hkjc_match_id": fixture},
    }


def ledger_for(*watches: dict) -> dict:
    return {
        "watch": {row["match_id"]: row for row in watches},
        "bets": [{"bet_id": "native-unchanged"}], "log": [],
        "wilson_validation": {"conditions": {"sig": {
            "condition_number": 8,
            "active_evidence": {
                "version": 2, "evidence_hash": "frozen",
                "cumulative_hits": 41, "cumulative_decided": 59,
            },
        }}},
    }


def raw_match(*, pool_status: str = "SELLING", line_status: str = "SELLING", odds: str = "1.95") -> dict:
    return {
        "id": "hkjc-1", "kickOffTime": stamp(120), "status": "PREEVENT",
        "homeTeam": {"id": "home-1", "name_ch": "曼城", "name_en": "Manchester City"},
        "awayTeam": {"id": "away-1", "name_ch": "阿仙奴", "name_en": "Arsenal"},
        "tournament": {"name_ch": "英格蘭超級聯賽", "name_en": "Premier League"},
        "foPools": [{"oddsType": "HIL", "status": pool_status, "lines": [{
            "status": line_status, "condition": "2.5", "combinations": [
                {"selections": [{"str": "H"}], "currentOdds": odds},
                {"selections": [{"str": "L"}], "currentOdds": "1.90"},
            ],
        }]}],
    }


class ReverseT5QuoteValidationTests(unittest.TestCase):
    def test_exact_line_orientation_freshness_and_fixture_cover_hdc_hil_chl(self) -> None:
        captured, kickoff = reciprocal._time(stamp()), reciprocal._time(stamp(60))
        assert captured is not None and kickoff is not None
        rows = [
            {"code": "HDC", "side": "H", "line": -0.25, "odds": 1.91,
             "source": "hkjc_public_board", "observed_at": stamp(-1),
             "fixture_identity": {"hkjc_match_id": "hkjc-1"}},
            quote(observed=stamp(-1)),
            {"code": "CHL", "side": "L", "line": 10.5, "odds": 1.92,
             "source": "hkjc_public_board", "observed_at": stamp(-1),
             "fixture_identity": {"hkjc_match_id": "hkjc-1"}},
        ]
        for market, side, line in (("HDC", "H", -0.25), ("HIL", "H", 2.5), ("CHL", "L", 10.5)):
            value, reason = reciprocal._provided_hkjc_quote(rows, "hkjc-1", market, side, line, captured, kickoff)
            self.assertIsNone(reason); self.assertIsNotNone(value)
        _, reason = reciprocal._provided_hkjc_quote([quote(fixture="other")], "hkjc-1", "HIL", "H", 2.5, captured, kickoff)
        self.assertEqual(reason, "hkjc_fixture_identity_mismatch")
        _, reason = reciprocal._provided_hkjc_quote([quote(observed=stamp(-3))], "hkjc-1", "HIL", "H", 2.5, captured, kickoff)
        self.assertEqual(reason, "hkjc_execution_quote_stale_at_capture")

    def test_closed_suspended_and_unknown_statuses_are_not_quotes(self) -> None:
        for kwargs in (
            {"pool_status": "CLOSED"}, {"line_status": "SUSPENDED"}, {"pool_status": "UNKNOWN"},
            {"pool_status": "OPEN"}, {"line_status": "OPEN"},
        ):
            with self.subTest(kwargs=kwargs):
                quotes, reason = bridge._board_quotes(raw_match(**kwargs), observed_at=stamp())
                self.assertEqual(quotes, [])
                self.assertEqual(reason, "hkjc_pool_or_line_not_sellable")


class ReverseT5DecisionTests(unittest.TestCase):
    def _evaluate(self, value: dict, provided: list[dict], *, existing: dict | None = None, unavailable: str | None = None):
        value = copy.deepcopy(value)
        value["hkjc_match_id"] = "hkjc-1"
        ledger = existing if existing is not None else ledger_for(value)
        admission = {
            "signature": "sig", "history": {"hits": 41, "decided": 59},
            "definition": {"system": "crown", "market": "HIL", "stage": "T-5"},
            "arithmetic": admission_arithmetic(41, 59, 1.65),
        }
        native_rows = [{"match_id": value["match_id"], "stage": row["stage"], "kickoff": value["kickoff"], "predicted_at": row["ts"], "market_predictions": []} for row in value["stages"]]
        with patch.object(reciprocal, "_native_t5", return_value=True), \
             patch.object(reciprocal, "_native_match_rows", return_value=native_rows), \
             patch.object(reciprocal, "_native_crown_signal", side_effect=lambda _current, market, _watch, _stage_at: ((signal(), None) if market == "HIL" else (None, "crown_signal_missing"))), \
             patch.object(reciprocal, "formal_registry_candidates", return_value=[{}]), \
             patch.object(reciprocal, "match_formal_registry", return_value={value["match_id"]: [{}]}), \
             patch.object(reciprocal, "matching_admissions", return_value=([admission], "wilson_pass")):
            created, _ = reciprocal.evaluate_new_t5(
                ledger, value, ranking=[], counterpart_quotes=provided,
                counterpart_captured_at=stamp(), counterpart_unavailable_reason=unavailable,
                require_complete_history=True, record_native_observation=False,
            )
        return ledger, created

    def test_complete_low_and_sufficient_paths_remain_isolated_and_idempotent(self) -> None:
        low, created = self._evaluate(watch(), [quote(odds=1.50)])
        ns = low[reciprocal.NAMESPACE]
        self.assertEqual(created, [])
        self.assertEqual(ns["decisions"][0]["decision"], "NO_BET_LOW_ODDS")
        self.assertEqual(len(ns["decision_outbox"]), 1)
        good, created = self._evaluate(watch(), [quote(odds=1.95)])
        self.assertEqual(len(created), 1)
        self.assertTrue(good[reciprocal.NAMESPACE]["bets"][0]["simulation_only"])
        self.assertEqual(good["bets"], [{"bet_id": "native-unchanged"}])
        self.assertNotIn("observations", good["wilson_validation"])
        again, repeated = self._evaluate(watch(), [quote(odds=1.95)], existing=good)
        self.assertEqual(repeated, [])
        self.assertEqual(len(again[reciprocal.NAMESPACE]["bets"]), 1)

    def test_incomplete_wrong_fixture_and_unavailable_status_are_research_only(self) -> None:
        cases = (
            (watch(complete=False), [quote()], None, "footbreak_three_stage_history_incomplete"),
            (watch(), [quote(fixture="other")], None, "hkjc_fixture_identity_mismatch"),
            (watch(), [], "hkjc_pool_or_line_not_sellable", "hkjc_pool_or_line_not_sellable"),
        )
        for value, provided, unavailable, expected in cases:
            with self.subTest(expected=expected):
                ledger, created = self._evaluate(value, provided, unavailable=unavailable)
                ns = ledger[reciprocal.NAMESPACE]
                self.assertEqual(created, [])
                self.assertEqual(ns["decisions"], [])
                self.assertEqual(ns["decision_outbox"], [])
                self.assertEqual(ns["bets"], [])
                self.assertIn(expected, [row["reason"] for row in ns["research_observations"]])

    def test_research_dedupe_is_stable_across_capture_retries(self) -> None:
        ns = reciprocal.ensure_namespace({})
        first = reciprocal.record_research_observation(ns, fixture="crown-1", market="HIL", side="H", line=2.5, stage_at="t5", reason="stale", captured_at="first")
        second = reciprocal.record_research_observation(ns, fixture="crown-1", market="HIL", side="H", line=2.5, stage_at="t5", reason="stale", captured_at="later")
        self.assertIs(first, second)
        self.assertEqual(len(ns["research_observations"]), 1)
        self.assertEqual(ns["research_observations"][0]["captured_at"], "first")


class ReverseT5DurabilityTests(unittest.TestCase):
    def _patch_evaluator(self):
        admission = {"signature": "sig", "history": {"hits": 41, "decided": 59}, "definition": {}, "arithmetic": admission_arithmetic(41, 59, 1.65)}
        return patch.multiple(
            reciprocal,
            _native_t5=lambda *_args: True,
            _native_match_rows=lambda value, _time: [{"match_id": value["match_id"], "stage": row["stage"], "kickoff": value["kickoff"], "predicted_at": row["ts"], "market_predictions": []} for row in value["stages"]],
            _native_crown_signal=lambda _current, market, _watch, _stage_at: ((signal(), None) if market == "HIL" else (None, "crown_signal_missing")),
            formal_registry_candidates=lambda *_args, **_kwargs: [{}],
            match_formal_registry=lambda rows, *_args, **_kwargs: {rows[0]["match_id"]: [{}]},
            matching_admissions=lambda *_args, **_kwargs: ([admission], "wilson_pass"),
        )

    def test_crash_after_native_commit_recovers_exactly_one_durable_job(self) -> None:
        with tempfile.TemporaryDirectory() as directory, patch.dict(os.environ, {bridge.ENV_ENABLED: "1"}, clear=False):
            config = replace(settings(), state_dir=Path(directory))
            value = watch()
            ledger = ledger_for(value)
            # This is the native commit's only bridge action; no worker launch
            # is required, so a crash immediately afterward cannot lose it.
            self.assertTrue(bridge.enqueue_committed_t5(ledger, value))
            save_ledger(config, ledger)
            with patch.object(bridge, "fetch_matches", return_value=[raw_match()]), self._patch_evaluator():
                first = bridge.drain_pending_jobs(config)
                second = bridge.drain_pending_jobs(config)
            durable = load_ledger(config)
            self.assertEqual(first, {"claimed": 1, "completed": 1})
            self.assertEqual(second, {"claimed": 0, "completed": 0})
            self.assertEqual(durable[bridge.JOB_NAMESPACE]["jobs"][0]["state"], "COMPLETED")
            self.assertEqual(len(durable[reciprocal.NAMESPACE]["bets"]), 1)
            self.assertEqual(len(durable[reciprocal.NAMESPACE]["decisions"]), 1)
            self.assertEqual(durable["bets"], [{"bet_id": "native-unchanged"}])

    def test_closed_board_job_completes_research_only(self) -> None:
        with tempfile.TemporaryDirectory() as directory, patch.dict(os.environ, {bridge.ENV_ENABLED: "1"}, clear=False):
            config = replace(settings(), state_dir=Path(directory))
            value = watch(); ledger = ledger_for(value)
            bridge.enqueue_committed_t5(ledger, value); save_ledger(config, ledger)
            with patch.object(bridge, "fetch_matches", return_value=[raw_match(pool_status="CLOSED")]), self._patch_evaluator():
                bridge.drain_pending_jobs(config)
            durable = load_ledger(config)
            ns = durable[reciprocal.NAMESPACE]
            self.assertEqual(ns["decisions"], [])
            self.assertEqual(ns["decision_outbox"], [])
            self.assertEqual(ns["bets"], [])
            self.assertIn("hkjc_pool_or_line_not_sellable", [row["reason"] for row in ns["research_observations"]])

    def test_open_board_job_completes_research_only_without_decision_or_bet(self) -> None:
        with tempfile.TemporaryDirectory() as directory, patch.dict(os.environ, {bridge.ENV_ENABLED: "1"}, clear=False):
            config = replace(settings(), state_dir=Path(directory))
            value = watch()
            ledger = ledger_for(value)
            bridge.enqueue_committed_t5(ledger, value); save_ledger(config, ledger)
            with patch.object(bridge, "fetch_matches", return_value=[raw_match(pool_status="OPEN")]), self._patch_evaluator():
                bridge.drain_pending_jobs(config)
            ns = load_ledger(config)[reciprocal.NAMESPACE]
            self.assertEqual(ns["decisions"], [])
            self.assertEqual(ns["decision_outbox"], [])
            self.assertEqual(ns["bets"], [])
            self.assertIn("hkjc_pool_or_line_not_sellable", [row["reason"] for row in ns["research_observations"]])

    def test_isolated_merge_preserves_full_bilateral_collections_and_pending_outbox(self) -> None:
        current = reciprocal.ensure_namespace({})
        decision_ids = [f"decision-{number}" for number in range(4000)]
        attempt_ids = [f"attempt-{number}" for number in range(4000)]
        current["decisions"] = [{"decision_id": value} for value in decision_ids]
        current["decision_outbox"] = [
            {"outbox_id": f"outbox-{value}", "decision_id": value, "delivery": "PENDING"}
            for value in decision_ids
        ]
        current["counterpart_attempts"] = [{"fingerprint": value} for value in attempt_ids]
        current["bets"] = [{"bet_id": "existing-isolated-bet", "simulation_only": True}]
        outcome = {
            "research_observations": [{"fingerprint": "research-new"}],
            "counterpart_attempts": [{"fingerprint": "attempt-new"}],
            "decisions": [{"decision_id": "decision-new"}],
            "decision_outbox": [{"outbox_id": "outbox-decision-new", "decision_id": "decision-new", "delivery": "PENDING"}],
            "bets": [{"bet_id": "new-isolated-bet", "simulation_only": True}],
        }
        bridge._merge_isolated_namespace({reciprocal.NAMESPACE: current}, outcome)

        self.assertEqual(len(current["counterpart_attempts"]), 4001)
        self.assertEqual(len(current["decisions"]), 4001)
        self.assertEqual(len(current["decision_outbox"]), 4001)
        self.assertEqual(len(current["bets"]), 2)
        self.assertEqual({row["decision_id"] for row in current["decisions"]}, {*decision_ids, "decision-new"})
        self.assertEqual(
            {row["outbox_id"] for row in current["decision_outbox"]},
            {*(f"outbox-{value}" for value in decision_ids), "outbox-decision-new"},
        )
        self.assertTrue(all(row["delivery"] == "PENDING" for row in current["decision_outbox"]))

    @unittest.skipUnless(os.name == "posix", "requires fork")
    def test_slow_multi_fixture_matching_never_blocks_native_state_commit(self) -> None:
        with tempfile.TemporaryDirectory() as directory, patch.dict(os.environ, {bridge.ENV_ENABLED: "1"}, clear=False):
            config = replace(settings(), state_dir=Path(directory))
            one, two = watch("crown-1"), watch("crown-2")
            ledger = ledger_for(one, two)
            bridge.enqueue_committed_t5(ledger, one); bridge.enqueue_committed_t5(ledger, two)
            save_ledger(config, ledger)

            def slow_cjk_match(target, events):
                time.sleep(0.45)  # models a cold CJK/uconv matcher for each job
                return Match(events[0], False, 1.0, None)

            context = __import__("multiprocessing").get_context("fork")
            with patch.object(bridge, "fetch_matches", return_value=[raw_match()]), \
                 patch.object(bridge, "same_event_for_hkjc", side_effect=slow_cjk_match), self._patch_evaluator():
                child = context.Process(target=bridge.drain_pending_jobs, args=(config,))
                child.start()
                time.sleep(0.12)
                started = time.monotonic()
                urgent = {
                    "match_id": "native-urgent", "kickoff_hkt": stamp(90),
                    "stage": "T-5", "status": "DATA_MISSING",
                }

                def native_sync(live, prediction, *_args, **_kwargs):
                    live.setdefault("watch", {})[prediction["match_id"]] = {
                        "match_id": prediction["match_id"],
                        "kickoff": prediction["kickoff_hkt"],
                        "stages": [{"stage": prediction["stage"], "ts": stamp()}],
                    }
                    return []

                # This is the actual native commit helper, not a synthetic
                # state-lock probe.  The bridge child is in its deliberately
                # slow strict matcher, but its lock was released after it took
                # its immutable snapshots, so this must finish promptly.
                with patch.object(crown_engine, "sync_prediction", side_effect=native_sync), \
                     patch.object(crown_engine, "recompute_stats"), \
                     patch.object(crown_engine, "merge_predictions", return_value=[]):
                    crown_engine._commit_stage_predictions(config, "tick", [urgent])
                self.assertLess(time.monotonic() - started, 0.25)
                child.join(timeout=3.0)
                self.assertFalse(child.is_alive())
            self.assertIn("native-urgent", load_ledger(config)["watch"])

    @unittest.skipUnless(os.name == "posix", "requires fork")
    def test_bounded_batch_persists_each_completed_job_before_later_timeout(self) -> None:
        with tempfile.TemporaryDirectory() as directory, patch.dict(os.environ, {bridge.ENV_ENABLED: "1"}, clear=False):
            config = replace(settings(), state_dir=Path(directory))
            one, two = watch("crown-1"), watch("crown-2")
            ledger = ledger_for(one, two)
            bridge.enqueue_committed_t5(ledger, one)
            bridge.enqueue_committed_t5(ledger, two)
            save_ledger(config, ledger)

            def first_then_stall(snapshot, *_args):
                fixture = snapshot["job"]["match_id"]
                if fixture == "crown-2":
                    time.sleep(2.0)
                return {
                    "research_observations": [{
                        "fingerprint": f"observation-{fixture}",
                        "fixture": fixture,
                    }],
                }

            with patch.object(bridge, "_fetch_board", return_value=([], [], stamp())), \
                 patch.object(bridge, "_evaluate_snapshot", side_effect=first_then_stall):
                status, detail = crown_run._run_reverse_t5_drain(config, 0.35)

            self.assertEqual((status, detail), ("deferred", None))
            durable = load_ledger(config)
            jobs = {
                row["match_id"]: row["state"]
                for row in durable[bridge.JOB_NAMESPACE]["jobs"]
            }
            self.assertEqual(jobs, {"crown-1": "COMPLETED", "crown-2": "RUNNING"})
            observations = durable[reciprocal.NAMESPACE]["research_observations"]
            self.assertEqual(
                [row["fingerprint"] for row in observations],
                ["observation-crown-1"],
            )

            def complete_retry(snapshot, *_args):
                fixture = snapshot["job"]["match_id"]
                return {
                    "research_observations": [{
                        "fingerprint": f"observation-{fixture}",
                        "fixture": fixture,
                    }],
                }

            with patch.object(bridge, "_fetch_board", return_value=([], [], stamp())), \
                 patch.object(bridge, "_evaluate_snapshot", side_effect=complete_retry):
                retried = bridge.drain_pending_jobs(config)

            self.assertEqual(retried, {"claimed": 1, "completed": 1})
            durable = load_ledger(config)
            jobs = {
                row["match_id"]: row["state"]
                for row in durable[bridge.JOB_NAMESPACE]["jobs"]
            }
            self.assertEqual(jobs, {"crown-1": "COMPLETED", "crown-2": "COMPLETED"})
            self.assertEqual(
                [row["fingerprint"] for row in durable[reciprocal.NAMESPACE]["research_observations"]],
                ["observation-crown-1", "observation-crown-2"],
            )


class ReverseT5LifecycleTests(unittest.TestCase):
    @unittest.skipUnless(os.name == "posix", "requires fork")
    def test_outer_worker_timeout_terminates_and_reaps_without_shutdown_wait(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            config = replace(settings(), state_dir=Path(directory))
            marker = Path(directory) / "late-worker"

            def hung(send, _config):
                time.sleep(1.0)
                marker.write_text("should not happen", encoding="utf-8")
                send.close()

            started = time.monotonic()
            with patch.object(crown_run, "_reverse_t5_drain_process", hung):
                status, detail = crown_run._run_reverse_t5_drain(config, 0.10)
            self.assertEqual((status, detail), ("deferred", None))
            self.assertLess(time.monotonic() - started, 0.75)
            time.sleep(0.15)
            self.assertFalse(marker.exists())

    def test_tick_engine_has_no_reverse_provider_or_worker_callsite(self) -> None:
        # The deadline-owned engine remains provider-isolated.  Main may use
        # only a bounded post-commit child after native persistence and the
        # priority notification attempt; sweep remains the recovery owner.
        self.assertNotIn("_run_reverse_t5_drain", inspect.getsource(crown_run._run_tick_engine))
        main = inspect.getsource(crown_run.main)
        self.assertEqual(main.count("_run_reverse_t5_drain("), 2)
        self.assertIn('if args.mode == "sweep":', main)
        self.assertLess(
            main.index("_run_tick_notification("),
            main.rindex("_run_reverse_t5_drain("),
        )

    def test_tick_reverse_drain_is_post_commit_bounded_and_failure_isolated(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            config = replace(
                settings(), state_dir=Path(directory), web_root=Path(directory) / "web",
            )
            order: list[str] = []

            def engine(*_args):
                order.append("native_commit")
                return {"ok": True, "mode": "tick", "fresh_condition_predictions": []}

            def notify(*_args):
                order.append("native_notification")
                return "complete", None

            def drain(_config, budget):
                order.append("reverse_drain")
                self.assertGreater(budget, 0)
                self.assertLessEqual(budget, crown_run._TICK_REVERSE_T5_DRAIN_MAX_SECONDS)
                return "error", "SyntheticFailure"

            with patch.dict(os.environ, {bridge.ENV_ENABLED: "1"}), \
                 patch.object(crown_run, "settings", return_value=config), \
                 patch.object(crown_run, "_tick_pass_deadline_seconds", return_value=30), \
                 patch.object(crown_run, "_run_tick_engine", side_effect=engine), \
                 patch.object(crown_run, "_run_tick_notification", side_effect=notify), \
                 patch.object(crown_run, "_run_reverse_t5_drain", side_effect=drain), \
                 patch.object(crown_run, "_run_tick_postprocess", return_value=("complete", None)), \
                 patch.object(crown_run, "schedule_footbreak_execution_evidence_projection"), \
                 patch.object(crown_run, "_write_tick_health"), \
                 patch("sys.argv", ["crown.run", "tick"]):
                self.assertEqual(crown_run.main(), 0)

            self.assertEqual(
                order,
                ["native_commit", "native_notification", "reverse_drain"],
            )


if __name__ == "__main__":
    unittest.main()
