from __future__ import annotations
import copy,json,os,subprocess,tempfile,unittest
from pathlib import Path
from analysis.wilson_registry_manifest import build_manifest,definition_signature
from analysis.wilson_validation import EDGE_BUFFER,_fixture_market_hash,_version_hash,wilson95,STRATEGY,portfolio_name,recompute_namespace,ensure_namespace,project_granular_ranking_evidence,admission_arithmetic,apply_active_evidence,commit_bet
NOW="2026-08-20T00:00:00+08:00"
def ev(sig,h=50,d=80):
 lo=wilson95(h,d)[0]; v={"condition_signature":sig,"version":1,"prior_version":None,"prior_evidence_hash":None,"batch_fixture_market_hashes":[],"batch_hits":0,"batch_decided":0,"cumulative_hits":h,"cumulative_decided":d,"wilson95_lower_raw":lo,"minimum_acceptable_odds_raw":1/(lo-EDGE_BUFFER),"activation_boundary_at":NOW,"created_at":NOW,"migration_baseline":True}; v["evidence_hash"]=_version_hash(v); return v
def frozen(system,market,path,stage,number):
 tiers="低" if "→" not in path else "→".join(["低"]*len(path.split("→")))
 key=[f"system={system}",f"market={market}",f"path={path}",f"decision={stage}","direction=x","role=x","bucket=x","tier=低"]
 if "→" in path:key.append(f"tier_path={tiers}")
 d={"system":system,"version":"granular-condition-v1","market":market,"stage":stage,"path":path,"direction":"x","role":"x","line_bucket":"x","odds_tier":"低","movement":"x","odds_trajectory":tiers if "→" in path else "","miner_key":key}; s=definition_signature(d); a={"hash":"a"*64,"version":"v1","as_of":NOW};v=ev(s);projection={k:v.get(k) for k in ("version","cumulative_hits","cumulative_decided","wilson95_lower_raw","minimum_acceptable_odds_raw","minimum_acceptable_odds_display","activation_boundary_at","created_at","evidence_hash")};return s,{"signature":s,"condition_number":number,"frozen_at":NOW,"definition":d,"historical_evidence":{"hits":50,"decided":80,"artifact":a},"evidence_versions":[v],"active_evidence_version":1,"active_evidence_hash":v["evidence_hash"],"active_evidence":projection}
def ledger(system="footbreak"):
 items=[frozen(system,"HDC","首預","首預",1),frozen(system,"HIL","首預→T-30","T-30",2),frozen(system,"CHL","首預→T-30→T-5","T-5",3)]; return {"bets":[],"wilson_validation":{"schema_version":2,"system":system,"activation_at":NOW,"condition_order":[x[0] for x in items],"conditions":dict(items),"observations":[],"audit":[]}}
def rehash(v):v["evidence_hash"]=_version_hash(v)
def add_real(l,system,i,result="Won"):
 ns=l["wilson_validation"];s=ns["condition_order"][2];f=ns["conditions"][s];stage=f"2026-08-21T{i//60:02d}:{i%60:02d}:00+08:00";odds=2.5
 a={"signature":s,"definition":copy.deepcopy(f["definition"]),"history":copy.deepcopy(f["historical_evidence"]),"arithmetic":admission_arithmetic(f["historical_evidence"]["hits"],f["historical_evidence"]["decided"],odds)}
 a,reason=apply_active_evidence(l,system,a,stage_at=stage,now=stage);assert reason is None
 r=commit_bet(l,system,{"match_id":f"fixture-{i}","kickoff":"2026-08-22T00:00:00+08:00"},"CHL",{"side":"H","line":0,"odds":odds},a,now=stage,market_label="角球",selected_label="角球",selected_role="home",selected_line=0);assert r
 r.update(status="SETTLED",result=result,pnl=1,settled_at="2026-08-22T01:00:00+08:00");l["bets"].append(r);return r
