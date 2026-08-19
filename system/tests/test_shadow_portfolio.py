"""Regression tests for the isolated Footbreak condition simulation portfolio."""
from __future__ import annotations

import json
import sys
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch

SYSTEM = Path(__file__).resolve().parents[1]
ROOT = SYSTEM.parent
if str(SYSTEM) not in sys.path:
    sys.path.insert(0, str(SYSTEM))

import condition_portfolio as cp
import record_picks
import settle

HKT = timezone(timedelta(hours=8))


def historical_row(fixture, code, *, hit=True, side="H", line=None, kickoff=None):
    kickoff = kickoff or datetime(2026, 1, 1, 20, tzinfo=HKT)
    line = line if line is not None else (-0.25 if code == "HDC" else 2.5 if code == "HIL" else 9.5)
    return {
        "match_id": fixture, "stage": "T-5", "kickoff": kickoff.isoformat(),
        "predicted_at": (kickoff - timedelta(minutes=5)).isoformat(),
        "market_grades": [{"code": code, "side": side, "line": line, "odds": 1.82,
                           "grade_status": "GRADED", "hit": hit}],
    }


def watch(*, fixture="future", codes=("HDC",), odds=1.83):
    kickoff = datetime(2099, 8, 1, 20, tzinfo=HKT)
    rows = []
    for code in codes:
        rows.append({
            "code": code, "side": "H", "line": -0.25 if code == "HDC" else 2.5 if code == "HIL" else 9.5,
            "odds": odds, "observed_at": (kickoff - timedelta(minutes=6)).isoformat(),
            "source": "hkjc_public_board",
            "market": {"HDC": "讓球", "HIL": "入球大細", "CHL": "角球大細"}[code],
        })
    return {"match_id": fixture, "league": "測試聯賽", "home": "主隊", "away": "客隊",
            "kickoff": kickoff.isoformat(), "stages": [{"stage": "T-5", "ts": (kickoff - timedelta(minutes=5)).isoformat(), "market_predictions": rows}]}


