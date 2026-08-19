from __future__ import annotations
import copy, json, tempfile, unittest
from unittest.mock import patch
from pathlib import Path
from datetime import datetime,timedelta,timezone
import sys
SYSTEM_DIR = Path(__file__).resolve().parents[1]
if str(SYSTEM_DIR) not in sys.path:
 sys.path.insert(0, str(SYSTEM_DIR))
import probability_research as p
from analysis.footbreak_probability_evidence import build as build_evidence
HKT=timezone(timedelta(hours=8))
def watch():
 k=datetime(2026,8,21,20,tzinfo=HKT); s={"stage":"T-5","ts":(k-timedelta(minutes=5)).isoformat(),"kickoff":k.isoformat(),"market_predictions":[{"code":"HIL","side":"H","line":2.5,"odds":1.85,"quote_source":"crown","observed_at":(k-timedelta(minutes=6)).isoformat()}]}
 return {"match_id":"p1","league":"L","home":"A","away":"B","kickoff":k.isoformat(),"stages":[s]}
def ranking(): return [{"market":"HIL","decision_stage":"T-5","path":"A→B","observed_path":"A→B","odds_tier":"1.80-1.89","direction":"H","role":"大","line_bucket":"2.5","selected_side":"H","selected_line":2.5,"total":{"hits":20,"decided":25}}]
class FootbreakProbabilityResearchTests(unittest.TestCase):
 def test_isolated_rows_variants_idempotent_and_unavailable_semantics(self):
  ledger={"bets":[{"bet_id":"v1"}],"independent_validation":{"conditions":{"x":{}}}}
  p.ensure_namespace(ledger,now="2026-08-20T10:00:00+08:00"); original=copy.deepcopy(ledger)
  candidate=ranking()[0] | {"observed_path":"T-5","path":"T-5","odds_tier":"≥1.70","direction":"A","role":"大","line_bucket":"≤2.5","selected_side":"H","selected_line":2.5}
  with patch.object(p, "match_upcoming", return_value={"p1":[candidate]}):
   made,audit=p.evaluate_new_t5(ledger,watch(),ranking=ranking(),evidence=None)
  self.assertEqual(len(made),2); self.assertEqual(ledger["bets"],original["bets"]); self.assertEqual(ledger["independent_validation"],original["independent_validation"])
  self.assertTrue(all(x["prediction_status"]=="unavailable" and not x["actionable_telegram"] for x in made))
  with patch.object(p, "match_upcoming", return_value={"p1":[candidate]}):
   self.assertEqual(p.evaluate_new_t5(ledger,watch(),ranking=ranking())[0],[])
 def test_activation_is_immutable_and_old_stage_is_rejected(self):
  ledger={}; p.ensure_namespace(ledger,now="2026-08-21T19:56:00+08:00")
  made,_=p.evaluate_new_t5(ledger,watch(),ranking=ranking()); self.assertEqual(made,[])
  self.assertEqual(p.ensure_namespace(ledger,now="2026-08-22T00:00:00+08:00")["activation_at"],"2026-08-21T19:56:00+08:00")
 def test_dashboard_smoke_states_missing_metrics_as_evidence_unavailable(self):
  root=Path(__file__).resolve().parents[2]
  app=(root/"hkjc-dashboard"/"app.js").read_text(encoding="utf-8")
  self.assertIn('data-testid="footbreak-probability-research"',app)
  self.assertIn('data-testid="footbreak-probability-evidence"',app)
  self.assertIn("機率驗證研究",app); self.assertIn("研究中／非正式推介",app)
  self.assertIn("未有證據",app); self.assertIn("Evidence artifact",app); self.assertIn("不用 Kelly、不發 Telegram、不自動升級",app)
 def test_artifact_as_of_staleness_malformed_and_frozen_probability(self):
  candidate=ranking()[0] | {"observed_path":"T-5","path":"T-5","odds_tier":"≥1.70","direction":"A","role":"大","line_bucket":"≤2.5","selected_side":"H","selected_line":2.5}
  raw={"match_id":"old","league":"L","kickoff":"2026-08-20T20:00:00+08:00","stages":[{"stage":"T-5","predicted_at":"2026-08-20T19:55:00+08:00","market_grades":[{"code":"HIL","side":"H","line":2.5,"odds":1.85,"quote_source":"board","observed_at":"2026-08-20T19:54:00+08:00","grade_status":"GRADED","hit":True,"result_recorded_at":"2026-08-20T22:00:00+08:00"}]}]}
  artifact=build_evidence([raw],generated_at="2026-08-21T18:00:00+08:00")
  with tempfile.TemporaryDirectory() as d:
   path=Path(d,"evidence.json"); path.write_text(json.dumps(artifact),encoding="utf-8")
   rows,meta=p.read_evidence_as_of(path,"2026-08-21T19:55:00+08:00")
   self.assertTrue(meta["available"]); self.assertEqual(len(rows),1)
   ledger={}; p.ensure_namespace(ledger,now="2026-08-21T18:00:00+08:00")
   with patch.object(p,"match_upcoming",return_value={"p1":[candidate]}): made,_=p.evaluate_new_t5(ledger,watch(),ranking=ranking(),evidence_path=path)
   self.assertTrue(all(row["prediction_status"]=="available" for row in made)); frozen=copy.deepcopy(made)
   artifact["entries"][0]["outcome"]="Lost"; artifact["entries_sha256"]=p._json_digest(artifact["entries"]); path.write_text(json.dumps(artifact),encoding="utf-8")
   with patch.object(p,"match_upcoming",return_value={"p1":[candidate]}): self.assertEqual(p.evaluate_new_t5(ledger,watch(),ranking=ranking(),evidence_path=path)[0],[])
   self.assertEqual(ledger[p.NAMESPACE]["rows"],frozen)
   path.write_text("{bad",encoding="utf-8"); self.assertFalse(p.read_evidence_as_of(path,"2026-08-21T19:55:00+08:00")[1]["available"])
   path.write_text(json.dumps(build_evidence([raw],generated_at="2026-08-18T00:00:00+08:00")),encoding="utf-8"); self.assertEqual(p.read_evidence_as_of(path,"2026-08-21T19:55:00+08:00")[1]["reason"],"evidence_artifact_stale")
