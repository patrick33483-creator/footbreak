import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
import notifier
import signal_guard as guard
import strategy_runtime as runtime
from policy import gates,key


class SignalGuardTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory()
        self.p=patch.object(notifier,"STATE",Path(self.tmp.name));self.p.start()
        self.db=notifier.state_db()
        self.rules=[{"id":rid,"active":True,"version_at":0,"version":1,"lock_ids":[rid]}
                    for rid in ["D0027","D0066","H","A"]]
        self.g={r["id"]:{"pass":True} for r in self.rules}
        self.now=1000000
        self.ko=self.now+300000
        self.a=self.hit("D0027","over")
        self.b=self.hit("D0066","under")

    def tearDown(self):
        self.db.close();self.p.stop();self.tmp.cleanup()

    def hit(self,rid,side,sid="1",market="OU"):
        return {"rule_id":rid,"sid":sid,"ko":self.ko,"market":market,"side":side,
                "line":3.0,"hk":.84,"six_t5_min_at":100,"strategy_version":1,
                "strategy_description":"測試條件","t5_at":100,
                "home":"主隊","away":"客隊","league":"測試"}

    def ready(self,live,now=None):
        return guard.filter_live(self.db,live,self.g,
            self.ko-120000 if now is None else now,0,self.rules)

    def selected(self,live):
        allowed,_=self.ready(live)
        full=gates([{"sid":str(i),"ko":i,"result":"W","pnl":.84,"hk":.84} for i in range(30)])
        return runtime.choose(allowed,{r["id"]:full for r in self.rules},
                              self.ko-120000,0,[],self.rules,{})

    def test_wait_collect_then_keep_both_as_warning(self):
        out,counts=self.ready([self.a],self.now)
        self.assertEqual(out,[])
        self.assertEqual(counts["collecting"],1)
        out,_=self.ready([self.a,self.b])
        self.assertEqual(len(out),2)
        self.assertTrue(all(h["conflict_warning"] for h in out))
        self.assertEqual(guard.status(self.db)["scope"],"whole_match_warning_not_veto")

    def test_across_scan_registry_change_retains_opposite(self):
        self.ready([self.a],self.now)
        self.rules[0]["active"]=False
        out,_=self.ready([self.b])
        self.assertEqual(len(out),1)
        self.assertIn("D0027",out[0]["conflict_evidence"]["OU"]["over"])
        self.assertTrue(out[0]["conflict_warning"])

    def test_low_odds_and_failed_gate_not_conflicts(self):
        self.g["D0066"]={"pass":False}
        out,_=self.ready([self.a,self.b])
        self.assertEqual(len(out),1)
        self.assertFalse(out[0]["conflict_warning"])
        self.g["D0066"]={"pass":True};self.b["hk"]=.69
        out,_=self.ready([self.a,self.b])
        self.assertFalse(out[0]["conflict_warning"])

    def test_same_side_cross_market_and_other_match_not_conflicts(self):
        home=self.hit("H","home",market="AH")
        out,_=self.ready([self.a,{**self.b,"side":"over"},home,{**self.b,"sid":"2"}])
        self.assertFalse(any(h["conflict_warning"] for h in out))

    def test_home_away_warning_applies_to_whole_match(self):
        home=self.hit("H","home",market="AH")
        away=self.hit("A","away",market="AH")
        out,_=self.ready([home,away,self.a])
        self.assertTrue(all(h["conflict_warning"] for h in out))

    def test_price_lines_do_not_hide_opposite_and_deadline(self):
        out,_=self.ready([self.a,{**self.b,"line":3.25}])
        self.assertTrue(all(h["conflict_warning"] for h in out))
        out,_=self.ready([self.a,self.b],self.ko)
        self.assertEqual(out,[])

    def test_both_directions_one_telegram_keep_two_records(self):
        selected=self.selected([self.a,self.b])
        groups=notifier.prepare_items(self.db,selected,self.ko-120000)
        with patch.object(notifier,"nowms",return_value=self.ko-120000),\
             patch.object(notifier,"send",return_value=("sent",{"date":1181,"message_id":9},None)) as send:
            self.assertEqual(guard.deliver(self.db,selected,groups,{}),2)
        self.assertEqual(send.call_count,1)
        text=send.call_args.args[0]
        self.assertTrue(text.startswith("條件衝突，不建議投注"))
        for word in ["D0027","D0066","買大","買細","測試條件"]:
            self.assertIn(word,text)
        rows=self.db.execute("SELECT status,message_id FROM items").fetchall()
        self.assertEqual([(r["status"],r["message_id"]) for r in rows],[("sent",9),("sent",9)])

    def test_later_opposite_updates_once_without_new_bet_or_lock(self):
        first=self.selected([self.a])
        groups=notifier.prepare_items(self.db,first,self.ko-120000)
        with patch.object(notifier,"nowms",return_value=self.ko-120000),\
             patch.object(notifier,"send",return_value=("sent",{"date":1181,"message_id":9},None)):
            guard.deliver(self.db,first,groups,{})
        self.ready([self.b],self.ko-100000)
        with patch.object(notifier,"nowms",return_value=self.ko-100000),\
             patch.object(notifier,"send",return_value=("sent",{"date":1201,"message_id":10},None)) as send:
            guard.deliver(self.db,[],{},{});guard.deliver(self.db,[],{},{})
        self.assertEqual(send.call_count,1)
        self.assertIn("D0066",send.call_args.args[0])
        self.assertEqual(self.db.execute("SELECT COUNT(*) FROM items").fetchone()[0],1)
        self.assertEqual(guard.status(self.db)["recent_conflicts"][0]["warning_status"],"sent")

    def test_warning_uncertain_never_auto_duplicates(self):
        first=self.selected([self.a])
        notifier.prepare_items(self.db,first,self.ko-120000)
        self.db.execute("UPDATE items SET status='sent'");self.db.commit()
        self.ready([self.b])
        with patch.object(notifier,"nowms",return_value=self.ko-100000),\
             patch.object(notifier,"send",return_value=("uncertain",None,"timeout")) as send:
            guard.deliver(self.db,[],{},{});guard.deliver(self.db,[],{},{})
        self.assertEqual(send.call_count,1)
        self.assertEqual(guard.status(self.db)["recent_conflicts"][0]["warning_status"],"uncertain")

    def test_late_ack_uncertain_and_postkickoff_no_warning(self):
        selected=self.selected([self.a,self.b])
        groups=notifier.prepare_items(self.db,selected,self.ko-120000)
        with patch.object(notifier,"nowms",return_value=self.ko-10000),\
             patch.object(notifier,"send",return_value=("sent",{"date":self.ko//1000,"message_id":9},None)):
            self.assertEqual(guard.deliver(self.db,selected,groups,{}),0)
        self.assertTrue(all(r[0]=="uncertain" for r in self.db.execute("SELECT status FROM items")))
        with patch.object(notifier,"nowms",return_value=self.ko+1),patch.object(notifier,"send") as send:
            guard.deliver(self.db,[],{},{})
        send.assert_not_called()

    def test_long_text_keeps_warning_and_directions(self):
        selected=self.selected([self.a,self.b])
        for h in selected:
            h["rule_descriptions"]={r:"長條件"*3000 for r in h["rules"]}
        text=guard.bundle_message(selected,1,selected[0]["conflict_evidence"])
        self.assertLess(len(text),4096)
        self.assertIn("不建議投注",text)
        self.assertIn("買大",text);self.assertIn("買細",text)

    def test_existing_pending_locks_are_not_bypassed(self):
        allowed,_=self.ready([self.a,self.b])
        g={r["id"]:{"pass":True} for r in self.rules}
        chosen=runtime.choose(allowed,g,self.ko-120000,0,
                 [{"lock_ids":["D0066"],"ko":self.ko-60000}],self.rules,{})
        self.assertEqual(len(chosen),1)
        self.assertTrue(chosen[0]["conflict_warning"])


if __name__=="__main__":
    unittest.main()
