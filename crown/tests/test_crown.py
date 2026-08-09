from __future__ import annotations

import gzip
import json
import tempfile
import unittest
from stat import S_IMODE
from dataclasses import replace
from datetime import datetime, timedelta
from pathlib import Path
from unittest.mock import patch

from crown.config import settings
from crown.dashboard_data import build, write_dashboard_data
from crown.engine import _candidates, _fresh, _wdl_prediction
from crown.hkjc import fetch_official_results
from crown.ledger import completed_stages, recompute_stats, stage_for, sync_prediction
from crown.lines import parse_hkjc_handicap, parse_hkjc_total, settle_handicap, settle_total
from crown.matching import (
    Event, bridge_titan_to_pinnapi, canonical_league_key, canonical_team_key, match_event,
    normalize_name, qualifiers, same_event_for_hkjc, same_identity_for_hkjc,
)
from crown.notify import _bet_label, notify_new
from crown.pinnapi import parse_fixtures, parse_lines
from crown.period import in_current_period, is_upcoming_in_current_period, period_bounds
from crown.prediction_history import archive_watch, grade_history
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

    def test_name_normalization_does_not_corrupt_embedded_club_tokens(self) -> None:
        self.assertEqual(normalize_name("Manchester City FC"), "manchestercity")
        self.assertEqual(normalize_name("Racing Club"), "racing")
        self.assertEqual(normalize_name("Vasco SC"), "vasco")
        self.assertEqual(normalize_name("曼彻斯特城(中)"), "曼彻斯特城")
        self.assertEqual(normalize_name("FC悉尼"), "悉尼")
        self.assertEqual(normalize_name("枥木市FC"), "枥木市")

    def test_reviewed_hong_kong_team_aliases_canonicalize(self) -> None:
        self.assertEqual(canonical_team_key("曼城"), canonical_team_key("曼彻斯特城"))
        self.assertEqual(canonical_team_key("飛燕諾"), canonical_team_key("费耶诺德"))
        self.assertEqual(canonical_team_key("阿仙奴"), canonical_team_key("阿森纳"))
        self.assertEqual(canonical_team_key("加爾斯"), canonical_team_key("哥德堡盖斯"))
        self.assertEqual(
            __import__("crown.matching", fromlist=["similarity"]).similarity(
                canonical_team_key("阿仙奴"), canonical_team_key("車路士")
            ),
            0.0,
        )
        for hkjc, crown in (
            ("競技體育會", "巴西竞技"),
            ("基斯奧馬", "克里丘马"),
            ("國際杜古", "图尔库国际"),
            ("拉迪", "拉赫蒂"),
            ("拿根亞", "桑托斯拉古纳"),
            ("CF 阿美利加", "墨西哥美洲(中)"),
        ):
            self.assertEqual(canonical_team_key(hkjc), canonical_team_key(crown))
        self.assertEqual(canonical_league_key("U20中北美錦標賽"), canonical_league_key("美青杯"))

    def test_near_exact_names_allow_unique_reschedule_but_not_fuzzy_match(self) -> None:
        shifted = Event("shifted", "League", "Alpha FC", "Beta FC", self.now + timedelta(minutes=60))
        self.assertEqual(match_event(self.target, [shifted]).event.id, "shifted")
        fuzzy = Event("fuzzy", "League", "Alfa", "Beto", self.now + timedelta(minutes=60))
        self.assertIsNone(match_event(self.target, [fuzzy]).event)

    def test_reschedule_fallback_rejects_ambiguous_and_reversed_hkjc_candidates(self) -> None:
        first = Event("one", "League", "Alpha", "Beta", self.now + timedelta(minutes=60),
                      {"home_team_id": "1", "away_team_id": "2"})
        second = Event("two", "League", "Alpha", "Beta", self.now + timedelta(minutes=61),
                       {"home_team_id": "1", "away_team_id": "2"})
        self.assertEqual(same_event_for_hkjc(self.target, [first, second]).reason, "ambiguous_candidate")
        reversed_row = Event("reverse", "League", "Beta", "Alpha", self.now,
                             {"home_team_id": "2", "away_team_id": "1"})
        self.assertIsNone(same_event_for_hkjc(self.target, [reversed_row]).event)
        identity = same_identity_for_hkjc(self.target, [reversed_row])
        self.assertEqual(identity.event.id, "reverse")
        self.assertTrue(identity.reversed)
        shifted = replace(reversed_row, id="shifted-reverse", kickoff=self.now + timedelta(minutes=11))
        self.assertEqual(
            same_identity_for_hkjc(self.target, [shifted]).reason,
            "no_exact_reversed_identity",
        )
        second_reverse = replace(reversed_row, id="second-reverse")
        self.assertEqual(
            same_identity_for_hkjc(self.target, [reversed_row, second_reverse]).reason,
            "ambiguous_reversed_identity",
        )

    def test_reversed_hkjc_identity_never_unlocks_pinnapi_pricing(self) -> None:
        titan = Event("titan", "美青杯", "美国U20", "墨西哥U20", self.now)
        hkjc = Event(
            "hkjc", "U20中北美錦標賽", "墨西哥U20", "美國U20", self.now,
            {"home_team_id": "11", "away_team_id": "22", "home_en": "Mexico U20",
             "away_en": "United States U20", "league_en": "CONCACAF U20 Championship"},
        )
        pinnapi = Event(
            "pin", "CONCACAF U20 Championship", "United States U20", "Mexico U20", self.now
        )
        bridge = bridge_titan_to_pinnapi(titan, [hkjc], [pinnapi])
        self.assertEqual(bridge.path, "hkjc_reversed_identity_only")
        self.assertEqual(bridge.hkjc.event.id, "hkjc")
        self.assertTrue(bridge.reversed)
        self.assertIsNone(bridge.event)
        self.assertEqual(bridge.reason, "hkjc_orientation_reversed_unpriced")

    def test_reviewed_20260809_fixture_batch_preserves_orientation_gate(self) -> None:
        direct = (
            (("巴西乙", "巴西竞技", "克里丘马"),
             ("巴西乙組聯賽", "競技體育會", "基斯奧馬")),
            (("芬超", "图尔库国际", "拉赫蒂"),
             ("芬蘭超級聯賽", "國際杜古", "拉迪")),
            (("中北美杯", "芝加哥火焰", "桑托斯拉古纳"),
             ("北美聯賽盃", "芝加哥火燄", "拿根亞")),
        )
        for number, (titan_row, hkjc_row) in enumerate(direct):
            titan = Event(f"t{number}", *titan_row, self.now)
            hkjc = Event(
                f"h{number}", *hkjc_row, self.now,
                {"home_team_id": f"{number}h", "away_team_id": f"{number}a"},
            )
            matched = same_event_for_hkjc(titan, [hkjc])
            self.assertEqual(matched.event.id, hkjc.id)
            self.assertFalse(matched.reversed)

        reversed_rows = (
            (("美青杯", "美国U20", "墨西哥U20"),
             ("U20中北美錦標賽", "墨西哥U20", "美國U20")),
            (("中北美杯", "墨西哥美洲(中)", "波特兰伐木者"),
             ("北美聯賽盃", "波特蘭伐木者", "CF 阿美利加")),
        )
        for number, (titan_row, hkjc_row) in enumerate(reversed_rows):
            titan = Event(f"rt{number}", *titan_row, self.now)
            hkjc = Event(
                f"rh{number}", *hkjc_row, self.now,
                {"home_team_id": f"r{number}h", "away_team_id": f"r{number}a"},
            )
            self.assertIsNone(same_event_for_hkjc(titan, [hkjc]).event)
            identity = same_identity_for_hkjc(titan, [hkjc])
            self.assertEqual(identity.event.id, hkjc.id)
            self.assertTrue(identity.reversed)

    def test_club_names_containing_youth_word_are_not_misclassified_as_reserves(self) -> None:
        juventude = Event("j", "巴西乙", "路禾利桑天奴", "青年人", self.now)
        argentinos = Event("a", "阿甲", "阿根廷青年人", "竞技俱乐部", self.now)
        youth_team = Event("y", "荷乙", "燕豪芬青年隊", "禾寧丹", self.now)
        self.assertNotIn("reserve", qualifiers(juventude))
        self.assertNotIn("reserve", qualifiers(argentinos))
        self.assertIn("reserve", qualifiers(youth_team))

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
        self.assertEqual(parsed["timestamp_basis"], "provider")
        self.assertEqual({(p["market"], p["line"], p["selection"]) for p in parsed["prices"]},
                         {("HDC", -0.25, "H"), ("HDC", -0.25, "A"), ("HIL", 2.75, "H"), ("HIL", 2.75, "L")})
        non_full = parse_lines({"event_id": "p1", "periods": {"num_1": {"totals": []}}})
        self.assertEqual(non_full["prices"], [])

    def test_pinnapi_missing_provider_timestamp_is_audited_as_response_observed(self) -> None:
        parsed = parse_lines({
            "event_id": "p1",
            "periods": {"num_0": {
                "spreads": [{"hdp": -0.25, "home": 1.9, "away": 2.0}],
                "totals": [{"points": 2.75, "over": 1.95, "under": 1.95}],
            }},
        }, "p1", observed_at=1786248001)
        self.assertTrue(parsed["timestamp_inferred"])
        self.assertEqual(parsed["timestamp_basis"], "response_observed")
        self.assertEqual(parsed["source_at"], 1786248001)

    def test_response_observed_timestamp_requires_explicit_policy_switch(self) -> None:
        crown = [
            {"market": "HDC", "line": -0.25, "selection": "H", "odds": 2.1, "source_at": 1000},
        ]
        reference = [
            {"market": "HDC", "line": -0.25, "selection": "H", "odds": 1.9, "source_at": 1000},
            {"market": "HDC", "line": -0.25, "selection": "A", "odds": 2.0, "source_at": 1000},
        ]
        closed = replace(settings(), source_max_age_seconds=90, allow_inferred_pinnapi_timestamp=False)
        candidates, reasons = _candidates(crown, reference, closed, 1001, True)
        self.assertEqual(candidates, [])
        self.assertEqual(reasons, ["pinnapi_source_timestamp_missing"])
        approved = replace(closed, allow_inferred_pinnapi_timestamp=True)
        candidates, reasons = _candidates(crown, reference, approved, 1001, True)
        self.assertEqual(len(candidates), 1)
        self.assertEqual(reasons, [])

    def test_wdl_prediction_uses_complete_no_vig_moneyline(self) -> None:
        view = _wdl_prediction([
            {"market": "1X2", "selection": "H", "odds": 2.0},
            {"market": "1X2", "selection": "D", "odds": 4.0},
            {"market": "1X2", "selection": "A", "odds": 4.0},
        ])
        self.assertEqual(view["forecast"], "主勝")
        self.assertAlmostEqual(view["outcome"]["home"], 0.5)
        self.assertAlmostEqual(sum(view["outcome"].values()), 1.0)
        self.assertIsNone(_wdl_prediction([
            {"market": "1X2", "selection": "H", "odds": 2.0},
        ])["forecast"])

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

    def test_matching_version_refreshes_only_stale_first_look(self) -> None:
        old_first = {
            "matching_version": "old",
            "stages": [{"stage": "首預"}],
        }
        old_late = {
            "matching_version": "old",
            "stages": [{"stage": "首預"}, {"stage": "T-30"}],
        }
        current_first = {
            "matching_version": "current",
            "stages": [{"stage": "首預"}],
        }
        self.assertEqual(completed_stages(old_first, "current"), set())
        self.assertEqual(completed_stages(old_late, "current"), {"首預", "T-30"})
        self.assertEqual(completed_stages(current_first, "current"), {"首預"})

    def test_empty_tick_merge_retains_sweep_prediction_and_prunes_only_stale(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            config = replace(settings(), state_dir=Path(directory))
            now = self.now
            sweep = {"match_id": "future", "kickoff_hkt": (now + timedelta(hours=3)).isoformat(),
                     "stage": "首預", "status": "DATA_MISSING"}
            stale = {"match_id": "old", "kickoff_hkt": (now - timedelta(hours=3)).isoformat(),
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

    def test_crown_period_runs_from_1200_to_next_1159_and_hides_started_matches(self) -> None:
        start, end = period_bounds(self.now)
        self.assertEqual(start.isoformat(), "2026-08-09T12:00:00+08:00")
        self.assertEqual(end.isoformat(), "2026-08-10T11:59:59+08:00")
        future = datetime(2026, 8, 9, 12, 5, tzinfo=start.tzinfo)
        started = datetime(2026, 8, 9, 12, 0, tzinfo=start.tzinfo)
        self.assertTrue(in_current_period(started, self.now))
        self.assertTrue(in_current_period(end, self.now))
        self.assertTrue(is_upcoming_in_current_period(future, self.now))
        self.assertFalse(is_upcoming_in_current_period(started, self.now))

    def test_dashboard_artifact_is_readable_while_state_stays_separate(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            config = replace(settings(), state_dir=root / "private-state", web_root=root / "web")
            output = write_dashboard_data(config)
            self.assertEqual(S_IMODE(output.stat().st_mode), 0o644)
            self.assertNotEqual(output.parent, config.state_dir)

    def test_dashboard_includes_persisted_prediction_history(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            config = replace(settings(), state_dir=root / "private-state", web_root=root / "web")
            config.state_dir.mkdir(parents=True)
            (config.state_dir / "prediction_history.json").write_text(
                '{"rows":[{"match_id":"old","stage":"T-5"}],"stats":{"predictions":1}}',
                encoding="utf-8",
            )
            payload = build(config)
            self.assertEqual(payload["prediction_history"]["rows"][0]["match_id"], "old")
            self.assertEqual(payload["prediction_history"]["stats"]["predictions"], 1)

    def test_prediction_history_archives_no_bet_stages_idempotently(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            config = replace(settings(), state_dir=Path(directory))
            ledger = {"watch": {"x": {
                "match_id": "x", "league": "L", "home": "A", "away": "B",
                "kickoff": "2026-08-09T12:05:00+08:00", "titan_match_id": "x",
                "stages": [{
                    "match_id": "x", "stage": "T-30", "ts": "2026-08-09T11:35:00+08:00",
                    "forecast": "主勝", "probability": .55,
                    "outcome": {"home": .55, "draw": .25, "away": .20},
                    "prediction_source": "pinnapi_1x2_no_vig",
                    "pick": None, "no_bet_reason": "資訊階段",
                }],
            }}}
            first = archive_watch(config, ledger)
            second = archive_watch(config, ledger)
            self.assertEqual(len(first["rows"]), 1)
            self.assertEqual(len(second["rows"]), 1)
            self.assertFalse(second["rows"][0]["simulated_bet"])
            self.assertEqual(second["stats"]["predictions"], 1)

    def test_prediction_history_grades_non_bet_by_verified_titan_identity(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            config = replace(settings(), state_dir=Path(directory))
            kickoff = datetime.now(self.now.tzinfo) - timedelta(hours=3)
            ledger = {"watch": {"x": {
                "match_id": "x", "league": "League", "home": "Alpha FC", "away": "Beta FC",
                "kickoff": kickoff.isoformat(), "titan_match_id": "x",
                "stages": [{
                    "match_id": "x", "stage": "T-30", "ts": (kickoff - timedelta(minutes=30)).isoformat(),
                    "forecast": "主勝", "probability": .60,
                    "outcome": {"home": .60, "draw": .25, "away": .15},
                    "pick": None,
                }],
            }}}
            archive_watch(config, ledger)
            result = {
                "id": "x", "league": "League", "home": "Alpha FC", "away": "Beta FC",
                "kickoff": kickoff, "home_score": 2, "away_score": 1,
            }
            with patch("crown.prediction_history.TitanClient.results", return_value=[result]), \
                 patch("crown.prediction_history.fetch_official_result_events", return_value=[]):
                history = grade_history(config)
            row = history["rows"][0]
            self.assertEqual(row["actual"], "主勝")
            self.assertTrue(row["correct"])
            self.assertEqual(row["result_source"], "titan_verified_identity")
            self.assertEqual(history["stats"]["graded"], 1)
            self.assertIsNotNone(history["stats"]["brier"])

    def test_recompute_stats_preserves_recovered_bet_results(self) -> None:
        ledger = {
            "bankroll": 50000,
            "watch": {},
            "log": [],
            "stats": {"staking": {"label": "階段一 · 建立樣本", "slope": -2.8}},
            "bets": [
                {"status": "SETTLED", "market": "讓球", "stake": 1000, "pnl": 900,
                 "result": "Won", "home": "A", "away": "B", "settled_at": "2026-08-08T10:00:00Z"},
                {"status": "SETTLED", "market": "讓球", "stake": 500, "pnl": -500,
                 "result": "Lost", "home": "C", "away": "D", "settled_at": "2026-08-08T11:00:00Z"},
                {"status": "VOIDED", "market": "入球大小", "stake": 2000, "pnl": None},
            ],
        }
        stats = recompute_stats(ledger, settings())
        self.assertEqual(stats["n_settled"], 2)
        self.assertEqual(stats["n_voided"], 1)
        self.assertEqual(stats["turnover"], 1500)
        self.assertEqual(stats["pnl"], 400)
        self.assertEqual(stats["res_counts"]["Won"], 1)
        self.assertEqual(stats["by_market"]["讓球"]["n"], 2)
        self.assertEqual(len(stats["curve"]), 2)

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

    def test_hkjc_official_results_paginate_and_require_confirmed_full_time(self) -> None:
        class Response:
            def __init__(self, payload, compressed=False):
                raw = json.dumps(payload).encode("utf-8")
                self.raw = gzip.compress(raw) if compressed else raw

            def __enter__(self):
                return self

            def __exit__(self, *_):
                return False

            def read(self):
                return self.raw

        filler = [
            {"id": f"other-{i}", "status": "MATCHENDED", "results": []}
            for i in range(20)
        ]
        first = {
            "data": {
                "matchNumByDate": {"total": 21},
                "matches": filler,
            }
        }
        second = {
            "data": {
                "matchNumByDate": {"total": 21},
                "matches": [{
                    "id": "wanted",
                    "status": "MATCHENDED",
                    "results": [
                        {
                            "homeResult": "9",
                            "awayResult": "9",
                            "ttlCornerResult": "99",
                            "payoutConfirmed": False,
                            "stageId": 5,
                            "resultType": 1,
                            "sequence": 99,
                        },
                        {
                            "homeResult": "2",
                            "awayResult": "1",
                            "ttlCornerResult": "11",
                            "payoutConfirmed": True,
                            "stageId": 5,
                            "resultType": 1,
                            "sequence": 2,
                        },
                    ],
                }],
            }
        }
        with patch(
            "crown.hkjc.urllib.request.urlopen",
            side_effect=[Response(first, compressed=True), Response(second)],
        ) as opener:
            rows = fetch_official_results({"wanted"}, {"2026-08-09"})
        self.assertEqual(opener.call_count, 2)
        self.assertEqual(rows["wanted"]["home_score"], 2)
        self.assertEqual(rows["wanted"]["away_score"], 1)
        self.assertEqual(rows["wanted"]["corners_total"], 11)
        self.assertEqual(rows["wanted"]["source"], "hkjc_official")


if __name__ == "__main__":
    unittest.main()
