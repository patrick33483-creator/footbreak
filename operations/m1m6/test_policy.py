import copy
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

import policy
import notifier

class RulesTest(unittest.TestCase):
    def setUp(self):
        self.ko=1790899200000
        self.now=self.ko-120000
        self.m={"sid":"1","kickoff_utc":self.ko,"home":"測試主","away":"測試客","league":"測試"}
        self.s={}
        for market in ["AH","OU"]:
            for stage,lag in [("initial",3600000),("T30",1800000),("T5",240000)]:
                self.s[(stage,market)]={"handicap":"0" if market=="AH" else "3.25","home_odds":.9,"away_odds":.9,"captured_at":self.ko-lag}
        self.s[("T5","AH")]["home_odds"]=.87
    def test_m1_boundary_and_dedupe(self):
        hits,_=policy.evaluate(self.m,self.s,{},self.now)
        self.assertEqual([h["rule_id"] for h in hits],["M1"])
        self.s[("T5","AH")]["away_odds"]=.93
        hits,_=policy.evaluate(self.m,self.s,{},self.now)
        self.assertEqual(len(hits),1)
    def test_same_line_required(self):
        self.s[("T30","AH")]["handicap"]="-0.25"
        hits,_=policy.evaluate(self.m,self.s,{},self.now)
        self.assertNotIn("M1",[h["rule_id"] for h in hits])
    def test_missing_bad_timing_and_postkickoff(self):
        for key,at in [(("T30","AH"),self.ko-20*60000),(("T5","OU"),self.ko),(("initial","AH"),self.ko-100000)]:
            s=copy.deepcopy(self.s);s[key]["captured_at"]=at
            self.assertEqual(policy.evaluate(self.m,s,{},self.now)[0],[])
        s=copy.deepcopy(self.s);del s[("initial","OU")]
        self.assertEqual(policy.evaluate(self.m,s,{},self.now)[0],[])
    def test_quarterline_settlement(self):
        h={"market":"OU","side":"under","line":3.25,"hk":.8}
        self.assertEqual(policy.settle(h,{"home_score":1,"away_score":2,"fetched_at":1})["pnl"],.4)
        self.assertEqual(policy.settle(h,{"home_score":1,"away_score":3,"fetched_at":1})["result"],"L")
    def test_or_windows_and_no_push_replacement(self):
        h=[{"sid":str(i),"ko":i,"result":"W","pnl":.8} for i in range(30)]
        h[-1].update(result="L",pnl=-1)
        self.assertTrue(policy.gates(h)["20"]["pass"])
        h[-2].update(result="L",pnl=-1)
        self.assertFalse(policy.gates(h)["20"]["pass"])
        self.assertTrue(policy.gates(h)["30"]["pass"])
        for r in h[-20:]:
            r.update(result="P",pnl=0)
        self.assertFalse(policy.gates(h)["pass"])
    def test_batch_lock_same_scan_dedupe_and_cutover(self):
        h=policy.evaluate(self.m,self.s,{},self.now)[0][0]
        second={**h,"sid":"2"};duplicate={**h,"rule_id":"M4"}
        gate={i:{"pass":True} for i in ["M1","M4"]}
        self.assertEqual(len(policy.choose_batch([h,second,duplicate],gate,self.now,0)),2)
        self.assertEqual(policy.choose_batch([h,second],gate,self.now,0,locked=True),[])
        self.assertEqual(policy.choose_batch([h],gate,self.now,self.now),[])
        self.assertEqual(policy.choose_batch([h],gate,self.ko,0),[])
    def test_future_model_not_used(self):
        cp={"INITIAL":{"locked_at_ms":self.ko+1,"prediction":{"pred_ah":"主"}}}
        self.assertIsNone(policy.initial_model(cp,self.ko,self.now))
    def test_result_must_be_official_and_available(self):
        f={"status":"完","home_score":1,"away_score":0,"fetched_at":self.ko+100}
        self.assertFalse(policy.valid_result(f,self.ko,self.ko))
        self.assertTrue(policy.valid_result(f,self.ko,self.ko+101))
        f["status"]="取消"
        self.assertFalse(policy.valid_result(f,self.ko,self.ko+101))

class BatchLedgerTest(unittest.TestCase):
    def test_wait_for_all_and_preserve_uncertain_no_resend(self):
        with tempfile.TemporaryDirectory() as tmp,patch.object(notifier,"STATE",Path(tmp)):
            db=notifier.state_db()
            db.execute("INSERT INTO batches(id,created_at,status) VALUES(1,1,'open')")
            for sid,status in [("1","sent"),("2","uncertain")]:
                h={"sid":sid,"ko":10,"market":"OU","side":"under","line":3.25,"hk":.8}
                db.execute("INSERT INTO items(bet_key,batch_id,sid,ko,payload,status) VALUES(?,1,?,10,?,?)",
                           (sid,sid,json.dumps(h),status))
            db.commit()
            f={"status":"完","home_score":1,"away_score":0,"fetched_at":20}
            self.assertEqual(notifier.reconcile(db,{"1":f},30)["waiting"],1)
            self.assertIsNone(notifier.reconcile(db,{"1":f,"2":f},30))
            self.assertEqual(db.execute("SELECT status FROM batches").fetchone()[0],"closed")
            self.assertEqual(db.execute("SELECT status FROM items WHERE sid='2'").fetchone()[0],"uncertain")
            db.close()
    def test_interrupted_prepared_is_not_backfilled(self):
        with tempfile.TemporaryDirectory() as tmp,patch.object(notifier,"STATE",Path(tmp)):
            db=notifier.state_db()
            db.execute("INSERT INTO batches(id,created_at,status) VALUES(1,1,'open')")
            db.execute("INSERT INTO items(bet_key,batch_id,sid,ko,payload,status) VALUES('x',1,'1',100,'{}','prepared')")
            db.commit()
            self.assertIsNone(notifier.reconcile(db,{},30))
            self.assertEqual(db.execute("SELECT status FROM items").fetchone()[0],"skipped")
            db.close()

if __name__=="__main__":
    unittest.main()