class ConditionPortfolioTests(unittest.TestCase):
    def test_wilson_thresholds_three_markets_and_idempotency(self):
        forty_nine = [historical_row(f"s{i}", "HDC", hit=True, kickoff=datetime(2026, 1, 1, 20, tzinfo=HKT) + timedelta(days=i)) for i in range(49)]
        created, audit = cp.evaluate_new_t5({"bets": []}, watch(), history_rows=forty_nine)
        self.assertEqual(created, [])
        self.assertEqual(audit[0]["reason"], "no_frozen_historical_condition")

        sixty = [historical_row(f"s{i}", "HDC", hit=i < 40, kickoff=datetime(2026, 1, 1, 20, tzinfo=HKT) + timedelta(days=i)) for i in range(59)]
        created, audit = cp.evaluate_new_t5({"bets": []}, watch(), history_rows=sixty)
        self.assertEqual(created, [])
        self.assertEqual(audit[0]["reason"], "wilson_gate_not_passed")

        rows = []
        for i in range(59):
            when = datetime(2026, 3, 1, 20, tzinfo=HKT) + timedelta(days=i)
            rows.extend(historical_row(f"all{i}", code, hit=True, kickoff=when) for code in ("HDC", "HIL", "CHL"))
        ledger = {"bets": []}
        created, audit = cp.evaluate_new_t5(ledger, watch(codes=("HDC", "HIL", "CHL")), history_rows=rows)
        self.assertEqual(len(created), 3)
        self.assertTrue(all(bet["stake"] == 500 for bet in created))
        self.assertLessEqual(sum(bet["stake"] for bet in created), 1500)
        self.assertTrue(all(bet["frozen_historical_evidence"]["decided"] >= 50 for bet in created))
        ledger["bets"].extend(created)
        self.assertEqual(cp.evaluate_new_t5(ledger, watch(codes=("HDC", "HIL", "CHL")), history_rows=rows)[0], [])

    def test_invalid_quote_and_canonical_settlement(self):
        rows = [historical_row(f"ok{i}", "HDC", kickoff=datetime(2026, 4, 1, 20, tzinfo=HKT) + timedelta(days=i)) for i in range(59)]
        invalid = watch(odds=1.0)
        self.assertEqual(cp.evaluate_new_t5({"bets": []}, invalid, history_rows=rows)[0], [])
        post = watch()
        post["stages"][0]["market_predictions"][0]["observed_at"] = "2099-08-01T20:00:00+08:00"
        self.assertEqual(cp.evaluate_new_t5({"bets": []}, post, history_rows=rows)[0], [])
        cases = [
            ({"code": "HDC", "condition": -0.25, "side": "H"}, {"goals_home": 1, "goals_away": 0}),
            ({"code": "HIL", "condition": 2.5, "side": "H"}, {"goals_total": 3}),
            ({"code": "CHL", "condition": 9.5, "side": "L"}, {"corners_total": 9}),
        ]
        for fields, result in cases:
            label, pnl = settle.settle_bet(fields | {"stake": 1000, "odds": 1.8}, result)
            self.assertEqual(label, "Won")
            self.assertGreater(pnl, 0)

    def test_t30_and_replay_do_not_evaluate_and_stats_ignore_legacy(self):
        kickoff = datetime.now(HKT) + timedelta(minutes=20)
        t30 = {"match_id": "one", "stage": "T-30", "kickoff_hkt": kickoff.isoformat(), "league": "測試聯賽", "home": "主", "away": "客", "can_bet": False, "candidates": [], "weather": {}, "final": {}, "open": {}, "now": {}, "movement": {}, "adjustments": [], "mults": {}, "outcome": {}}
        t5 = t30 | {"stage": "T-5"}
        with tempfile.TemporaryDirectory() as directory:
            Path(directory, "predictions.json").write_text(json.dumps([t30, t5], ensure_ascii=False), encoding="utf-8")
            with patch.object(record_picks, "HERE", directory), patch.object(record_picks, "LEDGER", str(Path(directory, "sim_ledger.json"))), patch.object(record_picks, "ACCURACY_HISTORY", str(Path(directory, "accuracy_history.json"))), patch.object(record_picks, "evaluate_new_t5", return_value=([], [])) as evaluate, patch.dict("os.environ", {}, clear=False):
                record_picks.sync()
                record_picks.sync()
        self.assertEqual(evaluate.call_count, 1)
        ledger = {"bets": [
            {"portfolio": "legacy", "strategy": "old", "status": "SETTLED", "stake": 9000, "pnl": 9000},
            {"portfolio": cp.PORTFOLIO, "strategy": cp.STRATEGY, "status": "SETTLED", "stake": 500, "pnl": 250, "result": "Won"},
        ]}
        stats = settle.recompute(ledger)
        self.assertEqual((stats["turnover"], stats["pnl"]), (500.0, 250.0))

    def test_missing_fixture_context_fails_closed_before_bet_creation(self):
        rows = [
            historical_row(
                f"ctx{i}", "HDC",
                kickoff=datetime(2026, 7, 1, 20, tzinfo=HKT) + timedelta(days=i),
            )
            for i in range(59)
        ]
        missing_league = watch()
        missing_league["league"] = ""
        created, audit = cp.evaluate_new_t5(
            {"bets": []}, missing_league, history_rows=rows,
        )
        self.assertEqual(created, [])
        self.assertEqual(
            audit[0]["reason"], "missing_fixture_context_for_public_condition_bet",
        )
        diagnostics = ledger = {"bets": []}
        created, _ = cp.evaluate_new_t5(
            diagnostics, missing_league, history_rows=rows,
        )
        self.assertEqual(created, [])
        self.assertEqual(
            {row["reason"] for row in diagnostics["wilson_validation"]["audit"]},
            {"missing_fixture_context_for_public_condition_bet"},
        )


class ConditionDashboardSourceTests(unittest.TestCase):
    def test_wilson_condition_simulation_and_read_only_archive_are_public(self):
        index = (ROOT / "hkjc-dashboard" / "index.html").read_text(encoding="utf-8")
        app = (ROOT / "hkjc-dashboard" / "app.js").read_text(encoding="utf-8")
        source = (SYSTEM / "gen_app_data.py").read_text(encoding="utf-8")
        self.assertIn('data-view="ledger">獨立驗證倉', index)
        self.assertNotIn('data-view="shadow"', index)
        self.assertNotIn('id="viewShadow"', index)
        self.assertNotIn("renderShadow", app)
        self.assertIn("footbreak_wilson_test", app)
        self.assertIn("Wilson 測試攻略", app)
        self.assertIn("首次原生 T-5", app)
        self.assertIn("已封存／退役", app)
        self.assertIn("前瞻", app)
        self.assertNotIn("Prospective PnL", app)
        self.assertIn("每注", app)
        self.assertIn("fx-teams", app)
        self.assertIn("leagueDisplay(m.league)", app)
        self.assertIn("league_display.js", index)
        self.assertNotIn('"shadow_bets"', source)
        self.assertIn("_public_bet", source)
        self.assertIn("retired_v1", source)
        for text in (
            "Wilson 95%", "最低可接受賠率", "走水不計入命中率分母",
        ):
            self.assertIn(text, app)
        self.assertIn('"odds_tiers"', source)


if __name__ == "__main__":
    unittest.main()