class ManifestTest(unittest.TestCase):
 def assertRejected(self,l,reason,system="footbreak"):
  m=build_manifest(l,system); self.assertFalse(m["valid"],m); self.assertIn(reason,m["rejection_reasons"])
 def first(self,l):return l["wilson_validation"]["conditions"][l["wilson_validation"]["condition_order"][0]]
 def test_all_stages_markets_and_systems(self):
  for system in ("footbreak","crown"):
   l=ledger(system); before=copy.deepcopy(l); m=build_manifest(l,system); self.assertTrue(m["valid"],m); self.assertEqual(l,before); self.assertEqual(m["decision_stage_counts"],{"T-30":1,"T-5":1,"首預":1}); self.assertEqual([r["market"] for r in m["conditions"]],["HDC","HIL","CHL"]); self.assertEqual([r["current_matcher_can_structurally_admit"] for r in m["conditions"]],[False,False,True]); self.assertFalse(m["recovery"]["implemented"])
 def test_namespace_schema_types_empty_order_and_numbers(self):
  probes=[("unsupported_namespace_schema",lambda l:l["wilson_validation"].update(schema_version=999)),("invalid_namespace_activation_at",lambda l:l["wilson_validation"].update(activation_at="bad")),("invalid_conditions_type",lambda l:l["wilson_validation"].update(conditions=[])),("invalid_condition_order_type",lambda l:l["wilson_validation"].update(condition_order=None)),("invalid_bets_type",lambda l:l.update(bets=None)),("invalid_observations_type",lambda l:l["wilson_validation"].update(observations=None)),("duplicate_condition_order",lambda l:l["wilson_validation"]["condition_order"].append(l["wilson_validation"]["condition_order"][0])),("invalid_or_duplicate_condition_number",lambda l:self.first(l).update(condition_number=2))]
  for reason,mut in probes:
   with self.subTest(reason):l=ledger();mut(l);self.assertRejected(l,reason)
  l=ledger();l["wilson_validation"]["conditions"]={};l["wilson_validation"]["condition_order"]=[];self.assertRejected(l,"empty_registry")
  l=ledger();s=l["wilson_validation"]["condition_order"][0];l["wilson_validation"]["conditions"][s]=42;self.assertRejected(l,"ordered_condition_not_object")
 def test_history_v1_and_artifact_binding(self):
  for reason,mut in [("missing_historical_evidence",lambda f:f.pop("historical_evidence")),("missing_historical_artifact",lambda f:f["historical_evidence"].pop("artifact")),("historical_v1_counts_mismatch",lambda f:f["historical_evidence"].update(hits=49)),("historical_v1_boundary_mismatch",lambda f:f["historical_evidence"]["artifact"].update(as_of="2026-08-19T00:00:00+08:00")),("invalid_v1_baseline",lambda f:f["evidence_versions"][0].update(prior_version=9))]:
   l=ledger();f=self.first(l);mut(f); rehash(f["evidence_versions"][0]);self.assertRejected(l,reason)
 def test_chain_arithmetic_hash_batch_and_chronology(self):
  mutations=[("invalid_evidence_counts",lambda v:v.update(cumulative_hits=99,cumulative_decided=1)),("evidence_arithmetic_mismatch",lambda v:v.update(wilson95_lower_raw=.1)),("invalid_evidence_timestamp",lambda v:v.update(created_at="bad")),("invalid_batch_hashes",lambda v:v.update(batch_fixture_market_hashes=["x","x"])),("evidence_prior_pointer_mismatch",lambda v:v.update(prior_version=99)),("evidence_boundary_regression",lambda v:v.update(activation_boundary_at="2026-08-19T00:00:00+08:00"))]
  for reason,mut in mutations:
   with self.subTest(reason):
    l=ledger();f=self.first(l);p=f["evidence_versions"][0];lo=wilson95(60,100)[0];v={"condition_signature":f["signature"],"version":2,"prior_version":1,"prior_evidence_hash":p["evidence_hash"],"batch_fixture_market_hashes":[format(i,"064x") for i in range(20)],"batch_hits":10,"batch_decided":20,"cumulative_hits":60,"cumulative_decided":100,"wilson95_lower_raw":lo,"minimum_acceptable_odds_raw":1/(lo-EDGE_BUFFER),"activation_boundary_at":"2026-08-21T00:00:00+08:00","created_at":"2026-08-21T00:00:00+08:00"};mut(v);rehash(v);f["evidence_versions"].append(v);self.assertRejected(l,reason)
 def test_definition_market_tier_path_and_signature_drift(self):
  for mutate in (lambda d:(d.update(market="XYZ"),d.update(miner_key=[x.replace("market=HDC","market=XYZ") for x in d["miner_key"]])),lambda d:d.update(miner_key=["system=footbreak","market=HDC"]),lambda d:d["miner_key"].append("tier_path=低")):
   l=ledger();f=self.first(l);mutate(f["definition"]);old=f["signature"];new=definition_signature(f["definition"]);f["signature"]=new;l["wilson_validation"]["condition_order"][0]=new;l["wilson_validation"]["conditions"]={new:f,**{k:v for k,v in l["wilson_validation"]["conditions"].items() if k!=old}};f["evidence_versions"][0]["condition_signature"]=new;rehash(f["evidence_versions"][0]);self.assertRejected(l,"invalid_formal_matcher_axes")
 def test_activity_exact_qualification_refunds_duplicates_and_x20(self):
  l=ledger();base=add_real(l,"footbreak",1);l["bets"] += [{**base,"portfolio":"crown_wilson_test"},{**base,"strategy":"foreign"},{**base,"result":"Refunded"}];m=build_manifest(l,"footbreak");r=m["conditions"][2];self.assertEqual(r["formal_rows"],2);self.assertEqual(r["prospective_x20"]["decided"],1);self.assertEqual(r["prospective_x20"]["hits"],1);self.assertGreaterEqual(r["rejected_same_signature_activity"],2);self.assertEqual(r["safely_recoverable_missed_rows"],0)
 def test_production_26_becomes_20_plus_6(self):
  for system in ("footbreak","crown"):
   l=ledger(system)
   for i in range(1,27):add_real(l,system,i,"Won" if i%2 else "Lost")
   recompute_namespace(l,system);m=build_manifest(l,system);r=m["conditions"][2];self.assertTrue(m["valid"],m);self.assertEqual(r["prospective_x20"]["decided"],6);self.assertEqual(r["prospective_x20"]["hits"],3)
 def test_production_40_rows_create_two_supported_versions(self):
  for system in ("footbreak","crown"):
   l=ledger(system)
   for i in range(1,41):add_real(l,system,i,"Won" if i%2 else "Lost")
   recompute_namespace(l,system);m=build_manifest(l,system);self.assertTrue(m["valid"],m);f=l["wilson_validation"]["conditions"][l["wilson_validation"]["condition_order"][2]];self.assertEqual(len(f["evidence_versions"]),3);self.assertEqual(m["conditions"][2]["prospective_x20"]["decided"],0)
 def test_merged_batch_provenance_tampering_rejects(self):
  for mutation in ("schema","post_hoc","excluded","status","result"):
   l=ledger()
   for i in range(1,21):add_real(l,"footbreak",i,"Won" if i%2 else "Lost")
   recompute_namespace(l,"footbreak")
   for row in l["bets"]:
    if mutation=="schema":row["rollover_provenance"]["schema_version"]=999
    elif mutation=="post_hoc":row["post_hoc_backfill"]=True
    elif mutation=="excluded":row["exclude_from_simulation"]=True
   if mutation=="status":l["bets"][0]["status"]="VOIDED"
   if mutation=="result":l["bets"][0]["result"]="Refunded"
   self.assertRejected(l,"unverifiable_same_signature_activity" if mutation in {"schema","post_hoc","excluded"} else "evidence_batch_rows_unverifiable")
 def test_admitted_snapshot_temporal_window_for_both_systems(self):
  for system in ("footbreak","crown"):
   l=ledger(system)
   for i in range(1,21):add_real(l,system,i,"Won" if i%2 else "Lost")
   recompute_namespace(l,system);f=l["wilson_validation"]["conditions"][l["wilson_validation"]["condition_order"][2]];v2=f["evidence_versions"][1];row=l["bets"][0];marker=row["rollover_provenance"];marker["admitted_evidence_version"]=2;marker["admitted_evidence_hash"]=v2["evidence_hash"];row["evidence_version"]=2;row["evidence_hash"]=v2["evidence_hash"];h=row["frozen_historical_evidence"];h.update(hits=v2["cumulative_hits"],decided=v2["cumulative_decided"],evidence_version=2,evidence_hash=v2["evidence_hash"]);self.assertRejected(l,"unverifiable_same_signature_activity",system)
   l=ledger(system);row=add_real(l,system,1);row["rollover_provenance"]["stage_at"]=NOW;self.assertRejected(l,"unverifiable_same_signature_activity",system)
 def test_strict_integer_schema_and_version_identities(self):
  for value in (True,2.0):
   l=ledger();l["wilson_validation"]["schema_version"]=value;self.assertRejected(l,"unsupported_namespace_schema")
  for value in (True,1.0):
   l=ledger();row=add_real(l,"footbreak",1);row["rollover_provenance"]["schema_version"]=value;self.assertRejected(l,"unverifiable_same_signature_activity")
  for value in (True,1.0):
   l=ledger();row=add_real(l,"footbreak",1);row["rollover_provenance"]["admitted_evidence_version"]=value;row["evidence_version"]=value;row["frozen_historical_evidence"]["evidence_version"]=value;self.assertRejected(l,"unverifiable_same_signature_activity")
  l=ledger();f=self.first(l);f["evidence_versions"][0]["version"]=1.0;rehash(f["evidence_versions"][0]);self.assertRejected(l,"evidence_version_sequence_mismatch")
 def test_remaining_integer_bindings_reject_bool_and_float_both_systems(self):
  for system in ("footbreak","crown"):
   for field,value in (("condition_number",3.0),("condition_number",True),("hits",50.0),("hits",True),("decided",80.0),("decided",True),("pushes",0.0),("pushes",False)):
    l=ledger(system);f=self.first(l)
    if field=="condition_number":f[field]=value
    else:f["historical_evidence"][field]=value
    self.assertRejected(l,"condition_number_order_mismatch" if field=="condition_number" else ("invalid_historical_pushes" if field=="pushes" else "invalid_historical_counts"),system)
   for field,value in (("condition_number",3.0),("condition_number",True),("hits",50.0),("hits",True),("decided",80.0),("decided",True),("pushes",0.0),("pushes",False)):
    l=ledger(system);row=add_real(l,system,1)
    if field=="condition_number":row[field]=value
    else:row["frozen_historical_evidence"][field]=value
    self.assertRejected(l,"unverifiable_same_signature_activity",system)
   for field,value in (("eligible_decided",1.0),("eligible_decided",True),("eligible_hits",1.0),("eligible_hits",True),("required",20.0),("required",True)):
    l=ledger(system);add_real(l,system,1);recompute_namespace(l,system);f=l["wilson_validation"]["conditions"][l["wilson_validation"]["condition_order"][2]];f["pending_rollover_progress"][field]=value;self.assertRejected(l,"pending_progress_mismatch",system)
   l=ledger(system);add_real(l,system,1);recompute_namespace(l,system);f=l["wilson_validation"]["conditions"][l["wilson_validation"]["condition_order"][2]];f["pending_rollover_progress"]["excluded"]["before_snapshot_boundary"]=False;self.assertRejected(l,"pending_progress_mismatch",system)
   for value in (1.0,True):
    l=ledger(system);self.first(l)["last_rollover_count"]=value;self.assertRejected(l,"invalid_last_rollover_count",system)
 def test_active_tail_hash_and_projection_exact_both_systems(self):
  for system in ("footbreak","crown"):
   for mutation,reason in (
    (lambda f:f.pop("active_evidence_version"),"missing_active_evidence_projection"),
    (lambda f:f.update(active_evidence_version=1.0),"invalid_active_evidence_version"),
    (lambda f:f.update(active_evidence_version=True),"invalid_active_evidence_version"),
    (lambda f:f.pop("active_evidence_hash"),"missing_active_evidence_projection"),
    (lambda f:f.update(active_evidence_hash="f"*64),"invalid_active_evidence_hash"),
    (lambda f:f.pop("active_evidence"),"missing_active_evidence_projection"),
    (lambda f:f.update(active_evidence={"version":999}),"invalid_active_evidence_projection"),
    (lambda f:f["active_evidence"].update(version=1.0),"invalid_active_evidence_projection"),
    (lambda f:f["active_evidence"].update(cumulative_decided=True),"invalid_active_evidence_projection"),
    (lambda f:f["active_evidence"].update(cumulative_hits=999),"invalid_active_evidence_projection"),
   ):
    l=ledger(system);f=l["wilson_validation"]["conditions"][l["wilson_validation"]["condition_order"][2]];mutation(f);self.assertRejected(l,reason,system)
 def test_delayed_granular_migration_and_missing_observations_are_valid(self):
  for system in ("footbreak","crown"):
   path="首預→T-30→T-5";tier="低→低→低";ranking=[{"system":system,"market":"HDC","key":[f"system={system}","market=HDC",f"path={path}","decision=T-5","direction=A→A→A","role=主讓","bucket=0.25–0.5","tier=低",f"tier_path={tier}","movement=不變"],"observed_path":path,"decision_stage":"T-5","direction":"A→A→A","role":"主讓","line_bucket":"0.25–0.5","odds_tier":"低","odds_trajectory":tier,"movement":"不變","total":{"hits":141,"decided":231,"pushes":0},"holdout":{"hits":44,"decided":71,"pushes":0},"source_artifact":{"hash":"a"*64,"version":"v1","as_of":"2026-08-19T22:55:00+08:00"}}]
   l={"bets":[]};ensure_namespace(l,system,now="2026-08-20T10:00:00+08:00");project_granular_ranking_evidence(l,system,ranking,now="2026-08-20T22:00:00+08:00");self.assertNotIn("observations",l["wilson_validation"]);m=build_manifest(l,system);self.assertTrue(m["valid"],m)
 def test_extra_axis_fabricated_batch_and_tampered_rows_reject(self):
  l=ledger();f=self.first(l);old=f["signature"];f["definition"]["miner_key"].append("foo=bar");new=definition_signature(f["definition"]);f["signature"]=new;f["evidence_versions"][0]["condition_signature"]=new;rehash(f["evidence_versions"][0]);ns=l["wilson_validation"];ns["condition_order"][0]=new;ns["conditions"][new]=ns["conditions"].pop(old);self.assertRejected(l,"invalid_formal_matcher_axes")
  l=ledger();s=l["wilson_validation"]["condition_order"][2];f=l["wilson_validation"]["conditions"][s];p=f["evidence_versions"][0];lo=wilson95(60,100)[0];v={"condition_signature":s,"version":2,"prior_version":1,"prior_evidence_hash":p["evidence_hash"],"batch_fixture_market_hashes":[format(i+5000,"064x") for i in range(20)],"batch_hits":10,"batch_decided":20,"cumulative_hits":60,"cumulative_decided":100,"wilson95_lower_raw":lo,"minimum_acceptable_odds_raw":1/(lo-EDGE_BUFFER),"activation_boundary_at":"2026-08-21T00:20:00+08:00","created_at":"2026-08-21T00:21:00+08:00"};rehash(v);f["evidence_versions"].append(v);self.assertRejected(l,"evidence_batch_rows_unverifiable")
  for mutation in ("history","fixture_hash"):
   l=ledger();s=l["wilson_validation"]["condition_order"][2];f=l["wilson_validation"]["conditions"][s];a=f["evidence_versions"][0];fixture="fixture-x";marker={"schema_version":1,"system":"footbreak","condition_signature":s,"native_pre_kickoff_t5":True,"stage_at":"2026-08-21T01:00:00+08:00","fixture_market_hash":_fixture_market_hash("footbreak",fixture,"CHL"),"admitted_evidence_version":1,"admitted_evidence_hash":a["evidence_hash"]};row={"portfolio":portfolio_name("footbreak"),"strategy":STRATEGY,"frozen_condition_signature":s,"condition_number":3,"frozen_condition_definition":f["definition"],"frozen_historical_evidence":f["historical_evidence"],"match_id":fixture,"market":"CHL","stage":"T-5","first_native_pre_kickoff_t5":True,"simulation_only":True,"real_betting_enabled":False,"rollover_provenance":marker,"evidence_version":1,"evidence_hash":a["evidence_hash"],"status":"SETTLED","result":"Won"}
   if mutation=="history":row["frozen_historical_evidence"]={"hits":999}
   else:row["rollover_provenance"]["fixture_market_hash"]="f"*64
   l["bets"]=[row];self.assertRejected(l,"unverifiable_same_signature_activity")
  l=ledger();self.first(l)["historical_evidence"]["artifact"]["hash"]="not-a-hash";self.assertRejected(l,"invalid_historical_artifact")
 def test_malformed_never_raises(self):
  for value in (None,[],{"wilson_validation":{"schema_version":2,"system":"footbreak","activation_at":NOW,"conditions":{},"condition_order":[],"observations":None,"audit":None},"bets":None}):self.assertFalse(build_manifest(value,"footbreak")["valid"])
 def test_output_same_symlink_and_hardlink_are_rejected(self):
  with tempfile.TemporaryDirectory() as td:
   root=Path(td);src=root/"ledger.json";src.write_text(json.dumps(ledger()),encoding="utf-8");before=src.read_bytes()
   aliases=[src,root/"sym.json",root/"hard.json"];aliases[1].symlink_to(src);os.link(src,aliases[2])
   for out in aliases:
    p=subprocess.run(["python","-m","analysis.wilson_registry_manifest","--system","footbreak","--ledger",str(src),"--output",str(out)],cwd=Path(__file__).parents[2],capture_output=True,text=True);self.assertNotEqual(p.returncode,0);self.assertEqual(src.read_bytes(),before)
if __name__=="__main__":unittest.main()
