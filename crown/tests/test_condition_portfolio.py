from __future__ import annotations

import tempfile
import time
import unittest
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch

from crown.condition_portfolio import FIXED_STAKE, STARTING_BANKROLL, STRATEGY, evaluate_new_t5
from crown.config import settings
from crown.dashboard_data import build
from crown.ledger import (
    _market_predictions, condition_bets, reconcile_pending_formal_admissions,
    recompute_stats, sync_prediction,
)
from crown.settle import _settle
from crown.state import save_ledger


HKT = timezone(timedelta(hours=8))


def historical_row(
    fixture: str,
    code: str,
    *,
    hit: bool = True,
    side: str = "H",
    line: float = -0.25,
    odds: float = 1.82,
    kickoff: datetime | None = None,
) -> dict:
    kickoff = kickoff or datetime(2026, 1, 1, 20, tzinfo=HKT)
    if code != "HDC":
        line = 2.5 if code == "HIL" else 9.5
    return {
        "match_id": fixture,
        "stage": "T-5",
        "kickoff": kickoff.isoformat(),
        "predicted_at": (kickoff - timedelta(minutes=5)).isoformat(),
        "market_grades": [{
            "code": code, "side": side, "line": line, "odds": odds,
            "grade_status": "GRADED", "hit": hit,
        }],
    }


def watch(*, fixture: str = "future", codes: tuple[str, ...] = ("HDC",), odds: float = 1.83) -> dict:
    kickoff = datetime(2099, 8, 1, 20, tzinfo=HKT)
    predictions = []
    for code in codes:
        predictions.append({
            "code": code, "side": "H", "line": -0.25 if code == "HDC" else 2.5 if code == "HIL" else 9.5,
            "odds": odds, "observed_at": (kickoff - timedelta(minutes=6)).isoformat(),
            "quote_source": "titan007-crown-id-3",
            "market": {"HDC": "皇冠讓球", "HIL": "皇冠入球大細", "CHL": "HKJC角球大細"}[code],
        })
    return {
        "match_id": fixture, "league": "測試聯賽", "home": "主隊", "away": "客隊",
        "kickoff": kickoff.isoformat(),
        "stages": [{
            "stage": "T-5", "kickoff_hkt": kickoff.isoformat(),
            "ts": (kickoff - timedelta(minutes=5)).isoformat(),
            "market_predictions": predictions,
        }],
    }


