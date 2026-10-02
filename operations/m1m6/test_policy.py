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
        h=[{"sid":str(i),"ko":i,"result":"W","pnl":.8,"hk":.8} for i in range(30)]
        h[-1].update(result="L",pnl=-1)
        self.assertTrue(policy.gates(h)["20"]["pass"])
        h[-2].update(result="L",pnl=-1)
        self.assertFalse(policy.gates(h)["20"]["pass"])
        h[-3].update(result="L",pnl=-1)
        self.assertFalse(policy.gates(h)["20"]["pass"])
        self.assertTrue(policy.gates(h)["30"]["pass"])
        for r in h[-20:]:
            r.update(result="P",pnl=0)
        self.assertFalse(policy.gates(h)["pass"])
    def test_no_lock_dedupe_and_cutover(self):
        h=policy.evaluate(self.m,self.s,{},self.now)[0][0]
        second={**h,"sid":"2"};duplicate={**h,"rule_id":"M4"}
        gate={i:{"pass":True} for i in ["M1","M4"]}
        self.assertEqual(len(policy.choose_batch([h,second,duplicate],gate,self.now,0)),2)
        self.assertEqual(len(policy.choose_batch([h,second],gate,self.now,0,locked=True)),2)
        self.assertEqual(policy.choose_batch([h],gate,self.now,self.now),[])
        self.assertEqual(policy.choose_batch([h],gate,self.ko,0),[])
    def test_future_model_not_used(self):
        cp={"INITIAL":{"locked_at_ms":self.ko+1,"prediction":{"pred_ah":"主"}}}
        self.assertIsNone(policy.initial_model(cp,self.ko,self.now))
    def test_any_kickoff_allowed_despite_legacy_lock(self):
        h=policy.evaluate(self.m,self.s,{},self.now)[0][0]
        same={**h,"sid":"2"}
        later={**h,"sid":"3","ko":h["ko"]+60000}
        gate={"M1":{"pass":True}}
        lock={"batch_id":1,"kickoff_utc":h["ko"]}
        selected=policy.choose_batch([h,same,later],gate,self.now,0,lock,{policy.key(h)})
        self.assertEqual([x["sid"] for x in selected],["2","3"])
        self.assertEqual(policy.choose_batch([same],gate,h["ko"],0,lock),[])
        self.assertEqual(len(policy.choose_batch([same],gate,self.now,0,{"batch_id":1})),1)
        self.assertEqual(policy.choose_batch([same],{"M1":{"pass":False}},self.now,0,lock),[])
    def test_all_qualified_kickoffs_selected(self):
        h=policy.evaluate(self.m,self.s,{},self.now)[0][0]
        later={**h,"sid":"2","ko":h["ko"]+60000}
        selected=policy.choose_batch([later,h],{"M1":{"pass":True}},self.now,0)
        self.assertEqual({x["sid"] for x in selected},{"1","2"})
    def test_incomplete_thirty_does_not_block_passing_twenty(self):
        h=[{"sid":str(i),"ko":i,"result":"W","pnl":.8,"hk":.8} for i in range(22)]
        h[-1].update(result="L",pnl=-1)
        g=policy.gates(h)
        self.assertTrue(g["20"]["pass"])
        self.assertFalse(g["30"]["pass"])
        self.assertTrue(g["pass"])
        h[-2].update(result="L",pnl=-1)
        self.assertFalse(policy.gates(h)["pass"])
        h[-3].update(result="L",pnl=-1)
        self.assertFalse(policy.gates(h)["pass"])
    def test_new_threshold_boundaries_and_push_denominator(self):
        h=[{"sid":str(i),"ko":i,"result":"W","pnl":.8,"hk":.8} for i in range(30)]
        for r in h[-3:]:
            r.update(result="L",pnl=-1)
        self.assertTrue(policy.gates(h)["30"]["pass"])  # 27/30
        h[-4].update(result="L",pnl=-1)
        self.assertFalse(policy.gates(h)["pass"])
        h[-4].update(result="P",pnl=0)
        self.assertTrue(policy.gates(h)["30"]["pass"])  # 26/29 exceeds 87.5%
        h[-3].update(result="W",pnl=.8)
        self.assertTrue(policy.gates(h)["30"]["pass"])  # 27/29
        h=[{"sid":str(i),"ko":i,"result":"W","pnl":.8,"hk":.8} for i in range(20)]
        h[-1].update(result="P",pnl=0);h[-2].update(result="L",pnl=-1)
        self.assertTrue(policy.gates(h)["20"]["pass"])  # 18/19 exceeds 92.5%
        h[-2].update(result="W",pnl=.8)
        self.assertTrue(policy.gates(h)["20"]["pass"])  # 19/19
        h[-3].update(result="L",pnl=-1)
        self.assertTrue(policy.gates(h)["20"]["pass"])  # 18/19
        h[-4].update(result="L",pnl=-1)
        self.assertFalse(policy.gates(h)["20"]["pass"])  # 17/19
    def test_result_must_be_official_and_available(self):
        f={"status":"完","home_score":1,"away_score":0,"fetched_at":self.ko+100}
        self.assertFalse(policy.valid_result(f,self.ko,self.ko))
        self.assertTrue(policy.valid_result(f,self.ko,self.ko+101))
        f["status"]="取消"
        self.assertFalse(policy.valid_result(f,self.ko,self.ko+101))
    def test_price_floor_before_window_selection_and_push_exclusion(self):
        rows=[{"sid":str(i),"ko":i,"result":"W","pnl":.7,"hk":.7} for i in range(30)]
        low=[{"sid":str(i),"ko":i,"result":"W","pnl":.69,"hk":.69} for i in range(30,70)]
        g=policy.gates(rows+low)
        self.assertEqual(g["20"]["sids"],[str(i) for i in range(10,30)])
        self.assertEqual(g["30"]["n"],30)
        self.assertEqual(g["30"]["min_selected_hk"],.7)
        self.assertTrue(g["pass"])
        self.assertFalse(policy.gates(rows[:19]+low)["pass"])
        rows[-1].update(result="P",pnl=0)
        self.assertEqual(policy.gates(rows+low)["20"]["den"],19)
        self.assertFalse(policy.eligible_price({"hk":.699999}))
        self.assertFalse(policy.eligible_price({}))
        self.assertTrue(policy.eligible_price({"hk":.7}))
    def test_old_low_price_bet_still_settles(self):
        h={"market":"OU","side":"under","line":3.25,"hk":.65}
        self.assertEqual(policy.settle(h,{"home_score":1,"away_score":0,"fetched_at":1})["pnl"],.65)

