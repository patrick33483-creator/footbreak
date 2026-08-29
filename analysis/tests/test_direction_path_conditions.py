import json
import sqlite3
import tempfile
import unittest
from pathlib import Path

from analysis.direction_path_conditions import DEFAULT_PUBLIC, build_report, update


class DirectionPathConditionsTest(unittest.TestCase):
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
                    "initial": {"side": "O", "line": 2.5, "odds": 1.6},
                    "T30": {"side": "O", "line": 2.5, "odds": 1.6},
                    "T5": {"side": "O", "line": 2.5, "odds": 1.60 if index == 0 else 1.80},
                    "settlement": "loss" if index == 20 else "win",
                    "unit_return": -1.0 if index == 20 else 0.8,
                    "cohort": "prospective",
                }
            )
        condition = build_report(rows)["conditions"][0]
        self.assertEqual(condition["prospective"]["all_odds"]["observations"], 21)
        self.assertEqual(condition["prospective"]["odds_gte_1_70"]["observations"], 20)
        versions = condition["prospective"]["versions"]
        self.assertEqual(versions["active_version"], 2)
        self.assertEqual(versions["progress"], 1)
        self.assertEqual(versions["active"]["hits"], 0)
        self.assertEqual(versions["active"]["decided"], 1)
        self.assertEqual(versions["active"]["hit_rate"], 0)
        self.assertGreater(condition["prospective"]["all_odds"]["wilson_95"]["low"], 0)

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
                db.execute(
                    "INSERT INTO research_timeline_snapshots VALUES(?,?,?,?,?,?,?,?,?,?)",
                    (1, stage, "pinnacle", "OU", "2.5", "O", 1.65, 1, captured, origin),
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
            self.assertEqual(condition["prospective"]["all_odds"]["observations"], 1)
            self.assertEqual(condition["prospective"]["odds_gte_1_70"]["observations"], 0)


if __name__ == "__main__":
    unittest.main()