class ConditionPortfolioTests(unittest.TestCase):
    def setUp(self) -> None:
        self.config = settings()

    def test_minimum_fifty_and_wilson_gate_are_enforced(self) -> None:
        forty_nine = [
            historical_row(f"forty-nine-{i}", "HDC", hit=True,
                           kickoff=datetime(2026, 1, 1, 20, tzinfo=HKT) + timedelta(days=i))
            for i in range(49)
        ]
        created, audit = evaluate_new_t5({"bets": []}, watch(), self.config, history_rows=forty_nine)
        self.assertEqual(created, [])
        self.assertEqual(audit[0]["reason"], "no_frozen_historical_condition")

        weak = [historical_row(f"weak-{i}", "HDC", hit=i < 40,
                               kickoff=datetime(2026, 2, 1, 20, tzinfo=HKT) + timedelta(days=i))
                for i in range(59)]
        created, audit = evaluate_new_t5({"bets": []}, watch(), self.config, history_rows=weak)
        self.assertEqual(created, [])
        self.assertEqual(audit[0]["reason"], "wilson_gate_not_passed")

    def test_wilson_eligible_fifty_nine_samples_create_fixed_stake(self) -> None:
        rows = [
            historical_row(f"win-{i}", "HDC", hit=i < 50,
                           kickoff=datetime(2026, 3, 1, 20, tzinfo=HKT) + timedelta(days=i))
            for i in range(59)
        ]
        created, audit = evaluate_new_t5({"bets": []}, watch(), self.config, history_rows=rows)
        self.assertEqual(len(created), 1)
        bet = created[0]
        self.assertEqual(bet["stake"], FIXED_STAKE)
        self.assertEqual(bet["strategy"], STRATEGY)
        self.assertEqual(bet["code"], "HDC")
        self.assertEqual(bet["market_label"], "讓球")
        self.assertEqual((bet["selected_role"], bet["selected_line"]), ("主讓", -.25))
        self.assertEqual(bet["label"], "讓球 · 主讓 -0.25")
        self.assertNotRegex(bet["label"], r"\b(?:HDC|HIL|CHL|A|B|C)\b")
        # The discovery holdout is already a subset of these 59 rows. It is
        # retained for audit but must not be counted a second time.
        self.assertEqual((bet["frozen_historical_evidence"]["hits"], bet["frozen_historical_evidence"]["decided"]), (50, 59))
        self.assertEqual(bet["evidence_version"], 1)
        self.assertTrue(bet["wilson_admission"]["passes"])
        created_audit = next(item for item in audit if item["status"] == "CREATED")
        self.assertEqual(created_audit["wilson_admission"]["actual_decimal_odds_raw"], 1.83)
        self.assertTrue(created_audit["wilson_admission"]["passes"])

    def test_frozen_registry_admits_native_low_odds_without_live_research_card(self) -> None:
        """A frozen formal condition survives an empty/re-ranked research list."""
        rows = [
            historical_row(f"registry-{i}", "HDC", hit=i < 50, odds=1.50,
                           kickoff=datetime(2026, 3, 1, 20, tzinfo=HKT) + timedelta(days=i))
            for i in range(59)
        ]
        ledger = {"bets": []}
        # First call freezes the formal condition.  Its sub-minimum native
        # price is still a condition observation, never a paper bet.
        created, audit = evaluate_new_t5(
            ledger, watch(fixture="registry-first", odds=1.30), self.config,
            history_rows=rows,
        )
        self.assertEqual(created, [])
        self.assertTrue(any(item["status"] == "MATCHED_NO_BET" for item in audit))
        self.assertEqual(len(ledger["wilson_validation"]["observations"]), 1)
        self.assertEqual(ledger["bets"], [])
        # The mutable research ranking is now empty.  Existing frozen formal
        # registry identity still matches the next exact native T-5.
        created, audit = evaluate_new_t5(
            ledger, watch(fixture="registry-second", odds=1.30), self.config,
            ranking=[],
        )
        self.assertEqual(created, [])
        self.assertTrue(any(item["status"] == "MATCHED_NO_BET" for item in audit))
        self.assertEqual(len(ledger["wilson_validation"]["observations"]), 2)

    def test_all_three_markets_can_be_bought_once_for_one_fixture(self) -> None:
        rows = []
        for index in range(59):
            kickoff_at = datetime(2026, 4, 1, 20, tzinfo=HKT) + timedelta(days=index)
            rows.extend(historical_row(f"shared-{index}", code, kickoff=kickoff_at)
                        for code in ("HDC", "HIL", "CHL"))
        created, audit = evaluate_new_t5(
            {"bets": []}, watch(codes=("HDC", "HIL", "CHL")), self.config, history_rows=rows,
        )
        self.assertEqual(len(created), 3)
        self.assertLessEqual(sum(bet["stake"] for bet in created), 1500)
        self.assertTrue(all(bet["stake"] == 500 for bet in created))
        self.assertEqual(sum(item["status"] == "CREATED" for item in audit), 3)

    def test_only_current_quote_direction_can_be_selected(self) -> None:
        rows = [
            historical_row(f"direction-{i}", "HDC", hit=True,
                           kickoff=datetime(2026, 4, 1, 20, tzinfo=HKT) + timedelta(days=i))
            for i in range(59)
        ]
        created, audit = evaluate_new_t5({"bets": []}, watch(), self.config, history_rows=rows)
        self.assertEqual(len(created), 1)
        self.assertEqual(created[0]["selected_side"], "H")

    def test_idempotency_invalid_quote_and_current_fixture_leakage_fail_closed(self) -> None:
        rows = [historical_row(f"ok-{i}", "HDC", kickoff=datetime(2026, 5, 1, 20, tzinfo=HKT) + timedelta(days=i))
                for i in range(59)]
        ledger = {"bets": []}
        created, _ = evaluate_new_t5(ledger, watch(), self.config, history_rows=rows)
        ledger["bets"].extend(created)
        repeated, audit = evaluate_new_t5(ledger, watch(), self.config, history_rows=rows)
        self.assertEqual(repeated, [])
        self.assertIn("idempotent_existing_market", {row["reason"] for row in audit})

        bad_watch = watch(odds=1.0)
        created, audit = evaluate_new_t5({"bets": []}, bad_watch, self.config, history_rows=rows)
        self.assertEqual(created, [])
        self.assertEqual(audit[0]["reason"], "selected_odds_invalid_or_missing")

        no_source = watch()
        no_source["stages"][0]["market_predictions"][0].pop("quote_source")
        created, audit = evaluate_new_t5({ "bets": [] }, no_source, self.config, history_rows=rows)
        self.assertEqual(created, [])
        self.assertEqual(audit[0]["reason"], "selected_source_observation_invalid_or_missing")

        missing_odds = watch()
        missing_odds["stages"][0]["market_predictions"][0]["odds"] = None
        created, audit = evaluate_new_t5({ "bets": [] }, missing_odds, self.config, history_rows=rows)
        self.assertEqual(created, [])
        self.assertEqual(audit[0]["reason"], "selected_odds_invalid_or_missing")

        only_current = [
            historical_row("future", "HDC", kickoff=datetime(2026, 6, 1, 20, tzinfo=HKT) + timedelta(days=i))
            for i in range(20)
        ]
        created, _ = evaluate_new_t5({"bets": []}, watch(), self.config, history_rows=only_current)
        self.assertEqual(created, [])

    def test_missing_source_is_not_relabelled_as_provider_evidence(self) -> None:
        """The ledger must not manufacture a valid source for validation entry."""
        kickoff = "2099-08-01T20:00:00+08:00"
        selected, _ = _market_predictions([{
            "code": "HDC", "market": "皇冠讓球", "side": "H", "line": -.25,
            "odds": 1.82, "observed_at": "2099-08-01T19:54:00+08:00",
        }], kickoff)
        self.assertEqual(len(selected), 1)
        self.assertIsNone(selected[0]["quote_source"])
        self.assertIsNone(selected[0]["source"])

    def test_t30_never_creates_and_stats_ignore_legacy_rows(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            config = replace(self.config, state_dir=Path(directory), telegram_enabled=False)
            prediction = {
                "match_id": "t30-only", "league": "L", "home": "A", "away": "B",
                "kickoff_hkt": "2099-08-01T20:00:00+08:00", "stage": "T-30",
                "forecast_candidates": [{
                    "code": "HDC", "side": "H", "line": -0.25, "odds": 1.8,
                    "observed_at": "2099-08-01T19:20:00+08:00",
                }],
            }
            ledger = {"bets": [], "watch": {}, "log": [], "stats": {}}
            self.assertEqual(sync_prediction(ledger, prediction, config), [])
            self.assertEqual(ledger["bets"], [])

        ledger = {"bets": [
            {"portfolio": "legacy", "strategy": "old", "status": "SETTLED", "stake": 9000, "pnl": 9000},
            {"portfolio": "crown_wilson_test", "strategy": STRATEGY, "status": "SETTLED",
             "market": "HIL", "stake": 500, "pnl": 250, "result": "Won"},
        ]}
        stats = recompute_stats(ledger, self.config)
        self.assertEqual(stats["turnover"], 500)
        self.assertEqual(stats["pnl"], 250)
        self.assertEqual(len(condition_bets(ledger)), 1)

    def test_early_stage_is_marked_then_reconciled_without_bet_or_notification(self):
        with tempfile.TemporaryDirectory() as directory:
            config = replace(
                self.config, state_dir=Path(directory), telegram_enabled=False,
            )
            prediction = {
                "match_id": "early-durable", "league": "L",
                "home": "A", "away": "B",
                "kickoff_hkt": "2099-08-01T20:00:00+08:00",
                "stage": "首預", "status": "PREDICTION_READY",
                "forecast_candidates": [{
                    "code": "HIL", "side": "H", "line": 2.5, "odds": 1.5,
                    "observed_at": "2099-08-01T18:00:00+08:00",
                    "source": "titan007-crown-id-3",
                }],
            }
            ledger = {
                "bankroll": 50000, "bets": [], "watch": {},
                "log": [], "stats": {},
            }
            self.assertEqual(
                sync_prediction(
                    ledger, prediction, config, defer_formal_admission=True,
                ),
                [],
            )
            snapshot = ledger["watch"]["early-durable"]["stages"][0]
            self.assertTrue(snapshot["formal_admission_pending"])
            self.assertEqual(ledger["bets"], [])

            calls = []

            def observation(value, card, _config, stage, **_kwargs):
                calls.append(stage)
                value["wilson_validation"].setdefault("observations", []).append({
                    "observation_id": "crown-early-once", "match_id": card["match_id"],
                    "stage": stage, "formal_bet": False,
                })
                return [], []

            with patch("crown.ledger.evaluate_wilson_stage", side_effect=observation):
                self.assertEqual(
                    reconcile_pending_formal_admissions(ledger, config), [],
                )
                self.assertEqual(
                    reconcile_pending_formal_admissions(ledger, config), [],
                )
            self.assertEqual(calls, ["首預"])
            self.assertFalse(snapshot["formal_admission_pending"])
            self.assertEqual(ledger["bets"], [])
            self.assertEqual(
                ledger["native_t5_direct_notifications"]["outbox"], [],
            )

    def test_early_stage_reconciliation_crash_remains_retryable(self):
        with tempfile.TemporaryDirectory() as directory:
            config = replace(self.config, state_dir=Path(directory))
            ledger = {
                "bankroll": 50000, "bets": [], "watch": {},
                "log": [], "stats": {},
            }
            prediction = {
                "match_id": "early-retry", "league": "L", "home": "A", "away": "B",
                "kickoff_hkt": "2099-08-01T20:00:00+08:00",
                "stage": "T-30", "status": "PREDICTION_READY",
                "forecast_candidates": [{
                    "code": "HDC", "side": "H", "line": -0.25, "odds": 1.8,
                    "observed_at": "2099-08-01T19:20:00+08:00",
                    "source": "titan007-crown-id-3",
                }],
            }
            sync_prediction(
                ledger, prediction, config, defer_formal_admission=True,
            )
            snapshot = ledger["watch"]["early-retry"]["stages"][0]
            with patch(
                "crown.ledger.evaluate_wilson_stage",
                side_effect=RuntimeError("matcher crash"),
            ):
                reconcile_pending_formal_admissions(ledger, config)
            self.assertTrue(snapshot["formal_admission_pending"])
            with patch(
                "crown.ledger.evaluate_wilson_stage", return_value=([], []),
            ) as evaluate:
                reconcile_pending_formal_admissions(ledger, config)
                reconcile_pending_formal_admissions(ledger, config)
            self.assertEqual(evaluate.call_count, 1)
            self.assertFalse(snapshot["formal_admission_pending"])

    def test_early_worker_never_consumes_t5(self):
        with tempfile.TemporaryDirectory() as directory:
            config = replace(self.config, state_dir=Path(directory))
            ledger = {
                "bankroll": 50000, "bets": [], "watch": {},
                "log": [], "stats": {},
            }
            kickoff = "2099-08-01T20:00:00+08:00"
            for stage, observed in (
                ("首預", "2099-08-01T18:00:00+08:00"),
                ("T-5", "2099-08-01T19:54:00+08:00"),
            ):
                sync_prediction(
                    ledger,
                    {
                        "match_id": "stage-filter", "league": "L",
                        "home": "A", "away": "B", "kickoff_hkt": kickoff,
                        "stage": stage, "status": "PREDICTION_READY",
                        "forecast_candidates": [{
                            "code": "HIL", "side": "H", "line": 2.75,
                            "odds": 1.8, "observed_at": observed,
                            "source": "titan007-crown-id-3",
                        }],
                    },
                    config,
                    defer_formal_admission=True,
                )
            with patch(
                "crown.ledger.evaluate_wilson_stage", return_value=([], []),
            ) as early, patch("crown.ledger.evaluate_new_t5") as t5:
                reconcile_pending_formal_admissions(
                    ledger,
                    config,
                    allowed_stages={"首預", "T-30"},
                )
            stages = {
                row["stage"]: row
                for row in ledger["watch"]["stage-filter"]["stages"]
            }
            early.assert_called_once()
            t5.assert_not_called()
            self.assertFalse(stages["首預"]["formal_admission_pending"])
            self.assertTrue(stages["T-5"]["formal_admission_pending"])

    def test_completed_stage_replay_cannot_overwrite_bound_quote(self):
        with tempfile.TemporaryDirectory() as directory:
            config = replace(self.config, state_dir=Path(directory))
            ledger = {
                "bankroll": 50000, "bets": [], "watch": {},
                "log": [], "stats": {},
            }
            def prediction(odds, observed):
                return {
                    "match_id": "immutable-replay", "league": "L",
                    "home": "A", "away": "B",
                    "kickoff_hkt": "2099-08-01T20:00:00+08:00",
                    "stage": "首預", "status": "PREDICTION_READY",
                    "forecast_candidates": [{
                        "code": "HIL", "side": "H", "line": 2.5,
                        "odds": odds, "observed_at": observed,
                        "source": "titan007-crown-id-3",
                    }],
                }
            sync_prediction(
                ledger, prediction(1.5, "2099-08-01T18:00:00+08:00"),
                config, defer_formal_admission=True,
            )
            snapshot = ledger["watch"]["immutable-replay"]["stages"][0]
            original = (
                snapshot["ts"], snapshot["market_predictions"][0]["odds"],
                snapshot["formal_admission_snapshot_id"],
                snapshot["formal_admission_snapshot_hash"],
            )
            sync_prediction(
                ledger, prediction(2.5, "2099-08-01T18:30:00+08:00"),
                config, defer_formal_admission=True,
            )
            self.assertEqual(
                (
                    snapshot["ts"], snapshot["market_predictions"][0]["odds"],
                    snapshot["formal_admission_snapshot_id"],
                    snapshot["formal_admission_snapshot_hash"],
                ),
                original,
            )
            consumed = []
            with patch(
                "crown.ledger.evaluate_wilson_stage",
                side_effect=lambda _l, watch, _c, _s, **_k: (
                    consumed.append(
                        watch["stages"][0]["market_predictions"][0]["odds"],
                    ) or [], []
                ),
            ):
                reconcile_pending_formal_admissions(ledger, config)
            self.assertEqual(consumed, [1.5])

    def test_watch_context_tamper_rejects_bound_job_before_evaluation(self):
        with tempfile.TemporaryDirectory() as directory:
            config = replace(self.config, state_dir=Path(directory))
            ledger = {
                "bankroll": 50000, "bets": [], "watch": {},
                "log": [], "stats": {},
            }
            prediction = {
                "match_id": "context-bound", "league": "Original League",
                "home": "Original Home", "away": "Original Away",
                "hkjc_match_id": "HK-ORIGINAL",
                "titan_match_id": "TITAN-ORIGINAL",
                "pinnapi_event_id": "PIN-ORIGINAL",
                "kickoff_hkt": "2099-08-01T20:00:00+08:00",
                "stage": "首預", "status": "PREDICTION_READY",
                "forecast_candidates": [{
                    "code": "HIL", "side": "H", "line": 2.5, "odds": 1.5,
                    "observed_at": "2099-08-01T18:00:00+08:00",
                    "source": "titan007-crown-id-3",
                }],
            }
            sync_prediction(
                ledger, prediction, config, defer_formal_admission=True,
            )
            card = ledger["watch"]["context-bound"]
            card.update({
                "league": "Tampered League",
                "home": "Tampered Home",
                "away": "Tampered Away",
                "hkjc_match_id": "HK-TAMPERED",
                "native_fixture_id": "NATIVE-TAMPERED",
                "titan_match_id": "TITAN-TAMPERED",
                "pinnapi_event_id": "PIN-TAMPERED",
                "kickoff": "2099-08-01T21:00:00+08:00",
                "kickoff_hkt": "2099-08-01T21:00:00+08:00",
            })
            with patch("crown.ledger.evaluate_wilson_stage") as evaluate:
                emitted = reconcile_pending_formal_admissions(ledger, config)
            snapshot = card["stages"][0]
            evaluate.assert_not_called()
            self.assertEqual(emitted, [])
            self.assertFalse(snapshot["formal_admission_pending"])
            self.assertEqual(snapshot["formal_admission_status"], "REJECTED")
            self.assertEqual(
                snapshot["formal_admission_reason"],
                "immutable_formal_snapshot_binding_mismatch",
            )

    def test_snapshot_tamper_and_post_kickoff_t5_expire_without_evaluation(self):
        with tempfile.TemporaryDirectory() as directory:
            config = replace(self.config, state_dir=Path(directory))
            kickoff = datetime(2099, 8, 1, 20, tzinfo=HKT)
            base = {
                "match_id": "bound-t5", "league": "L", "home": "A", "away": "B",
                "kickoff_hkt": kickoff.isoformat(), "stage": "T-5",
                "status": "PREDICTION_READY",
                "forecast_candidates": [{
                    "code": "HIL", "side": "H", "line": 2.5, "odds": 1.8,
                    "observed_at": (kickoff - timedelta(minutes=6)).isoformat(),
                    "source": "titan007-crown-id-3",
                }],
            }
            for mode in ("tamper", "expired"):
                ledger = {
                    "bankroll": 50000, "bets": [], "watch": {},
                    "log": [], "stats": {},
                }
                sync_prediction(
                    ledger, base | {"match_id": f"bound-{mode}"}, config,
                    defer_formal_admission=True,
                )
                snapshot = ledger["watch"][f"bound-{mode}"]["stages"][0]
                if mode == "tamper":
                    snapshot["market_predictions"][0]["odds"] = 9.99
                    current = kickoff - timedelta(hours=1)
                else:
                    current = kickoff
                with patch("crown.ledger.evaluate_new_t5") as evaluate, \
                     patch("crown.ledger.record_new_native_t5") as outbox:
                    self.assertEqual(
                        reconcile_pending_formal_admissions(
                            ledger, config, now=current,
                        ),
                        [],
                    )
                evaluate.assert_not_called()
                outbox.assert_not_called()
                self.assertEqual(ledger["bets"], [])
                self.assertFalse(snapshot["formal_admission_pending"])
                self.assertEqual(
                    snapshot["formal_admission_status"],
                    "REJECTED" if mode == "tamper" else "EXPIRED",
                )

    def test_t5_crossing_kickoff_discards_all_evaluator_and_outbox_changes(self):
        with tempfile.TemporaryDirectory() as directory:
            config = replace(self.config, state_dir=Path(directory))
            kickoff = datetime.now(HKT) + timedelta(seconds=0.15)
            ledger = {
                "bankroll": 50000, "bets": [], "watch": {},
                "log": [], "stats": {},
            }
            prediction = {
                "match_id": "cross-kickoff", "league": "L",
                "home": "A", "away": "B",
                "kickoff_hkt": kickoff.isoformat(), "stage": "T-5",
                "status": "PREDICTION_READY",
                "forecast_candidates": [{
                    "code": "HIL", "side": "H", "line": 2.5, "odds": 1.8,
                    "observed_at": (kickoff - timedelta(minutes=6)).isoformat(),
                    "source": "titan007-crown-id-3",
                }],
            }
            sync_prediction(
                ledger, prediction, config, defer_formal_admission=True,
            )
            fake_bet = {
                "bet_id": "cross-kickoff-bet",
                "created_at": datetime.now(HKT).isoformat(),
                "market_label": "入球大細",
                "frozen_condition_definition": {"path": "T-5"},
            }
            def delayed(*_args, **_kwargs):
                # Also simulate an evaluator-side observation mutation that
                # must be rolled back with the returned bet.
                _args[0].setdefault("wilson_validation", {}).setdefault(
                    "observations", [],
                ).append({"observation_id": "must-be-discarded"})
                time.sleep(0.20)
                return [fake_bet], []

            with patch(
                "crown.ledger.evaluate_new_t5", side_effect=delayed,
            ) as evaluate, patch(
                "crown.ledger.record_new_native_t5",
            ) as outbox, patch(
                "crown.ledger.challenger_v2.evaluate_new_t5",
            ) as challenger:
                emitted = reconcile_pending_formal_admissions(ledger, config)
            snapshot = ledger["watch"]["cross-kickoff"]["stages"][0]
            self.assertEqual(evaluate.call_count, 1)
            self.assertEqual(emitted, [])
            self.assertEqual(ledger["bets"], [])
            self.assertEqual(
                (ledger.get("wilson_validation") or {}).get("observations") or [],
                [],
            )
            outbox.assert_not_called()
            challenger.assert_not_called()
            self.assertFalse(snapshot["formal_admission_pending"])
            self.assertEqual(snapshot["formal_admission_status"], "EXPIRED")

    def test_canonical_settlement_for_three_markets(self) -> None:
        cases = [
            ({"code": "HDC", "condition": -0.25, "side": "H"}, {"home_score": 1, "away_score": 0}),
            ({"code": "HIL", "condition": 2.5, "side": "H"}, {"home_score": 2, "away_score": 1}),
            ({"code": "CHL", "condition": 9.5, "side": "L"}, {"corners_total": 9}),
        ]
        for values, score in cases:
            with self.subTest(code=values["code"]):
                bet = values | {"status": "PENDING", "stake": 1000, "odds": 1.8}
                self.assertTrue(_settle(bet, score, "test"))
                self.assertEqual(bet["result"], "Won")
                self.assertEqual(bet["status"], "SETTLED")

    def test_dashboard_projects_only_active_portfolio(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            config = replace(self.config, state_dir=root / "state", web_root=root / "web")
            config.web_root.mkdir()
            ledger = {
                "bankroll": STARTING_BANKROLL, "watch": {}, "log": [], "stats": {},
                "bets": [
                    {"portfolio": "legacy", "strategy": "old", "status": "PENDING", "stake": 1},
                    {"portfolio": "crown_wilson_test", "strategy": STRATEGY, "status": "PENDING", "stake": 500},
                ],
                "shadow_bets": [{"status": "PENDING"}], "handicap_world": {"bets": []},
            }
            save_ledger(config, ledger)
            payload = build(config)
        self.assertEqual(len(payload["ledger"]["bets"]), 1)
        self.assertNotIn("shadow_bets", payload["ledger"])
        self.assertNotIn("handicap_world", payload["ledger"])
        self.assertTrue(payload["ledger"]["independent_validation"]["retired_v1"]["read_only"])

    def test_dashboard_labels_are_chinese_and_do_not_show_legacy_totals(self) -> None:
        root = Path(__file__).resolve().parents[2]
        app = (root / "crown" / "dashboard" / "app.js").read_text(encoding="utf-8")
        index = (root / "crown" / "dashboard" / "index.html").read_text(encoding="utf-8")
        self.assertIn('data-view="ledger">獨立驗證倉', index)
        for label in ("Wilson 測試攻略", "Wilson 95%", "最低可接受賠率", "前瞻"):
            self.assertIn(label, app)
        self.assertNotIn("Prospective PnL", app)
        self.assertIn("wilson-test-strategy-v1", app)


if __name__ == "__main__":
    unittest.main()
