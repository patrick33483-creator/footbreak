from __future__ import annotations

import tempfile
import unittest
from stat import S_IMODE
from stat import S_IMODE
from dataclasses import replace
from datetime import datetime, timedelta
from pathlib import Path
from unittest.mock import patch

from crown.config import settings
from crown.dashboard_data import write_dashboard_data
from crown.dashboard_data import write_dashboard_data
from crown.engine import _fresh
from crown.ledger import stage_for, sync_prediction
from crown.lines import parse_hkjc_handicap, parse_hkjc_total, settle_handicap, settle_total
from crown.matching import Event, bridge_titan_to_pinnapi, match_event, same_event_for_hkjc
from crown.notify import _bet_label, notify_new
from crown.pinnapi import parse_fixtures, parse_lines
from crown.state import load_predictions, merge_predictions, save_predictions
from crown.titan import crown_prices_from_pages


class CrownSafetyTests(unittest.TestCase):
    def setUp(self) -> None:
        self.now = datetime(2026, 8, 9, 12, 0, tzinfo=__import__("crown.common", fromlist=["HKT"]).HKT)
        self.target = Event("t", "League", "Alpha FC", "Beta FC", self.now)

    def test_asian_line_normalization_and_settlement(self) -> None:
        self.assertEqual(parse_hkjc_handicap("0/-0.5"), -0.25)
        self.assertEqual(parse_hkjc_handicap("0/+0.5"), 0.25)
        self.assertEqual(parse_hkjc_total("2.5/3"), 2.75)
        self.assertIsNone(parse_hkjc_total("2.2"))
        self.assertEqual(settle_handicap(-0.25, "H", 1, 1), "Half Lost")
        self.assertEqual(settle_total(2.75, "H", 2, 1), "Half Won")

    def test_matching_requires_unique_same_event_and_team_ids_for_hkjc(self) -> None:
        good = Event("good", "League", "Alpha", "Beta", self.now + timedelta(minutes=2))
        self.assertEqual(match_event(self.target, [good]).event.id, "good")
        ambiguous = Event("second", "League", "Alpha", "Beta", self.now + timedelta(minutes=3))
        self.assertEqual(match_event(self.target, [good, ambiguous]).reason, "ambiguous_candidate")
        no_ids = Event("h", "League", "Alpha", "Beta", self.now, {})
        self.assertIsNone(same_event_for_hkjc(self.target, [no_ids]).event)
        ids = Event("h2", "League", "Alpha", "Beta", self.now, {"home_team_id": "1", "away_team_id": "2"})
        self.assertEqual(same_event_for_hkjc(self.target, [ids]).event.id, "h2")

    def test_bilingual_titan_hkjc_pinnapi_bridge_preserves_orientation(self) -> None:
        titan = Event("titan", "澳昆超", "昆士兰狮队", "布里斯班狮吼青年队", self.now)
        hkjc = Event("hkjc", "澳洲全國聯賽昆士蘭", "FC獅子", "布里斯班獅吼B隊", self.now,
                     {"home_team_id": "11", "away_team_id": "22", "home_en": "Queensland Lions",
                      "away_en": "Brisbane Roar Youth", "league_en": "Australia NPL Queensland"})
        pinnapi = Event("pin", "Australia NPL Queensland", "Queensland Lions", "Brisbane Roar Youth", self.now)
        bridge = bridge_titan_to_pinnapi(titan, [hkjc], [pinnapi])
        self.assertEqual(bridge.path, "hkjc_bilingual_bridge")
        self.assertEqual(bridge.hkjc.event.id, "hkjc")
        self.assertEqual(bridge.event.id, "pin")
        self.assertFalse(bridge.reversed)

    def test_bilingual_bridge_rejects_ambiguous_pinnapi_event(self) -> None:
        titan = Event("titan", "澳昆超", "昆士兰狮队", "布里斯班狮吼青年队", self.now)
        hkjc = Event("hkjc", "澳洲全國聯賽昆士蘭", "FC獅子", "布里斯班獅吼B隊", self.now,
                     {"home_team_id": "11", "away_team_id": "22", "home_en": "Queensland Lions",
                      "away_en": "Brisbane Roar Youth", "league_en": "Australia NPL Queensland"})
        candidates = [
            Event("pin-1", "Australia NPL Queensland", "Queensland Lions", "Brisbane Roar Youth", self.now),
            Event("pin-2", "Australia NPL Queensland", "Queensland Lions", "Brisbane Roar Youth", self.now),
        ]
        bridge = bridge_titan_to_pinnapi(titan, [hkjc], candidates)
        self.assertIsNotNone(bridge.hkjc.event)
        self.assertIsNone(bridge.event)
        self.assertEqual(bridge.reason, "hkjc_to_pinnapi:ambiguous_candidate")

    def test_pinnapi_accepts_only_full_match_quarter_lines(self) -> None:
        payload = {"event_id": "p1", "source_timestamp": 1786248000, "periods": {"num_0": {
            "spreads": [{"hdp": -0.25, "home": 1.9, "away": 2.0}],
            "totals": [{"points": 2.75, "over": 1.95, "under": 1.95}],
        }}}
        parsed = parse_lines(payload, "p1", observed_at=1786248001)
        self.assertFalse(parsed["timestamp_inferred"])
        self.assertEqual({(p["market"], p["line"], p["selection"]) for p in parsed["prices"]},
                         {("HDC", -0.25, "H"), ("HDC", -0.25, "A"), ("HIL", 2.75, "H"), ("HIL", 2.75, "L")})
        non_full = parse_lines({"event_id": "p1", "periods": {"num_1": {"totals": []}}})
        self.assertEqual(non_full["prices"], [])

    def test_pinnapi_fixture_parser_drops_live_and_bad_time(self) -> None:
        result = parse_fixtures({"fixtures": [
            {"event_id": "ok", "league_name": "L", "home": "A", "away": "B", "starts": 1786248000},
            {"event_id": "live", "league_name": "L", "home": "A", "away": "B", "starts": 1786248000, "live": True},
            {"event_id": "bad", "league_name": "L", "home": "A", "away": "B", "starts": 1},
        ]})
        self.assertEqual([row["id"] for row in result], ["ok"])

    def test_source_freshness_fails_closed_when_stale_or_future(self) -> None:
        config = replace(settings(), source_max_age_seconds=90)
        self.assertEqual(_fresh({"source_at": 1000}, config, 1089), (True, None))
        self.assertEqual(_fresh({"source_at": 1000}, config, 1091)[0], False)
        self.assertEqual(_fresh({"source_at": 1100}, config, 1000)[0], False)

    def test_titan_crown_company_id_three_is_not_name_fallback(self) -> None:
        html = """<tr data-id='4'><td oddstype='wholeOdds'>0.90</td><td oddstype='wholeOdds' goals='0.5'>x</td><td oddstype='wholeOdds'>0.90</td></tr>
        <tr data-id='3'><td oddstype='wholeOdds'>0.95</td><td oddstype='wholeOdds' goals='0.25'>x</td><td oddstype='wholeOdds'>0.85</td></tr>"""
        prices = crown_prices_from_pages(html, html, "3", observed_at=100)
        self.assertEqual(len(prices), 4)
        self.assertEqual(prices[0]["line"], -0.25)
        self.assertEqual(prices[0]["odds"], 1.95)

    def test_stage_windows_and_simulated_ledger_are_idempotent(self) -> None:
        self.assertEqual(stage_for(30, False, set()), "T-30")
        self.assertIsNone(stage_for(30, False, {"T-30"}))
        self.assertEqual(stage_for(5, False, set()), "T-5")
        with tempfile.TemporaryDirectory() as directory:
            config = replace(settings(), state_dir=Path(directory), telegram_enabled=False)
            ledger = {"bankroll": 50000, "bets": [], "watch": {}, "log": [], "stats": {}}
            t30 = {"match_id": "x", "league": "L", "home": "A", "away": "B", "kickoff_hkt": "2026-08-09T12:05:00+08:00",
                   "stage": "T-30", "status": "REFERENCE_READY", "pick": None, "lead_view": None, "market_sources": {}}
            self.assertEqual(sync_prediction(ledger, t30, config), [])
            self.assertEqual(ledger["bets"], [])
            t5 = t30 | {"stage": "T-5", "conviction": 70, "pick": {
                "market": "HDC", "code": "HDC", "condition": "-0.25", "side": "H", "label": "HDC H -0.25",
                "odds": 2.0, "stake": 100, "prob": .55, "ev": .1,
            }}
            self.assertEqual(len(sync_prediction(ledger, t5, config)), 1)
            self.assertEqual(sync_prediction(ledger, t5, config), [])
            self.assertEqual(len(ledger["bets"]), 1)
            self.assertTrue(ledger["bets"][0]["simulation_only"])
            self.assertFalse(ledger["bets"][0]["real_betting_enabled"])

    def test_empty_tick_merge_retains_sweep_prediction_and_prunes_only_stale(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            config = replace(settings(), state_dir=Path(directory))
            now = self.now
            sweep = {"match_id": "future", "kickoff_hkt": (now + timedelta(hours=4)).isoformat(),
                     "stage": "首預", "status": "DATA_MISSING"}
            stale = {"match_id": "old", "kickoff_hkt": (now - timedelta(hours=7)).isoformat(),
                     "stage": "首預", "status": "DATA_MISSING"}
            save_predictions(config, [sweep, stale])
            retained = merge_predictions(config, [], now=now)
            self.assertEqual([row["match_id"] for row in retained], ["future"])
            # A later T-30 update replaces the current card but an empty tick
            # after it must retain that useful card.
            update = sweep | {"stage": "T-30", "status": "REFERENCE_READY"}
            self.assertEqual(merge_predictions(config, [update], now=now)[0]["stage"], "T-30")
            self.assertEqual(load_predictions(config)[0]["stage"], "T-30")
            self.assertEqual(merge_predictions(config, [], now=now)[0]["stage"], "T-30")

    def test_dashboard_artifact_is_readable_while_state_stays_separate(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            config = replace(settings(), state_dir=root / "private-state", web_root=root / "web")
            output = write_dashboard_data(config)
            self.assertEqual(S_IMODE(output.stat().st_mode), 0o644)
            self.assertNotEqual(output.parent, config.state_dir)

    def test_dashboard_artifact_is_readable_while_state_stays_separate(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            config = replace(settings(), state_dir=root / "private-state", web_root=root / "web")
            output = write_dashboard_data(config)
            self.assertEqual(S_IMODE(output.stat().st_mode), 0o644)
            self.assertNotEqual(output.parent, config.state_dir)

    def test_notifications_are_deduplicated_and_disabled_by_default(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            config = replace(settings(), state_dir=Path(directory), telegram_enabled=False)
            ledger = {"bets": [{"bet_id": "a", "status": "PENDING", "home": "A", "away": "B", "label": "x", "odds": 2}]}
            with patch("crown.notify._send") as sender:
                self.assertEqual(notify_new(ledger, config), 1)
                self.assertEqual(notify_new(ledger, config), 0)
                sender.assert_called_once()

    def test_crown_notification_uses_selected_team_handicap_view(self) -> None:
        home = {"market": "HDC", "side": "H", "line": -0.25, "home": "主隊", "away": "客隊"}
        away = {"market": "HDC", "side": "A", "line": 0.25, "home": "主隊", "away": "客隊"}
        self.assertEqual(_bet_label(home), "讓球 · 主隊 -0/0.5")
        self.assertEqual(_bet_label(away), "讓球 · 客隊 -0/0.5")


if __name__ == "__main__":
    unittest.main()
