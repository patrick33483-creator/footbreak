from __future__ import annotations

import gzip
import json
import os
import shutil
import subprocess
import tempfile
import unittest
from stat import S_IMODE
from dataclasses import replace
from datetime import datetime, timedelta
from pathlib import Path
from unittest.mock import Mock, patch

from crown.config import settings
from crown.dashboard_data import build, write_dashboard_data
from crown.engine import _candidates, _crown_market_forecasts, _fixture_baseline_prediction, _fresh, _hkjc_chl_candidates, _hkjc_chl_forecasts, _prediction, _refresh_crown_quote, _skip_new_confirmed_empty_crown, _sweep_rows_with_due_existing, _tick_rows_from_predictions, _wdl_prediction
from crown.hkjc import fetch_official_results
from crown.ledger import (
    completed_stages,
    market_entry_thresholds,
    recompute_stats,
    stage_for,
    sync_prediction,
)
from crown.lines import parse_hkjc_handicap, parse_hkjc_total, settle_handicap, settle_total
from crown.matching import (
    BridgeMatch, Event, Match, bridge_titan_to_pinnapi, canonical_league_key, canonical_team_key, match_event,
    normalize_name, qualifiers, same_event_for_hkjc, same_identity_for_hkjc,
)
from crown.notify import _bet_label, notify_new
from crown.pinnapi import parse_fixtures, parse_lines
from crown.period import in_current_period, is_upcoming_in_current_period, period_bounds
from crown.prediction_history import (
    _persist_learning_exclusion,
    archive_watch,
    calculate_stats,
    grade_history,
)
from analysis.learning_store import LearningStore
from crown import settle as crown_settle
from crown.state import load_predictions, merge_predictions, save_predictions
from crown.titan import (
    crown_prices_from_pages,
    parse_crown_fixture_ids,
    parse_match_header,
    parse_match_statistics,
    parse_schedule_page,
)


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

    def test_titan_live_detail_corner_statistics_are_parsed_fail_closed(self) -> None:
        source = (
            '<script>var teamTvStatisticData = '
            '"0,5,2,71,29^2,2,1,67,33^11,56%,44%,56,44";</script>'
        )
        self.assertEqual(
            parse_match_statistics(source),
            {"corners_home": 5, "corners_away": 2, "corners_total": 7},
        )
        self.assertIsNone(parse_match_statistics("var teamTvStatisticData = '';"))
        self.assertIsNone(parse_match_statistics(
            'var teamTvStatisticData = "0,not-a-number,2,0,0";'
        ))

    def test_titan_completed_match_header_score_is_parsed_fail_closed(self) -> None:
        fields = [""] * 16
        fields[0], fields[1], fields[4] = "中央骏马", "南市台钢", "-1"
        fields[5], fields[10], fields[11], fields[15] = (
            "20260811110000", "2", "2", "亚挑联"
        )
        result = parse_match_header("^".join(fields), "3031468")
        self.assertEqual(result["home_score"], 2)
        self.assertEqual(result["away_score"], 2)
        fields[4] = "3"
        self.assertIsNone(parse_match_header("^".join(fields), "3031468"))

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

    def test_negative_ev_is_not_flattened_to_neutral_confidence(self) -> None:
        crown = [
            {"market": "HDC", "line": -0.25, "selection": "H", "odds": 1.80, "source_at": 1000},
        ]
        reference = [
            {"market": "HDC", "line": -0.25, "selection": "H", "odds": 1.90, "source_at": 1000},
            {"market": "HDC", "line": -0.25, "selection": "A", "odds": 2.00, "source_at": 1000},
        ]
        config = replace(settings(), source_max_age_seconds=90)
        candidates, _ = _candidates(crown, reference, config, 1001, False)
        self.assertLess(candidates[0]["ev"], 0)
        self.assertLess(candidates[0]["conviction"], 50)

    def test_hkjc_corner_candidate_uses_exact_pinnapi_line_and_is_not_crown_odds(self) -> None:
        config = replace(settings(), source_max_age_seconds=90, allow_inferred_pinnapi_timestamp=False)
        hkjc = [{
            "condition": "9.5",
            "status": "AVAILABLE",
            "odds": {"H": 2.20, "L": 1.70},
            "provider": "HKJC",
            "source": "hkjc_chl",
        }]
        pinnapi = [
            {"market": "CHL", "line": 9.5, "selection": "H", "odds": 1.90, "source_at": 1000},
            {"market": "CHL", "line": 9.5, "selection": "L", "odds": 2.00, "source_at": 1000},
        ]
        candidates, reasons = _hkjc_chl_candidates(hkjc, pinnapi, config, 1001, False)
        self.assertEqual(reasons, [])
        self.assertEqual(len(candidates), 2)
        candidate = next(row for row in candidates if row["side"] == "H")
        self.assertEqual(candidate["market"], "HKJC角球大細")
        self.assertEqual(candidate["label"], "HKJC角球大細 大 9.5")
        self.assertEqual(candidate["provider"], "HKJC")
        self.assertEqual(candidate["reference"], "pinnapi_corner_exact_full_match")
        self.assertNotIn("Crown", candidate["label"])

        no_match, no_match_reasons = _hkjc_chl_candidates(
            hkjc, pinnapi[:1], config, 1001, False
        )
        self.assertEqual(no_match, [])
        self.assertIn("no_complete_pinnapi_CHL_9.5", no_match_reasons)

    def test_hkjc_corner_forecast_uses_main_complete_line_without_ev(self) -> None:
        forecasts, reasons = _hkjc_chl_forecasts([
            {
                "condition": "9.5",
                "main": True,
                "odds": {"H": 1.97, "L": 1.74},
            },
            {
                "condition": "10.5",
                "main": False,
                "odds": {"H": 2.48, "L": 1.47},
            },
        ])
        self.assertEqual(reasons, [])
        self.assertEqual(len(forecasts), 1)
        forecast = forecasts[0]
        self.assertEqual(forecast["code"], "CHL")
        self.assertEqual(forecast["line"], 9.5)
        self.assertEqual(forecast["side"], "L")
        self.assertEqual(forecast["reference"], "hkjc_full_market_no_vig")
        self.assertTrue(forecast["forecast_only"])
        self.assertNotIn("ev", forecast)
        self.assertNotIn("kelly_raw", forecast)

    def test_hkjc_corner_candidate_fails_closed_for_stale_or_inferred_pinnapi_reference(self) -> None:
        config = replace(settings(), source_max_age_seconds=90, allow_inferred_pinnapi_timestamp=False)
        hkjc = [{"condition": "10", "odds": {"H": 1.95, "L": 1.95}}]
        stale = [
            {"market": "CHL", "line": 10, "selection": "H", "odds": 1.90, "source_at": 800},
            {"market": "CHL", "line": 10, "selection": "L", "odds": 2.00, "source_at": 800},
        ]
        candidates, reasons = _hkjc_chl_candidates(hkjc, stale, config, 1001, False)
        self.assertEqual(candidates, [])
        self.assertIn("pinnapi_corner_source_stale_CHL_10", reasons)
        candidates, reasons = _hkjc_chl_candidates(hkjc, stale, config, 1001, True)
        self.assertEqual(candidates, [])
        self.assertEqual(reasons, ["pinnapi_corner_source_timestamp_missing"])

    def test_corner_feed_failure_does_not_remove_crown_standard_candidate(self) -> None:
        config = replace(settings(), source_max_age_seconds=90, allow_inferred_pinnapi_timestamp=False)
        kickoff = self.now + timedelta(minutes=5)
        titan = {"id": "titan", "league": "L", "home": "A", "away": "B", "kickoff": kickoff}
        h_event = Event("hkjc", "L", "A", "B", kickoff, {"home_team_id": "h", "away_team_id": "a"})
        p_event = Event("pin", "L", "A", "B", kickoff)
        bridge = BridgeMatch(Match(h_event, False, 1.0, None), Match(p_event, False, 1.0, None),
                             "hkjc_bilingual_bridge", None)
        h_match = {"id": "hkjc", "foPools": []}
        titan_client = Mock()
        titan_client.crown_prices.return_value = [
            {"market": "HDC", "line": -0.25, "selection": "H", "odds": 2.20, "source_at": 1000},
        ]
        pinnapi_client = Mock()
        pinnapi_client.lines.return_value = {
            "prices": [
                {"market": "HDC", "line": -0.25, "selection": "H", "odds": 1.90, "source_at": 1000},
                {"market": "HDC", "line": -0.25, "selection": "A", "odds": 2.00, "source_at": 1000},
            ],
            "source_at": 1000, "timestamp_inferred": False, "timestamp_basis": "provider",
        }
        pinnapi_client.corner_lines.side_effect = RuntimeError("specials down")
        with patch("crown.engine.datetime") as mocked_datetime, \
                patch("crown.engine.time.time", return_value=1001), \
                patch("crown.engine._hkjc_chl", return_value=[
                    {"condition": "9.5", "odds": {"H": 1.9, "L": 1.9}}
                ]):
            mocked_datetime.now.return_value = self.now
            prediction = _prediction(titan, bridge, h_match, "T-5", config, titan_client, pinnapi_client)
        self.assertTrue(any(row["code"] == "HDC" for row in prediction["candidates"]))
        self.assertFalse(any(row["code"] == "CHL" for row in prediction["candidates"]))
        self.assertIn("pinnapi_corner_lines_unavailable_RuntimeError", prediction["corner_no_bet_reason"])

    def test_prediction_skips_corner_provider_when_hkjc_has_no_chl_market(self) -> None:
        config = replace(settings(), source_max_age_seconds=90)
        kickoff = self.now + timedelta(minutes=5)
        titan = {"id": "titan", "league": "L", "home": "A", "away": "B", "kickoff": kickoff}
        h_event = Event("hkjc", "L", "A", "B", kickoff, {"home_team_id": "h", "away_team_id": "a"})
        p_event = Event("pin", "L", "A", "B", kickoff)
        bridge = BridgeMatch(Match(h_event, False, 1.0, None), Match(p_event, False, 1.0, None),
                             "hkjc_bilingual_bridge", None)
        titan_client = Mock()
        titan_client.crown_prices.return_value = [
            {"market": "HDC", "line": -0.25, "selection": "H", "odds": 2.20, "source_at": 1000},
        ]
        pinnapi_client = Mock()
        pinnapi_client.lines.return_value = {
            "prices": [
                {"market": "HDC", "line": -0.25, "selection": "H", "odds": 1.90, "source_at": 1000},
                {"market": "HDC", "line": -0.25, "selection": "A", "odds": 2.00, "source_at": 1000},
            ],
            "source_at": 1000, "timestamp_inferred": False, "timestamp_basis": "provider",
        }
        with patch("crown.engine.datetime") as mocked_datetime, \
                patch("crown.engine.time.time", return_value=1001), \
                patch("crown.engine._hkjc_chl", return_value=[]):
            mocked_datetime.now.return_value = self.now
            _prediction(titan, bridge, {"id": "hkjc"}, "T-5", config, titan_client, pinnapi_client)
        pinnapi_client.corner_lines.assert_not_called()

    def test_crown_full_market_forecast_uses_central_complete_line_without_ev(self) -> None:
        config = replace(settings(), source_max_age_seconds=90)
        prices = [
            {"market": "HDC", "line": -0.25, "selection": "H", "odds": 1.80, "source_at": 1000},
            {"market": "HDC", "line": -0.25, "selection": "A", "odds": 2.05, "source_at": 1000},
            {"market": "HDC", "line": -0.75, "selection": "H", "odds": 2.60, "source_at": 1000},
            {"market": "HDC", "line": -0.75, "selection": "A", "odds": 1.45, "source_at": 1000},
            {"market": "HIL", "line": 2.5, "selection": "H", "odds": 1.90, "source_at": 1000},
            {"market": "HIL", "line": 2.5, "selection": "L", "odds": 2.00, "source_at": 1000},
        ]
        forecasts, reasons = _crown_market_forecasts(prices, config, 1001)
        self.assertEqual(reasons, [])
        hdc = next(row for row in forecasts if row["code"] == "HDC")
        self.assertEqual(hdc["line"], -0.25)
        self.assertEqual(hdc["side"], "H")
        self.assertTrue(hdc["forecast_only"])
        self.assertNotIn("ev", hdc)
        self.assertNotIn("kelly_raw", hdc)

    def test_unmapped_pinnapi_still_records_crown_forecast_but_never_bets(self) -> None:
        config = replace(settings(), source_max_age_seconds=90)
        kickoff = self.now + timedelta(minutes=5)
        titan = {"id": "titan", "league": "L", "home": "A", "away": "B", "kickoff": kickoff}
        bridge = BridgeMatch(
            Match(None, False, 0.0, "team_name_similarity_below_floor"),
            Match(None, False, 0.0, "no_candidate_in_kickoff_window"),
            None,
            "titan_to_hkjc:team_name_similarity_below_floor",
        )
        titan_client = Mock()
        titan_client.crown_prices.return_value = [
            {"market": "HDC", "line": -0.25, "selection": "H", "odds": 1.80, "source_at": 1000},
            {"market": "HDC", "line": -0.25, "selection": "A", "odds": 2.05, "source_at": 1000},
            {"market": "HIL", "line": 2.5, "selection": "H", "odds": 1.90, "source_at": 1000},
            {"market": "HIL", "line": 2.5, "selection": "L", "odds": 2.00, "source_at": 1000},
        ]
        pinnapi_client = Mock()
        with patch("crown.engine.datetime") as mocked_datetime, patch("crown.engine.time.time", return_value=1001):
            mocked_datetime.now.return_value = self.now
            prediction = _prediction(
                titan, bridge, None, "T-5", config, titan_client, pinnapi_client
            )
        self.assertEqual(prediction["status"], "PREDICTION_READY")
        self.assertEqual(prediction["verdict"], "已預測")
        self.assertEqual({row["code"] for row in prediction["forecast_candidates"]}, {"HDC", "HIL"})
        self.assertEqual(prediction["candidates"], [])
        self.assertIsNone(prediction["pick"])
        self.assertIsNone(prediction["shadow_pick"])
        self.assertFalse(prediction["sharp_reference_available"])
        self.assertIn("未過影子倉信念門檻", prediction["shadow_no_bet_reason"])
        self.assertEqual(prediction["edge_reference_status"], "unavailable")
        self.assertIn("不計算 EV", prediction["edge_reference_note"])
        pinnapi_client.lines.assert_not_called()

        ledger = {"bankroll": 50000, "bets": [], "watch": {}, "log": [], "stats": {}}
        self.assertEqual(sync_prediction(ledger, prediction, config), [])
        snapshot = ledger["watch"]["titan"]["stages"][0]
        self.assertEqual({row["code"] for row in snapshot["market_predictions"]}, {"HDC", "HIL"})
        self.assertEqual(ledger["bets"], [])
        self.assertEqual(ledger["shadow_bets"], [])

    def test_unmapped_pinnapi_keeps_hkjc_corner_forecast_without_creating_bet(self) -> None:
        config = replace(settings(), source_max_age_seconds=90)
        kickoff = self.now + timedelta(minutes=5)
        titan = {
            "id": "corner-unmapped", "league": "L", "home": "A",
            "away": "B", "kickoff": kickoff,
        }
        bridge = BridgeMatch(
            Match(Event("hkjc", "L", "A", "B", kickoff), False, 1.0, None),
            Match(None, False, 0.0, "team_name_similarity_below_floor"),
            "hkjc_bilingual_bridge",
            "hkjc_to_pinnapi:team_name_similarity_below_floor",
        )
        titan_client = Mock()
        titan_client.crown_prices.return_value = [
            {
                "market": "HDC", "line": -0.25, "selection": "H",
                "odds": 1.80, "source_at": 1000,
            },
            {
                "market": "HDC", "line": -0.25, "selection": "A",
                "odds": 2.05, "source_at": 1000,
            },
        ]
        pinnapi_client = Mock()
        with patch("crown.engine.datetime") as mocked_datetime, \
                patch("crown.engine.time.time", return_value=1001), \
                patch("crown.engine._hkjc_chl", return_value=[{
                    "condition": "9.5",
                    "main": True,
                    "odds": {"H": 1.97, "L": 1.74},
                    "provider": "HKJC",
                    "source": "hkjc_chl",
                }]):
            mocked_datetime.now.return_value = self.now
            prediction = _prediction(
                titan, bridge, {"id": "hkjc"}, "T-5", config,
                titan_client, pinnapi_client,
            )
        corner = next(
            row for row in prediction["forecast_candidates"]
            if row["code"] == "CHL"
        )
        self.assertEqual(corner["reference"], "hkjc_full_market_no_vig")
        self.assertTrue(corner["forecast_only"])
        self.assertNotIn("ev", corner)
        self.assertEqual(prediction["candidates"], [])
        self.assertIsNone(prediction["pick"])
        pinnapi_client.lines.assert_not_called()
        pinnapi_client.corner_lines.assert_not_called()

        ledger = {
            "bankroll": 50000, "bets": [], "shadow_bets": [],
            "watch": {}, "log": [], "stats": {},
        }
        self.assertEqual(sync_prediction(ledger, prediction, config), [])
        snapshot = ledger["watch"]["corner-unmapped"]["stages"][0]
        stored = next(
            row for row in snapshot["market_predictions"]
            if row["code"] == "CHL"
        )
        self.assertEqual(stored["source"], "hkjc_full_market_no_vig")
        self.assertEqual(ledger["bets"], [])
        self.assertEqual(ledger["shadow_bets"], [])

    def test_empty_current_crown_uses_last_quote_for_forecast_only_and_never_bets(self) -> None:
        config = replace(settings(), source_max_age_seconds=90)
        kickoff = self.now + timedelta(minutes=5)
        titan = {"id": "titan", "league": "L", "home": "A", "away": "B", "kickoff": kickoff}
        bridge = BridgeMatch(
            Match(None, False, 0.0, "no_candidate_in_kickoff_window"),
            Match(None, False, 0.0, "no_candidate_in_kickoff_window"),
            None,
            "direct_same_script:no_candidate_in_kickoff_window",
        )
        previous = [
            {"market": "HDC", "line": -0.25, "selection": "H", "odds": 1.80, "source_at": 100},
            {"market": "HDC", "line": -0.25, "selection": "A", "odds": 2.05, "source_at": 100},
            {"market": "HIL", "line": 2.5, "selection": "H", "odds": 1.90, "source_at": 100},
            {"market": "HIL", "line": 2.5, "selection": "L", "odds": 2.00, "source_at": 100},
        ]
        titan_client = Mock()
        titan_client.crown_prices.return_value = []
        pinnapi_client = Mock()
        with patch("crown.engine.datetime") as mocked_datetime, \
                patch("crown.engine.time.time", return_value=1001), \
                patch("crown.engine._hkjc_chl", return_value=[{
                    "condition": "9.5",
                    "main": True,
                    "odds": {"H": 1.97, "L": 1.74},
                    "provider": "HKJC",
                    "source": "hkjc_chl",
                }]):
            mocked_datetime.now.return_value = self.now
            prediction = _prediction(
                titan, bridge, {"id": "hkjc"}, "T-5", config,
                titan_client, pinnapi_client,
                crown_snapshot={"prices": [], "asian_ok": True, "total_ok": True},
                previous_crown_prices=previous,
            )
        self.assertEqual(prediction["status"], "PREDICTION_READY")
        self.assertEqual(prediction["verdict"], "已預測")
        self.assertTrue(prediction["crown_quote_cached_forecast_only"])
        self.assertEqual(
            {row["code"] for row in prediction["forecast_candidates"]},
            {"HDC", "HIL", "CHL"},
        )
        self.assertEqual(prediction["book_odds"]["crown"], previous)
        self.assertEqual(prediction["candidates"], [])
        self.assertIsNone(prediction["pick"])
        self.assertIn("禁止計算 edge 及投注", prediction["no_bet_reason"])
        pinnapi_client.lines.assert_not_called()

    def test_sweep_skips_confirmed_empty_crown_only_without_existing_card(self) -> None:
        empty = {"prices": [], "asian_ok": True, "total_ok": True}
        self.assertTrue(_skip_new_confirmed_empty_crown(empty, None))
        self.assertFalse(_skip_new_confirmed_empty_crown(empty, {
            "match_id": "existing",
            "book_odds": {"crown": [{"market": "HDC", "odds": 1.8}]},
        }))
        self.assertFalse(_skip_new_confirmed_empty_crown({
            "prices": [], "asian_ok": False, "total_ok": True,
        }, None))

    def test_missing_pinnapi_mapping_never_blocks_valid_crown_forecast(self) -> None:
        config = replace(settings(), source_max_age_seconds=90)
        kickoff = self.now + timedelta(minutes=5)
        titan = {"id": "titan", "league": "L", "home": "A", "away": "B", "kickoff": kickoff}
        bridge = BridgeMatch(
            Match(None, False, 0.0, "no_candidate_in_kickoff_window"),
            Match(None, False, 0.0, "no_candidate_in_kickoff_window"),
            "none",
            "titan_to_hkjc:no_candidate_in_kickoff_window;"
            "direct_same_script:no_candidate_in_kickoff_window",
        )
        crown = [
            {"market": "HDC", "line": -0.25, "selection": "H", "odds": 1.80, "source_at": 1000},
            {"market": "HDC", "line": -0.25, "selection": "A", "odds": 2.05, "source_at": 1000},
            {"market": "HIL", "line": 2.5, "selection": "H", "odds": 1.90, "source_at": 1000},
            {"market": "HIL", "line": 2.5, "selection": "L", "odds": 2.00, "source_at": 1000},
        ]
        titan_client = Mock()
        pinnapi_client = Mock()
        with patch("crown.engine.datetime") as mocked_datetime, patch("crown.engine.time.time", return_value=1001):
            mocked_datetime.now.return_value = self.now
            prediction = _prediction(
                titan, bridge, None, "T-5", config, titan_client, pinnapi_client,
                crown_snapshot={"prices": crown},
            )
        self.assertEqual(prediction["status"], "PREDICTION_READY")
        self.assertEqual(prediction["verdict"], "已預測")
        self.assertEqual({row["code"] for row in prediction["forecast_candidates"]}, {"HDC", "HIL"})
        self.assertEqual(prediction["edge_reference_status"], "unavailable")
        self.assertIn("不計算 EV", prediction["edge_reference_note"])
        self.assertIn("未過影子倉信念門檻", prediction["shadow_no_bet_reason"])
        self.assertEqual(prediction["candidates"], [])
        self.assertIsNone(prediction["pick"])
        self.assertIsNone(prediction["shadow_pick"])
        pinnapi_client.lines.assert_not_called()

    def test_missing_pinnapi_mapping_creates_isolated_confidence_shadow_bet(self) -> None:
        config = replace(settings(), source_max_age_seconds=90, bankroll=50000)
        kickoff = self.now + timedelta(minutes=5)
        titan = {"id": "titan", "league": "L", "home": "Alpha", "away": "Beta", "kickoff": kickoff}
        bridge = BridgeMatch(
            Match(None, False, 0.0, "no_candidate_in_kickoff_window"),
            Match(None, False, 0.0, "no_candidate_in_kickoff_window"),
            "none",
            "direct_same_script:no_candidate_in_kickoff_window",
        )
        crown = [
            {"market": "HDC", "line": -0.25, "selection": "H", "odds": 1.40, "source_at": 1000},
            {"market": "HDC", "line": -0.25, "selection": "A", "odds": 3.00, "source_at": 1000},
            {"market": "HIL", "line": 2.5, "selection": "H", "odds": 1.90, "source_at": 1000},
            {"market": "HIL", "line": 2.5, "selection": "L", "odds": 2.00, "source_at": 1000},
        ]
        pinnapi_client = Mock()
        with patch("crown.engine.datetime") as mocked_datetime, patch("crown.engine.time.time", return_value=1001):
            mocked_datetime.now.return_value = self.now
            prediction = _prediction(
                titan, bridge, None, "T-5", config, Mock(), pinnapi_client,
                crown_snapshot={"prices": crown},
            )
        self.assertEqual(prediction["status"], "PREDICTION_READY")
        self.assertEqual(prediction["verdict"], "已預測")
        self.assertIsNone(prediction["pick"])
        self.assertEqual(prediction["shadow_status"], "SHADOW_READY")
        self.assertTrue(prediction["shadow_pick"]["confidence_only"])
        self.assertTrue(prediction["shadow_pick"]["shadow_only"])
        self.assertIsNone(prediction["shadow_pick"]["ev"])
        self.assertEqual(prediction["shadow_pick"]["stake"], 1000)
        pinnapi_client.lines.assert_not_called()

        ledger = {"bankroll": 50000, "bets": [], "watch": {}, "log": [], "stats": {}}
        created = sync_prediction(ledger, prediction, config)
        self.assertEqual(created, [])
        self.assertEqual(ledger["bets"], [])
        self.assertEqual(len(ledger["shadow_bets"]), 1)
        shadow = ledger["shadow_bets"][0]
        self.assertEqual(shadow["portfolio"], "shadow")
        self.assertTrue(shadow["confidence_only"])
        self.assertIsNone(shadow["ev"])
        self.assertEqual(shadow["stake"], 1000)
        self.assertEqual(sync_prediction(ledger, prediction, config), [])
        self.assertEqual(len(ledger["shadow_bets"]), 1)
        official_stats = recompute_stats(ledger, config)
        self.assertEqual(official_stats["n_pending"], 0)
        self.assertEqual(ledger["shadow_stats"]["n_pending"], 1)

    def test_shadow_results_never_change_official_stats_or_entry_learning(self) -> None:
        config = replace(settings(), bankroll=50000, min_edge=0.02, confidence_floor=58)
        official = [{
            "status": "SETTLED", "code": "HIL", "market": "HIL", "stake": 100,
            "pnl": -100, "result": "Lost", "model_prob": 0.60,
        } for _ in range(29)]
        shadow = [{
            "status": "SETTLED", "code": "HIL", "market": "HIL", "stake": 1000,
            "pnl": 900, "result": "Won", "model_prob": 0.70,
            "portfolio": "shadow", "shadow_only": True,
        } for _ in range(20)]
        ledger = {
            "bankroll": 50000, "bets": official, "shadow_bets": shadow,
            "watch": {}, "log": [], "stats": {}, "shadow_stats": {},
        }
        stats = recompute_stats(ledger, config)
        self.assertEqual(stats["n_settled"], 29)
        self.assertEqual(stats["pnl"], -2900)
        self.assertEqual(ledger["shadow_stats"]["n_settled"], 20)
        self.assertEqual(ledger["shadow_stats"]["pnl"], 18000)
        policy = market_entry_thresholds(ledger, "HIL", config)
        self.assertEqual(policy["n_settled"], 29)
        self.assertEqual(policy["reason"], "insufficient_market_sample")

    def test_shadow_comparison_uses_only_official_bets_from_first_shadow_bet(self) -> None:
        config = settings()
        official = [
            {
                "bet_id": "old", "status": "SETTLED", "result": "Lost",
                "stake": 1000, "pnl": -1000,
                "created_at": "2026-08-10T09:00:00+08:00",
            },
            {
                "bet_id": "same-period", "status": "SETTLED", "result": "Won",
                "stake": 1000, "pnl": 900,
                "created_at": "2026-08-11T10:30:00+08:00",
            },
        ]
        shadow = [{
            "bet_id": "shadow", "status": "SETTLED", "result": "Won",
            "stake": 1000, "pnl": 800,
            # Legacy rows without an offset are Hong Kong local time.
            "created_at": "2026-08-11T10:00:00",
        }]
        ledger = {
            "bankroll": 50000, "bets": official, "shadow_bets": shadow,
            "watch": {}, "log": [], "stats": {}, "shadow_stats": {},
        }
        recompute_stats(ledger, config)
        comparison = ledger["shadow_stats"]["comparison"]
        self.assertEqual(comparison["definition"], "from_first_shadow_bet")
        self.assertEqual(comparison["official_total_bets"], 1)
        self.assertEqual(comparison["shadow_total_bets"], 1)
        self.assertEqual(comparison["official"]["pnl"], 900)
        self.assertEqual(comparison["shadow"]["pnl"], 800)

    def test_market_entry_thresholds_wait_for_thirty_samples_then_tighten(self) -> None:
        config = replace(settings(), min_edge=0.02, confidence_floor=58)
        losing = [{
            "status": "SETTLED", "code": "HIL", "stake": 100,
            "pnl": -100, "result": "Lost", "model_prob": 0.60,
        } for _ in range(30)]
        ledger = {"bets": losing[:29]}
        small = market_entry_thresholds(ledger, "HIL", config)
        self.assertEqual(small["min_edge"], 0.02)
        self.assertEqual(small["confidence_floor"], 58)
        self.assertEqual(small["reason"], "insufficient_market_sample")

        ledger["bets"] = losing
        tightened = market_entry_thresholds(ledger, "HIL", config)
        self.assertEqual(tightened["min_edge"], 0.04)
        self.assertEqual(tightened["confidence_floor"], 62)
        self.assertEqual(tightened["reason"], "severe_market_underperformance")

    def test_every_crown_fixture_gets_scoreable_prediction_without_any_quote(self) -> None:
        config = replace(settings(), source_max_age_seconds=90)
        kickoff = self.now + timedelta(minutes=30)
        titan = {
            "id": "crown-only", "league": "Crown League",
            "home": "Home", "away": "Away", "kickoff": kickoff,
        }
        bridge = BridgeMatch(
            Match(None, False, 0.0, "no_candidate_in_kickoff_window"),
            Match(None, False, 0.0, "no_candidate_in_kickoff_window"),
            "none", "no_reference",
        )
        titan_client = Mock()
        titan_client.crown_prices.return_value = []
        pinnapi_client = Mock()
        with patch("crown.engine.datetime") as mocked_datetime:
            mocked_datetime.now.return_value = self.now
            prediction = _prediction(
                titan, bridge, None, "首預", config, titan_client, pinnapi_client
            )
        self.assertEqual(prediction["status"], "PREDICTION_READY")
        self.assertEqual(prediction["verdict"], "已預測")
        self.assertIn(prediction["forecast"], {"主勝", "和", "客勝"})
        self.assertGreater(prediction["probability"], 0)
        self.assertTrue(prediction["baseline_low_confidence"])
        self.assertEqual(prediction["prediction_source"], "fixture_prior_low_confidence_v1")
        self.assertIsNone(prediction["no_bet_reason"])
        self.assertEqual(prediction["candidates"], [])
        self.assertIsNone(prediction["pick"])
        pinnapi_client.lines.assert_not_called()

        ledger = {"bankroll": 50000, "bets": [], "watch": {}, "log": [], "stats": {}}
        self.assertEqual(sync_prediction(ledger, prediction, config), [])
        snapshot = ledger["watch"]["crown-only"]["stages"][0]
        self.assertEqual(snapshot["stage"], "首預")
        self.assertEqual(snapshot["status"], "PREDICTION_READY")
        self.assertEqual(snapshot["forecast"], prediction["forecast"])
        self.assertTrue(snapshot["baseline_low_confidence"])
        self.assertEqual(snapshot["prediction_era"], "2026-08-12-hkjc-corner-forecast-v4")
        self.assertEqual(ledger["bets"], [])

    def test_fixture_baseline_uses_crown_handicap_direction_when_available(self) -> None:
        home = _fixture_baseline_prediction([{"code": "HDC", "side": "H"}])
        away = _fixture_baseline_prediction([{"code": "HDC", "side": "A"}])
        self.assertEqual(home["forecast"], "主勝")
        self.assertEqual(away["forecast"], "客勝")
        self.assertTrue(home["baseline_low_confidence"])
        self.assertTrue(away["baseline_low_confidence"])

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

    def test_titan_crown_parser_prefers_visible_current_odds(self) -> None:
        html = """<tr data-id='3'>
          <td oddstype='wholeLastOdds' style='display: none;'>0.02</td>
          <td oddstype='wholeLastOdds' goals='-0.25' style='display: none;'>x</td>
          <td oddstype='wholeLastOdds' style='display: none;'>7.14</td>
          <td oddstype='wholeOdds'>0.96</td>
          <td oddstype='wholeOdds' goals='-0.25'>x</td>
          <td oddstype='wholeOdds'>0.92</td>
        </tr>"""
        prices = crown_prices_from_pages(html, None, "3", observed_at=100)
        self.assertEqual([(row["line"], row["odds"]) for row in prices], [(0.25, 1.96), (0.25, 1.92)])

    def test_titan_schedule_includes_rows_revealed_by_crown_filter(self) -> None:
        source = """
        <tr sId="123"><td>瑞士超</td><td>08-09 20:00</td><td>未</td>
          <td>洛桑</td><td>-</td><td>年青人</td><td></td></tr>
        <tr style="display: none;" sId="999"><td>隱藏聯賽</td><td>08-09 21:00</td><td>未</td>
          <td>隱藏主隊</td><td>-</td><td>隱藏客隊</td><td></td></tr>
        """
        rows = parse_schedule_page(source, "20260809")
        self.assertEqual([row["id"] for row in rows], ["123", "999"])

    def test_titan_company_feed_is_the_crown_fixture_filter(self) -> None:
        source = """<?xml version='1.0' encoding='UTF-8'?>
        <c><match><m>123,99,0,0.90,0.90</m></match>
        <ids>123,999,</ids><jcIds></jcIds><isMaintain>0</isMaintain></c>"""
        self.assertEqual(parse_crown_fixture_ids(source), {"123", "999"})

    def test_stage_windows_and_simulated_ledger_are_idempotent(self) -> None:
        self.assertEqual(stage_for(120, True, set()), "首預")
        self.assertIsNone(stage_for(120, True, {"首預"}))
        # A timed worker must recover a missing first look before it can
        # write a T-30/T-5-only fixture history.
        self.assertEqual(stage_for(30, False, set()), "首預")
        self.assertEqual(stage_for(30, False, {"首預"}), "T-30")
        self.assertEqual(stage_for(30, False, {"T-30"}), "首預")
        self.assertIsNone(stage_for(120, False, set()))
        self.assertEqual(stage_for(5, False, set()), "首預")
        self.assertEqual(stage_for(5, False, {"首預"}), "T-5")
        self.assertEqual(stage_for(0.1, False, {"首預"}), "T-5")
        self.assertIsNone(stage_for(0, False, set()))
        self.assertIsNone(stage_for(-0.1, False, set()))
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

    def test_first_discovery_timestamp_is_preserved_across_later_stages(self) -> None:
        config = settings()
        ledger = {"bankroll": 50000, "bets": [], "watch": {}, "log": [], "stats": {}}
        first = {
            "match_id": "discovery", "league": "L", "home": "A", "away": "B",
            "kickoff_hkt": "2026-08-09T14:00:00+08:00", "stage": "首預",
            "status": "PREDICTION_READY", "discovered_at": "2026-08-09T12:00:00+08:00",
        }
        sync_prediction(ledger, first, config)
        later = first | {
            "stage": "T-30", "discovered_at": "2026-08-09T13:30:00+08:00",
        }
        sync_prediction(ledger, later, config)
        self.assertEqual(
            ledger["watch"]["discovery"]["discovered_at"],
            "2026-08-09T12:00:00+08:00",
        )

    def test_data_missing_stage_remains_due_for_recovery_without_replaying_success(self) -> None:
        missing = {
            "matching_version": "same",
            "stages": [{"stage": "首預", "status": "DATA_MISSING"}],
        }
        self.assertEqual(completed_stages(missing, "same"), set())
        self.assertEqual(stage_for(120, True, completed_stages(missing, "same")), "首預")
        recovered = {
            "matching_version": "same",
            "stages": [{"stage": "首預", "status": "PREDICTION_READY"}],
        }
        self.assertEqual(completed_stages(recovered, "same"), {"首預"})
        self.assertIsNone(stage_for(120, True, completed_stages(recovered, "same")))

    def test_prediction_era_refreshes_only_first_look(self) -> None:
        first_only = {
            "matching_version": "same",
            "prediction_era": "old",
            "stages": [{"stage": "首預", "status": "PREDICTION_READY"}],
        }
        self.assertEqual(
            completed_stages(first_only, "same", "new"),
            set(),
        )
        with_late_stage = {
            **first_only,
            "stages": [
                {"stage": "首預", "status": "PREDICTION_READY"},
                {"stage": "T-30", "status": "PREDICTION_READY"},
            ],
        }
        self.assertEqual(
            completed_stages(with_late_stage, "same", "new"),
            {"首預", "T-30"},
        )

    def test_chl_simulation_stores_hkjc_provider_and_has_its_own_market_stats(self) -> None:
        config = settings()
        ledger = {"bankroll": 50000, "bets": [], "watch": {}, "log": [], "stats": {}}
        prediction = {
            "match_id": "corner", "league": "L", "home": "A", "away": "B",
            "kickoff_hkt": "2026-08-09T12:05:00+08:00", "stage": "T-5",
            "conviction": 70, "market_sources": {"CHL": "HKJC, not Crown"},
            "pick": {
                "market": "HKJC角球大細", "code": "CHL", "condition": "9.5", "line": 9.5,
                "side": "H", "label": "HKJC角球大細 大 9.5", "odds": 2.0, "stake": 100,
                "prob": .55, "ev": .1, "provider": "HKJC", "source": "hkjc_chl",
                "bookmaker": "HKJC", "reference": "pinnapi_corner_exact_full_match",
                "reference_provider": "PinnAPI",
            },
        }
        sync_prediction(ledger, prediction, config)
        bet = ledger["bets"][0]
        self.assertEqual(bet["market"], "HKJC角球大細")
        self.assertEqual(bet["provider"], "HKJC")
        self.assertEqual(bet["source"], "hkjc_chl")
        bet.update({"status": "SETTLED", "result": "Won", "pnl": 100})
        stats = recompute_stats(ledger, config)
        self.assertEqual(stats["by_market"]["HKJC角球大細"]["n"], 1)

    def test_quote_refresh_preserves_prediction_stage_and_replaces_stale_price(self) -> None:
        previous = {
            "match_id": "x", "stage": "T-30", "pick": None,
            "book_odds": {"crown": [{"odds": 1.80}], "hkjc_chl": [{"odds": 1.90}]},
        }
        titan = {
            "id": "x", "league": "L", "home": "A", "away": "B",
            "kickoff": self.now + timedelta(hours=2),
        }
        client = __import__("unittest.mock", fromlist=["Mock"]).Mock()
        client.crown_price_snapshot.return_value = {
            "prices": [{"market": "HDC", "odds": 2.01}],
            "asian_ok": True,
            "total_ok": True,
        }
        with patch("crown.engine.datetime") as mocked_datetime:
            mocked_datetime.now.return_value = self.now
            refreshed = _refresh_crown_quote(previous, titan, client)
        self.assertEqual(refreshed["stage"], "T-30")
        self.assertEqual(refreshed["book_odds"]["crown"][0]["odds"], 2.01)
        self.assertEqual(refreshed["book_odds"]["hkjc_chl"][0]["odds"], 1.90)

    def test_quote_refresh_retains_only_market_whose_fetch_failed(self) -> None:
        previous = {
            "match_id": "x", "stage": "首預",
            "book_odds": {"crown": [
                {"market": "HDC", "odds": 1.80},
                {"market": "HIL", "odds": 1.91},
            ]},
        }
        titan = {
            "id": "x", "league": "L", "home": "A", "away": "B",
            "kickoff": self.now + timedelta(hours=2),
        }
        client = __import__("unittest.mock", fromlist=["Mock"]).Mock()
        snapshot = {
            "prices": [{"market": "HIL", "odds": 2.02}],
            "asian_ok": False,
            "total_ok": True,
        }
        with patch("crown.engine.datetime") as mocked_datetime:
            mocked_datetime.now.return_value = self.now
            refreshed = _refresh_crown_quote(previous, titan, client, snapshot)
        prices = {row["market"]: row["odds"] for row in refreshed["book_odds"]["crown"]}
        self.assertEqual(prices, {"HDC": 1.80, "HIL": 2.02})
        self.assertEqual(refreshed["crown_quote_stale_markets"], ["HDC"])

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

    def test_tick_preflight_uses_only_due_local_cards(self) -> None:
        predictions = [
            {"match_id": "t5", "league": "L", "home": "A", "away": "B",
             "kickoff_hkt": (self.now + timedelta(minutes=5)).isoformat()},
            {"match_id": "later", "league": "L", "home": "C", "away": "D",
             "kickoff_hkt": (self.now + timedelta(hours=2)).isoformat()},
            {"match_id": "done", "league": "L", "home": "E", "away": "F",
             "kickoff_hkt": (self.now + timedelta(minutes=5)).isoformat()},
        ]
        ledger = {"watch": {"done": {"matching_version": "current",
                                     "stages": [{"stage": "T-5"}]}}}
        rows = _tick_rows_from_predictions(predictions, ledger, self.now)
        self.assertEqual([row["id"] for row in rows], ["t5"])

    def test_sweep_recovers_due_first_look_omitted_from_titan_list(self) -> None:
        kickoff = self.now + timedelta(hours=3)
        titan = [{
            "id": "listed", "league": "L", "home": "A", "away": "B",
            "kickoff": kickoff + timedelta(hours=1),
        }]
        cards = [
            {
                "match_id": "omitted", "league": "L", "home": "C", "away": "D",
                "kickoff_hkt": kickoff.isoformat(),
            },
            {
                "match_id": "listed", "league": "L", "home": "A", "away": "B",
                "kickoff_hkt": (kickoff + timedelta(hours=1)).isoformat(),
            },
        ]
        ledger = {
            "watch": {
                "omitted": {
                    "matching_version": "old",
                    "prediction_era": "old",
                    "stages": [{"stage": "首預", "status": "PREDICTION_READY"}],
                },
            },
        }
        rows = _sweep_rows_with_due_existing(titan, cards, ledger, self.now)
        self.assertEqual([row["id"] for row in rows], ["omitted", "listed"])

    def test_sweep_does_not_recover_existing_card_after_t30(self) -> None:
        kickoff = self.now + timedelta(hours=3)
        cards = [{
            "match_id": "late-stage", "league": "L", "home": "A", "away": "B",
            "kickoff_hkt": kickoff.isoformat(),
        }]
        ledger = {
            "watch": {
                "late-stage": {
                    "matching_version": "old",
                    "prediction_era": "old",
                    "stages": [
                        {"stage": "首預", "status": "PREDICTION_READY"},
                        {"stage": "T-30", "status": "PREDICTION_READY"},
                    ],
                },
            },
        }
        self.assertEqual(
            _sweep_rows_with_due_existing([], cards, ledger, self.now),
            [],
        )

    def test_late_quote_refresh_never_rolls_back_t5_card(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            config = replace(settings(), state_dir=Path(directory), telegram_enabled=False)
            now = self.now
            kickoff = (now + timedelta(minutes=5)).isoformat()
            t5 = {
                "match_id": "future", "kickoff_hkt": kickoff, "stage": "T-5",
                "verdict": "模擬注", "pick": {"label": "HDC H -0.5"},
                "book_odds": {"crown": [{"odds": 1.8}]},
            }
            save_predictions(config, [t5])
            stale_sweep = {
                "match_id": "future", "kickoff_hkt": kickoff, "stage": "T-30",
                "verdict": "傾向", "pick": None, "_quote_refresh_only": True,
                "book_odds": {"crown": [{"odds": 1.9}]},
                "crown_quote_refreshed_at": now.isoformat(),
            }
            merged = merge_predictions(config, [stale_sweep], now=now)[0]
            self.assertEqual(merged["stage"], "T-5")
            self.assertEqual(merged["verdict"], "模擬注")
            self.assertEqual(merged["pick"]["label"], "HDC H -0.5")
            self.assertEqual(merged["book_odds"]["crown"][0]["odds"], 1.9)
            self.assertNotIn("_quote_refresh_only", merged)

    def test_crown_period_runs_from_1200_to_next_1159(self) -> None:
        start, end = period_bounds(self.now)
        self.assertEqual(start.isoformat(), "2026-08-09T12:00:00+08:00")
        self.assertEqual(end.isoformat(), "2026-08-10T11:59:59+08:00")
        future = datetime(2026, 8, 9, 12, 5, tzinfo=start.tzinfo)
        started = datetime(2026, 8, 9, 12, 0, tzinfo=start.tzinfo)
        self.assertTrue(in_current_period(started, self.now))
        self.assertTrue(in_current_period(end, self.now))
        self.assertTrue(is_upcoming_in_current_period(future, self.now))
        self.assertFalse(is_upcoming_in_current_period(started, self.now))

    def test_dashboard_keeps_started_crown_matches_until_the_daily_rollover(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            config = replace(settings(), state_dir=root / "private-state", web_root=root / "web")
            started = (datetime.now(self.now.tzinfo) - timedelta(minutes=2)).isoformat()
            save_predictions(config, [{
                "match_id": "started-crown",
                "kickoff_hkt": started,
                "hkjc_match_id": None,
                "book_odds": {"crown": [{"market": "HDC"}]},
                "status": "REFERENCE_READY",
            }])
            payload = build(config)
            self.assertEqual([row["match_id"] for row in payload["matches"]], ["started-crown"])

    def test_dashboard_artifact_is_readable_while_state_stays_separate(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            config = replace(settings(), state_dir=root / "private-state", web_root=root / "web")
            output = write_dashboard_data(config)
            self.assertEqual(S_IMODE(output.stat().st_mode), 0o644)
            self.assertNotEqual(output.parent, config.state_dir)

    def test_dashboard_live_board_uses_verified_crown_prices_as_the_master_list(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            config = replace(settings(), state_dir=root / "private-state", web_root=root / "web")
            # Anchor inside the active board period instead of adding one hour:
            # between 11:00 and 11:59 HKT that addition crosses the noon
            # rollover and makes this test depend on wall-clock time.
            kickoff = (period_bounds()[0] + timedelta(hours=1)).isoformat()
            save_predictions(config, [
                {"match_id": "crown-only", "kickoff_hkt": kickoff, "hkjc_match_id": None,
                 "book_odds": {"crown": [{"market": "HDC"}]}, "status": "DATA_MISSING"},
                {"match_id": "hkjc-only", "kickoff_hkt": kickoff, "hkjc_match_id": "h1",
                 "book_odds": {"crown": []}, "status": "DATA_MISSING"},
            ])
            payload = build(config)
            self.assertEqual([row["match_id"] for row in payload["matches"]], ["crown-only"])
            self.assertEqual(payload["summary"]["crown_matches"], 1)

    def test_dashboard_includes_persisted_prediction_history(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            config = replace(settings(), state_dir=root / "private-state", web_root=root / "web")
            config.state_dir.mkdir(parents=True)
            (config.state_dir / "prediction_history.json").write_text(
                '{"rows":[{"match_id":"old","stage":"T-5","kickoff":"2026-08-10T01:00:00+08:00",'
                '"market_predictions":[{"code":"HDC","condition":-0.5,"side":"H"}]}],'
                '"stats":{"predictions":1}}',
                encoding="utf-8",
            )
            payload = build(config)
            self.assertEqual(payload["prediction_history"]["rows"][0]["match_id"], "old")
            # A persisted row without an immutable model-version tag remains
            # auditable, but cannot be attributed to the current scorecard.
            stats = payload["prediction_history"]["stats"]
            self.assertEqual(stats["predictions"], 0)
            self.assertEqual(stats["all_history_audit"]["predictions"], 1)

    def test_dashboard_projects_persisted_ledger_stage_missing_from_history(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            config = replace(
                settings(),
                state_dir=root / "private-state",
                web_root=root / "web",
            )
            config.state_dir.mkdir(parents=True)
            (config.state_dir / "prediction_history.json").write_text(
                '{"rows":[],"stats":{}}',
                encoding="utf-8",
            )
            from crown.state import load_ledger, save_ledger
            ledger = load_ledger(config)
            ledger["watch"] = {
                "future": {
                    "match_id": "future",
                    "league": "League",
                    "home": "Home",
                    "away": "Away",
                    "kickoff": "2026-08-14T01:00:00+08:00",
                    "stages": [{
                        "match_id": "future",
                        "stage": "首預",
                        "ts": "2026-08-13T18:00:00+08:00",
                        "forecast": "主勝",
                        "outcome": {"home": .55, "draw": .25, "away": .20},
                        "market_predictions": [{
                            "code": "HDC",
                            "condition": -.5,
                            "line": -.5,
                            "side": "H",
                            "label": "Home -0.5",
                            "probability": .58,
                        }],
                    }],
                },
            }
            save_ledger(config, ledger)

            payload = build(config)
            rows = payload["prediction_history"]["rows"]
            self.assertEqual([(row["match_id"], row["stage"]) for row in rows], [
                ("future", "首預"),
            ])
            self.assertEqual(
                rows[0]["display_projection"],
                "persisted_ledger_pending_history_sync",
            )
            self.assertEqual(payload["prediction_history"]["stats"]["predictions"], 1)

    def test_dashboard_stage_completeness_counts_unique_fixtures_and_due_stages(self) -> None:
        from crown.common import HKT
        from crown.dashboard_data import stage_completeness

        now = datetime(2026, 8, 13, 22, 0, tzinfo=HKT)
        matches = [
            {"match_id": "future", "kickoff_hkt": "2026-08-13T23:00:00+08:00"},
            {"match_id": "t30-due", "kickoff_hkt": "2026-08-13T22:15:00+08:00"},
            {"match_id": "started", "kickoff_hkt": "2026-08-13T21:55:00+08:00"},
            # A duplicate market row must not inflate the fixture denominator.
            {"match_id": "started", "kickoff_hkt": "2026-08-13T21:55:00+08:00"},
        ]
        ledger = {"watch": {
            "future": {"stages": [{"stage": "首預", "status": "OK"}]},
            "t30-due": {"stages": [
                {"stage": "首預", "status": "OK"},
                {"stage": "T-30", "status": "DATA_MISSING"},
            ]},
            "started": {"stages": [
                {"stage": "首預", "status": "OK"},
                {"stage": "T-30", "status": "OK"},
                {"stage": "T-5", "status": "OK"},
            ]},
        }}

        summary = stage_completeness(matches, ledger, now=now)

        self.assertEqual(summary["fixtures_total"], 3)
        self.assertEqual(summary["fixtures_with_overdue_stage"], 1)
        self.assertFalse(summary["healthy"])
        self.assertEqual(
            summary["stages"]["首預"],
            {"recorded": 3, "due": 3, "missing_due": 0, "not_due": 0, "completeness": 1.0},
        )
        self.assertEqual(
            summary["stages"]["T-30"],
            {"recorded": 1, "due": 2, "missing_due": 1, "not_due": 1, "completeness": 0.5},
        )
        self.assertEqual(
            summary["stages"]["T-5"],
            {"recorded": 1, "due": 1, "missing_due": 0, "not_due": 2, "completeness": 1.0},
        )

    def test_dashboard_stage_completeness_fails_visible_for_missing_first_look(self) -> None:
        from crown.common import HKT
        from crown.dashboard_data import stage_completeness

        summary = stage_completeness(
            [{"match_id": "future", "kickoff_hkt": "2026-08-14T01:00:00+08:00"}],
            {"watch": {}},
            now=datetime(2026, 8, 13, 22, 0, tzinfo=HKT),
        )

        self.assertEqual(summary["stages"]["首預"]["missing_due"], 1)
        self.assertEqual(summary["stages"]["T-30"]["not_due"], 1)
        self.assertEqual(summary["stages"]["T-5"]["not_due"], 1)
        self.assertEqual(summary["fixtures_with_overdue_stage"], 1)
        self.assertFalse(summary["healthy"])

    def test_dashboard_history_row_wins_over_duplicate_ledger_projection(self) -> None:
        from crown.prediction_history import project_watch_rows

        history_row = {
            "match_id": "same",
            "stage": "首預",
            "kickoff": "2026-08-14T01:00:00+08:00",
            "predicted_at": "2026-08-13T18:00:00+08:00",
            "result_status": "已核對",
            "market_predictions": [{
                "code": "HDC", "line": -.5, "side": "H",
            }],
        }
        ledger = {"watch": {"same": {
            "match_id": "same",
            "kickoff": "2026-08-14T01:00:00+08:00",
            "stages": [{
                "match_id": "same",
                "stage": "首預",
                "market_predictions": [{
                    "code": "HDC", "line": -.5, "side": "H",
                }],
            }],
        }}}
        rows = project_watch_rows([history_row], ledger)
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["result_status"], "已核對")
        self.assertNotIn("display_projection", rows[0])

    def test_prediction_history_removes_non_finite_market_lines(self) -> None:
        from crown.prediction_history import normalize_history

        history = normalize_history({
            "rows": [{
                "match_id": "macarthur", "kickoff": "2026-08-11T19:00:00+08:00",
                "market_predictions": [
                    {
                        "code": "HDC", "condition": "NaN", "side": "H",
                        "probability": 0.61, "label": "FC麥克阿瑟 NaN",
                    },
                    {
                        "code": "HIL", "condition": 2.5, "side": "H",
                        "probability": 0.58, "label": "大 2.5",
                    },
                ],
            }],
            "stats": {},
        })
        self.assertEqual(len(history["rows"]), 1)
        self.assertEqual(
            [item["code"] for item in history["rows"][0]["market_predictions"]],
            ["HIL"],
        )

    def test_crown_frontend_filters_non_finite_history_lines_before_render(self) -> None:
        app = (Path(__file__).parents[1] / "dashboard" / "app.js").read_text(
            encoding="utf-8",
        )
        index = (Path(__file__).parents[1] / "dashboard" / "index.html").read_text(
            encoding="utf-8",
        )
        self.assertIn("Number.isFinite(Number(rawLine))", app)
        self.assertIn("20260814-data-health-shadow-condition-transition-stats-v3", index)

    def test_crown_fixture_list_uses_stage_aware_pending_status(self) -> None:
        root = Path(__file__).parents[1] / "dashboard"
        app = (root / "app.js").read_text(encoding="utf-8")
        index = (root / "index.html").read_text(encoding="utf-8")

        self.assertIn("function nextStageText(m, mins)", app)
        self.assertIn("if (t5) return '○ T-5 完成 · 唔買';", app)
        self.assertIn("if (t30) return '○ T-30 完成 · 等 T-5';", app)
        self.assertIn("if (mins > 40) return '○ 等 T-30';", app)
        self.assertIn("if (mins >= 20) return '○ 正等 T-30 處理';", app)
        self.assertIn("return '○ 錯過 T-30 · 等 T-5';", app)
        self.assertIn("nextStageText(m, mm)", app)
        self.assertNotIn("? '○ 唔買' : '○ 等 T-5'", app)
        self.assertIn("app.js?v=20260814-data-health-shadow-condition-transition-stats-v3", index)

    def test_crown_history_orders_fixture_groups_and_stages(self) -> None:
        node = shutil.which("node")
        if not node:
            self.skipTest("node is unavailable")
        smoke = Path(__file__).with_name("prediction_history_order_smoke.mjs")
        subprocess.run([node, str(smoke)], check=True)

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
                    "market_predictions": [{
                        "code": "HDC", "condition": -0.5, "line": -0.5,
                        "side": "H", "label": "A -0.5", "probability": .60,
                    }],
                    "pick": None, "no_bet_reason": "資訊階段",
                }],
            }}}
            first = archive_watch(config, ledger)
            second = archive_watch(config, ledger)
            self.assertEqual(len(first["rows"]), 1)
            self.assertEqual(len(second["rows"]), 1)
            self.assertFalse(second["rows"][0]["simulated_bet"])
            self.assertEqual(second["stats"]["predictions"], 1)

    def test_prediction_history_advances_valid_learning_snapshot_reference(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            config = replace(settings(), state_dir=Path(directory))
            stage = {
                "match_id": "x", "stage": "T-30",
                "ts": "2026-08-09T11:35:00+08:00",
                "forecast": "主勝",
                "outcome": {"home": .55, "draw": .25, "away": .20},
                "market_predictions": [{
                    "code": "HIL", "condition": 2.5, "line": 2.5,
                    "side": "H", "label": "大 2.5", "probability": .57,
                }],
                "learning_snapshot_id": 1,
                "learning_attempt": 1,
                "learning_pre_kickoff": True,
                "learning_payload_sha256": "old",
            }
            ledger = {"watch": {"x": {
                "match_id": "x", "league": "L", "home": "A", "away": "B",
                "kickoff": "2026-08-09T12:05:00+08:00",
                "stages": [stage],
            }}}
            first = archive_watch(config, ledger)
            first["rows"][0]["actual"] = "主勝"
            first["rows"][0]["result_status"] = "已核實"
            (config.state_dir / "prediction_history.json").write_text(
                json.dumps(first), encoding="utf-8"
            )

            stage.update({
                "ts": "2026-08-09T11:36:00+08:00",
                "learning_snapshot_id": 2,
                "learning_attempt": 2,
                "learning_payload_sha256": "new",
            })
            second = archive_watch(config, ledger)
            row = second["rows"][0]
            self.assertEqual(row["learning_snapshot_id"], 2)
            self.assertEqual(row["learning_attempt"], 2)
            self.assertEqual(row["learning_payload_sha256"], "new")
            self.assertEqual(row["actual"], "主勝")
            self.assertEqual(row["result_status"], "已核對")

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
                    "market_predictions": [{
                        "code": "HDC", "condition": -0.5, "line": -0.5,
                        "side": "H", "label": "主 -0.5", "probability": .62,
                    }],
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
            self.assertEqual(row["market_grades"][0]["settlement"], "Won")
            market = history["stats"]["by_market"]["HDC"]
            # This legacy fixture deliberately has no selected odds. It stays
            # in immutable history but is excluded from every priced statistic.
            self.assertEqual(market["hits"], 0)
            self.assertEqual(market["all_odds"]["hits"], 0)
            self.assertEqual(market["excluded_missing_odds"], 1)

    def test_prediction_history_market_accuracy_is_split_by_stage(self) -> None:
        stages = ("首預", "T-30", "T-5")
        rows = [
            {
                "match_id": "same-match",
                "stage": stage,
                "market_grades": [{
                    "code": "HDC",
                    "side": "H",
                    "line": -0.5,
                    "odds": 1.9,
                    "grade_status": "GRADED",
                    "settlement": "Won" if hit else "Lost",
                    "hit": hit,
                    "brier": .16 if hit else .36,
                    "log_loss": .51 if hit else .92,
                }],
            }
            for stage, hit in zip(stages, (True, False, True))
        ]

        stats = calculate_stats(rows)

        self.assertEqual(stats["by_market"]["HDC"]["decided"], 3)
        self.assertEqual(stats["by_market"]["HDC"]["hits"], 2)
        self.assertEqual(stats["by_stage_market"]["首預"]["HDC"]["hits"], 1)
        self.assertEqual(stats["by_stage_market"]["T-30"]["HDC"]["hits"], 0)
        self.assertEqual(stats["by_stage_market"]["T-5"]["HDC"]["hits"], 1)
        for stage in stages:
            self.assertEqual(
                stats["by_stage_market"][stage]["HDC"]["decided"], 1
            )
        consensus = stats["three_stage_consensus"]["markets"]["HDC"]
        self.assertEqual(consensus["same_direction"]["fixtures"], 1)
        self.assertEqual(consensus["same_direction"]["primary"]["decided"], 1)
        self.assertEqual(consensus["same_direction"]["primary"]["hits"], 1)
        self.assertEqual(consensus["same_direction"]["primary"]["accuracy"], 1.0)
        transitions = stats["three_stage_transitions"]["conditions"]
        self.assertIn("same_direction_line_moved", transitions)
        self.assertEqual(
            transitions["same_direction_line_moved"]["markets"]["HDC"]
            ["aggregate"]["tiers"]["at_or_above_1_70"]["fixtures"],
            0,
        )

    def test_prediction_history_excludes_explicit_titan_postponement(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            config = replace(settings(), state_dir=Path(directory))
            kickoff = datetime.now(self.now.tzinfo) - timedelta(hours=3)
            ledger = {"watch": {"x": {
                "match_id": "x", "league": "League",
                "home": "Alpha FC", "away": "Beta FC",
                "kickoff": kickoff.isoformat(), "titan_match_id": "x",
                "stages": [{
                    "match_id": "x", "stage": "T-5",
                    "ts": (kickoff - timedelta(minutes=5)).isoformat(),
                    "forecast": "主勝", "probability": .60,
                    "market_predictions": [{
                        "code": "HDC", "condition": -0.5, "line": -0.5,
                        "side": "H", "label": "主 -0.5", "probability": .62,
                    }],
                }],
            }}}
            archive_watch(config, ledger)
            status = {
                "id": "x", "league": "League",
                "home": "Alpha FC", "away": "Beta FC",
                "kickoff": kickoff, "status": "推迟",
                "home_score": None, "away_score": None,
            }
            with patch(
                "crown.prediction_history.TitanClient.results",
                return_value=[status],
            ), patch(
                "crown.prediction_history.fetch_official_result_events",
                return_value=[],
            ), patch(
                "crown.prediction_history.fetch_official_match_statuses",
                return_value={},
            ):
                history = grade_history(config)
            row = history["rows"][0]
            self.assertEqual(row["result_status"], "不計")
            self.assertEqual(
                row["result_source"], "titan_exact_id_terminal_status"
            )
            self.assertEqual(
                row["market_grades"][0]["reason"], "fixture_not_played"
            )
            self.assertEqual(history["result_sync"]["excluded_now"], 1)

    def test_terminal_history_exclusion_is_persisted_to_learning_store(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "learning.sqlite"
            with LearningStore(path) as store:
                snapshot = store.record_snapshot(
                    "crown", "terminal-1", "T-5",
                    "2026-08-09T11:55:00+08:00",
                    "2026-08-09T12:00:00+08:00",
                    {"market_predictions": []},
                )
            row = {
                "match_id": "terminal-1",
                "learning_snapshot_id": snapshot["snapshot_id"],
                "market_grades": [{
                    "code": "HIL", "condition": "2.5", "side": "H",
                    "grade_status": "NOT_APPLICABLE",
                }],
            }
            with patch.dict(os.environ, {"LEARNING_DB_PATH": str(path)}):
                _persist_learning_exclusion(
                    row, "MATCHPOSTPONED",
                    "hkjc_official_exact_id_terminal_status",
                )
            with LearningStore(path) as store:
                result = store._connection.execute(  # noqa: SLF001
                    "SELECT terminal_status FROM results WHERE system = 'crown'"
                ).fetchone()
                grade = store._connection.execute(  # noqa: SLF001
                    "SELECT state FROM grades WHERE snapshot_id = ? AND market = 'HIL'",
                    (snapshot["snapshot_id"],),
                ).fetchone()
        self.assertEqual(result["terminal_status"], "MATCHPOSTPONED")
        self.assertEqual(grade["state"], "NOT_APPLICABLE")

    def test_valid_hkjc_score_wins_over_refunded_pool_status(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            config = replace(settings(), state_dir=Path(directory))
            kickoff = datetime.now(self.now.tzinfo) - timedelta(hours=3)
            ledger = {"watch": {"x": {
                "match_id": "x", "hkjc_match_id": "50072834",
                "league": "League", "home": "Alpha FC", "away": "Beta FC",
                "kickoff": kickoff.isoformat(), "titan_match_id": "x",
                "stages": [{
                    "match_id": "x", "stage": "T-5",
                    "ts": (kickoff - timedelta(minutes=5)).isoformat(),
                    "forecast": "客勝", "probability": .60,
                    "market_predictions": [{
                        "code": "HIL", "condition": 2.5, "line": 2.5,
                        "side": "H", "label": "大 2.5", "probability": .62,
                    }],
                }],
            }}}
            archive_watch(config, ledger)
            official = {
                "id": "50072834", "league": "League",
                "home": "Alpha FC", "away": "Beta FC",
                "kickoff": kickoff, "home_score": 1, "away_score": 2,
            }
            with patch(
                "crown.prediction_history.TitanClient.results", return_value=[]
            ), patch(
                "crown.prediction_history.fetch_official_result_events",
                return_value=[official],
            ), patch(
                "crown.prediction_history.fetch_official_match_statuses",
                return_value={"50072834": {
                    "status": "INPLAYMATCHENDED",
                    "refund_pools": ["CHL"],
                }},
            ):
                history = grade_history(config)
            row = history["rows"][0]
            self.assertEqual(row["result_status"], "已核對")
            self.assertEqual(row["score"], "1-2")
            self.assertEqual(row["result_source"], "hkjc_official_exact_id")

    def test_prediction_history_recovers_exact_titan_detail_omitted_from_index(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            config = replace(settings(), state_dir=Path(directory))
            kickoff = datetime.now(self.now.tzinfo) - timedelta(hours=8)
            ledger = {"watch": {"3031468": {
                "match_id": "3031468", "titan_match_id": "3031468",
                "league": "亚挑联", "home": "中央骏马", "away": "南市台钢",
                "kickoff": kickoff.isoformat(),
                "stages": [{
                    "match_id": "3031468", "stage": "T-5",
                    "ts": (kickoff - timedelta(minutes=5)).isoformat(),
                    "forecast": "和局", "probability": .40,
                    "market_predictions": [{
                        "code": "HIL", "condition": 3.5, "line": 3.5,
                        "side": "H", "label": "大 3.5", "probability": .58,
                    }],
                }],
            }}}
            archive_watch(config, ledger)
            detail = {
                "id": "3031468", "league": "亚挑联",
                "home": "中央骏马", "away": "南市台钢",
                "kickoff": kickoff, "status": "完",
                "home_score": 2, "away_score": 2,
                "corners_home": 3, "corners_away": 7, "corners_total": 10,
            }
            with patch(
                "crown.prediction_history.TitanClient.results", return_value=[]
            ), patch(
                "crown.prediction_history.TitanClient.result_detail",
                return_value=detail,
            ), patch(
                "crown.prediction_history.fetch_official_result_events",
                return_value=[],
            ), patch(
                "crown.prediction_history.fetch_official_match_statuses",
                return_value={},
            ):
                history = grade_history(config)
            row = history["rows"][0]
            self.assertEqual(row["result_status"], "已核對")
            self.assertEqual(row["score"], "2-2")
            self.assertEqual(row["result_detail"]["corners_total"], 10)
            self.assertEqual(
                row["result_source"], "titan007_direct_detail_exact_id"
            )

    def test_normalizer_retries_ended_hkjc_row_wrongly_marked_not_applicable(self) -> None:
        from crown.prediction_history import normalize_history

        row = {
            "match_id": "x", "stage": "T-5",
            "market_predictions": [{
                "code": "HIL", "line": 2.5, "side": "H", "probability": .6,
            }],
            "result_status": "不計",
            "result_source": "hkjc_official_exact_id_terminal_status",
            "result_detail": {"terminal_status": "INPLAYMATCHENDED"},
            "market_grades": [{"code": "HIL", "grade_status": "NOT_APPLICABLE"}],
        }
        history = normalize_history({"rows": [row]})
        recovered = history["rows"][0]
        self.assertEqual(recovered["result_status"], "待賽果")
        self.assertEqual(recovered["market_grades"], [])
        self.assertIsNone(recovered["result_source"])

    def test_prediction_history_recovers_changed_titan_id_by_unique_identity(self) -> None:
        from crown.prediction_history import _result

        kickoff = datetime(2026, 8, 11, 7, 0, tzinfo=self.now.tzinfo)
        row = {
            "match_id": "old-id", "titan_match_id": "old-id",
            "league": "哥伦甲秋", "home": "麦德林独立", "away": "百万富翁",
            "kickoff": kickoff.isoformat(),
        }
        replacement = {
            "id": "new-id", "league": "哥伦甲秋",
            "home": "麦德林独立", "away": "百万富翁",
            "kickoff": kickoff, "home_score": 1, "away_score": 2,
        }
        score, source = _result(row, {"new-id": replacement}, {}, [])
        self.assertEqual((score["home_score"], score["away_score"]), (1, 2))
        self.assertEqual(source, "titan_verified_unique_identity_fallback")

    def test_prediction_history_uses_strict_titan_fallback_after_hkjc_grace(self) -> None:
        from crown.prediction_history import _result

        kickoff = datetime.now(self.now.tzinfo) - timedelta(hours=13)
        row = {
            "match_id": "3031468",
            "titan_match_id": "3031468",
            "hkjc_match_id": "hkjc-delayed",
            "league": "League",
            "home": "中央骏马",
            "away": "南市台钢",
            "kickoff": kickoff.isoformat(),
        }
        titan = {
            "id": "3031468",
            "league": "League",
            "home": "中央骏马",
            "away": "南市台钢",
            "kickoff": kickoff,
            "home_score": 2,
            "away_score": 2,
        }
        score, source = _result(row, {"3031468": titan}, {}, [])
        self.assertEqual((score["home_score"], score["away_score"]), (2, 2))
        self.assertEqual(source, "titan_verified_identity_after_hkjc_grace")

    def test_prediction_history_waits_for_hkjc_during_result_grace(self) -> None:
        from crown.prediction_history import _result

        kickoff = datetime.now(self.now.tzinfo) - timedelta(hours=3)
        row = {
            "match_id": "recent",
            "titan_match_id": "recent",
            "hkjc_match_id": "hkjc-recent",
            "league": "League",
            "home": "Alpha",
            "away": "Beta",
            "kickoff": kickoff.isoformat(),
        }
        titan = {
            "id": "recent",
            "league": "League",
            "home": "Alpha",
            "away": "Beta",
            "kickoff": kickoff,
            "home_score": 1,
            "away_score": 0,
        }
        score, source = _result(row, {"recent": titan}, {}, [])
        self.assertIsNone(score)
        self.assertIsNone(source)

    def test_prediction_history_rejects_ambiguous_titan_identity_fallback(self) -> None:
        from crown.prediction_history import _result

        kickoff = datetime(2026, 8, 11, 7, 0, tzinfo=self.now.tzinfo)
        row = {
            "match_id": "old-id", "titan_match_id": "old-id",
            "league": "League", "home": "Alpha", "away": "Beta",
            "kickoff": kickoff.isoformat(),
        }
        candidates = {
            key: {
                "id": key, "league": "League", "home": "Alpha", "away": "Beta",
                "kickoff": kickoff, "home_score": 1, "away_score": 0,
            }
            for key in ("replacement-a", "replacement-b")
        }
        score, source = _result(row, candidates, {}, [])
        self.assertIsNone(score)
        self.assertIsNone(source)

    def test_prediction_history_orients_reversed_titan_identity_fallback(self) -> None:
        from crown.prediction_history import _result

        kickoff = datetime(2026, 8, 11, 7, 0, tzinfo=self.now.tzinfo)
        row = {
            "match_id": "old-id", "titan_match_id": "old-id",
            "league": "League", "home": "Alpha", "away": "Beta",
            "kickoff": kickoff.isoformat(),
        }
        replacement = {
            "id": "new-id", "league": "League", "home": "Beta", "away": "Alpha",
            "kickoff": kickoff, "home_score": 3, "away_score": 1,
        }
        score, source = _result(row, {"new-id": replacement}, {}, [])
        self.assertEqual((score["home_score"], score["away_score"]), (1, 3))
        self.assertEqual(source, "titan_verified_unique_identity_fallback")

    def test_verified_history_retries_and_grades_late_official_corner_result(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            config = replace(settings(), state_dir=Path(directory))
            kickoff = datetime.now(self.now.tzinfo) - timedelta(hours=3)
            ledger = {"watch": {"x": {
                "match_id": "x", "league": "League", "home": "Alpha FC", "away": "Beta FC",
                "kickoff": kickoff.isoformat(), "titan_match_id": "x", "hkjc_match_id": "h1",
                "stages": [{
                    "match_id": "x", "stage": "T-5", "ts": (kickoff - timedelta(minutes=5)).isoformat(),
                    "forecast": "主勝", "probability": .60,
                    "market_predictions": [{
                        "code": "CHL", "condition": 10.5, "line": 10.5,
                        "side": "L", "label": "細 10.5 角球", "probability": .62,
                    }],
                    "pick": None,
                }],
            }}}
            first = archive_watch(config, ledger)
            first["rows"][0].update({
                "actual": "主勝",
                "score": "2-1",
                "correct": True,
                "result_status": "已核對",
                "market_grades": [{
                    **first["rows"][0]["market_predictions"][0],
                    "grade_status": "NOT_APPLICABLE",
                    "reason": "corners_result_missing",
                }],
            })
            (config.state_dir / "prediction_history.json").write_text(
                json.dumps(first), encoding="utf-8"
            )
            official = {
                "id": "h1", "league": "League", "home": "Alpha FC", "away": "Beta FC",
                "kickoff": kickoff, "home_score": 2, "away_score": 1, "corners_total": 9,
            }
            with patch("crown.prediction_history.TitanClient.results", return_value=[]), \
                 patch("crown.prediction_history.fetch_official_result_events", return_value=[official]):
                history = grade_history(config)
            row = history["rows"][0]
            self.assertEqual(row["result_source"], "hkjc_official_exact_id")
            self.assertEqual(row["result_detail"]["corners_total"], 9)
            self.assertEqual(row["market_grades"][0]["grade_status"], "GRADED")
            self.assertEqual(row["market_grades"][0]["settlement"], "Won")
            self.assertEqual(history["result_sync"]["graded_now"], 1)

    def test_official_score_merges_exact_hkjc_footbreak_corner_cache(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            config = replace(settings(), state_dir=root / "crown")
            kickoff = datetime.now(self.now.tzinfo) - timedelta(hours=3)
            ledger = {"watch": {"x": {
                "match_id": "x", "league": "League", "home": "Alpha FC", "away": "Beta FC",
                "kickoff": kickoff.isoformat(), "titan_match_id": "x", "hkjc_match_id": "h1",
                "stages": [{
                    "match_id": "x", "stage": "T-5", "ts": (kickoff - timedelta(minutes=5)).isoformat(),
                    "forecast": "主勝", "probability": .60,
                    "market_predictions": [{
                        "code": "CHL", "condition": 10.5, "line": 10.5,
                        "side": "L", "label": "細 10.5 角球", "probability": .62,
                    }],
                    "pick": None,
                }],
            }}}
            archive_watch(config, ledger)
            footbreak_ledger = root / "sim_ledger.json"
            result_cache = root / "results"
            result_cache.mkdir()
            footbreak_ledger.write_text(json.dumps({
                "watch": {"h1": {"fixture_id": "fixture-1"}},
            }), encoding="utf-8")
            (result_cache / "fixture-1.json").write_text(json.dumps({
                "goals_home": 2, "goals_away": 1, "corners_total": 9,
            }), encoding="utf-8")
            official = {
                "id": "h1", "league": "League", "home": "Alpha FC", "away": "Beta FC",
                "kickoff": kickoff, "home_score": 2, "away_score": 1, "corners_total": None,
            }
            with patch.dict("os.environ", {
                     "FOOTBREAK_LEDGER_PATH": str(footbreak_ledger),
                     "FOOTBREAK_RESULT_CACHE_DIR": str(result_cache),
                 }), patch("crown.prediction_history.TitanClient.results", return_value=[]), \
                 patch("crown.prediction_history.fetch_official_result_events", return_value=[official]):
                history = grade_history(config)
            row = history["rows"][0]
            self.assertEqual(row["result_detail"]["corners_total"], 9)
            self.assertEqual(
                row["result_source"],
                "hkjc_official_exact_id+footbreak_corner_exact_hkjc_id",
            )
            self.assertEqual(row["market_grades"][0]["grade_status"], "GRADED")
            self.assertEqual(row["market_grades"][0]["settlement"], "Won")
            self.assertEqual(history["result_sync"]["footbreak_cached_rows"], 1)

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

    def test_crown_simulated_bet_notification_is_retired(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            config = replace(settings(), state_dir=Path(directory), telegram_enabled=False)
            ledger = {"bets": [{"bet_id": "a", "status": "PENDING", "home": "A", "away": "B", "label": "x", "odds": 2}]}
            with patch("crown.notify._send") as sender:
                self.assertEqual(notify_new(ledger, config), 0)
                self.assertEqual(notify_new(ledger, config), 0)
                sender.assert_not_called()

    def test_t5_corner_forecast_notification_is_retired(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            config = replace(
                settings(), state_dir=Path(directory), telegram_enabled=False
            )
            stage = {
                "match_id": "corner-1", "stage": "T-5",
                "kickoff_hkt": "2099-08-12T20:00:00+08:00",
                "league": "測試聯賽",
                "home": "主隊",
                "away": "客隊",
                "market_predictions": [{
                    "market": "HKJC角球大細",
                    "code": "CHL",
                    "side": "L",
                    "line": 9.5,
                    "odds": 1.88,
                }],
            }
            ledger = {"bets": [], "watch": {"corner-1": {
                "match_id": "corner-1",
                "kickoff": stage["kickoff_hkt"],
                "kickoff_hkt": stage["kickoff_hkt"],
                "league": stage["league"],
                "home": stage["home"], "away": stage["away"],
                "stages": [stage],
            }}}
            with patch("crown.notify._send") as sender:
                self.assertEqual(notify_new(ledger, config, ["corner-1"]), 0)
                self.assertEqual(notify_new(ledger, config, ["corner-1"]), 0)
                sender.assert_not_called()

    def test_corner_forecast_notification_rejects_non_t5_and_started_matches(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            config = replace(
                settings(), state_dir=Path(directory), telegram_enabled=False
            )
            base = {
                "match_id": "corner-2",
                "stage": "T-30",
                "kickoff_hkt": "2099-08-12T20:00:00+08:00",
                "forecast_candidates": [{
                    "code": "CHL", "side": "H", "line": 10.5, "prob": 0.52
                }],
            }
            started = base | {
                "match_id": "corner-3",
                "stage": "T-5",
                "kickoff_hkt": "2020-08-12T20:00:00+08:00",
            }
            with patch("crown.notify._send") as sender:
                self.assertEqual(
                    notify_new({"bets": []}, config, [base, started]), 0
                )
                sender.assert_not_called()

    def test_crown_notification_uses_selected_team_handicap_view(self) -> None:
        home = {"market": "HDC", "side": "H", "line": -0.25, "home": "主隊", "away": "客隊"}
        away = {"market": "HDC", "side": "A", "line": 0.25, "home": "主隊", "away": "客隊"}
        self.assertEqual(_bet_label(home), "讓球 · 主隊 -0/0.5")
        self.assertEqual(_bet_label(away), "讓球 · 客隊 -0/0.5")

    def test_corner_notification_is_explicitly_hkjc_chinese_market_name(self) -> None:
        corner = {"market": "HKJC角球大細", "code": "CHL", "side": "H", "line": 9.5}
        self.assertEqual(_bet_label(corner), "HKJC角球大細 · 大 9.5")

    def test_shadow_bet_settles_separately_from_official_portfolio(self) -> None:
        config = settings()
        ledger = {
            "bankroll": 50000, "watch": {}, "log": [], "stats": {},
            "shadow_stats": {}, "bets": [],
            "shadow_bets": [{
                "bet_id": "shadow|3019098|HIL|2.5|H",
                "match_id": "3019098", "titan_match_id": "3019098",
                "league": "德乙", "home": "纽伦堡", "away": "德累斯顿",
                "kickoff": "2020-01-01T12:00:00+08:00",
                "market": "HIL", "code": "HIL", "condition": "2.5", "side": "H",
                "odds": 2.0, "stake": 1000, "status": "PENDING",
                "portfolio": "shadow", "shadow_only": True,
            }],
        }
        titan = [{
            "id": "3019098", "league": "德乙", "home": "纽伦堡",
            "away": "德累斯顿",
            "kickoff": datetime.fromisoformat("2020-01-01T12:00:00+08:00"),
            "home_score": 3, "away_score": 0,
        }]
        with patch("crown.settle.load_ledger", return_value=ledger), \
             patch("crown.settle._refresh_live", return_value={}), \
             patch("crown.settle.fetch_official_results", return_value={}), \
             patch("crown.settle.fetch_official_match_statuses", return_value={}), \
             patch("crown.settle.TitanClient.results", return_value=titan), \
             patch("crown.settle.save_ledger"):
            result = crown_settle.settle_due(config)
        self.assertEqual(result["settled"], 0)
        self.assertEqual(result["shadow_settled"], 1)
        self.assertEqual(result["pending"], 0)
        self.assertEqual(result["shadow_pending"], 0)
        self.assertEqual(ledger["stats"]["n_settled"], 0)
        self.assertEqual(ledger["shadow_stats"]["n_settled"], 1)
        self.assertEqual(ledger["shadow_bets"][0]["result"], "Won")
        self.assertEqual(ledger["shadow_bets"][0]["pnl"], 1000)

    def test_shadow_bet_recovers_exact_titan_detail_omitted_from_result_index(self) -> None:
        config = settings()
        ledger = {
            "bankroll": 50000, "watch": {}, "log": [], "stats": {},
            "shadow_stats": {}, "bets": [],
            "shadow_bets": [{
                "bet_id": "shadow|3056238|HDC|0|A",
                "match_id": "3056238", "titan_match_id": "3056238",
                "league": "葡U23", "home": "葡萄牙体育U23", "away": "莱里亚U23",
                "kickoff": "2020-01-01T12:00:00+08:00",
                "market": "讓球", "code": "HDC", "condition": "0", "side": "A",
                "odds": 1.6, "stake": 1000, "status": "PENDING",
                "portfolio": "shadow", "shadow_only": True,
            }],
        }
        detail = {
            "id": "3056238", "league": "葡U23",
            "home": "葡萄牙体育U23", "away": "莱里亚U23",
            "kickoff": datetime.fromisoformat("2020-01-01T12:00:00+08:00"),
            "status": "完", "home_score": 1, "away_score": 1,
        }
        with patch("crown.settle.load_ledger", return_value=ledger), \
             patch("crown.settle._refresh_live", return_value={}), \
             patch("crown.settle.fetch_official_results", return_value={}), \
             patch("crown.settle.fetch_official_match_statuses", return_value={}), \
             patch("crown.settle.TitanClient.results", return_value=[]), \
             patch("crown.settle.TitanClient.result_detail", return_value=detail), \
             patch("crown.settle.save_ledger"):
            result = crown_settle.settle_due(config)
        self.assertEqual(result["shadow_settled"], 1)
        self.assertEqual(result["shadow_pending"], 0)
        self.assertEqual(ledger["shadow_bets"][0]["result"], "Refunded")
        self.assertEqual(
            ledger["shadow_bets"][0]["settlement_source"],
            "titan007_detail_exact_id_identity",
        )

    def test_shadow_detail_fallback_rejects_identity_mismatch(self) -> None:
        config = settings()
        ledger = {
            "bankroll": 50000, "watch": {}, "log": [], "stats": {},
            "shadow_stats": {}, "bets": [],
            "shadow_bets": [{
                "bet_id": "shadow|3056238|HDC|0|A",
                "match_id": "3056238", "titan_match_id": "3056238",
                "league": "葡U23", "home": "葡萄牙体育U23", "away": "莱里亚U23",
                "kickoff": "2020-01-01T12:00:00+08:00",
                "market": "讓球", "code": "HDC", "condition": "0", "side": "A",
                "odds": 1.6, "stake": 1000, "status": "PENDING",
                "portfolio": "shadow", "shadow_only": True,
            }],
        }
        wrong_detail = {
            "id": "3056238", "league": "葡U23",
            "home": "其他球隊", "away": "錯誤球隊",
            "kickoff": datetime.fromisoformat("2020-01-01T12:00:00+08:00"),
            "status": "完", "home_score": 1, "away_score": 1,
        }
        with patch("crown.settle.load_ledger", return_value=ledger), \
             patch("crown.settle._refresh_live", return_value={}), \
             patch("crown.settle.fetch_official_results", return_value={}), \
             patch("crown.settle.fetch_official_match_statuses", return_value={}), \
             patch("crown.settle.TitanClient.results", return_value=[]), \
             patch("crown.settle.TitanClient.result_detail", return_value=wrong_detail), \
             patch("crown.settle.save_ledger"):
            result = crown_settle.settle_due(config)
        self.assertEqual(result["shadow_settled"], 0)
        self.assertEqual(result["shadow_pending"], 1)
        self.assertEqual(ledger["shadow_bets"][0]["status"], "PENDING")

    def test_chl_settlement_uses_hkjc_exact_id_corners_even_when_live_cache_exists(self) -> None:
        config = settings()
        ledger = {
            "bankroll": 50000, "watch": {}, "log": [], "stats": {},
            "bets": [{
                "bet_id": "corner", "match_id": "titan-match", "hkjc_match_id": "hkjc-match",
                "pinnapi_event_id": "pin-match", "league": "L", "home": "A", "away": "B",
                "kickoff": "2020-01-01T12:00:00+08:00",
                "market": "HKJC角球大細", "code": "CHL", "condition": "9.5", "side": "H",
                "odds": 2.0, "stake": 100, "status": "PENDING",
            }],
        }
        official = {
            "hkjc-match": {
                "home_score": 0, "away_score": 0, "corners_total": 10,
                "source": "hkjc_official",
            }
        }
        with patch("crown.settle.load_ledger", return_value=ledger), \
             patch("crown.settle._refresh_live", return_value={"pin-match": {"seen_live": True}}), \
             patch("crown.settle.fetch_official_results", return_value=official), \
             patch("crown.settle.TitanClient.results") as titan_results, \
             patch("crown.settle.save_ledger"):
            result = crown_settle.settle_due(config)
        self.assertEqual(result["settled"], 1)
        self.assertEqual(ledger["bets"][0]["result"], "Won")
        self.assertEqual(ledger["bets"][0]["score"], {"corners_total": 10})
        self.assertEqual(ledger["bets"][0]["settlement_source"], "hkjc_official_exact_id_corners")
        titan_results.assert_not_called()

    def test_chl_settlement_uses_verified_titan_detail_when_official_corners_are_missing(self) -> None:
        config = settings()
        ledger = {
            "bankroll": 50000, "watch": {}, "log": [], "stats": {},
            "bets": [{
                "bet_id": "corner", "match_id": "3019098", "titan_match_id": "3019098",
                "hkjc_match_id": "50072724", "league": "德乙", "home": "纽伦堡",
                "away": "德累斯顿", "kickoff": "2020-01-01T12:00:00+08:00",
                "market": "HKJC角球大細", "code": "CHL", "condition": "10.5", "side": "H",
                "odds": 2.0, "stake": 100, "status": "PENDING",
            }],
        }
        official = {
            "50072724": {
                "home_score": 3, "away_score": 0, "corners_total": None,
                "source": "hkjc_official",
            }
        }
        titan = [{
            "id": "3019098", "league": "德乙", "home": "纽伦堡", "away": "德累斯顿",
            "kickoff": datetime.fromisoformat("2020-01-01T12:00:00+08:00"),
            "home_score": 3, "away_score": 0,
        }]
        with patch("crown.settle.load_ledger", return_value=ledger), \
             patch("crown.settle.fetch_official_results", return_value=official), \
             patch("crown.settle.TitanClient.results", return_value=titan), \
             patch("crown.settle.TitanClient.result_detail", return_value={"corners_total": 11}), \
             patch("crown.settle.save_ledger"):
            result = crown_settle.settle_due(config)
        self.assertEqual(result["settled"], 1)
        self.assertEqual(ledger["bets"][0]["result"], "Won")
        self.assertEqual(ledger["bets"][0]["score"], {"corners_total": 11})
        self.assertEqual(
            ledger["bets"][0]["settlement_source"],
            "hkjc_official_score+titan007_detail_exact_id_identity",
        )

    def test_chl_titan_detail_rejects_official_score_mismatch(self) -> None:
        config = settings()
        ledger = {
            "bankroll": 50000, "watch": {}, "log": [], "stats": {},
            "bets": [{
                "bet_id": "corner", "match_id": "3019098", "titan_match_id": "3019098",
                "hkjc_match_id": "50072724", "league": "德乙", "home": "纽伦堡",
                "away": "德累斯顿", "kickoff": "2020-01-01T12:00:00+08:00",
                "code": "CHL", "condition": "10.5", "side": "H",
                "odds": 2.0, "stake": 100, "status": "PENDING",
            }],
        }
        official = {
            "50072724": {
                "home_score": 2, "away_score": 0, "corners_total": None,
                "source": "hkjc_official",
            }
        }
        titan = [{
            "id": "3019098", "league": "德乙", "home": "纽伦堡", "away": "德累斯顿",
            "kickoff": datetime.fromisoformat("2020-01-01T12:00:00+08:00"),
            "home_score": 3, "away_score": 0,
        }]
        with patch("crown.settle.load_ledger", return_value=ledger), \
             patch("crown.settle.fetch_official_results", return_value=official), \
             patch("crown.settle.TitanClient.results", return_value=titan), \
             patch("crown.settle.TitanClient.result_detail") as detail, \
             patch("crown.settle.save_ledger"):
            result = crown_settle.settle_due(config)
        self.assertEqual(result["settled"], 0)
        self.assertEqual(ledger["bets"][0]["status"], "PENDING")
        detail.assert_not_called()

    def test_dashboard_api_settlement_fails_if_history_grading_fails(self) -> None:
        from crown.dashboard_api import perform_settlement

        config = settings()
        ledger = {"bets": [{"status": "SETTLED"}]}
        dashboard = {"ledger": ledger, "matches": []}
        with patch(
            "crown.dashboard_api.run",
            return_value={"ok": True, "settled": 1, "pending": 2},
        ), patch(
            "crown.dashboard_api.load_ledger",
            return_value=ledger,
        ), patch(
            "crown.dashboard_api.update_history",
            side_effect=RuntimeError("grading unavailable"),
        ), patch(
            "crown.dashboard_api.write_dashboard_data",
        ) as publisher, patch(
            "crown.dashboard_api.read_published_data",
            return_value=dashboard,
        ):
            with self.assertRaisesRegex(RuntimeError, "grading unavailable"):
                perform_settlement(config)

        publisher.assert_not_called()

    def test_dashboard_api_reads_only_valid_published_snapshot(self) -> None:
        from crown.dashboard_api import read_published_data

        payload = {"schema_version": "crown-dashboard-v2", "matches": []}
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "data.json").write_text(
                json.dumps(payload), encoding="utf-8"
            )
            self.assertEqual(
                read_published_data(replace(settings(), web_root=root)),
                payload,
            )

    def test_crown_history_uses_footbreak_result_layout_and_large_score(self) -> None:
        root = Path(__file__).resolve().parents[1] / "dashboard"
        app = (root / "app.js").read_text(encoding="utf-8")
        styles = (root / "styles.css").read_text(encoding="utf-8")

        self.assertIn("const HISTORY_STAGE_RANK = { '首預': 1, 'T-30': 2, 'T-5': 3 };", app)
        self.assertIn("function historyFixtureIdentity(row, index)", app)
        self.assertIn("function orderHistoryRows(rows)", app)
        self.assertIn("right.kickoff - left.kickoff || left.key.localeCompare(right.key)", app)
        self.assertNotIn("全部預測紀錄", app)
        self.assertIn('class="history-market-row"', app)
        self.assertIn("function historyCornerResult(r, p)", app)
        self.assertIn('class="market-actual">賽果 <b>', app)
        self.assertIn('class="history-result-cell"', app)
        self.assertIn('class="tbl-wrap"><table class="t history-table"', app)
        self.assertNotIn("<colgroup>", app)
        self.assertNotIn("history-table-wrap", app)
        self.assertIn(".history-result-cell .hist-result b", styles)
        self.assertIn("font-size: 1.4375rem", styles)
        self.assertIn("table-layout: fixed", styles)
        self.assertIn(".history-table th:nth-child(5) { width: 29%; }", styles)
        self.assertIn(".history-table > tbody > tr > td", styles)
        self.assertIn(".history-table .history-result-cell", styles)
        self.assertIn("grid-template-columns: minmax(0, 1fr) max-content", styles)
        self.assertIn("overflow-wrap: anywhere", styles)
        self.assertIn("font: 600 12px/1.6 var(--sans)", styles)
        index = (root / "index.html").read_text(encoding="utf-8")
        self.assertIn("styles.css?v=20260814-data-health-shadow-condition-transition-stats-v3", index)
        self.assertIn("app.js?v=20260814-data-health-shadow-condition-transition-stats-v3", index)
        self.assertIn("const HISTORY_STAGE_RANK = { '首預': 1, 'T-30': 2, 'T-5': 3 };", app)
        self.assertIn("row.kickoff_hkt || row.kickoff", app)
        self.assertIn('id="scrollTop"', index)
        self.assertIn('id="scrollBottom"', index)
        self.assertIn("function updateScrollDock()", app)
        self.assertIn("function scrollToPageBottom()", app)
        self.assertIn("document.documentElement.scrollHeight", app)
        self.assertIn('data-view="shadow">影子倉', index)
        self.assertIn('id="viewShadow"', index)
        self.assertIn("function renderShadow()", app)
        self.assertIn("盤口未提供", app)
        self.assertIn("!Number.isFinite(Number(x))", app)
        self.assertIn("不計入正式模擬倉、動態門檻、自動學習、凱利階段或 Telegram 通知", app)
        self.assertIn("同期表現對照", app)
        self.assertIn("card-shadow-comparison", app)
        self.assertIn("同期已結算樣本未各自達到 30 筆", app)
        self.assertIn("minimum-scale=1", index)
        self.assertIn("viewport-fit=cover", index)
        self.assertIn("min-width: 100%", styles)
        self.assertIn(".warnbar,", styles)
        self.assertIn(
            "${HISTORY_STAGE === 'all' ? '全部紀錄' : `${HISTORY_STAGE} 紀錄`}",
            app,
        )
        self.assertIn(
            '<span class="sub">${rows.length} 筆 · 最新開賽時間優先',
            app,
        )
        self.assertIn("historyTable(rows, '暫時未有預測紀錄。')", app)
        self.assertNotIn("const gradedRows =", app)
        self.assertNotIn("const pendingRows =", app)
        self.assertNotIn("const excludedRows =", app)
        self.assertIn("X-Crown-Action", app)
        self.assertIn("賽果核對完成，已更新到最新資料", app)
        self.assertIn("const FINISHED_MATCH_GRACE_MINUTES = 150", app)
        self.assertIn("LIST = displayableMatches(LIST)", app)
        self.assertIn("暫時冇未完場賽事", app)

    def test_prediction_history_fetches_titan_corner_detail_after_strict_checks(self) -> None:
        from crown.prediction_history import _merge_titan_corner_detail, _result
        from crown.titan import TitanClient

        row = {
            "match_id": "2961746",
            "titan_match_id": "2961746",
            "league": "北美聯賽盃",
            "home": "聖地亞哥FC",
            "away": "迪祖亞拿",
            "kickoff": "2026-08-10T10:00:00+08:00",
        }
        titan = {
            "id": "2961746",
            "league": "中北美杯",
            "home": "圣地亚哥",
            "away": "蒂华纳",
            "kickoff": datetime(2026, 8, 10, 10, 0, tzinfo=self.now.tzinfo),
            "home_score": 1,
            "away_score": 0,
        }
        client = Mock(spec=TitanClient)
        client.result_detail.return_value = {
            "corners_home": 6,
            "corners_away": 3,
            "corners_total": 9,
        }
        matched_score, matched_source = _result(
            row, {"2961746": titan}, {}, []
        )
        self.assertEqual(matched_score["home_score"], 1)
        self.assertEqual(matched_source, "titan_verified_identity")
        result, source, reason = _merge_titan_corner_detail(
            row,
            {"home_score": 1, "away_score": 0, "corners_total": None},
            "hkjc_official_exact_id",
            {"2961746": titan},
            client,
        )
        self.assertEqual(result["corners_total"], 9)
        self.assertEqual(reason, "filled")
        self.assertIn("exact_id_identity_score", source)
        client.result_detail.assert_called_once_with("2961746")

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
                            "ttlCornerResult": "-1",
                            "payoutConfirmed": True,
                            "stageId": 5,
                            "resultType": 1,
                            "sequence": 2,
                        },
                        {
                            "homeResult": "5",
                            "awayResult": "7",
                            "ttlCornerResult": "-1",
                            "payoutConfirmed": True,
                            "stageId": 5,
                            "resultType": 2,
                            "sequence": 3,
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
        self.assertEqual(rows["wanted"]["corners_total"], 12)
        self.assertEqual(rows["wanted"]["source"], "hkjc_official")


if __name__ == "__main__":
    unittest.main()
