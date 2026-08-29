import json
import sqlite3
import tempfile
import unittest
from pathlib import Path

from analysis.direction_path_conditions import (
    DEFAULT_PUBLIC,
    build_report,
    extract_radar_data,
    update,
)


class DirectionPathConditionsTest(unittest.TestCase):
    def make_radar_db(self) -> sqlite3.Connection:
        db = sqlite3.connect(":memory:")
        db.row_factory = sqlite3.Row
        db.executescript(
            """
            CREATE TABLE matches(
              id INTEGER PRIMARY KEY,kickoff_utc INTEGER,home_team TEXT,away_team TEXT
            );
            CREATE TABLE research_timeline_snapshots(
              match_id INTEGER,stage TEXT,provider TEXT,market TEXT,line_key TEXT,
              selection TEXT,decimal_odds REAL,is_main INTEGER,captured_at INTEGER,
              source_updated_at INTEGER,origin TEXT
            );
            CREATE TABLE research_results(
              match_id INTEGER,home_score INTEGER,away_score INTEGER,
              corners_total INTEGER,source TEXT
            );
            CREATE TABLE results(match_id INTEGER,home_score INTEGER,away_score INTEGER);
            """
        )
        return db

    def add_pair(
        self,
        db: sqlite3.Connection,
        stage: str,
        left_odds: float,
        right_odds: float,
        *,
        market: str = "OU",
        provider: str = "pinnacle",
        captured: int,
        origin: str | None = None,
    ) -> None:
        left, right = ("H", "A") if market == "AH" else ("O", "U")
        source_origin = origin or ("external_opening" if stage == "initial" else "live_observation")
        for selection, odds, source_at in (
            (left, left_odds, captured - 2000),
            (right, right_odds, captured - 1000),
        ):
            db.execute(
                "INSERT INTO research_timeline_snapshots VALUES(?,?,?,?,?,?,?,?,?,?,?)",
                (1, stage, provider, market, "2.5", selection, odds, 1, captured, source_at, source_origin),
            )

    def test_public_report_belongs_to_v2(self):
        self.assertEqual(
            DEFAULT_PUBLIC,
            "/var/www/stage_engine_v2/direction-path-conditions.json",
        )

    def test_wilson_versions_and_price_tier_are_separate(self):
        rows = []
        for index in range(21):
            rows.append(
                {
                    "fixture_id": str(index),
                    "kickoff": index,
                    "provider": "pinnacle",
                    "market": "OU",
                    "direction_path": "O→O→O",
                    "initial": {"side": "O", "line": 2.5, "odds": 1.91},
                    "T30": {"side": "O", "line": 2.5, "odds": 1.91},
                    "T5": {"side": "O", "line": 2.5, "odds": 1.71 if index == 0 else 1.91},
                    "settlement": "loss" if index == 20 else "win",
                    "unit_return": -1.0 if index == 20 else 0.8,
                    "cohort": "prospective",
                }
            )
        condition = build_report(rows)["conditions"][0]
        self.assertEqual(condition["prospective"]["odds_gt_1_7"]["observations"], 21)
        self.assertEqual(condition["prospective"]["odds_gt_1_75"]["observations"], 20)
        versions = condition["prospective"]["versions"]
        self.assertEqual(versions["active_version"], 2)
        self.assertEqual(versions["progress"], 1)
        self.assertEqual(versions["active"]["hits"], 0)
        self.assertEqual(versions["active"]["decided"], 1)
        self.assertEqual(versions["active"]["hit_rate"], 0)
        self.assertGreater(condition["prospective"]["odds_gt_1_7"]["wilson_95"]["low"], 0)

    def test_partial_path_is_tracked_but_not_extracted(self):
        db = self.make_radar_db()
        kickoff = 2_000_000
        db.execute("INSERT INTO matches VALUES(1,?,?,?)", (kickoff, "主隊", "客隊"))
        self.add_pair(db, "initial", 1.87, 2.05, captured=1_000_000)
        self.add_pair(db, "T30", 1.90, 2.00, captured=1_500_000)
        complete, tracking = extract_radar_data(db, tracking_now_ms=kickoff)
        self.assertEqual(complete, [])
        self.assertEqual(len(tracking), 1)
        self.assertEqual(tracking[0]["direction_path"], "O→O→?")
        self.assertEqual(tracking[0]["next_required"], "T5")
        self.assertIsNone(tracking[0]["condition_id"])

    def test_complete_path_maps_to_condition_and_uses_lower_odds(self):
        db = self.make_radar_db()
        kickoff = 2_000_000
        db.execute("INSERT INTO matches VALUES(1,?,?,?)", (kickoff, "主隊", "客隊"))
        self.add_pair(db, "initial", 1.87, 2.05, captured=1_000_000)
        self.add_pair(db, "T30", 1.90, 2.00, captured=1_500_000)
        self.add_pair(db, "T5", 2.05, 1.87, captured=1_900_000)
        complete, tracking = extract_radar_data(db, tracking_now_ms=kickoff)
        self.assertEqual(len(complete), 1)
        self.assertEqual(complete[0]["direction_path"], "O→O→U")
        self.assertEqual(complete[0]["initial"]["at"], 999_000)
        self.assertEqual(tracking[0]["condition_id"], "AUTO-PINNACLE-OU-OOU-L2_5")
        self.assertTrue(tracking[0]["complete"])

    def test_ah_ou_and_cou_use_the_same_three_stage_direction_rule(self):
        db = self.make_radar_db()
        kickoff = 2_000_000
        db.execute("INSERT INTO matches VALUES(1,?,?,?)", (kickoff, "主隊", "客隊"))
        for market in ("AH", "OU", "COU"):
            self.add_pair(db, "initial", 1.80, 2.05, market=market, captured=1_000_000)
            self.add_pair(db, "T30", 2.03, 1.82, market=market, captured=1_500_000)
            self.add_pair(db, "T5", 1.84, 2.01, market=market, captured=1_900_000)
        complete, tracking = extract_radar_data(db, tracking_now_ms=kickoff)
        self.assertEqual(
            {row["market"]: row["direction_path"] for row in complete},
            {
                "AH": "H→A→H",
                "OU": "O→U→O",
                "COU": "O→U→O",
            },
        )
        self.assertEqual({row["market"] for row in tracking}, {"AH", "OU", "COU"})
        self.assertTrue(all(row["eligible"] for row in tracking))

    def test_fake_live_initial_is_rejected(self):
        db = self.make_radar_db()
        kickoff = 2_000_000
        db.execute("INSERT INTO matches VALUES(1,?,?,?)", (kickoff, "主隊", "客隊"))
        self.add_pair(
            db,
            "initial",
            1.87,
            2.05,
            captured=1_000_000,
            origin="live_observation",
        )
        self.add_pair(db, "T30", 1.90, 2.00, captured=1_500_000)
        self.add_pair(db, "T5", 1.95, 1.98, captured=1_900_000)
        complete, tracking = extract_radar_data(db, tracking_now_ms=kickoff)
        self.assertEqual(complete, [])
        self.assertEqual(tracking[0]["next_required"], "initial")

    def test_same_line_and_strict_three_stage_price_rule(self):
        base = {
            "fixture_id": "strict",
            "kickoff": 1,
            "provider": "pinnacle",
            "market": "OU",
            "direction_path": "O→O→O",
            "initial": {"side": "O", "line": 2.5, "odds": 1.71},
            "T30": {"side": "O", "line": 2.5, "odds": 1.70},
            "T5": {"side": "O", "line": 2.5, "odds": 1.90},
            "settlement": "win",
            "unit_return": 0.9,
            "cohort": "prospective",
        }
        self.assertEqual(build_report([base])["summary"]["prospective"], 0)
        changed_line = json.loads(json.dumps(base))
        changed_line["T30"]["line"] = 2.75
        changed_line["T30"]["odds"] = 1.90
        self.assertEqual(build_report([changed_line])["summary"]["prospective"], 0)
        eligible = json.loads(json.dumps(base))
        eligible["T30"]["odds"] = 1.7001
        report = build_report([eligible])
        self.assertEqual(report["summary"]["prospective"], 1)
        self.assertEqual(report["conditions"][0]["line"], 2.5)

    def test_seed_and_live_rows_are_idempotent(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            seed = root / "seed.json"
            state = root / "state.json"
            public = root / "public.json"
            db_path = root / "radar.sqlite"
            seed.write_text("[]", encoding="utf-8")
            db = sqlite3.connect(db_path)
            db.executescript(
                """
                CREATE TABLE matches(
                  id INTEGER PRIMARY KEY,kickoff_utc INTEGER,home_team TEXT,away_team TEXT
                );
                CREATE TABLE research_timeline_snapshots(
                  match_id INTEGER,stage TEXT,provider TEXT,market TEXT,line_key TEXT,
                  selection TEXT,decimal_odds REAL,is_main INTEGER,captured_at INTEGER,
                  origin TEXT
                );
                CREATE TABLE research_results(
                  match_id INTEGER,home_score INTEGER,away_score INTEGER,
                  corners_total INTEGER,source TEXT
                );
                CREATE TABLE results(match_id INTEGER,home_score INTEGER,away_score INTEGER);
                """
            )
            db.execute("INSERT INTO matches VALUES(1,2000000,'主隊','客隊')")
            for stage, captured, origin in (
                ("initial", 1000000, "external_opening"),
                ("T30", 1500000, "live_observation"),
                ("T5", 1900000, "live_observation"),
            ):
                selected_odds = 1.71 if stage == "initial" else 1.80
                db.execute(
                    "INSERT INTO research_timeline_snapshots VALUES(?,?,?,?,?,?,?,?,?,?)",
                    (1, stage, "pinnacle", "OU", "2.5", "O", selected_odds, 1, captured, origin),
                )
                db.execute(
                    "INSERT INTO research_timeline_snapshots VALUES(?,?,?,?,?,?,?,?,?,?)",
                    (1, stage, "pinnacle", "OU", "2.5", "U", 2.10, 1, captured, origin),
                )
            db.commit()
            db.close()
            uri = f"file:{db_path}?mode=ro"
            first = update(str(seed), uri, str(state), str(public))
            second = update(str(seed), uri, str(state), str(public))
            self.assertEqual(first["summary"]["prospective"], 1)
            self.assertEqual(second["summary"]["prospective"], 1)
            condition = second["conditions"][0]
            self.assertEqual(condition["prospective"]["odds_gt_1_7"]["observations"], 1)
            self.assertEqual(condition["prospective"]["odds_gt_1_75"]["observations"], 0)


if __name__ == "__main__":
    unittest.main()
