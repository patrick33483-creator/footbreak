"""Offline regression tests for Footbreak's PinnAPI sharp-provider adapter."""
from __future__ import annotations

import json

import os
import subprocess
import sys
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[2]
SYSTEM = ROOT / "system"
if str(SYSTEM) not in sys.path:
    sys.path.insert(0, str(SYSTEM))

import run_predict
import sharp
from crown.pinnapi import parse_corner_lines, parse_lines


class FootbreakPinnapiSharpTests(unittest.TestCase):
    def test_pinnapi_parser_and_native_structure_preserve_home_handicap_orientation(self) -> None:
        parsed = parse_lines({
            "event_id": "event-1", "source_timestamp": 1786248000,
            "periods": {"num_0": {
                "moneyline": {"home": 2.1, "draw": 3.4, "away": 3.7},
                # PinnAPI hdp is already home perspective: -0.25 = home gives.
                "spreads": [{"hdp": -0.25, "home": 1.91, "away": 1.99, "is_main": True}],
                "totals": [{"points": 2.75, "over": 1.95, "under": 1.95, "is_main": True}],
            }},
        }, "event-1", observed_at=1786248001)
        self.assertFalse(parsed["timestamp_inferred"])
        prices = [dict(row, provider="pinnapi") for row in parsed["prices"]]
        structured = sharp.structure(prices, "Home", "Away")
        self.assertEqual(structured["HDC"][0]["condition"], "-0.25")
        self.assertEqual(structured["HDC"][0]["odds"], {"H": 1.91, "A": 1.99})
        self.assertEqual(structured["HIL"][0]["condition"], "2.75")
        self.assertEqual(structured["HAD"][0]["odds"]["D"], 3.4)
        self.assertEqual(structured["CHL"], [])

    def test_corner_prices_merge_into_chl_without_changing_standard_markets(self) -> None:
        normal = parse_lines({
            "event_id": "event-1", "source_timestamp": 1786248000,
            "periods": {"num_0": {
                "spreads": [{"hdp": -0.25, "home": 1.91, "away": 1.99, "is_main": True}],
                "totals": [{"points": 2.75, "over": 1.95, "under": 1.95, "is_main": True}],
            }},
        }, "event-1", observed_at=1786248001)
        corners = parse_corner_lines({
            "events": [{
                "event_id": "corner-1", "league_name": "League Corners",
                "home": "Home (Corners)", "away": "Away (Corners)",
                "source_timestamp": 1786248000,
                "periods": {"num_0": {
                    "totals": {"9.5": {"points": 9.5, "over": 1.92, "under": 1.96}},
                }},
            }],
        }, "event-1", observed_at=1786248001)

        class Client:
            def lines(self, event_id):
                assert event_id == "event-1"
                return normal

            def corner_lines(self, event_id):
                assert event_id == "event-1"
                return corners

        with patch.object(sharp, "_client", return_value=Client()):
            prices = sharp.fetch_odds(["event-1"])["event-1"]
        structured = sharp.structure(prices, "Home", "Away")

        self.assertEqual(structured["HDC"][0]["odds"], {"H": 1.91, "A": 1.99})
        self.assertEqual(structured["HIL"][0]["odds"], {"H": 1.95, "L": 1.95})
        self.assertEqual(structured["CHL"], [{
            "lineId": None, "condition": "9.5", "main": True,
            "status": "AVAILABLE", "odds": {"H": 1.92, "L": 1.96},
        }])
        self.assertTrue(all(row["event_id"] == "event-1" for row in prices))

    def test_corner_failure_fails_closed_without_breaking_standard_markets(self) -> None:
        normal = parse_lines({
            "event_id": "event-1", "source_timestamp": 1786248000,
            "periods": {"num_0": {
                "spreads": [{"hdp": 0, "home": 1.91, "away": 1.99}],
                "totals": [{"points": 2.5, "over": 1.95, "under": 1.95}],
            }},
        }, "event-1", observed_at=1786248001)

        class Client:
            def lines(self, _event_id):
                return normal

            def corner_lines(self, _event_id):
                raise RuntimeError("specials unavailable")

        with patch.object(sharp, "_client", return_value=Client()):
            structured = sharp.structure(sharp.fetch_odds(["event-1"])["event-1"], "Home", "Away")

        self.assertEqual(len(structured["HDC"]), 1)
        self.assertEqual(len(structured["HIL"]), 1)
        self.assertEqual(structured["CHL"], [])

    def test_pinnapi_fixture_adapter_preserves_english_identity_and_kickoff(self) -> None:
        fixture = sharp._fixture_from_pinnapi({
            "id": "123", "league": "England - Premier League", "home": "Home FC", "away": "Away FC",
            "kickoff": 1786248000,
        })
        self.assertEqual(fixture["id"], "123")
        self.assertEqual(fixture["home_team_display"], "Home FC")
        self.assertTrue(fixture["start_date"].endswith("Z"))
        self.assertEqual(fixture["league"]["name"], "England - Premier League")

    def test_fixture_list_excludes_corner_child_events(self) -> None:
        class Client:
            def fixtures(self):
                return [
                    {
                        "id": "parent", "league": "Cup", "home": "San Diego FC",
                        "away": "Club Tijuana", "kickoff": 1786248000,
                        "parent_id": None,
                    },
                    {
                        "id": "corners", "league": "Cup Corners",
                        "home": "San Diego FC (Corners)",
                        "away": "Club Tijuana (Corners)", "kickoff": 1786248000,
                        "parent_id": "parent",
                    },
                ]

        with patch.object(sharp, "_client", return_value=Client()):
            fixtures = sharp.list_fixtures()
        self.assertEqual([row["id"] for row in fixtures], ["parent"])

    def test_unique_reversed_fixture_is_oriented_to_hkjc(self) -> None:
        kickoff = datetime(2026, 8, 10, 2, 15, tzinfo=timezone.utc)
        match = {
            "id": "50072659",
            "homeTeam": {"name_en": "Portland Timbers"},
            "awayTeam": {"name_en": "CF America"},
            "tournament": {"name_en": "Leagues Cup"},
        }
        fixture = {
            "id": "1633316620",
            "start_date": kickoff.isoformat(),
            "home_team_display": "Club America",
            "away_team_display": "Portland Timbers",
            "league": {"name": "Leagues Cup"},
        }

        found, score = sharp.match_fixture(match, [fixture], kickoff)

        self.assertEqual(found["id"], fixture["id"])
        self.assertTrue(found["_orientation_reversed"])
        self.assertEqual(score, 1.0)

    def test_reversed_fixture_prices_swap_sides_and_handicap_sign(self) -> None:
        prices = [
            {"market": "1X2", "selection": "H", "odds": 2.1},
            {"market": "1X2", "selection": "D", "odds": 3.4},
            {"market": "1X2", "selection": "A", "odds": 3.7},
            {"market": "HDC", "line": -0.25, "selection": "H", "odds": 1.91},
            {"market": "HDC", "line": -0.25, "selection": "A", "odds": 1.99},
            {"market": "HIL", "line": 2.75, "selection": "H", "odds": 1.95},
            {"market": "HIL", "line": 2.75, "selection": "L", "odds": 1.95},
        ]

        structured = sharp._native_structure(sharp.orient_prices(prices, True))

        self.assertEqual(structured["HAD"][0]["odds"], {
            "H": 3.7, "D": 3.4, "A": 2.1,
        })
        self.assertEqual(structured["HDC"][0]["condition"], "0.25")
        self.assertEqual(structured["HDC"][0]["odds"], {
            "H": 1.99, "A": 1.91,
        })
        self.assertEqual(structured["HIL"][0]["odds"], {
            "H": 1.95, "L": 1.95,
        })

    def test_pinnapi_lines_failure_propagates(self) -> None:
        class BrokenClient:
            def lines(self, event_id):
                raise RuntimeError("upstream outage")

        with patch.object(sharp, "_client", return_value=BrokenClient()):
            with self.assertRaises(sharp.ProviderError):
                sharp.fetch_odds(["123"])

    def test_prediction_failure_persists_fail_closed_stage_decision(self) -> None:
        kickoff = datetime.now(timezone.utc) + timedelta(minutes=30)
        match = {
            "id": "m1", "status": "PREEVENT",
            "homeTeam": {"name_ch": "主隊"}, "awayTeam": {"name_ch": "客隊"},
        }
        fixture = {"id": "p1", "home_team_display": "Home", "away_team_display": "Away"}
        with tempfile.TemporaryDirectory() as directory:
            previous_here, previous_snap = run_predict.HERE, run_predict.HK_SNAP
            try:
                run_predict.HERE = directory
                run_predict.HK_SNAP = os.path.join(directory, "hk_snapshots.json")
                target = Path(directory) / "predictions.json"
                Path(directory, "sim_ledger.json").write_text(json.dumps({"watch": {
                    "m1": {"kickoff": kickoff.isoformat(), "fixture_id": "p1", "stages": []}
                }}), encoding="utf-8")
                with patch.object(run_predict.H, "fetch_matches", return_value=[match]), \
                     patch.object(run_predict.H, "parse_kickoff", return_value=kickoff), \
                     patch.object(run_predict.S, "list_fixtures", return_value=[fixture]), \
                     patch.object(run_predict.S, "match_fixture", return_value=(fixture, 1.0)), \
                     patch.object(run_predict, "analyse_match", side_effect=sharp.ProviderError("PinnAPI down")):
                    results = run_predict.main(mode="due", horizon_min=90)
                self.assertEqual(results, [])
                self.assertEqual(run_predict.pending_watch_match_ids(), ["m1"])
                self.assertEqual(json.loads(target.read_text(encoding="utf-8")), [])
            finally:
                run_predict.HERE, run_predict.HK_SNAP = previous_here, previous_snap

    def test_unusable_sharp_model_persists_fail_closed_stage_decision(self) -> None:
        kickoff = datetime.now(timezone.utc) + timedelta(minutes=30)
        match = {
            "id": "m1", "status": "PREEVENT",
            "homeTeam": {"name_ch": "主隊"}, "awayTeam": {"name_ch": "客隊"},
        }
        fixture = {"id": "p1", "home_team_display": "Home", "away_team_display": "Away"}
        with tempfile.TemporaryDirectory() as directory:
            previous_here, previous_snap = run_predict.HERE, run_predict.HK_SNAP
            try:
                run_predict.HERE = directory
                run_predict.HK_SNAP = os.path.join(directory, "hk_snapshots.json")
                Path(directory, "sim_ledger.json").write_text(json.dumps({"watch": {
                    "m1": {"kickoff": kickoff.isoformat(), "fixture_id": "p1", "stages": []}
                }}), encoding="utf-8")
                with patch.object(run_predict.H, "fetch_matches", return_value=[match]), \
                     patch.object(run_predict.H, "parse_kickoff", return_value=kickoff), \
                     patch.object(run_predict.S, "list_fixtures", return_value=[fixture]), \
                     patch.object(run_predict.S, "match_fixture", return_value=(fixture, 1.0)), \
                     patch.object(run_predict, "analyse_match", return_value={"skip": "no full-match lines"}):
                    results = run_predict.main(mode="due", horizon_min=90)
                self.assertEqual(results, [])
                self.assertEqual(run_predict.pending_watch_match_ids(), ["m1"])
            finally:
                run_predict.HERE, run_predict.HK_SNAP = previous_here, previous_snap

    def test_run_all_exits_before_ledger_or_dashboard_steps_on_prediction_failure(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            log = root / "calls.log"
            fake_python = root / "python3"
            fake_python.write_text(
                "#!/usr/bin/env bash\n"
                "printf '%s\\n' \"$*\" >> \"$TEST_CALL_LOG\"\n"
                "if [ \"$1\" = run_predict.py ]; then exit 17; fi\n"
                "exit 0\n",
                encoding="utf-8",
            )
            fake_python.chmod(0o755)
            env = os.environ | {"PATH": f"{root}:{os.environ['PATH']}", "TEST_CALL_LOG": str(log)}
            result = subprocess.run(["bash", "run_all.sh", "tick"], cwd=SYSTEM, env=env,
                                    text=True, capture_output=True)
            self.assertEqual(result.returncode, 17)
            self.assertEqual(
                log.read_text(encoding="utf-8").splitlines(),
                ["run_predict.py 90"],
            )


if __name__ == "__main__":
    unittest.main()

class PinnapiLastGoodFallbackTests(unittest.TestCase):
    def test_exact_fresh_last_good_is_diagnostic_only(self) -> None:
        kickoff = datetime.now(timezone.utc) + timedelta(minutes=30)
        fixture = {
            "id": "event-1", "start_date": kickoff.isoformat(),
            "home_team_display": "Home FC", "away_team_display": "Away FC",
        }
        parsed = {"prices": [{"market": "HIL", "line": 2.5, "selection": "H", "odds": 1.91,
                              "source_at": datetime.now(timezone.utc).timestamp()},
                             {"market": "HIL", "line": 2.5, "selection": "L", "odds": 1.99,
                              "source_at": datetime.now(timezone.utc).timestamp()}]}
        class GoodClient:
            def lines(self, _event_id): return parsed
            def corner_lines(self, _event_id): return {"prices": []}
        class BrokenClient:
            def lines(self, _event_id): raise RuntimeError("outage")

        with tempfile.TemporaryDirectory() as directory, \
             patch.object(sharp, "CACHE", directory), \
             patch.object(sharp, "LIVE_RETRY_ATTEMPTS", 1), \
             patch.object(sharp, "_client", return_value=GoodClient()):
            live = sharp.fetch_odds(["event-1"], fixture_identities={"event-1": fixture})["event-1"]
            self.assertTrue(all(row["provider_live"] for row in live))
            with patch.object(sharp, "_client", return_value=BrokenClient()):
                fallback = sharp.fetch_odds(["event-1"], fixture_identities={"event-1": fixture})["event-1"]
        self.assertTrue(all(not row["provider_live"] for row in fallback))
        self.assertTrue(all(row["source"] == "fallback" for row in fallback))
        self.assertTrue(all(row["data_age_seconds"] <= sharp.LAST_GOOD_TTL_SECONDS for row in fallback))

    def test_no_identity_never_allows_last_good_fallback(self) -> None:
        class BrokenClient:
            def lines(self, _event_id): raise RuntimeError("outage")
        with tempfile.TemporaryDirectory() as directory, \
             patch.object(sharp, "CACHE", directory), \
             patch.object(sharp, "LIVE_RETRY_ATTEMPTS", 1), \
             patch.object(sharp, "_client", return_value=BrokenClient()):
            with self.assertRaises(sharp.ProviderError):
                sharp.fetch_odds(["event-1"])
