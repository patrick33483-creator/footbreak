import unittest
from n2_first_tg_watch import choose,text_for

class ObserverTests(unittest.TestCase):
    def setUp(self):
        self.h={"sid":"x","league":"測試","home":"主","away":"客","kickoff_utc":2000000,
                "t5_at":1700000,"line":2.25,"odds":1.70}
    def test_no_natural_no_success(self):
        self.assertIsNone(choose([],[],{},1900000))
    def test_wait_for_notifier(self):
        self.assertIsNone(choose([self.h],[],{},1800000))
    def test_before_kickoff_warning(self):
        self.assertEqual(choose([self.h],[],{},1940000)["kind"],"prestart_warning")
    def test_warning_not_repeat(self):
        self.assertIsNone(choose([self.h],[],{"warning_alerted":True},1940000))
    def test_missed(self):
        self.assertEqual(choose([self.h],[],{},2030000)["kind"],"missed")
    def test_missed_not_repeat(self):
        self.assertIsNone(choose([self.h],[],{"missed_alerted":True},2030000))
    def test_success(self):
        e=choose([self.h],[{**self.h,"notified_at":1900000}],{},1940000)
        self.assertTrue(e["on_time"]);self.assertEqual(e["seconds_before"],100)
        e["checked_at"]=1940000
        self.assertIn("不代表手機推送到達或已讀",text_for(e))
    def test_at_kickoff_fails(self):
        e=choose([self.h],[{**self.h,"notified_at":2000000}],{},2030000)
        self.assertFalse(e["on_time"])
    def test_first_by_send_time(self):
        e=choose([self.h],[{**self.h,"sid":"y","notified_at":1980000},
                          {**self.h,"notified_at":1900000}],{},2030000)
        self.assertEqual(e["sid"],"x")
    def test_no_matching_snapshot_does_not_claim_match(self):
        e=choose([],[{**self.h,"notified_at":1900000}],{},2030000)
        self.assertFalse(e["rule_recheck_ok"])

if __name__=="__main__":unittest.main()
