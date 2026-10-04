import datetime as dt
import json
import sqlite3
import unittest
from pathlib import Path
from performance_view import summarize,HKT


class PerformanceViewTests(unittest.TestCase):
    def setUp(self):
        self.db=sqlite3.connect(":memory:");self.db.row_factory=sqlite3.Row
        self.db.execute("""CREATE TABLE items(bet_key TEXT PRIMARY KEY,sid TEXT,ko INTEGER,
            attempt_at INTEGER,ack_at INTEGER,status TEXT,payload TEXT,result_json TEXT)""")
        self.start=int(dt.datetime(2026,10,4,12,18,tzinfo=HKT).timestamp()*1000)
        self.epoch={"id":"test","started_at":self.start,"excluded_sids":["old"]}
    def tearDown(self):
        self.db.close()
    def add(self,k,result="W",pnl=.8,**kw):
        r={"bet_key":k,"sid":k,"ko":self.start+3600000,"attempt_at":self.start+1000,
           "ack_at":self.start+2000,"status":"sent","payload":{"rules":["D1","D2"]},
           "result_json":None if result is None else {"result":result,"pnl":pnl}}
        r.update(kw)
        self.db.execute("INSERT INTO items VALUES(?,?,?,?,?,?,?,?)",
            (r["bet_key"],r["sid"],r["ko"],r["attempt_at"],r["ack_at"],r["status"],
             json.dumps(r["payload"]),json.dumps(r["result_json"]) if r["result_json"] else None))
    def get(self,now=None):
        return summarize(self.db,self.epoch,self.start if now is None else now)
    def test_zero_and_missing_epoch(self):
        self.assertFalse(summarize(self.db,None,self.start)["ready"])
        s=self.get()
        self.assertEqual(s["total"]["pnl"],0)
        self.assertEqual(s["daily"],[])
        self.assertIsNone(s["first_match_at"])
    def test_old_match_old_attempt_late_settlement_excluded(self):
        self.add("existing",sid="old")
        self.add("old-attempt",attempt_at=self.start-1)
        self.add("old-kickoff",ko=self.start-1,ack_at=self.start-100)
        self.add("new")
        self.assertEqual(self.get()["total"]["notified"],1)
    def test_exact_once_multistrategy_and_all_outcomes(self):
        for k,result,pnl in [("1","W",.8),("2","HW",.4),("3","P",0),("4","HL",-.5),("5","L",-1),("6",None,0)]:
            self.add(k,result,pnl)
        s=self.get()["total"]
        self.assertEqual((s["notified"],s["settled"],s["pending"],s["wins"],s["losses"]),(6,5,1,2,2))
        self.assertAlmostEqual(s["pnl"],-.3)
        self.assertAlmostEqual(s["roi_pct"],-6)
    def test_full_ledger_not_limited_by_page(self):
        for i in range(125):
            self.add(str(i))
        s=self.get()["total"]
        self.assertEqual(s["settled"],125)
        self.assertEqual(s["pnl"],100)
    def test_warning_unconfirmed_and_late_ack_excluded(self):
        self.add("warn",payload={"conflict_warning":True})
        self.add("uncertain",status="uncertain")
        self.add("skipped",status="skipped")
        self.add("late",ack_at=self.start+3600000)
        s=self.get()
        self.assertEqual(s["total"]["notified"],0)
        self.assertEqual(s["excluded"],{"warning":1,"unconfirmed":3,"invalid_result":0})
    def test_daily_hong_kong_kickoff_not_settlement_date(self):
        before=int(dt.datetime(2026,10,4,23,59,tzinfo=HKT).timestamp()*1000)
        after=int(dt.datetime(2026,10,5,0,1,tzinfo=HKT).timestamp()*1000)
        self.add("one",ko=before)
        self.add("two","L",-1,ko=after)
        s=self.get(after)
        self.assertEqual([d["date"] for d in s["daily"]],["2026-10-05","2026-10-04"])
        self.assertEqual(s["today"]["pnl"],-1)
        self.assertEqual(s["total"]["pnl"],-.2)
    def test_pending_settlement_recomputed_without_mutating_ledger(self):
        self.add("pending",None)
        self.assertEqual(self.get()["total"]["pending"],1)
        self.db.execute("UPDATE items SET result_json=?",(json.dumps({"result":"HL","pnl":-.5}),))
        before=self.db.total_changes
        s=self.get()
        self.assertEqual(s["total"]["pnl"],-.5)
        self.assertEqual(s["total"]["pending"],0)
        self.assertEqual(before,self.db.total_changes)
    def test_invalid_result_not_silently_profit(self):
        self.add("bad","?",20)
        self.add("nan","W",float("nan"))
        self.assertEqual(self.get()["excluded"]["invalid_result"],2)
        self.assertEqual(self.get()["total"]["pnl"],0)
    def test_reset_action_idempotent_and_ui_contains_result_colours(self):
        folder=Path(__file__).parent
        ops=(folder/"dynamic_ops.py").read_text()
        self.assertIn('if epoch_path.exists():',ops)
        self.assertIn('if reset_stats and epoch["id"]!=epoch_id:',ops)
        self.assertIn('assert epoch_before==epoch_after',ops)
        html=(folder/"panel.html").read_text()
        for marker in ('m-result-win','m-result-loss','m-profit-total','m-profit-today','m-daily-profit'):
            self.assertIn(marker,html)
    def test_strategy_overlap_is_not_double_counted_in_total(self):
        self.add("shared",payload={"rules":["D1","D1","D2"],"rule_versions":{"D1":1,"D2":2}})
        self.add("solo","L",-1,payload={"rules":["D1"],"rule_versions":{"D1":3}})
        s=self.get();groups={b["id"]:b for b in s["by_strategy"]}
        self.assertEqual(s["total"]["notified"],2)
        self.assertEqual(s["total"]["pnl"],-.2)
        self.assertEqual(groups["D1"]["notified"],2)
        self.assertEqual(groups["D1"]["pnl"],-.2)
        self.assertEqual(groups["D1"]["versions"],["1","3"])
        self.assertEqual(groups["D2"]["notified"],1)
        self.assertEqual(groups["D2"]["pnl"],.8)
    def test_strategy_pending_warning_and_old_scope(self):
        self.add("new",None,payload={"rules":["M5"]})
        self.add("warn",payload={"rules":["D9"],"conflict_warning":True})
        self.add("old",payload={"rules":["D0"]})
        s=self.get()
        self.assertEqual([b["id"] for b in s["by_strategy"]],["M5"])
        self.assertEqual(s["by_strategy"][0]["pending"],1)
        self.assertEqual(s["by_strategy"][0]["pnl"],0)
        self.assertEqual(s["started_at"],self.start)
    def test_strategy_fallback_retains_unlabelled_profit(self):
        self.add("fallback",payload={"rule_id":"M5","strategy_version":1})
        self.add("missing",payload={})
        ids={b["id"] for b in self.get()["by_strategy"]}
        self.assertEqual(ids,{"M5","未記錄策略"})


if __name__=="__main__":
    unittest.main()
