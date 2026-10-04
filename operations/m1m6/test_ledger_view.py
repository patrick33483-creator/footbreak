import copy
import unittest
from pathlib import Path
import ledger_view as view
from policy import GATE_VERSION

class LedgerViewTest(unittest.TestCase):
    def test_league_display_uses_saved_field_with_escaped_fallback(self):
        panel=Path(__file__).with_name("panel.html").read_text()
        self.assertIn("香港開賽／聯賽／賽事",panel)
        self.assertIn('class="m-league m-muted"',panel)
        self.assertIn('esc(String(r.league??"").trim()||"未有聯賽資料")',panel)
    def setUp(self):
        self.p={"market":"AH","side":"home","hk":.8,"features":{"hour":0},"rules":["OLD"]}
        self.rules=[{"id":"NEW","active":True,"market":"AH","side":"home",
                     "clauses":[[{"family":"hour","op":"eq","value":0}]]}]
    def test_settled_always_retained_even_low_price_and_unavailable(self):
        self.assertEqual(view.classify({"hk":.6},{"result":"W"},False,[])["display_reason"],"settled")
    def test_current_conditions_not_old_strategy_id(self):
        got=view.classify(self.p,None,True,self.rules)
        self.assertTrue(got["display_keep"])
        self.assertEqual(got["qualifying_rules"],["NEW"])
        self.p["rules"]=["NEW"];self.p["features"]["hour"]=1
        self.assertFalse(view.classify(self.p,None,True,self.rules)["display_keep"])
    def test_floor_and_missing_features(self):
        self.p["hk"]=.69
        self.assertFalse(view.classify(self.p,None,True,self.rules)["display_keep"])
        self.p["hk"]=.7
        self.assertTrue(view.classify(self.p,None,True,self.rules)["display_keep"])
        del self.p["features"]
        self.assertEqual(view.classify(self.p,None,True,self.rules)["display_reason"],"saved_features_missing")
    def test_current_gate_and_version_required(self):
        reg={"updated_at":10,"gate_version":GATE_VERSION,"strategies":self.rules}
        self.assertEqual(view.context(reg,{"NEW":{"pass":False}},20),(True,[]))
        self.assertEqual(view.context(reg,{"NEW":{"pass":True}},20),(True,self.rules))
        reg["gate_version"]="old"
        self.assertFalse(view.context(reg,{"NEW":{"pass":True}},20)[0])
        self.assertFalse(view.classify(self.p,None,False,self.rules)["display_keep"])
    def test_no_input_mutation_or_lock_based_exclusion(self):
        self.p.update(ko=1,status="uncertain",pending_result_count=1)
        before=copy.deepcopy(self.p)
        self.assertTrue(view.classify(self.p,None,True,self.rules)["display_keep"])
        self.assertEqual(self.p,before)
    def test_summary_and_unrelated_direction(self):
        rows=[]
        for p,result in [(self.p,None),({**self.p,"side":"away"},None),({"hk":.6},{"result":"W"})]:
            rows.append({"result":result,**view.classify(p,result,True,self.rules)})
        stats=view.summary(rows,True)
        self.assertEqual((stats["completed"],stats["qualified_pending"],stats["hidden"]),(1,1,1))

if __name__=="__main__":
    unittest.main()
