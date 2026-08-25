"""Strict read-only, signature-keyed audit of a persisted Wilson registry.

Recovery is intentionally absent. Malformed input is represented as an invalid
manifest; it is never repaired, normalized, or admitted.
"""
from __future__ import annotations
import argparse, hashlib, json, math, os, tempfile
from collections import Counter
from pathlib import Path
from typing import Any
from analysis.wilson_validation import (
    BINARY_DECIDED_RESULTS, BINARY_HIT_RESULTS, DECISION_STAGE, EDGE_BUFFER,
    MIN_DECIDED, ROLLOVER_BATCH_SIZE, SCHEMA_VERSION, STRATEGY,
    _eligible_rollover_rows, _fixture_market_hash, _time, _version_hash, condition_signature,
    formal_matcher_axes, portfolio_name, wilson95,
)
SYSTEMS=("footbreak","crown")

def canonical_hash(v:Any)->str:
 return hashlib.sha256(json.dumps(v,ensure_ascii=False,sort_keys=True,separators=(",",":")).encode()).hexdigest()
def definition_signature(d:dict[str,Any])->str:
 raw=json.dumps(d,ensure_ascii=False,sort_keys=True,separators=(",",":")); return hashlib.sha256(raw.encode()).hexdigest()[:24]
def _int(v:Any)->int|None:
 return v if isinstance(v,int) and not isinstance(v,bool) else None
def _finite(v:Any)->float|None:
 try: x=float(v)
 except (TypeError,ValueError): return None
 return x if math.isfinite(x) else None
def _equal(a:Any,b:Any)->bool:
 x,y=_finite(a),_finite(b); return x is not None and y is not None and abs(x-y)<=1e-12
def _hash64(v:Any)->bool: return isinstance(v,str) and len(v)==64 and all(c in "0123456789abcdef" for c in v)
def _activity(ledger:dict[str,Any],ns:dict[str,Any],system:str)->tuple[list[dict[str,Any]],list[dict[str,Any]]]:
 bets=ledger.get("bets"); obs=ns.get("observations", [])
 if not isinstance(bets,list) or not isinstance(obs,list): return [],[]
 good_bets=[r for r in bets if isinstance(r,dict) and r.get("portfolio")==portfolio_name(system) and r.get("strategy")==STRATEGY]
 good_obs=[r for r in obs if isinstance(r,dict) and r.get("portfolio")==f"{system}_wilson_observations" and r.get("strategy")==STRATEGY and r.get("formal_bet") is False and r.get("stage")==DECISION_STAGE and r.get("first_native_pre_kickoff_t5") is True and isinstance(r.get("rollover_provenance"),dict) and r.get("status") in {"PENDING","SETTLED","VOIDED"}]
 return good_bets,good_obs

