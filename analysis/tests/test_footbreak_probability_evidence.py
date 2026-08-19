from __future__ import annotations
import unittest
from analysis.footbreak_probability_evidence import build
from analysis.probability_research import hierarchical_estimate

KICK="2026-08-20T20:00:00+08:00"; PRED="2026-08-20T19:55:00+08:00"; OBS="2026-08-20T19:54:00+08:00"; DONE="2026-08-20T22:30:00+08:00"
def grade(hit=True, **extra): return {"code":"HIL","side":"H","line":2.5,"odds":1.85,"quote_source":"board","observed_at":OBS,"grade_status":"GRADED","hit":hit,"result_recorded_at":DONE}|extra
def match(**extra): return {"match_id":"m1","league":"League","kickoff":KICK,"stages":[{"stage":"T-5","predicted_at":PRED,"market_grades":[grade()]}]}|extra

class FootbreakEvidenceTests(unittest.TestCase):
 def test_complete_native_raw_stage_generates_all_backoff_axes_deterministically(self):
  a=build([match()],generated_at="2026-08-21T00:00:00+08:00",source_boundary_at="2026-08-21T00:00:00+08:00")
  b=build([match()],generated_at="2026-08-21T00:00:00+08:00",source_boundary_at="2026-08-21T00:00:00+08:00")
  self.assertEqual(a,b); self.assertEqual(a["coverage"]["accepted_rows"],1)
  row=a["entries"][0]
  self.assertEqual({"system","stage","market","path","odds_tier","direction","role","line_bucket"},set(row)&{"system","stage","market","path","odds_tier","direction","role","line_bucket"})
  estimate=hierarchical_estimate({key:row.get(key) for key in ("system","stage","market","path","odds_tier","direction","role","line_bucket","league")},a["entries"])
  self.assertTrue(estimate["available"]); self.assertTrue(all(level["raw_n"] == 1 for level in estimate["levels"]))
 def test_missing_axis_posthoc_and_push_are_excluded_without_guessing(self):
  broken=match(match_id="bad"); broken["stages"][0]["market_grades"][0].pop("quote_source")
  post=match(match_id="post"); post["stages"][0]["post_hoc_backfill"]=True
  push=match(match_id="push"); push["stages"][0]["market_grades"][0]["hit"]=None
  artifact=build([broken,post,push],generated_at="2026-08-21T00:00:00+08:00")
  self.assertEqual(artifact["coverage"]["accepted_rows"],0)
  excluded=artifact["coverage"]["excluded"]
  self.assertGreater(excluded["invalid_quote_provenance"],0); self.assertGreater(excluded["post_hoc_or_backfill_or_excluded"],0); self.assertGreater(excluded["undecidable_or_push"],0)
 def test_source_boundary_rejects_result_not_known_by_boundary(self):
  late=match(); late["stages"][0]["market_grades"][0]["result_recorded_at"]="2026-08-22T00:00:00+08:00"
  artifact=build([late],generated_at="2026-08-21T00:00:00+08:00",source_boundary_at="2026-08-21T00:00:00+08:00")
  self.assertEqual(artifact["coverage"]["accepted_rows"],0); self.assertIn("result_recorded_after_source_boundary",artifact["coverage"]["excluded"])
 def test_three_stages_generate_all_four_canonical_t5_terminal_paths_and_stable_identity(self):
  stages=[]
  for name, at in (("首預","2026-08-20T18:00:00+08:00"),("T-30","2026-08-20T19:30:00+08:00"),("T-5",PRED)):
   stages.append({"stage":name,"predicted_at":at,"market_grades":[grade()]})
  artifact=build([match(stages=stages)],generated_at="2026-08-21T00:00:00+08:00")
  self.assertEqual({row["path"] for row in artifact["entries"]},{"T-5","首預→T-5","T-30→T-5","首預→T-30→T-5"})
  self.assertEqual(len({row["fixture_market_key"] for row in artifact["entries"]}),1)
  self.assertEqual(len({row["evidence_id"] for row in artifact["entries"]}),4)
  terminal=next(row for row in artifact["entries"] if row["path"]=="T-5")
  estimate=hierarchical_estimate({key:terminal.get(key) for key in ("system","stage","market","path","odds_tier","direction","role","line_bucket","league")},artifact["entries"])
  self.assertTrue(estimate["available"]); self.assertEqual(estimate["levels"][0]["raw_n"],1)
