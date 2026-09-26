import unittest
import copy
from alow_first_tg_watch import assess,choose,expected_side,VERSION
class MonitorTests(unittest.TestCase):
    def setUp(self):
        self.start=1000000;self.ko=2000000
        self.h={"sid":"one","side":"A","line":0,"odds":1.60,"trigger_home_dec":2.15,
            "ah_at":1700000,"ou_at":1700000,"t5_at":1700000,"kickoff_utc":self.ko}
        self.n={"sid":"one","notified_at":1800000,"kickoff_utc":self.ko,
            "receipt":{"version":VERSION,"side":"A","pick":"away","display":"買客","line":0,"odds":1.60,
                "trigger_home_dec":2.15,"kickoff_utc":self.ko,"telegram_message_id":44,"telegram_date_ms":1799000}}
    def test_bands(self):
        for price,side in [(1.699,None),(1.70,"H"),(1.799,"H"),(1.8,None),(1.999,None),(2,"H"),(2.099,"H"),(2.1,"A")]:
            self.assertEqual(expected_side(price),side)
    def test_reverse_actual_price_pass(self):
        self.assertTrue(assess(self.n,self.h,self.start)["passed"])
    def test_wrong_side_fails(self):
        self.n["receipt"]["side"]="H";self.assertFalse(assess(self.n,self.h,self.start)["passed"])
    def test_wrong_actual_odds_fails(self):
        self.n["receipt"]["odds"]=2.15;self.assertFalse(assess(self.n,self.h,self.start)["passed"])
    def test_late_ledger(self):
        self.n["notified_at"]=self.ko;self.assertFalse(assess(self.n,self.h,self.start)["on_time"])
    def test_late_ack(self):
        self.n["receipt"]["telegram_date_ms"]=self.ko;self.assertFalse(assess(self.n,self.h,self.start)["on_time"])
    def test_missing_receipt(self):
        self.n["receipt"]=None;self.assertFalse(assess(self.n,self.h,self.start)["passed"])
    def test_stale_snapshots(self):
        self.h["ou_at"]=self.start-1;self.assertFalse(assess(self.n,self.h,self.start)["passed"])
    def test_first_notice_wins(self):
        later=copy.deepcopy(self.n);later["sid"]="two";later["notified_at"]+=1000
        event=choose([self.h],[later,self.n],{},1900000,self.start)
        self.assertEqual(event["sid"],"one");self.assertTrue(event["terminal"])
    def test_warning_then_dedupe(self):
        event=choose([self.h],[],{},1950000,self.start);self.assertEqual(event["kind"],"prestart_warning")
        self.assertIsNone(choose([self.h],[],{"warning_alerted":True},1950000,self.start))
    def test_miss_then_dedupe(self):
        event=choose([self.h],[],{},2040000,self.start);self.assertEqual(event["kind"],"missed")
        self.assertIsNone(choose([self.h],[],{"missed_alerted":True},2040000,self.start))
    def test_no_event(self):
        self.assertIsNone(choose([],[],{},1500000,self.start))
if __name__=="__main__":unittest.main()