def build_manifest(ledger:Any,system:str)->dict[str,Any]:
 reasons=Counter(); rows=[]
 try:
  if system not in SYSTEMS: raise ValueError("unsupported_system")
  if not isinstance(ledger,dict): raise ValueError("ledger_not_object")
  ns=ledger.get("wilson_validation")
  if not isinstance(ns,dict): raise ValueError("missing_or_invalid_wilson_namespace")
  if _int(ns.get("schema_version"))!=SCHEMA_VERSION: reasons["unsupported_namespace_schema"]+=1
  if "granular_ranking_initial_migration_version" in ns and _int(ns.get("granular_ranking_initial_migration_version")) != 1:
   reasons["invalid_granular_migration_version"] += 1
  if ns.get("system")!=system: reasons["system_mismatch"]+=1
  if _time(ns.get("activation_at")) is None: reasons["invalid_namespace_activation_at"]+=1
  conditions=ns.get("conditions"); order=ns.get("condition_order")
  if not isinstance(conditions,dict): reasons["invalid_conditions_type"]+=1; conditions={}
  if not isinstance(order,list): reasons["invalid_condition_order_type"]+=1; order=[]
  if not conditions or not order: reasons["empty_registry"]+=1
  order_strings=[]
  for x in order:
   if not isinstance(x,str) or not x: reasons["invalid_order_signature"]+=1
   else: order_strings.append(x)
  if len(order_strings)!=len(set(order_strings)): reasons["duplicate_condition_order"]+=1
  if set(order_strings)-set(conditions): reasons["ordered_signature_missing_condition"]+=len(set(order_strings)-set(conditions))
  if set(conditions)-set(order_strings): reasons["condition_missing_from_order"]+=len(set(conditions)-set(order_strings))
  numbers=[]
  if not isinstance(ledger.get("bets"),list): reasons["invalid_bets_type"]+=1
  if "observations" in ns and not isinstance(ns.get("observations"),list): reasons["invalid_observations_type"]+=1
  if not isinstance(ns.get("audit"),list): reasons["invalid_audit_type"]+=1
  good_bets,good_obs=_activity(ledger,ns,system)
  migration_boundary=ns.get("rollover_migration_at") or ns.get("granular_ranking_initial_migration_completed_at") or ns.get("activation_at")
  if _time(migration_boundary) is None: reasons["invalid_migration_boundary"]+=1
  for position,sig in enumerate(order_strings,1):
   rr=[]; frozen=conditions.get(sig)
   if not isinstance(frozen,dict): reasons["ordered_condition_not_object"]+=1; continue
   number=_int(frozen.get("condition_number")); numbers.append(number)
   definition=frozen.get("definition")
   if not isinstance(definition,dict): rr.append("missing_or_invalid_definition"); definition={}
   if frozen.get("signature")!=sig: rr.append("stored_signature_key_mismatch")
   if not definition or definition_signature(definition)!=sig: rr.append("definition_signature_drift")
   if number!=position: rr.append("condition_number_order_mismatch")
   if definition.get("system")!=system: rr.append("definition_system_mismatch")
   candidate={**definition,"key":definition.get("miner_key")}
   rebuilt,_=condition_signature(system,candidate) if definition else (None,{})
   if rebuilt!=sig: rr.append("production_signature_roundtrip_failed")
   definition_axes=formal_matcher_axes(
       candidate, system=system, decision_stage=str(definition.get("stage") or ""),
   ) if definition else None
   axes=formal_matcher_axes(
       candidate, system=system, decision_stage=DECISION_STAGE,
   ) if definition else None
   if definition_axes is None: rr.append("invalid_formal_matcher_axes")
   history=frozen.get("historical_evidence")
   if not isinstance(history,dict): rr.append("missing_historical_evidence"); history={}
   hh,hd=_int(history.get("hits")),_int(history.get("decided"))
   if hh is None or hd is None or hh<0 or hd<MIN_DECIDED or hh>hd: rr.append("invalid_historical_counts")
   if "pushes" in history and (_int(history.get("pushes")) is None or _int(history.get("pushes")) < 0):
    rr.append("invalid_historical_pushes")
   artifact=history.get("artifact")
   if not isinstance(artifact,dict): rr.append("missing_historical_artifact"); artifact={}
   if not _hash64(artifact.get("hash")) or not artifact.get("version") or _time(artifact.get("as_of")) is None: rr.append("invalid_historical_artifact")
   versions=frozen.get("evidence_versions")
   if not isinstance(versions,list) or not versions: rr.append("missing_or_invalid_evidence_versions"); versions=[]
   prev=None; prev_boundary=None; prev_created=None; used_batch_hashes=set()
   for i,v in enumerate(versions,1):
    if not isinstance(v,dict): rr.append("malformed_evidence_version"); prev=None; continue
    vi=_int(v.get("version")); bh=_int(v.get("batch_hits")); bd=_int(v.get("batch_decided")); ch=_int(v.get("cumulative_hits")); cd=_int(v.get("cumulative_decided")); hashes=v.get("batch_fixture_market_hashes")
    if vi!=i: rr.append("evidence_version_sequence_mismatch")
    if v.get("condition_signature")!=sig: rr.append("evidence_signature_mismatch")
    if not _hash64(v.get("evidence_hash")) or v.get("evidence_hash")!=_version_hash(v): rr.append("evidence_hash_drift")
    boundary=_time(v.get("activation_boundary_at")); created=_time(v.get("created_at"))
    if boundary is None or created is None: rr.append("invalid_evidence_timestamp")
    if boundary and created and created<boundary: rr.append("evidence_created_before_boundary")
    if prev_boundary and boundary and boundary<prev_boundary: rr.append("evidence_boundary_regression")
    if prev_created and created and created < prev_created: rr.append("evidence_created_at_regression")
    invalid_counts = (
        any(x is None for x in (bh,bd,ch,cd))
        or any(x is not None and x < 0 for x in (bh,bd,ch,cd))
        or (bh is not None and bd is not None and bh > bd)
        or (ch is not None and cd is not None and ch > cd)
    )
    if invalid_counts: rr.append("invalid_evidence_counts")
    if not isinstance(hashes,list) or len(hashes)!=len(set(hashes)) or any(not _hash64(x) for x in hashes): rr.append("invalid_batch_hashes")
    if isinstance(hashes, list):
     if used_batch_hashes.intersection(x for x in hashes if isinstance(x, str)):
      rr.append("reused_batch_fixture_market_hash")
     used_batch_hashes.update(x for x in hashes if isinstance(x, str))
    expected=None if invalid_counts or not cd else wilson95(ch,cd)[0]
    minimum=1/(expected-EDGE_BUFFER) if expected is not None and expected>EDGE_BUFFER else None
    if not invalid_counts and (not _equal(v.get("wilson95_lower_raw"),expected) or (minimum is None and v.get("minimum_acceptable_odds_raw") is not None) or (minimum is not None and not _equal(v.get("minimum_acceptable_odds_raw"),minimum))): rr.append("evidence_arithmetic_mismatch")
    prior_version = v.get("prior_version")
    if i==1:
     if prior_version is not None or v.get("prior_evidence_hash") is not None or bh!=0 or bd!=0 or hashes!=[]: rr.append("invalid_v1_baseline")
     expected_boundary=artifact.get("as_of") or frozen.get("frozen_at") or migration_boundary
     if hh!=ch or hd!=cd: rr.append("historical_v1_counts_mismatch")
     if _time(expected_boundary) is None or _time(expected_boundary)!=boundary: rr.append("historical_v1_boundary_mismatch")
    else:
     if prev is None or _int(prior_version) is None or _int(prior_version)!=_int(prev.get("version")) or v.get("prior_evidence_hash")!=prev.get("evidence_hash"): rr.append("evidence_prior_pointer_mismatch")
     if prev and None not in (ch,cd,bh,bd) and (ch!=prev.get("cumulative_hits",0)+bh or cd!=prev.get("cumulative_decided",0)+bd): rr.append("evidence_cumulative_mismatch")
     migration=(i==2 and v.get("initial_migration_full_cohort") is True and v.get("batch_fixture_market_ids_unavailable_from_legacy_aggregate") is True)
     if migration:
      cohort=v.get("legacy_prospective_cohort");
      migration_markers = [
          ns.get("rollover_migration_at"),
          ns.get("granular_ranking_initial_migration_completed_at"),
      ]
      valid_markers = {_time(x) for x in migration_markers if _time(x) is not None}
      cohort_hits = _int(cohort.get("hits")) if isinstance(cohort, dict) else None
      cohort_decided = _int(cohort.get("decided")) if isinstance(cohort, dict) else None
      cohort_pushes = _int(cohort.get("pushes")) if isinstance(cohort, dict) else None
      if not isinstance(cohort,dict) or cohort_hits!=bh or cohort_decided!=bd or cohort_pushes is None or cohort_pushes<0 or not (bd and 0<=bh<=bd) or hashes!=[] or boundary not in valid_markers: rr.append("invalid_migration_v2")
     elif bd!=ROLLOVER_BATCH_SIZE or len(hashes)!=ROLLOVER_BATCH_SIZE: rr.append("invalid_ordinary_batch")
    prev=v; prev_boundary=boundary or prev_boundary; prev_created=created or prev_created
   active=versions[-1] if versions and isinstance(versions[-1],dict) else {}
   if (
       "active_evidence_version" not in frozen
       or "active_evidence_hash" not in frozen
       or "active_evidence" not in frozen
   ):
    rr.append("missing_active_evidence_projection")
   if "active_evidence_version" in frozen and (
       _int(frozen.get("active_evidence_version")) is None
       or _int(frozen.get("active_evidence_version")) != _int(active.get("version"))
   ):
    rr.append("invalid_active_evidence_version")
   if (
       not _hash64(frozen.get("active_evidence_hash"))
       or frozen.get("active_evidence_hash") != active.get("evidence_hash")
   ):
    rr.append("invalid_active_evidence_hash")
   expected_active_projection = {
       key: active.get(key) for key in (
           "version", "cumulative_hits", "cumulative_decided",
           "wilson95_lower_raw", "minimum_acceptable_odds_raw",
           "minimum_acceptable_odds_display", "activation_boundary_at",
           "created_at", "evidence_hash",
       )
   }
   active_projection = frozen.get("active_evidence")
   if (
       not isinstance(active_projection, dict)
       or active_projection != expected_active_projection
       or _int(active_projection.get("version")) is None
       or _int(active_projection.get("cumulative_hits")) is None
       or _int(active_projection.get("cumulative_decided")) is None
   ):
    rr.append("invalid_active_evidence_projection")
   version_by_number = {
       v.get("version"): v for v in versions if isinstance(v, dict)
   }
   qualified=[]; activity_rejections=Counter()
   candidate_activity = []
   for collection in (ledger.get("bets",[]), ns.get("observations",[])):
    if isinstance(collection, list):
     candidate_activity.extend(
         x for x in collection if isinstance(x, dict)
         and str(x.get("frozen_condition_signature") or "") == sig
     )
   for activity in candidate_activity:
    is_bet = activity in good_bets
    is_observation = activity in good_obs
    if not (is_bet or is_observation):
     activity_rejections["wrong_portfolio_strategy_or_formal_shape"] += 1
     continue
    if str(activity.get("frozen_condition_signature") or "")!=sig: continue
    marker=activity.get("rollover_provenance")
    marker_version = _int(marker.get("admitted_evidence_version")) if isinstance(marker,dict) else None
    row_version = _int(activity.get("evidence_version"))
    row_history_version = _int(
        activity.get("frozen_historical_evidence", {}).get("evidence_version")
    ) if isinstance(activity.get("frozen_historical_evidence"), dict) else None
    admitted = version_by_number.get(marker_version) if marker_version is not None else None
    row_history = activity.get("frozen_historical_evidence")
    immutable_row_history = (
        {k: v for k, v in row_history.items()
         if k not in {"hits", "decided", "evidence_version", "evidence_hash"}}
        if isinstance(row_history, dict) else None
    )
    immutable_baseline = {
        k: v for k, v in history.items()
        if k not in {"hits", "decided", "evidence_version", "evidence_hash"}
    }
    history_matches_admitted = (
        isinstance(row_history, dict)
        and immutable_row_history == immutable_baseline
        and isinstance(admitted, dict)
        and _int(row_history.get("hits")) is not None
        and _int(row_history.get("decided")) is not None
        and _int(row_history.get("hits")) == _int(admitted.get("cumulative_hits"))
        and _int(row_history.get("decided")) == _int(admitted.get("cumulative_decided"))
        and (
            "pushes" not in row_history
            or (
                _int(row_history.get("pushes")) is not None
                and _int(row_history.get("pushes")) >= 0
            )
        )
        and row_history_version is not None
        and row_history_version == _int(admitted.get("version"))
        and row_history.get("evidence_hash") == admitted.get("evidence_hash")
    )
    if (
        not isinstance(marker,dict)
        or marker.get("condition_signature")!=sig
        or marker.get("system")!=system
        or _int(marker.get("schema_version")) != 1
        or marker.get("native_pre_kickoff_t5") is not True
        or bool(activity.get("post_hoc_backfill"))
        or bool(activity.get("exclude_from_simulation"))
        or activity.get("stage") != DECISION_STAGE
        or activity.get("first_native_pre_kickoff_t5") is not True
        or marker_version is None
        or row_version is None
        or marker_version != row_version
        or marker.get("admitted_evidence_hash")!=activity.get("evidence_hash")
        or not isinstance(admitted, dict)
        or admitted.get("evidence_hash") != marker.get("admitted_evidence_hash")
        or activity.get("market", activity.get("code")) != definition.get("market")
        or activity.get("frozen_condition_definition") != definition
        or not history_matches_admitted
        or _int(activity.get("condition_number")) is None
        or _int(activity.get("condition_number")) != number
    ):
     activity_rejections["invalid_signature_definition_or_evidence_binding"] += 1
     continue
    if _time(marker.get("stage_at")) is None:
     activity_rejections["invalid_native_t5_timestamp"] += 1
     continue
    stage_time = _time(marker.get("stage_at"))
    admitted_boundary = _time(admitted.get("activation_boundary_at")) if isinstance(admitted, dict) else None
    admitted_created = _time(admitted.get("created_at")) if isinstance(admitted, dict) else None
    later_versions = [
        v for v in versions if isinstance(v, dict)
        and _int(v.get("version")) is not None
        and _int(v.get("version")) > marker_version
    ]
    if (
        admitted_boundary is None
        or admitted_created is None
        or stage_time <= admitted_boundary
        or admitted_created > stage_time
        or any(
            _time(v.get("created_at")) is None
            or _time(v.get("activation_boundary_at")) is None
            or (
                _time(v.get("created_at")) <= stage_time
                and stage_time >= _time(v.get("activation_boundary_at"))
            )
            for v in later_versions
        )
    ):
     activity_rejections["stage_outside_admitted_version_window"] += 1
     continue
    fixture = str(activity.get("match_id") or "")
    market = str(activity.get("market") or activity.get("code") or "")
    if not fixture or marker.get("fixture_market_hash") != _fixture_market_hash(system, fixture, market):
     activity_rejections["fixture_market_hash_mismatch"] += 1
     continue
    if is_bet and (
        activity.get("simulation_only") is not True
        or activity.get("real_betting_enabled") is not False
    ):
     activity_rejections["not_isolated_simulation_bet"] += 1
     continue
    qualified.append(activity)
   if activity_rejections:
    rr.append("unverifiable_same_signature_activity")
   # Replay production eligibility sequentially. Rows retain their immutable
   # original admission version even when one recomputation creates v2 and v3.
   prior = versions[0] if versions and isinstance(versions[0], dict) else None
   for v in versions[1:]:
    if not isinstance(v, dict):
     continue
    if v.get("initial_migration_full_cohort") is True:
     prior = v
     continue
    batch_hashes = v.get("batch_fixture_market_hashes")
    eligible_for_prior, _batch_excluded = (
        _eligible_rollover_rows(qualified, system, sig, prior)
        if isinstance(prior, dict) else ([], {})
    )
    support = eligible_for_prior[:ROLLOVER_BATCH_SIZE]
    ordered_hashes = [item["fixture_market_hash"] for item in support]
    ambiguous_boundary = (
        len(eligible_for_prior) > ROLLOVER_BATCH_SIZE
        and support
        and support[-1]["stage_at"] == eligible_for_prior[ROLLOVER_BATCH_SIZE]["stage_at"]
    )
    if (
        not isinstance(batch_hashes, list)
        or ordered_hashes != batch_hashes
        or len(support) != ROLLOVER_BATCH_SIZE
        or ambiguous_boundary
        or sum(item["hit"] for item in support) != v.get("batch_hits")
        or not support
        or _time(support[-1]["stage_at"]) != _time(v.get("activation_boundary_at"))
    ):
     rr.append("evidence_batch_rows_unverifiable")
    prior = v
   # Do not filter by admitted version: production uses the active boundary,
   # so the six later v1-admitted rows in 26→20+6 remain pending under v2.
   eligible,excluded=_eligible_rollover_rows(qualified,system,sig,active) if active else ([],{"missing_or_invalid_provenance":0})
   pending=eligible
   pending_hits=sum(x["hit"] for x in pending)
   persisted=frozen.get("pending_rollover_progress")
   pending_integer_invalid = False
   if isinstance(persisted, dict):
    for key in ("eligible_decided", "eligible_hits", "required", "decided", "hits"):
     if key in persisted and (
         _int(persisted.get(key)) is None or _int(persisted.get(key)) < 0
     ):
      pending_integer_invalid = True
    persisted_excluded = persisted.get("excluded")
    if persisted_excluded is not None and (
        not isinstance(persisted_excluded, dict)
        or any(_int(value) is None or _int(value) < 0
               for value in persisted_excluded.values())
    ):
     pending_integer_invalid = True
   if persisted is not None and (
       not isinstance(persisted,dict)
       or pending_integer_invalid
       or _int(persisted.get("eligible_decided")) is None
       or _int(persisted.get("eligible_hits")) is None
       or _int(persisted.get("required")) is None
       or _int(persisted.get("eligible_decided"))!=len(pending)
       or _int(persisted.get("eligible_hits"))!=pending_hits
       or _int(persisted.get("required"))!=ROLLOVER_BATCH_SIZE
       or ("decided" in persisted and _int(persisted.get("decided")) != len(pending))
       or ("hits" in persisted and _int(persisted.get("hits")) != pending_hits)
       or ("excluded" in persisted and persisted.get("excluded") != excluded)
   ): rr.append("pending_progress_mismatch")
   if "last_rollover_count" in frozen and (
       _int(frozen.get("last_rollover_count")) is None
       or _int(frozen.get("last_rollover_count")) < 0
   ):
    rr.append("invalid_last_rollover_count")
   for reason in set(rr): reasons[reason]+=1
   statuses=Counter(str(x.get("status")) for x in qualified)
   rows.append({"condition_number":number,"order_position":position,"signature":sig,"definition":definition,"definition_hash":canonical_hash(definition) if definition else None,"market":definition.get("market"),"path":definition.get("path"),"decision_stage":definition.get("stage"),"path_terminal_stage":str(definition.get("path") or "").split("→")[-1] or None,"active_evidence_version":active.get("version"),"active_evidence_hash":active.get("evidence_hash"),"activation_boundary":active.get("activation_boundary_at"),"prospective_x20":{"hits":pending_hits,"decided":len(pending),"target":ROLLOVER_BATCH_SIZE,"excluded":excluded},"formal_rows":len(qualified),"formal_status_counts":dict(sorted(statuses.items())),"rejected_same_signature_activity":sum(activity_rejections.values()),"activity_rejection_reasons":dict(sorted(activity_rejections.items())),"current_matcher_can_structurally_admit":axes is not None,"safely_recoverable_missed_rows":0,"recovery_status":"HARD_DISABLED","rejection_reasons":sorted(set(rr)),"valid":not rr})
  good_numbers=[x for x in numbers if x is not None]
  if len(good_numbers)!=len(numbers) or len(good_numbers)!=len(set(good_numbers)): reasons["invalid_or_duplicate_condition_number"]+=1
  out={"schema":"wilson-registry-manifest-v2","system":system,"namespace_schema_version":ns.get("schema_version"),"namespace_activation_at":ns.get("activation_at"),"condition_count":len(rows),"decision_stage_counts":dict(sorted(Counter(str(r.get("decision_stage") or "MISSING") for r in rows).items())),"conditions":rows,"rejection_reasons":dict(sorted(reasons.items())),"valid":not reasons,"recovery":{"implemented":False,"mode":"audit-only","safely_recoverable_missed_rows":0,"reason":"Recovery is hard-disabled."}}
 except Exception as exc:
  out={"schema":"wilson-registry-manifest-v2","system":system,"condition_count":len(rows),"conditions":rows,"rejection_reasons":{"malformed_input":1,"detail":type(exc).__name__},"valid":False,"recovery":{"implemented":False,"mode":"audit-only","safely_recoverable_missed_rows":0,"reason":"Recovery is hard-disabled."}}
 out["manifest_hash"]=canonical_hash(out); return out

