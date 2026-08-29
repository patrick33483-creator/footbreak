import json
import sqlite3
import tempfile
import unittest
from pathlib import Path

from analysis.footbreak_direction_path_conditions import extract_footbreak, update


class FootbreakDirectionPathConditionsTest(unittest.TestCase):
    def make_db(self, path: Path) -> None:
        db = sqlite3.connect(path)
        db.executescript(
            """
            CREATE TABLE prediction_snapshots(
              snapshot_id INTEGER PRIMARY KEY,system TEXT,fixture_id TEXT,stage TEXT,
              generated_at TEXT,kickoff TEXT,payload_json TEXT,pre_kickoff INTEGER
            );
            CREATE TABLE stage_snapshot_reconciliations(
              system TEXT,fixture_id TEXT,stage TEXT,canonical_snapshot_id INTEGER
            );
            CREATE TABLE grades(
              grade_id INTEGER PRIMARY KEY,snapshot_id INTEGER,market TEXT,target TEXT,
              state TEXT,metrics_json TEXT,grade_attempt INTEGER
            );
            """
        )
        for index, (stage, side, odds) in enumerate(
            (("首預", "H", 1.65), ("T-30", "H", 1.68), ("T-5", "H", 1.69)), start=1
        ):
            payload = {
                "home": "主隊",
                "away": "客隊",
                "market_predictions": [
                    {"code": "HIL", "side": side, "line": 2.75, "odds": odds}
                ],
            }
            db.execute(
                "INSERT INTO prediction_snapshots VALUES(?,?,?,?,?,?,?,1)",
                (
                    index,
                    "footbreak",
                    "f1",
                    stage,
                    f"2026-08-28T0{index}:00:00+00:00",
                    "2026-08-28T10:00:00+00:00",
                    json.dumps(payload),
                ),
            )
            if stage == "T-5":
                db.execute(
                    "INSERT INTO grades VALUES(1,?,'HIL','2.75|H','GRADED',?,1)",
                    (index, json.dumps({"target": 1.0})),
                )
        db.commit()
        db.close()

    def test_complete_path_accumulates_below_170(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            db_path = root / "learning.sqlite"
            self.make_db(db_path)
            db = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
            db.row_factory = sqlite3.Row
            rows, _ = extract_footbreak(db)
            db.close()
            self.assertEqual(len(rows), 1)
            self.assertEqual(rows[0]["direction_path"], "O→O→O")
            self.assertEqual(rows[0]["settlement"], "win")
            report = update(
                f"file:{db_path}?mode=ro",
                str(root / "state.json"),
                str(root / "public.json"),
            )
            condition = report["conditions"][0]
            self.assertEqual(condition["historical"]["all_odds"]["observations"], 1)
            self.assertEqual(condition["historical"]["odds_gte_1_70"]["observations"], 0)

    def test_second_run_is_idempotent(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            db_path = root / "learning.sqlite"
            self.make_db(db_path)
            args = (
                f"file:{db_path}?mode=ro",
                str(root / "state.json"),
                str(root / "public.json"),
            )
            update(*args)
            report = update(*args)
            self.assertEqual(report["summary"]["unique_observations"], 1)
            self.assertEqual(report["summary"]["historical"], 1)
            self.assertEqual(report["summary"]["prospective"], 0)


if __name__ == "__main__":
    unittest.main()
