import json
import sqlite3
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch
from contextlib import closing

import result_refresh as events
import result_revisions as revisions
from policy import settle
from performance_view import summarize


class ScoreRevisionTest(unittest.TestCase):
    def setUp(self):
        self.db = sqlite3.connect(":memory:")
        self.db.row_factory = sqlite3.Row
        self.db.executescript("""
          CREATE TABLE items(bet_key TEXT PRIMARY KEY,batch_id INTEGER,sid TEXT,ko INTEGER,
            payload TEXT,status TEXT,attempt_at INTEGER,ack_at INTEGER,message_id INTEGER,
            result_json TEXT,error TEXT);
          CREATE TABLE batches(id INTEGER PRIMARY KEY,status TEXT,closed_at INTEGER);
          CREATE TABLE strategy_batches(id INTEGER PRIMARY KEY,strategy_id TEXT,closed_at INTEGER);
          CREATE TABLE strategy_batch_items(batch_id INTEGER,bet_key TEXT);
          INSERT INTO batches VALUES(1,'closed',2000);
          INSERT INTO batches VALUES(2,'open',NULL);
          INSERT INTO strategy_batches VALUES(1,'D0036',2000);
          INSERT INTO strategy_batches VALUES(2,'D0115',NULL);
          INSERT INTO strategy_batch_items VALUES(1,'1:OU:over:2.75');
          INSERT INTO strategy_batch_items VALUES(2,'pending');
        """)
        self.hit = {"sid": "1", "ko": 1000, "market": "OU", "side": "over", "line": 2.75,
                    "hk": .75, "rules": ["D0036"], "rule_id": "D0036", "strategy_version": 2}
        self.old = {"status": "完", "home_score": 0, "away_score": 2, "fetched_at": 2000}
        self.new = {"status": "完", "home_score": 1, "away_score": 3, "fetched_at": 3000}
        self.add(self.hit, self.old)

    def tearDown(self):
        self.db.close()

    def add(self, hit, f, key="1:OU:over:2.75", status="sent"):
        result = json.dumps(settle(hit, f)) if f else None
        self.db.execute("INSERT INTO items VALUES(?,1,?,?,?, ?,800,900,123,?,NULL)",
                        (key, hit["sid"], hit["ko"], json.dumps(hit), status, result))
        self.db.commit()

    def result(self):
        return json.loads(self.db.execute("SELECT result_json FROM items LIMIT 1").fetchone()[0])

    def locks(self):
        return {t: [tuple(r) for r in self.db.execute(f"SELECT * FROM {t}")]
                for t in ("batches", "strategy_batches", "strategy_batch_items")}

    def test_legacy_closed_loss_becomes_win_and_no_payload_or_lock_change(self):
        locks = self.locks()
        original = tuple(self.db.execute("SELECT payload,status,attempt_at,ack_at,message_id FROM items").fetchone())
        events.enqueue(self.db, "1", 2000)
        self.db.execute("UPDATE result_refresh_events SET completed_at=2500")
        r = revisions.synchronize(self.db, {"1": self.new}, 4000, {"1": 1000})
        self.assertEqual(r["items_corrected"], 1)
        self.assertEqual((self.result()["result"], self.result()["pnl"]), ("W", .75))
        self.assertEqual(self.locks(), locks)
        self.assertEqual(original, tuple(self.db.execute(
            "SELECT payload,status,attempt_at,ack_at,message_id FROM items").fetchone()))
        self.assertEqual(tuple(self.db.execute("SELECT revision,completed_at FROM result_refresh_events").fetchone()), (2, None))
        audit = self.db.execute("SELECT * FROM result_score_corrections").fetchone()
        self.assertEqual(json.loads(audit["old_result_json"])["pnl"], -1)
        self.assertEqual(json.loads(audit["new_result_json"])["pnl"], .75)
        again = revisions.synchronize(self.db, {"1": self.new}, 5000, {"1": 1000})
        self.assertEqual(again["items_corrected"], 0)
        self.assertEqual(self.db.execute("SELECT COUNT(*) FROM result_score_corrections").fetchone()[0], 1)

    def test_bad_nonfinal_future_or_stale_score_cannot_change_item(self):
        for f in ({**self.new, "status": "中"}, {**self.new, "home_score": -1},
                  {**self.new, "home_score": True}, {**self.new, "fetched_at": 5000},
                  {**self.new, "fetched_at": 1500}):
            revisions.synchronize(self.db, {"1": f}, 4000, {"1": 1000})
            self.assertEqual(self.result()["result"], "L")

    def test_repeated_source_corrections_and_same_outcome_are_audited(self):
        revisions.synchronize(self.db, {"1": self.new}, 4000, {"1": 1000})
        newer = {**self.new, "home_score": 0, "away_score": 4, "fetched_at": 5000}
        r = revisions.synchronize(self.db, {"1": newer}, 6000, {"1": 1000})
        self.assertEqual((r["sources_changed"], r["items_corrected"]), (1, 1))
        self.assertEqual((self.result()["score"], self.result()["pnl"]), ("0:4", .75))
        stale = revisions.synchronize(self.db, {"1": self.new}, 7000, {"1": 1000})
        self.assertEqual(stale["items_corrected"], 0)
        self.assertEqual(self.result()["score"], "0:4")
        latest = {**self.old, "fetched_at": 8000}
        revisions.synchronize(self.db, {"1": latest}, 9000, {"1": 1000})
        self.assertEqual(self.result()["result"], "L")

    def test_historical_only_revision_queues_without_any_notification(self):
        self.db.execute("DELETE FROM items")
        revisions.synchronize(self.db, {"1": self.old}, 2500, {"1": 1000})
        self.assertEqual(events.pending(self.db), [])
        r = revisions.synchronize(self.db, {"1": self.new}, 4000, {"1": 1000})
        self.assertEqual(r["queued_sids"], ["1"])
        self.assertEqual(r["items_corrected"], 0)
        self.assertEqual(self.db.execute("SELECT COUNT(*) FROM items").fetchone()[0], 0)

    def test_half_results_daily_total_and_strategy_attribution(self):
        self.db.execute("DELETE FROM items")
        hit = {**self.hit, "market": "AH", "side": "home", "line": -.25,
               "rules": ["D0115", "D0999"]}
        old = {**self.old, "home_score": 2}
        self.add(hit, old)
        epoch = {"id": "test", "started_at": 100, "excluded_sids": []}
        before = summarize(self.db, epoch, 4000)
        self.assertEqual(before["total"]["pnl"], -.5)
        revisions.synchronize(self.db, {"1": {**self.new, "home_score": 3, "away_score": 2}}, 4000, {"1": 1000})
        after = summarize(self.db, epoch, 4000)
        self.assertEqual(after["total"]["pnl"], .75)
        self.assertEqual(after["total"]["settled"], 1)
        self.assertEqual(after["daily"][0]["pnl"], .75)
        self.assertTrue(all(s["pnl"] == .75 for s in after["by_strategy"]))
        self.assertEqual(summarize(self.db, {"id": "later", "started_at": 1100}, 4000)["total"]["settled"], 0)

    def test_uncertain_is_corrected_but_skipped_and_unsettled_are_untouched(self):
        self.db.execute("UPDATE items SET status='uncertain'")
        self.add({**self.hit, "sid": "2"}, self.old, "skipped", "skipped")
        self.add({**self.hit, "sid": "3"}, None, "pending")
        r = revisions.synchronize(self.db, {s: self.new for s in ("1", "2", "3")}, 4000,
                                  {s: 1000 for s in ("1", "2", "3")})
        self.assertEqual(r["items_corrected"], 1)
        self.assertIsNone(self.db.execute("SELECT result_json FROM items WHERE bet_key='pending'").fetchone()[0])
        self.assertEqual(json.loads(self.db.execute(
            "SELECT result_json FROM items WHERE bet_key='skipped'").fetchone()[0])["result"], "L")

    def test_queue_failure_rolls_back_result_and_audit_together(self):
        revisions.schema(self.db)
        self.db.commit()
        with patch.object(revisions, "enqueue", side_effect=RuntimeError("injected")):
            with self.assertRaises(RuntimeError):
                revisions.synchronize(self.db, {"1": self.new}, 4000, {"1": 1000})
        self.assertEqual(self.result()["result"], "L")
        self.assertEqual(self.db.execute("SELECT COUNT(*) FROM result_score_corrections").fetchone()[0], 0)
        self.assertEqual(self.db.execute("SELECT COUNT(*) FROM result_source_versions").fetchone()[0], 0)

    def test_legacy_event_migration_and_midsearch_revision_not_consumed(self):
        with tempfile.TemporaryDirectory() as tmp, patch.object(events, "DB", Path(tmp) / "ledger.sqlite"):
            with closing(sqlite3.connect(events.DB)) as db, db:
                db.execute("CREATE TABLE result_refresh_events(sid TEXT PRIMARY KEY,queued_at INTEGER,completed_at INTEGER)")
                db.execute("INSERT INTO result_refresh_events VALUES('1',100,NULL)")
            first = events.snapshot_tokens()
            self.assertEqual(first, [{"sid": "1", "revision": 1}])
            with closing(sqlite3.connect(events.DB)) as db, db:
                events.enqueue(db, "1", 200, correction=True)
            events.complete_tokens(first, 300)
            second = events.snapshot_tokens()
            self.assertEqual(second, [{"sid": "1", "revision": 2}])
            events.complete_tokens(second, 400)
            self.assertEqual(events.snapshot_tokens(), [])
            with closing(sqlite3.connect(events.DB)) as db, db:
                events.enqueue(db, "1", 500, correction=True)
            self.assertEqual(events.snapshot_tokens(), [{"sid": "1", "revision": 3}])


if __name__ == "__main__":
    unittest.main()
