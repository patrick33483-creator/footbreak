from __future__ import annotations

import tempfile
import unittest
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch

from crown.condition_portfolio import FIXED_STAKE, STARTING_BANKROLL, STRATEGY, evaluate_new_t5
from crown.config import settings
from crown.dashboard_data import build
from crown.ledger import _market_predictions, condition_bets, recompute_stats, sync_prediction
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
            "code": code, "side": side, "line": line, "odds": 1.82,
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

    def test_exactly_sixty_percent_and_nine_samples_are_excluded(self) -> None:
        sixty = [
            historical_row(f"sixty-{i}", "HDC", hit=i < 6,
                           kickoff=datetime(2026, 1, 1, 20, tzinfo=HKT) + timedelta(days=i))
            for i in range(20)
        ]
        created, audit = evaluate_new_t5({"bets": []}, watch(), self.config, history_rows=sixty)
        self.assertEqual(created, [])
        self.assertEqual(audit[0]["reason"], "no_historical_condition_above_60pct_with_20_decided")

        nine = [historical_row(f"nine-{i}", "HDC", kickoff=datetime(2026, 2, 1, 20, tzinfo=HKT) + timedelta(days=i))
                for i in range(9)]
        created, _ = evaluate_new_t5({"bets": []}, watch(), self.config, history_rows=nine)
        self.assertEqual(created, [])

    def test_above_sixty_percent_and_twenty_samples_create_fixed_stake(self) -> None:
        rows = [
            historical_row(f"win-{i}", "HDC", hit=i < 13,
                           kickoff=datetime(2026, 3, 1, 20, tzinfo=HKT) + timedelta(days=i))
            for i in range(20)
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
        self.assertEqual((bet["condition_hits"], bet["condition_decided"]), (13, 20))
        self.assertGreater(bet["condition_accuracy"], .60)
        created_audit = next(item for item in audit if item["status"] == "CREATED")
        self.assertEqual(created_audit["selected_role"], "主讓")
        self.assertEqual(created_audit["selected_line"], -.25)
        self.assertEqual(created_audit["selected_odds"], 1.83)
        self.assertIn("讓球", created_audit["selected_label"])

    def test_all_three_markets_can_be_bought_once_for_one_fixture(self) -> None:
        rows = []
        for index in range(20):
            kickoff_at = datetime(2026, 4, 1, 20, tzinfo=HKT) + timedelta(days=index)
            rows.extend(historical_row(f"shared-{index}", code, kickoff=kickoff_at)
                        for code in ("HDC", "HIL", "CHL"))
        created, audit = evaluate_new_t5(
            {"bets": []}, watch(codes=("HDC", "HIL", "CHL")), self.config, history_rows=rows,
        )
        self.assertEqual(len(created), 2)
        self.assertLessEqual(sum(bet["stake"] for bet in created), 500)
        self.assertTrue(all(bet["stake"] == 250 for bet in created))
        self.assertEqual(sum(item["status"] == "CREATED" for item in audit), 2)

    def test_only_current_quote_direction_can_be_selected(self) -> None:
        candidate = {
            "market": "HDC", "label": "讓球｜T-5｜方向 主讓",
            "total": {"accuracy": .7, "hits": 14, "decided": 20, "wilson95": [.5, .8]},
            "specificity": 1, "badge": "樣本不足", "odds_tier": "≥1.70",
        }
        with patch("crown.condition_portfolio.mine", return_value={"ranking": [candidate]}), patch(
            "crown.condition_portfolio.match_upcoming",
            return_value={"future": [
                candidate | {"selected_side": "H", "selected_line": -0.25},
                candidate | {"selected_side": "A", "selected_line": 0.25},
            ]},
        ):
            created, audit = evaluate_new_t5({"bets": []}, watch(), self.config, history_rows=[])
        self.assertEqual(len(created), 1)
        self.assertEqual(created[0]["selected_side"], "H")

    def test_idempotency_invalid_quote_and_current_fixture_leakage_fail_closed(self) -> None:
        rows = [historical_row(f"ok-{i}", "HDC", kickoff=datetime(2026, 5, 1, 20, tzinfo=HKT) + timedelta(days=i))
                for i in range(20)]
        ledger = {"bets": []}
        created, _ = evaluate_new_t5(ledger, watch(), self.config, history_rows=rows)
        ledger["bets"].extend(created)
        repeated, audit = evaluate_new_t5(ledger, watch(), self.config, history_rows=rows)
        self.assertEqual(repeated, [])
        self.assertEqual(audit[0]["reason"], "idempotent_existing_market")

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
            {"portfolio": "crown_independent_validation", "strategy": STRATEGY, "status": "SETTLED",
             "market": "HIL", "stake": 250, "pnl": 125, "result": "Won"},
        ]}
        stats = recompute_stats(ledger, self.config)
        self.assertEqual(stats["turnover"], 250)
        self.assertEqual(stats["pnl"], 125)
        self.assertEqual(len(condition_bets(ledger)), 1)

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
                    {"portfolio": "crown_independent_validation", "strategy": STRATEGY, "status": "PENDING", "stake": 250},
                ],
                "shadow_bets": [{"status": "PENDING"}], "handicap_world": {"bets": []},
            }
            save_ledger(config, ledger)
            payload = build(config)
        self.assertEqual(len(payload["ledger"]["bets"]), 1)
        self.assertNotIn("shadow_bets", payload["ledger"])
        self.assertNotIn("handicap_world", payload["ledger"])
        self.assertTrue(payload["ledger"]["independent_validation"]["historical_discovery_archive"]["read_only"])

    def test_dashboard_labels_are_chinese_and_do_not_show_legacy_totals(self) -> None:
        root = Path(__file__).resolve().parents[2]
        app = (root / "crown" / "dashboard" / "app.js").read_text(encoding="utf-8")
        index = (root / "crown" / "dashboard" / "index.html").read_text(encoding="utf-8")
        self.assertIn('data-view="ledger">獨立驗證倉', index)
        for label in ("舊歷史發現期", "歷史發現／獨立驗證", "前瞻盈虧", "前瞻回報率"):
            self.assertIn(label, app)
        self.assertNotIn("Prospective PnL", app)
        self.assertIn("independent-validation-v1", app)


if __name__ == "__main__":
    unittest.main()