def _same_file(a:Path,b:Path)->bool:
 try:
  if a.resolve()==b.resolve(): return True
  return a.exists() and b.exists() and os.path.samefile(a,b)
 except OSError: return False

def main()->int:
 p=argparse.ArgumentParser(); p.add_argument("--system",required=True,choices=SYSTEMS); p.add_argument("--ledger",required=True,type=Path); p.add_argument("--output",type=Path); p.add_argument("--require-valid",action="store_true"); a=p.parse_args()
 try:
  if a.output and _same_file(a.ledger,a.output): raise ValueError("output aliases input ledger")
  before=a.ledger.read_bytes(); result=build_manifest(json.loads(before),a.system)
  if a.ledger.read_bytes()!=before: raise RuntimeError("ledger mutated during audit")
  text=json.dumps(result,ensure_ascii=False,sort_keys=True,indent=2)+"\n"
  if a.output:
   a.output.parent.mkdir(parents=True,exist_ok=True)
   fd,tmp=tempfile.mkstemp(prefix=a.output.name+".",dir=a.output.parent)
   try:
    with os.fdopen(fd,"w",encoding="utf-8") as f: f.write(text); f.flush(); os.fsync(f.fileno())
    if _same_file(a.ledger,a.output): raise ValueError("output aliases input ledger")
    os.replace(tmp,a.output)
   finally:
    if os.path.exists(tmp): os.unlink(tmp)
  else: print(text,end="")
  return 2 if a.require_valid and not result["valid"] else 0
 except Exception as exc:
  print(json.dumps({"valid":False,"error":str(exc),"recovery":{"implemented":False}})); return 2
if __name__=="__main__": raise SystemExit(main())
