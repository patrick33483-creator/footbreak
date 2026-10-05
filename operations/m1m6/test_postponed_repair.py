import copy
import sqlite3
import unittest
from repair_postponed_3056290 import SID, KO, validate, apply_verified


class PostponedRepairTest(unittest.TestCase):
    def setUp(self):
        self.match={"sid":SID,"home":"杜连斯U23","away":"埃斯托里尔U23","kickoff_utc":KO}
        self.prior={"sid":SID,"status":"推迟","home_score":None,"away_score":None,
                    "ht_home_score":None,"ht_away_score":None,"fetched_at":KO-1000000000}
        cells=[""]*80
        for index,value in {0:self.match["home"],1:self.match["away"],4:"-1",5:"20261006000000",
                            10:"2",11:"3",15:"葡U23",76:SID}.items():
            cells[index]=value
        self.cells=cells
        self.raw="^".join(cells)
        self.now=KO+8*3600000

    def test_current_final_exact_match_allowed(self):
        r=validate(self.match,self.prior,self.raw,self.now)
        self.assertEqual((r["sid"],r["home_score"],r["away_score"]),(SID,2,3))

    def test_other_fixture_or_changed_kickoff_rejected(self):
        for change in ({"sid":"1"},{"kickoff_utc":KO+60000},{"home":"other"}):
            with self.assertRaises(ValueError):
                validate({**self.match,**change},self.prior,self.raw,self.now)

    def test_nonfinal_wrong_sid_teams_date_or_score_rejected(self):
        for index,value in ((4,"0"),(76,"123"),(0,"other"),(5,"20260915000000"),(10,"1")):
            c=self.cells[:];c[index]=value
            with self.assertRaises(ValueError):
                validate(self.match,self.prior,"^".join(c),self.now)

    def test_cancelled_abandoned_recent_postponement_or_existing_score_rejected(self):
        for change in ({"status":"取消"},{"status":"腰斩"},{"status":"中断"},
                       {"fetched_at":KO+1},{"home_score":0}):
            with self.assertRaises(ValueError):
                validate(self.match,{**self.prior,**change},self.raw,self.now)

    def database(self):
        db=sqlite3.connect(":memory:");db.row_factory=sqlite3.Row
        db.executescript("""
          CREATE TABLE matches(sid TEXT,home TEXT,away TEXT,kickoff_utc INTEGER);
          CREATE TABLE finished_matches(sid TEXT PRIMARY KEY,status TEXT,home_score INTEGER,
            away_score INTEGER,ht_home_score INTEGER,ht_away_score INTEGER,fetched_at INTEGER);
          CREATE TABLE strategy_result_sync_audit(id INTEGER PRIMARY KEY,sid TEXT,observed_at INTEGER,
            written_at INTEGER,source_url TEXT,previous_json TEXT,evidence_json TEXT);
        """)
        db.execute("INSERT INTO matches VALUES(?,?,?,?)",tuple(self.match.values()))
        db.execute("INSERT INTO finished_matches VALUES(?,?,?,?,?,?,?)",tuple(self.prior.values()))
        db.execute("INSERT INTO finished_matches VALUES('other','完',4,0,2,0,1)")
        db.commit()
        self.addCleanup(db.close)
        return db

    def test_one_row_audited_and_repeat_is_noop(self):
        db=self.database()
        proof=validate(self.match,self.prior,self.raw,self.now)
        result=apply_verified(db,self.match,self.prior,proof,self.now+1)
        self.assertTrue(result["written"])
        self.assertEqual(db.execute("SELECT home_score FROM finished_matches WHERE sid='other'").fetchone()[0],4)
        self.assertEqual(db.execute("SELECT COUNT(*) FROM strategy_result_sync_audit").fetchone()[0],1)
        result=apply_verified(db,self.match,self.prior,proof,self.now+2)
        self.assertTrue(result["already_correct"])
        self.assertEqual(db.execute("SELECT COUNT(*) FROM strategy_result_sync_audit").fetchone()[0],1)

    def test_concurrent_result_or_expired_evidence_aborts(self):
        proof=validate(self.match,self.prior,self.raw,self.now)
        db=self.database()
        with self.assertRaises(ValueError):
            apply_verified(db,self.match,self.prior,proof,self.now+120001)
        db.execute("UPDATE finished_matches SET status='取消' WHERE sid=?",(SID,));db.commit()
        with self.assertRaises(ValueError):
            apply_verified(db,self.match,self.prior,proof,self.now+1)
        self.assertEqual(db.execute("SELECT COUNT(*) FROM strategy_result_sync_audit").fetchone()[0],0)


if __name__=="__main__":
    unittest.main()
