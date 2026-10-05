import unittest
from unittest.mock import patch
import policy
import dynamic_rules as dynamic


def rows(n=30):
    return [{"sid":str(i),"ko":i,"result":"W","pnl":.8,"hk":.8,
             "market":"OU","side":"over","line":2.75,"features":{}} for i in range(n)]


class RecentFiveGateTest(unittest.TestCase):
    def test_recent_loss_blocks_even_when_both_windows_pass(self):
        for result,pnl in (("L",-1),("HL",-.5)):
            history=rows()
            history[-1].update(result=result,pnl=pnl)
            g=policy.gates(history)
            self.assertTrue(g["20"]["pass"])
            self.assertTrue(g["30"]["pass"])
            self.assertTrue(g["rolling_pass"])
            self.assertFalse(g["recent5"]["pass"])
            self.assertFalse(g["pass"])

    def test_loss_in_each_of_last_five_blocks_but_sixth_does_not(self):
        for offset in range(1,7):
            history=rows()
            history[-offset].update(result="L",pnl=-1)
            self.assertEqual(policy.gates(history)["pass"],offset==6)

    def test_half_wins_and_pushes_allowed_and_no_backfill(self):
        history=rows()
        for r,label,pnl in zip(history[-5:],("W","HW","P","HW","W"),(.8,.4,0,.4,.8)):
            r.update(result=label,pnl=pnl)
        g=policy.gates(list(reversed(history)))
        self.assertTrue(g["pass"])
        self.assertEqual(g["recent5"]["sids"],["25","26","27","28","29"])
        self.assertEqual(g["recent5"]["results"],["W","HW","P","HW","W"])
        for r in history[-5:]:
            r.update(result="P",pnl=0)
        g=policy.gates(history)
        self.assertFalse(g["20"]["pass"])  # Only 15 nonpush, cannot backfill.
        self.assertTrue(g["30"]["pass"])
        self.assertTrue(g["pass"])  # No new minimum-win rule was requested.

    def test_rolling_or_is_still_required(self):
        history=rows()
        for r in history[-9:-5]:
            r.update(result="L",pnl=-1)
        g=policy.gates(history)
        self.assertTrue(g["recent5"]["pass"])
        self.assertFalse(g["rolling_pass"])
        self.assertFalse(g["pass"])
        self.assertFalse(policy.gates(rows(19))["pass"])
        self.assertFalse(policy.gates(rows(4))["recent5"]["pass"])

    def test_same_eligible_price_order_for_both_windows_and_five(self):
        history=rows()
        history.append({**history[-1],"sid":"30","ko":30,"hk":.69,"result":"L","pnl":-1})
        g=policy.gates(history)
        self.assertTrue(g["pass"])
        self.assertEqual(g["recent5"]["sids"],g["20"]["sids"][-5:])
        self.assertEqual(g["recent5"]["sids"],g["30"]["sids"][-5:])

    def test_scanner_registry_and_runtime_all_use_same_gate(self):
        history=rows()
        strategy={"market":"OU","side":"over","clauses":[[]],"fingerprint":"fixture"}
        for result,expected in (("W",True),("HW",True),("P",True),("HL",False),("L",False)):
            history[-1].update(result=result,pnl={"W":.8,"HW":.4,"P":0,"HL":-.5,"L":-1}[result])
            with patch.object(dynamic,"atoms_for",return_value=[]):
                found,_=dynamic.scan(history)
            self.assertEqual(bool(found),expected)
            self.assertEqual(policy.gates(dynamic.matched_rows(history,strategy))["pass"],expected)

    def test_source_score_correction_changes_five_without_changing_rule(self):
        history=rows()
        history[-1].update(result="L",pnl=-1)
        self.assertFalse(policy.gates(history)["pass"])
        history[-1]=policy.settle(history[-1],{"home_score":1,"away_score":3,"fetched_at":100})
        self.assertTrue(policy.gates(history)["pass"])


if __name__=="__main__":
    unittest.main()
