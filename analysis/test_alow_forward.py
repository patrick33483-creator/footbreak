import copy
import json
import sqlite3
import unittest
import tempfile
import time
from pathlib import Path
from unittest.mock import patch
from alow_forward import classify,price_milli,original_match,eligibility,initialize,record,grade,metrics,run_once,RULE_FIELDS

START=10000000
NOW=START+100000
KO=NOW+300000
def fixture(price=1.75):
    m={"sid":"s1","league":"test","home":"H","away":"A","kickoff_utc":KO,"has_crown":1,"snapshots":{}}
    for st,t in [("initial",KO-7200000),("T30",KO-1800000),("T5",NOW)]:
        for mk in ["AH","OU"]:
            m["snapshots"][f"{st}_{mk}"]={"handicap":"0" if mk=="AH" else "2.25",
               "home_odds":round(price-1,3),"away_odds":.9,"captured_at":t,"stage":st,"market":mk}
    return m
class ForwardTests(unittest.TestCase):
    def test_boundaries(self):
        for p,b in [(1.699,None),(1.70,"ALOW-P170-v1"),(1.799,"ALOW-P170-v1"),(1.8,None),
                    (1.9,None),(1.999,None),(2,"ALOW-P200-v1"),(2.099,"ALOW-P200-v1"),(2.1,None),(2.2,None)]:
            self.assertEqual(classify(price_milli(round(p-1,3))),b)
    def test_past_and_activation(self):
        m=fixture()
        self.assertIsNotNone(eligibility(m,NOW,START)[0])
        self.assertEqual(eligibility(m,KO,START)[1],"first_observed_after_kickoff")
        m["snapshots"]["T5_OU"]["captured_at"]=START-1
        self.assertEqual(eligibility(m,NOW,START)[1],"pre_activation_T5")
    def test_future_snapshot(self):
        m=fixture();m["snapshots"]["T5_AH"]["captured_at"]=NOW+1
        self.assertEqual(eligibility(m,NOW,START)[1],"future_or_postkickoff_snapshot")
    def test_rule(self):
        for key,value in [("T5_AH","-.25"),("T30_AH",".25"),("T5_OU","2.5")]:
            m=fixture();m["snapshots"][key]["handicap"]=value;self.assertIsNone(original_match(m))
        m=fixture();del m["snapshots"]["initial_AH"];self.assertIsNone(original_match(m))
    def test_six_not_required(self):
        m=fixture();del m["snapshots"]["initial_OU"];del m["snapshots"]["T30_OU"]
        found,_=eligibility(m,NOW,START)
        self.assertIsNotNone(found);self.assertFalse(found["strict_six"])
    def test_dedup_and_freeze(self):
        db=sqlite3.connect(":memory:");db.row_factory=sqlite3.Row;config={"effective_at_ms":START}
        initialize(db,config);self.assertTrue(record(db,fixture(),NOW,config))
        self.assertFalse(record(db,fixture(),NOW+1,config))
        self.assertFalse(record(db,fixture(2.05),NOW+2,config))
        s=dict(db.execute("SELECT * FROM signals").fetchone())
        self.assertEqual(s["price_milli"],1750);self.assertEqual(s["band"],"ALOW-P170-v1")
        self.assertEqual(db.execute("SELECT count(*) FROM signals").fetchone()[0],1)
        self.assertEqual(db.execute("SELECT reason FROM audit").fetchone()[0],"later_band_change_no_reentry")
    def test_no_posthoc(self):
        db=sqlite3.connect(":memory:");db.row_factory=sqlite3.Row
        initialize(db,{"effective_at_ms":START})
        self.assertFalse(record(db,fixture(),KO+1,{"effective_at_ms":START}))
        self.assertEqual(db.execute("SELECT count(*) FROM signals").fetchone()[0],0)
    def test_settlement(self):
        signal={"sid":"s1","kickoff":KO,"price_milli":2050}
        r={"status":"完","home_score":2,"away_score":1,"ht_home_score":1,"ht_away_score":0,"fetched_at":KO+7200000}
        for score,outcome,units in [(0,"L",-1),(1,"P",0),(2,"W",1.05)]:
            a=grade(signal,{**r,"home_score":score,"ht_home_score":0},KO,KO+8000000)
            self.assertEqual(a["outcome"],outcome);self.assertAlmostEqual(a["units"],units)
        self.assertEqual(grade(signal,r,KO+1,KO+8000000)["state"],"review")
        self.assertEqual(grade(signal,{**r,"status":"中場"},KO,KO+8000000)["state"],"pending")
        self.assertEqual(grade(signal,{**r,"home_score":-1},KO,KO+8000000)["state"],"review")
    def test_freeze_config(self):
        db=sqlite3.connect(":memory:");initialize(db,{"a":1})
        with self.assertRaises(RuntimeError):initialize(db,{"a":2})
    def test_zero_and_push(self):
        self.assertIsNone(metrics([])["hit_rate"])
        self.assertIsNone(metrics([{"state":"settled","outcome":"P","units":0}])["hit_rate"])
    def test_end_to_end_readonly_and_corrected_result(self):
        with tempfile.TemporaryDirectory() as temp:
            root=Path(temp);ops=root/"ops";ops.mkdir();data=root/"data";data.mkdir()
            config={"version":"test","effective_at_ms":START}
            (ops/"config.json").write_text(json.dumps(config))
            (data/"rules.json").write_text(json.dumps({"rules":[{"id":"ch-Alow",**RULE_FIELDS,"enabled":False}]}))
            prod=sqlite3.connect(data/"crown.db")
            prod.executescript("""
              CREATE TABLE matches(sid TEXT,league TEXT,home TEXT,away TEXT,kickoff_utc INTEGER,has_crown INTEGER);
              CREATE TABLE crown_snapshots(id INTEGER,sid TEXT,stage TEXT,market TEXT,handicap TEXT,home_odds REAL,away_odds REAL,captured_at INTEGER);
              CREATE TABLE finished_matches(sid TEXT,status TEXT,home_score INTEGER,away_score INTEGER,ht_home_score INTEGER,ht_away_score INTEGER,fetched_at INTEGER);
            """)
            m=fixture(2.05);prod.execute("INSERT INTO matches VALUES(?,?,?,?,?,?)",("s1","test","H","A",KO,1))
            for i,s in enumerate(m["snapshots"].values()):
                prod.execute("INSERT INTO crown_snapshots VALUES(?,?,?,?,?,?,?,?)",(i,"s1",s["stage"],s["market"],s["handicap"],s["home_odds"],s["away_odds"],s["captured_at"]))
            prod.commit()
            import hashlib
            before=hashlib.sha256((data/"crown.db").read_bytes()).hexdigest()
            with patch("alow_forward.time.time",return_value=NOW/1000):
                a=run_once(ops,root)
            self.assertEqual(a["bands"]["ALOW-P200-v1"]["main"]["observed"],1)
            self.assertEqual(a["bands"]["ALOW-P170-v1"]["main"]["observed"],0)
            self.assertEqual(before,hashlib.sha256((data/"crown.db").read_bytes()).hexdigest())
            prod.execute("INSERT INTO finished_matches VALUES(?,?,?,?,?,?,?)",("s1","完",2,1,0,0,KO+8000000));prod.commit()
            with patch("alow_forward.time.time",return_value=(KO+9000000)/1000):
                a=run_once(ops,root)
            self.assertEqual(a["bands"]["ALOW-P200-v1"]["main"]["pnl_units"],1.05)
            prod.execute("UPDATE finished_matches SET home_score=0,fetched_at=?",(KO+10000000,));prod.commit()
            with patch("alow_forward.time.time",return_value=(KO+11000000)/1000):
                a=run_once(ops,root)
            self.assertEqual(a["bands"]["ALOW-P200-v1"]["main"]["pnl_units"],-1)
            shadow=sqlite3.connect(ops/"ledger.sqlite")
            self.assertEqual(shadow.execute("SELECT count(*) FROM result_history").fetchone()[0],3)
            shadow.close();prod.close()
if __name__=="__main__":unittest.main()
