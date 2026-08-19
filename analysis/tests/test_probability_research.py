from __future__ import annotations
import unittest
from analysis.probability_research import hierarchical_estimate, promotion_gate, two_sided_no_vig

C={"system":"footbreak","stage":"T-5","market":"HIL","path":"A→B","odds_tier":"1.80-1.89","direction":"H","role":"大","line_bucket":"2.5","league":"L"}
def row(won=True, **more): return C | {"outcome":"Won" if won else "Lost"} | more

class ProbabilityResearchTests(unittest.TestCase):
 def test_hierarchy_is_deterministic_and_excludes_leakage_push_opposite(self):
  evidence=[row(True,fixture_market_key="f1"),row(False,fixture_market_key="f2"),row(True,league="Other",fixture_market_key="f3"),row(True,line_bucket="3.5",fixture_market_key="f4"),row(True,direction="L",fixture_market_key="f5"),row(True,post_hoc=True,fixture_market_key="f6"),row(True,outcome="Refunded",fixture_market_key="f7")]
  a=hierarchical_estimate(C,evidence); b=hierarchical_estimate(C,list(reversed(evidence)))
  self.assertTrue(a["available"]); self.assertEqual(a,b)
  levels={x["level"]:x for x in a["levels"]}
  self.assertEqual(levels["exact"]["raw_n"],2); self.assertEqual(levels["exact"]["raw_hits"],1)
  self.assertEqual(levels["no_league"]["raw_n"],3); self.assertEqual(levels["relaxed_line"]["raw_n"],4)
  self.assertEqual(a["prior_strength"],30); self.assertIn("wilson95",levels["exact"])
 def test_missing_evidence_and_promotion_fail_closed(self):
  self.assertFalse(hierarchical_estimate(C,None)["available"])
  gate=promotion_gate({"unique_fixtures":200,"roi":.1,"wilson95":[.7,.8],"weighted_break_even":.5,"available":False},{"available":False},clv_coverage=None,mean_clv=None)
  self.assertTrue(gate["blocked"]); self.assertIn("clv_coverage_below_threshold_or_unavailable",gate["reasons"])
 def test_each_cohort_counts_unique_fixture_market_and_conflicts_fail_closed(self):
  base=row(True,fixture_market_key="same")
  # Identical duplicates represent the same fixture-market and count once in
  # every backoff layer, even if upstream path publication repeats a row.
  repeated=[base,base|{"evidence_id":"dup"},base|{"evidence_id":"dup2"}]
  result=hierarchical_estimate(C,repeated)
  self.assertTrue(result["available"])
  self.assertEqual([level["raw_n"] for level in result["levels"]],[1,1,1,1])
  two=hierarchical_estimate(C,repeated+[base|{"fixture_market_key":"other","evidence_id":"other"}])
  self.assertEqual([level["raw_n"] for level in two["levels"]],[2,2,2,2])
  conflict=hierarchical_estimate(C,[base,base|{"outcome":"Lost","evidence_id":"conflict"}])
  self.assertFalse(conflict["available"])
  self.assertIn("fixture_market_duplicate_or_conflict",conflict["reason"])
  relaxed_duplicate=hierarchical_estimate(C,[base,base|{"line_bucket":"3.5","evidence_id":"relaxed-duplicate"}])
  self.assertTrue(relaxed_duplicate["available"])
  self.assertEqual(relaxed_duplicate["levels"][0]["raw_n"],1)
 def test_level_specific_retained_axes_match_the_fixed_backoff_spec(self):
  base=row(True,fixture_market_key="f1")
  # market prior removes path/role/line/league, but never direction/tier/market.
  broad=hierarchical_estimate(C,[
   base,
   base|{"fixture_market_key":"f2","path":"首預→T-5","role":"另一角色","line_bucket":"3.5","league":"Other"},
   base|{"fixture_market_key":"wrong-direction","direction":"L"},
   base|{"fixture_market_key":"wrong-tier","odds_tier":"≥2.00"},
   base|{"fixture_market_key":"wrong-market","market":"HDC"},
  ])
  self.assertTrue(broad["available"]); self.assertEqual(broad["levels"][0]["raw_n"],2)
  # relaxed_line merges line + league but keeps path and role.
  relaxed=hierarchical_estimate(C,[
   base,
   base|{"fixture_market_key":"f2","line_bucket":"3.5","league":"Other"},
   base|{"fixture_market_key":"wrong-path","path":"首預→T-5"},
   base|{"fixture_market_key":"wrong-role","role":"另一角色"},
  ])
  self.assertTrue(relaxed["available"]); self.assertEqual(relaxed["levels"][1]["raw_n"],2)
  # no_league merges league only and still requires exact line.
  no_league=hierarchical_estimate(C,[
   base, base|{"fixture_market_key":"f2","league":"Other"},
   base|{"fixture_market_key":"wrong-line","line_bucket":"3.5"},
  ])
  self.assertTrue(no_league["available"]); self.assertEqual(no_league["levels"][2]["raw_n"],2); self.assertEqual(no_league["levels"][3]["raw_n"],1)
  exact=hierarchical_estimate(C,[
   base, base|{"fixture_market_key":"different-league","league":"Other"},
   base|{"fixture_market_key":"different-line","line_bucket":"3.5"},
  ])
  self.assertTrue(exact["available"]); self.assertEqual(exact["levels"][3]["raw_n"],1)
 def test_two_sided_no_vig_requires_same_quote_identity(self):
  selected={"side":"H","line":2.5,"odds":1.9,"quote_source":"crown","observed_at":"2026-08-20T19:50:00+08:00"}
  quotes=[selected|{"code":"HIL"},{"code":"HIL","side":"L","line":2.5,"odds":2.0,"quote_source":"crown","observed_at":"2026-08-20T19:50:00+08:00"}]
  value=two_sided_no_vig(selected,quotes,fixture="x",market="HIL",kickoff="2026-08-20T20:00:00+08:00")
  self.assertTrue(value["available"]); self.assertAlmostEqual(value["value"],(1/1.9)/(1/1.9+1/2),6)
  quotes[1]["observed_at"]="2026-08-20T19:51:00+08:00"
  self.assertFalse(two_sided_no_vig(selected,quotes,fixture="x",market="HIL",kickoff="x")["available"])