class BatchLedgerTest(unittest.TestCase):
    def setUp(self):
        p=patch.object(notifier,"REGISTRY",Path("/nonexistent-test-registry.json"))
        p.start();self.addCleanup(p.stop)
    def test_tick_unsettled_groups_do_not_block_new_match(self):
        ko=1790899200000
        base={"rule_id":"M1","sid":"1","ko":ko,"ko_hkt":policy.fmt(ko),
              "market":"OU","side":"under","line":3.25,"hk":.8,
              "t5_at":ko-240000,"six_t5_min_at":ko-240000,
              "home":"test home","away":"test away","league":"test"}
        second={**base,"sid":"2"}
        later={**base,"sid":"3","ko":ko+600000}
        gate={"20":{"n":20,"wins":19,"den":20,"hit":.95,"pass":True},
              "30":{"n":20,"wins":19,"den":20,"hit":.95,"pass":False},"pass":True}
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp)
            cfg=root/"config.json"
            cfg.write_text(json.dumps({"enabled":True,"activated_at":ko-3600000}))
            with patch.object(notifier,"STATE",root),patch.object(notifier,"CONFIG",cfg), \
                 patch.object(notifier,"publish"),patch.object(notifier,"env",return_value={"TELEGRAM_BOT_TOKEN":"test","TELEGRAM_CHAT_ID":"test"}), \
                 patch.object(notifier,"gates",return_value=gate),patch.object(notifier,"collect") as collect, \
                 patch.object(notifier,"nowms") as clock,patch.object(notifier,"send") as send:
                clock.return_value=ko-120000
                send.side_effect=lambda *args:("sent",{"date":clock.return_value//1000,"message_id":123},None)
                collect.return_value=({"M1":[]},[base],[],{}, {})
                self.assertEqual(notifier.tick()["sent"],1)
                collect.return_value=({"M1":[]},[base,second,later],[],{}, {})
                out=notifier.tick()
                self.assertEqual(out["sent"],2)
                self.assertFalse(out["batch_lock_enabled"])
                self.assertIsNone(out["locked_batch"])
                self.assertEqual(out["pending_result_count"],3)
                self.assertEqual(notifier.tick()["sent"],0)
                db=notifier.state_db()
                self.assertEqual(db.execute("SELECT COUNT(*) FROM batches").fetchone()[0],2)
                self.assertEqual(db.execute("SELECT COUNT(*) FROM items WHERE batch_id=1").fetchone()[0],2)
                db.close()
                clock.return_value=ko+100000
                final={"status":"完","home_score":1,"away_score":0,"fetched_at":ko+90000}
                collect.return_value=({"M1":[]},[later],[],{"1":final}, {})
                self.assertEqual(notifier.tick()["sent"],0)
                collect.return_value=({"M1":[]},[later],[],{"1":final,"2":final}, {})
                self.assertEqual(notifier.tick()["sent"],0)
                db=notifier.state_db()
                self.assertEqual([r[0] for r in db.execute("SELECT sid FROM result_refresh_events ORDER BY sid")],["1","2"])
                self.assertEqual(db.execute("SELECT status FROM batches WHERE id=1").fetchone()[0],"closed")
                self.assertEqual(db.execute("SELECT batch_id FROM items WHERE sid='3'").fetchone()[0],2)
                db.close()
                self.assertEqual(send.call_count,3)
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
            self.assertEqual(notifier.reconcile(db,{"1":f},30)[0]["waiting"],1)
            self.assertEqual(notifier.reconcile(db,{"1":f,"2":f},30),[])
            self.assertEqual(db.execute("SELECT status FROM batches").fetchone()[0],"closed")
            self.assertEqual(db.execute("SELECT status FROM items WHERE sid='2'").fetchone()[0],"uncertain")
            db.close()
    def test_interrupted_prepared_is_not_backfilled(self):
        with tempfile.TemporaryDirectory() as tmp,patch.object(notifier,"STATE",Path(tmp)):
            db=notifier.state_db()
            db.execute("INSERT INTO batches(id,created_at,status) VALUES(1,1,'open')")
            db.execute("INSERT INTO items(bet_key,batch_id,sid,ko,payload,status) VALUES('x',1,'1',100,'{}','prepared')")
            db.commit()
            self.assertEqual(notifier.reconcile(db,{},30),[])
            self.assertEqual(db.execute("SELECT status FROM items").fetchone()[0],"skipped")
            db.close()
    def test_later_group_settles_while_earlier_result_missing(self):
        with tempfile.TemporaryDirectory() as tmp,patch.object(notifier,"STATE",Path(tmp)):
            db=notifier.state_db()
            for i in (1,2):
                db.execute("INSERT INTO batches(id,created_at,status,kickoff_utc) VALUES(?,1,'open',10)",(i,))
                h={"sid":str(i),"ko":10,"market":"OU","side":"under","line":3.25,"hk":.8}
                db.execute("INSERT INTO items(bet_key,batch_id,sid,ko,payload,status) VALUES(?,?,?,10,?,'sent')",
                           (str(i),i,str(i),json.dumps(h)))
            db.commit()
            f={"status":"完","home_score":1,"away_score":0,"fetched_at":20}
            pending=notifier.reconcile(db,{"2":f},30)
            self.assertEqual([r["batch_id"] for r in pending],[1])
            self.assertEqual(db.execute("SELECT status FROM batches WHERE id=2").fetchone()[0],"closed")
            self.assertIsNone(db.execute("SELECT result_json FROM items WHERE sid='1'").fetchone()[0])
            db.close()

if __name__=="__main__":
    unittest.main()
