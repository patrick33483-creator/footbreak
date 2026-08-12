from __future__ import annotations

import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

SYSTEM_DIR = Path(__file__).resolve().parents[1]
if str(SYSTEM_DIR) not in sys.path:
    sys.path.insert(0, str(SYSTEM_DIR))

import settle
import titan_results
import accuracy
from analysis.learning_store import LearningStore
from crown.common import HKT
from crown.titan import TitanClient
from datetime import datetime


class ResultSourceTests(unittest.TestCase):
    def test_fixture_backfill_covers_official_and_shadow_ledgers(self) -> None:
        ledger = {
            "bets": [{"match_id": "5001"}],
            "shadow_bets": [{"match_id": "5001", "portfolio": "shadow"}],
        }
        predictions = [{
            "match_id": "5001",
            "fixture_id": "fixture-5001",
            "league_id": "league-1",
        }]
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory, "predictions.json")
            path.write_text(json.dumps(predictions), encoding="utf-8")
            with patch.object(settle, "PREDS", str(path)):
                self.assertEqual(settle.backfill_fixture_ids(ledger), 2)
        self.assertEqual(ledger["bets"][0]["fixture_id"], "fixture-5001")
        self.assertEqual(
            ledger["shadow_bets"][0]["fixture_id"], "fixture-5001"
        )

    def test_hkjc_results_are_normalized_for_footbreak_settlement(self) -> None:
        official = {
            "50072040": {
                "home_score": 2,
                "away_score": 2,
                "corners_total": 9,
                "source": "hkjc_official",
            }
        }
        with patch("crown.hkjc.fetch_official_results", return_value=official) as fetch:
            rows = settle.fetch_hkjc_results({"50072040"}, {"2026-08-09"})
        fetch.assert_called_once_with(
            {"50072040"}, {"2026-08-08", "2026-08-09"}
        )
        self.assertEqual(rows["50072040"]["goals_home"], 2)
        self.assertEqual(rows["50072040"]["goals_away"], 2)
        self.assertEqual(rows["50072040"]["goals_total"], 4)
        self.assertEqual(rows["50072040"]["corners_total"], 9)
        self.assertEqual(rows["50072040"]["source"], "hkjc_official")

    def test_hkjc_non_result_statuses_are_exposed(self) -> None:
        official = {
            "50072899": {
                "status": "MATCHSUSPENDED",
                "refund_pools": ["HAD"],
                "payout_refund_pools": [],
                "source": "hkjc_official",
            }
        }
        with patch("crown.hkjc.fetch_official_match_statuses", return_value=official):
            rows = settle.fetch_hkjc_statuses({"50072899"}, {"2026-08-09"})
        self.assertEqual(rows["50072899"]["status"], "MATCHSUSPENDED")

    def test_settlement_voids_explicit_hkjc_postponement(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            ledger_path = Path(directory, "sim_ledger.json")
            ledger_path.write_text(json.dumps({
                "bankroll": 50000,
                "bets": [{
                    "bet_id": "b1", "match_id": "50072899",
                    "home": "主隊", "away": "客隊",
                    "kickoff": "2026-08-09 10:00",
                    "market": "讓球", "code": "HDC",
                    "condition": "-0.5", "side": "H",
                    "label": "主 -0.5", "odds": 2.0,
                    "stake": 100, "status": "PENDING",
                }],
                "watch": {}, "log": [], "stats": {},
            }), encoding="utf-8")
            status = {
                "50072899": {
                    "status": "MATCHPOSTPONED",
                    "refund_pools": [], "payout_refund_pools": [],
                    "source": "hkjc_official",
                },
            }
            with patch.object(settle, "LEDGER", str(ledger_path)), \
                 patch.object(settle, "fetch_hkjc_results", return_value={}), \
                 patch.object(settle, "fetch_hkjc_statuses", return_value=status):
                stats = settle.run(force=True)
            saved = json.loads(ledger_path.read_text(encoding="utf-8"))
        self.assertEqual(saved["bets"][0]["status"], "VOIDED")
        self.assertEqual(saved["bets"][0]["pnl"], 0.0)
        self.assertEqual(
            saved["bets"][0]["settlement_source"],
            "hkjc_official_exact_id_terminal_status",
        )
        self.assertEqual(stats["n_settled"], 0)

    def test_terminal_footbreak_exclusion_is_persisted_to_learning_store(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "learning.sqlite"
            with LearningStore(path) as store:
                snapshot = store.record_snapshot(
                    "footbreak", "terminal-2", "T-30",
                    "2026-08-09T11:30:00+08:00",
                    "2026-08-09T12:00:00+08:00",
                    {"market_predictions": []},
                )
            stages = [{
                "learning_snapshot_id": snapshot["snapshot_id"],
                "market_predictions": [{
                    "code": "HDC", "condition": "-0.5", "side": "H",
                    "probability": .6,
                }],
            }]
            with patch.dict(os.environ, {"LEARNING_DB_PATH": str(path)}):
                accuracy._persist_learning_exclusion(
                    "terminal-2", {"fixture_id": "optic-2"}, stages,
                    "MATCHCANCELLED", "hkjc_official_exact_id_terminal_status",
                )
            with LearningStore(path) as store:
                result = store._connection.execute(  # noqa: SLF001
                    "SELECT terminal_status FROM results WHERE system = 'footbreak'"
                ).fetchone()
                grade = store._connection.execute(  # noqa: SLF001
                    "SELECT state FROM grades WHERE snapshot_id = ? AND market = 'HDC'",
                    (snapshot["snapshot_id"],),
                ).fetchone()
        self.assertEqual(result["terminal_status"], "MATCHCANCELLED")
        self.assertEqual(grade["state"], "NOT_APPLICABLE")

    def test_corner_required_refreshes_an_incomplete_exact_fixture_cache(self) -> None:
        incomplete = {
            "fixture_id": "fx1", "goals_home": 1, "goals_away": 0,
            "goals_total": 1, "corners_home": None, "corners_away": None,
            "corners_total": None, "source": "opticodds_exact_fixture_id",
        }
        completed = {
            "fixture": {"id": "fx1", "status": "completed"},
            "scores": {
                "home": {"total": 1}, "away": {"total": 0},
            },
            "market_stats": {
                "home": {"team_total_corners": 6},
                "away": {"team_total_corners": 4},
            },
        }
        with tempfile.TemporaryDirectory() as directory, \
             patch.object(settle, "RESCACHE", directory), \
             patch.object(settle, "_call", return_value={"data": [completed]}) as call:
            Path(directory, "fx1.json").write_text(
                json.dumps(incomplete), encoding="utf-8"
            )
            result = settle.fetch_result("fx1", require_corners=True)
        call.assert_called_once()
        self.assertEqual(result["corners_total"], 10)

    def test_official_score_kept_when_exact_fixture_adds_corners(self) -> None:
        official = {
            "goals_home": 2, "goals_away": 1, "goals_total": 3,
            "corners_home": None, "corners_away": None,
            "corners_total": None, "source": "hkjc_official",
        }
        fallback = {
            "goals_home": 9, "goals_away": 9, "goals_total": 18,
            "corners_home": 7, "corners_away": 3, "corners_total": 10,
            "source": "opticodds_exact_fixture_id",
        }
        with patch.object(settle, "fetch_result", return_value=fallback) as fetch:
            result = settle.merge_missing_corners(official, "fx-safe")
        fetch.assert_called_once_with("fx-safe", require_corners=True)
        self.assertEqual(result["goals_home"], 2)
        self.assertEqual(result["goals_away"], 1)
        self.assertEqual(result["corners_total"], 10)
        self.assertIn("hkjc_official", result["source"])

    def test_titan_strict_reversed_identity_fills_only_missing_corners(self) -> None:
        official = {
            "goals_home": 1, "goals_away": 3, "goals_total": 4,
            "corners_home": None, "corners_away": None,
            "corners_total": None, "source": "hkjc_official",
        }
        record = {
            "match_id": "50072659", "league": "北美聯賽盃",
            "home": "波特蘭伐木者", "away": "CF 阿美利加",
            "kickoff": "2026-08-10 10:15",
        }
        rows = [{
            "id": "2961746", "league": "中北美杯",
            "home": "墨西哥美洲(中)", "away": "波特兰伐木者",
            "kickoff": datetime(2026, 8, 10, 10, 25, tzinfo=HKT),
            "home_score": 3, "away_score": 1,
        }]
        client = Mock(spec=TitanClient)
        client.result_detail.return_value = {
            "titan_id": "2961746", "corners_home": 5,
            "corners_away": 5, "corners_total": 10,
        }
        result = titan_results.merge_titan_corners(
            official, record, client=client, rows=rows
        )
        self.assertEqual(result["goals_home"], 1)
        self.assertEqual(result["goals_away"], 3)
        self.assertEqual(result["corners_total"], 10)
        self.assertEqual(result["titan_id"], "2961746")
        self.assertIn("strict_identity_score", result["source"])

    def test_titan_wrong_score_does_not_fill_corners(self) -> None:
        official = {
            "goals_home": 1, "goals_away": 0, "goals_total": 1,
            "corners_home": None, "corners_away": None,
            "corners_total": None, "source": "hkjc_official",
        }
        record = {
            "match_id": "50072681", "league": "北美聯賽盃",
            "home": "聖地亞哥FC", "away": "迪祖亞拿",
            "kickoff": "2026-08-10 10:00",
        }
        rows = [{
            "id": "wrong", "league": "中北美杯",
            "home": "圣地亚哥", "away": "蒂华纳",
            "kickoff": datetime(2026, 8, 10, 10, 10, tzinfo=HKT),
            "home_score": 0, "away_score": 1,
        }]
        client = Mock(spec=TitanClient)
        result = titan_results.merge_titan_corners(
            official, record, client=client, rows=rows
        )
        self.assertIsNone(result["corners_total"])
        client.result_detail.assert_not_called()

    def test_titan_exact_teams_and_score_allow_ten_minute_provider_offset(self) -> None:
        official = {
            "goals_home": 1, "goals_away": 0, "goals_total": 1,
            "corners_home": None, "corners_away": None,
            "corners_total": None, "source": "hkjc_official",
        }
        record = {
            "match_id": "50072681", "league": "North America - Leagues Cup",
            "home": "聖地亞哥FC", "away": "迪祖亞拿",
            "kickoff": "2026-08-10 10:00",
        }
        rows = [{
            "id": "2961747", "league": "中北美杯",
            "home": "圣地亚哥", "away": "蒂华纳",
            "kickoff": datetime(2026, 8, 10, 10, 10, tzinfo=HKT),
            "home_score": 1, "away_score": 0,
        }]
        client = Mock(spec=TitanClient)
        client.result_detail.return_value = {
            "corners_home": 6, "corners_away": 3, "corners_total": 9,
        }
        result = titan_results.merge_titan_corners(
            official, record, client=client, rows=rows
        )
        self.assertEqual(result["corners_total"], 9)
        client.result_detail.assert_called_once_with("2961747")

    def test_crown_exact_cross_source_id_recovers_corner_result(self) -> None:
        official = {
            "goals_home": 2, "goals_away": 0, "goals_total": 2,
            "corners_home": None, "corners_away": None,
            "corners_total": None, "source": "hkjc_official",
        }
        record = {
            "match_id": "50072793", "titan_match_id": "3009588",
            "league": "England - EFL Cup",
            "home": "普利茅夫", "away": "埃克塞特",
            "kickoff": "2026-08-11 03:00",
        }
        rows = [{
            "id": "3009588", "league": "英联杯",
            "home": "普利茅斯", "away": "埃克塞特城",
            "kickoff": datetime(2026, 8, 11, 3, 0, tzinfo=HKT),
            "home_score": 2, "away_score": 0,
        }]
        client = Mock(spec=TitanClient)
        client.result_detail.return_value = {
            "corners_home": 8, "corners_away": 3, "corners_total": 11,
        }
        result = titan_results.merge_titan_corners(
            official, record, client=client, rows=rows
        )
        self.assertEqual(result["corners_total"], 11)
        self.assertEqual(result["titan_id"], "3009588")
        self.assertIn("exact_cross_source_id_score", result["source"])

    def test_crown_titan_map_requires_one_unique_id_per_hkjc_match(self) -> None:
        payload = {
            "rows": [
                {"hkjc_match_id": "h1", "titan_match_id": "t1"},
                {"hkjc_match_id": "h1", "titan_match_id": "t1"},
                {"hkjc_match_id": "h2", "titan_match_id": "t2"},
                {"hkjc_match_id": "h2", "titan_match_id": "conflict"},
            ]
        }
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory, "prediction_history.json")
            path.write_text(json.dumps(payload), encoding="utf-8")
            result = titan_results.load_crown_titan_match_map(path)
        self.assertEqual(result, {"h1": "t1"})

    def test_public_refresh_uses_static_data_instead_of_missing_api_route(self) -> None:
        app = Path(SYSTEM_DIR.parent, "hkjc-dashboard", "app.js").read_text(
            encoding="utf-8"
        )
        self.assertNotIn("api/settle", app)
        self.assertIn("fetch('data.json?v='", app)
        index = Path(SYSTEM_DIR.parent, "hkjc-dashboard", "index.html").read_text(
            encoding="utf-8"
        )
        self.assertIn("styles.css?v=20260812-data-health-chl-v2", index)
        self.assertIn("app.js?v=20260812-data-health-chl-v2", index)
        self.assertIn("setInterval(() => refresh(true), 60000)", app)
        self.assertIn('class="history-result-cell"', app)

        styles = Path(SYSTEM_DIR.parent, "hkjc-dashboard", "styles.css").read_text(
            encoding="utf-8"
        )
        self.assertIn(".history-result-cell .hist-result", styles)
        self.assertIn(".history-result-cell .hist-result b", styles)

    def test_manual_settlement_runs_crown_directly_without_http_timeout(self) -> None:
        workflow = Path(
            SYSTEM_DIR.parent, ".github", "workflows", "settle.yml"
        ).read_text(encoding="utf-8")
        self.assertIn("deploy/crown-run.sh settle", workflow)
        self.assertNotIn("curl --fail", workflow)
        self.assertIn(
            "systemctl stop footbreak-tick.service footbreak-sweep.service footbreak-settle.service",
            workflow,
        )
        self.assertIn("rm -f /run/footbreak-t5-priority", workflow)
        self.assertIn('/var/www/crown/data.json', workflow)
        self.assertNotIn('/var/www/footbreak-crown/data.json', workflow)
        self.assertIn("systemctl stop crown-tick.service", workflow)
        deploy_workflow = Path(
            SYSTEM_DIR.parent, ".github", "workflows", "deploy.yml"
        ).read_text(encoding="utf-8")
        self.assertIn("group: production-maintenance", workflow)
        self.assertIn("group: production-maintenance", deploy_workflow)

    def test_health_check_accepts_intentional_sigterm_when_timer_is_active(self) -> None:
        health = Path(
            SYSTEM_DIR.parent, "deploy", "health-check.sh"
        ).read_text(encoding="utf-8")
        self.assertIn('timer="${service%.service}.timer"', health)
        self.assertIn('systemctl is-active --quiet "$timer"', health)
        self.assertIn('[ "$status" = 15 ]', health)


if __name__ == "__main__":
    unittest.main()
