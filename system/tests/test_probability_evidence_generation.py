from __future__ import annotations
import json, sys, tempfile, unittest
from datetime import datetime, timedelta
from pathlib import Path
from unittest.mock import patch
SYSTEM=Path(__file__).resolve().parents[1]
if str(SYSTEM) not in sys.path: sys.path.insert(0,str(SYSTEM))
import accuracy

class ProbabilityEvidenceGenerationTests(unittest.TestCase):
 def test_accuracy_publishes_bounded_raw_evidence_without_touching_bets(self):
  now=datetime.now(accuracy.HKT); kickoff=now-timedelta(hours=3); predicted=kickoff-timedelta(minutes=5); observed=kickoff-timedelta(minutes=6)
  ledger={"bets":[{"bet_id":"legacy"}],"watch":{"m": {"match_id":"m","league":"L","home":"H","away":"A","kickoff":kickoff.strftime("%Y-%m-%d %H:%M"),"stages":[{"prediction_era":accuracy.PREDICTION_ERA,"stage":"T-5","ts":predicted.isoformat(),"market_predictions":[{"code":"HIL","side":"H","line":2.5,"odds":1.85,"probability":.6,"quote_source":"board","observed_at":observed.isoformat()}]}]}}}
  with tempfile.TemporaryDirectory() as d:
   lp=Path(d,"ledger.json"); op=Path(d,"accuracy.json"); hp=Path(d,"history.json"); ep=Path(d,"footbreak_probability_evidence.json"); lp.write_text(json.dumps(ledger),encoding="utf-8")
   result={"goals_home":2,"goals_away":1,"goals_total":3,"corners_total":None,"source":"official"}
   with patch.object(accuracy,"LEDGER",str(lp)),patch.object(accuracy,"OUT",str(op)),patch.object(accuracy,"HISTORY_OUT",str(hp)),patch.object(accuracy.S,"fetch_hkjc_results",return_value={"m":result}): accuracy.run(fetch=True)
   artifact=json.loads(ep.read_text(encoding="utf-8"))
  self.assertEqual(artifact["system"],"footbreak"); self.assertEqual(artifact["coverage"]["accepted_rows"],1); self.assertEqual(ledger["bets"][0]["bet_id"],"legacy")
