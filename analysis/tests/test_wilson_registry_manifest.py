from __future__ import annotations
import copy, unittest
from analysis.wilson_registry_manifest import build_manifest, canonical_hash, definition_signature

def version(sig,n=1,prior=None):
 p={"condition_signature":sig,"version":n,"prior_version":n-1 if n>1 else None,"prior_evidence_hash":prior,"batch_fixture_market_hashes":[],"batch_hits":0,"batch_decided":0,"cumulative_hits":50,"cumulative_decided":80,"wilson95_lower_raw":.51,"minimum_acceptable_odds_raw":2.08,"activation_boundary_at":"2026-08-20T00:00:00+08:00"}; return {**p,"evidence_hash":canonical_hash(p)}
def frozen(system,market,path,stage,number):
 d={"system":system,"version":"granular-condition-v1","market":market,"stage":stage,"path":path,"direction":"x","role":"x","line_bucket":"x","odds_tier":"x","movement":"x","odds_trajectory":"x","miner_key":[f"system={system}",f"market={market}",f"path={path}",f"stage={stage}"]}; s=definition_signature(d); return s,{"signature":s,"condition_number":number,"definition":d,"historical_evidence":{"hits":50,"decided":80},"evidence_versions":[version(s)]}
def ledger(system):
 items=[frozen(system,"HDC","首預","首預",1),frozen(system,"HIL","首預→T-30","T-30",2),frozen(system,"CHL","首預→T-30→T-5","T-5",3)]; return {"bets":[],"wilson_validation":{"schema_version":2,"system":system,"activation_at":"2026-08-20T00:00:00+08:00","condition_order":[x[0] for x in items],"conditions":dict(items),"observations":[],"audit":[]}}
class ManifestTest(unittest.TestCase):
 def test_every_stage_and_market_both_systems_without_cross_contamination(self):
  for system in ("footbreak","crown"):
   l=ledger(system); before=copy.deepcopy(l); m=build_manifest(l,system); self.assertTrue(m["valid"]); self.assertEqual(l,before); self.assertEqual(m["decision_stage_counts"],{"T-30":1,"T-5":1,"首預":1}); self.assertEqual([r["market"] for r in m["conditions"]],["HDC","HIL","CHL"]); self.assertEqual([r["current_matcher_can_structurally_admit"] for r in m["conditions"]],[False,False,True]); self.assertTrue(all(r["definition"]["system"]==system for r in m["conditions"])); self.assertFalse(m["recovery"]["implemented"])
 def test_signature_definition_and_evidence_drift_fail_closed(self):
  l=ledger("footbreak"); first=l["wilson_validation"]["conditions"][l["wilson_validation"]["condition_order"][0]]; first["definition"]["market"]="HIL"; first["evidence_versions"][0]["batch_hits"]=99; m=build_manifest(l,"footbreak"); self.assertFalse(m["valid"]); self.assertIn("definition_signature_drift",m["rejection_reasons"]); self.assertIn("evidence_hash_drift",m["rejection_reasons"])
 def test_wrong_system_and_missing_provenance_rejected(self):
  l=ledger("crown"); m=build_manifest(l,"footbreak"); self.assertFalse(m["valid"]); self.assertIn("system_mismatch",m["rejection_reasons"]); l=ledger("footbreak"); s=l["wilson_validation"]["condition_order"][1]; del l["wilson_validation"]["conditions"][s]["evidence_versions"]; m=build_manifest(l,"footbreak"); self.assertIn("missing_evidence_versions",m["rejection_reasons"])
 def test_duplicate_activity_counted_but_never_recovered(self):
  l=ledger("footbreak"); s=l["wilson_validation"]["condition_order"][2]; l["bets"]=[{"frozen_condition_signature":s,"status":"REFUND"},{"frozen_condition_signature":s,"status":"REFUND"}]; m=build_manifest(l,"footbreak"); r=m["conditions"][2]; self.assertEqual(r["formal_rows"],2); self.assertEqual(r["formal_status_counts"],{"REFUND":2}); self.assertEqual(r["safely_recoverable_missed_rows"],0)
if __name__=="__main__": unittest.main()
