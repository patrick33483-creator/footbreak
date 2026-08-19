"""Footbreak probability challenger: isolated, append-only and research-only."""
from __future__ import annotations
import hashlib, json
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Iterable
from analysis.granular_conditions import MARKETS, _role, match_upcoming
from analysis.independent_validation import FIXED_STAKE, selection_signature
from analysis.probability_research import hierarchical_estimate, number, score_rows, promotion_gate
from condition_portfolio import _live_rows, _native_t5, _valid_selected, _audit_selection, parse_time

NAMESPACE="footbreak_probability_research"
STRATEGY="footbreak-hierarchical-probability-challenger-v1"
CUTOVER_AT="2026-08-19T20:00:00+08:00"
DECISION_STAGE="T-5"
HKT=timezone(timedelta(hours=8))
EVIDENCE_SCHEMA_VERSION=2
EVIDENCE_MAX_AGE_HOURS=48

def _now(): return datetime.now(HKT).isoformat(timespec="seconds")
def _boundary(ns):
    a,b=parse_time(CUTOVER_AT),parse_time(ns.get("activation_at")); return max(a,b) if a and b else None

def _json_digest(value: Any) -> str:
    return hashlib.sha256(json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), default=str).encode()).hexdigest()

def read_evidence_as_of(path: Path | str | None, stage_at: Any) -> tuple[list[dict[str, Any]] | None, dict[str, Any]]:
    """Load only a valid, fresh evidence artifact whose knowledge predates T-5."""
    cutoff=parse_time(stage_at)
    if path is None or cutoff is None:
        return None, {"available":False,"reason":"evidence_path_or_stage_timestamp_unavailable"}
    try:
        payload=json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, ValueError, TypeError):
        return None, {"available":False,"reason":"evidence_artifact_missing_or_malformed"}
    if not isinstance(payload,dict) or payload.get("schema_version") != EVIDENCE_SCHEMA_VERSION or payload.get("system") != "footbreak":
        return None, {"available":False,"reason":"evidence_artifact_schema_or_system_invalid"}
    generated=parse_time(payload.get("generated_at")); boundary=parse_time(payload.get("source_boundary_at"))
    entries=payload.get("entries")
    if generated is None or boundary is None or not isinstance(entries,list) or payload.get("entries_sha256") != _json_digest(entries):
        return None, {"available":False,"reason":"evidence_artifact_integrity_invalid"}
    if boundary > cutoff:
        return None, {"available":False,"reason":"evidence_artifact_source_boundary_after_candidate"}
    age=(cutoff-generated).total_seconds()/3600
    if generated > cutoff:
        return None, {"available":False,"reason":"evidence_artifact_generated_after_candidate"}
    if age > EVIDENCE_MAX_AGE_HOURS:
        return None, {"available":False,"reason":"evidence_artifact_stale","age_hours":round(age,3)}
    usable=[]
    for row in entries:
        decided=parse_time((row or {}).get("decided_at"))
        quote=parse_time((row or {}).get("quote_observed_at")); kickoff_at=parse_time((row or {}).get("kickoff")); native_at=parse_time((row or {}).get("native_stage_at"))
        required=("system","stage","market","path","odds_tier","direction","role","line_bucket","fixture_market_key","evidence_id")
        if not isinstance(row,dict) or row.get("system")!="footbreak" or row.get("stage")!="T-5" or any(row.get(key) in (None,"") for key in required) or row.get("outcome") not in {"Won","Lost"} or decided is None or quote is None or kickoff_at is None or native_at is None or quote >= kickoff_at or native_at >= kickoff_at or decided > boundary:
            return None, {"available":False,"reason":"evidence_artifact_entry_invalid"}
        if decided <= cutoff:
            usable.append(row)
    signature=_json_digest({"artifact":payload["entries_sha256"],"generated_at":payload["generated_at"],"as_of":cutoff.isoformat(),"ids":[row.get("evidence_id") for row in usable]})
    return usable, {"available":True,"artifact_entries_sha256":payload["entries_sha256"],"artifact_generated_at":payload["generated_at"],"source_boundary_at":payload["source_boundary_at"],"as_of":cutoff.isoformat(),"as_of_evidence_sha256":signature,"usable_rows":len(usable),"coverage":payload.get("coverage") or {}}

def ensure_namespace(ledger: dict[str,Any], *, now: str|None=None) -> dict[str,Any]:
    ns=ledger.setdefault(NAMESPACE,{})
    if not isinstance(ns,dict): raise ValueError("footbreak probability research namespace must be an object")
    ns.setdefault("schema_version",1); ns.setdefault("strategy",STRATEGY); ns.setdefault("cutover_at",CUTOVER_AT)
    if ns["cutover_at"]!=CUTOVER_AT: raise ValueError("Footbreak research cutover is immutable")
    ns.setdefault("activation_at",now or _now()); ns.setdefault("mode","research_only")
    ns.setdefault("real_betting_enabled",False); ns.setdefault("kelly_enabled",False); ns.setdefault("telegram_actionable_enabled",False)
    ns.setdefault("rows",[]); ns.setdefault("dedupe_keys",[]); ns.setdefault("audit",[])
    return ns

def _context(candidate:dict[str,Any], selected:dict[str,Any], watch:dict[str,Any]) -> dict[str,Any]:
    key=[str(x) for x in (candidate.get("key") or [])]
    def get(name,*fallback):
        for k in (name,*fallback):
            if candidate.get(k) not in (None,""): return candidate[k]
            found=next((v.split("=",1)[1] for v in key if v.startswith(k+"=")),None)
            if found not in (None,""): return found
        return None
    market=str(candidate.get("market") or selected.get("code") or "").upper(); side=str(selected.get("side") or "").upper()
    line=number(selected.get("line",selected.get("condition")))
    return {"system":"footbreak","stage":DECISION_STAGE,"market":market,"path":get("observed_path","path"),
      "odds_tier":get("odds_tier"),"direction":get("direction","selected_side","side") or side,
      "role":get("role","selected_role") or _role(market,side,line),"line_bucket":get("line_bucket","bucket"),
      "league":get("league")}

def evaluate_new_t5(ledger:dict[str,Any], watch:dict[str,Any], *, ranking:Iterable[dict[str,Any]]|None, evidence:Iterable[dict[str,Any]]|None=None, evidence_path:Path|str|None=None) -> tuple[list[dict[str,Any]],list[dict[str,Any]]]:
    ns=ensure_namespace(ledger); fixture=str(watch.get("match_id") or ""); stages=watch.get("stages") or []
    stage=next((s for s in stages if isinstance(s,dict) and s.get("stage")==DECISION_STAGE),None); audit=[]
    boundary=_boundary(ns)
    stage_at=parse_time((stage or {}).get("ts") or (stage or {}).get("source_snapshot_at")); kickoff=parse_time((stage or {}).get("kickoff") or watch.get("kickoff") or watch.get("kickoff_hkt"))
    if not fixture or not stage or not _native_t5(stage,stage.get("kickoff") or watch.get("kickoff")) or not boundary or not stage_at or stage_at<=boundary or not kickoff or stage_at>=kickoff:
        audit=[{"market":"*","status":"SKIPPED","reason":"research_policy_activation_native_t5_ineligible"}]; ns["audit"]=(ns["audit"]+audit)[-1600:]; return [],audit
    if ranking is None: audit=[{"market":"*","status":"SKIPPED","reason":"frozen_ranking_unavailable"}]; ns["audit"]=(ns["audit"]+audit)[-1600:]; return [],audit
    evidence_meta={"available": evidence is not None, "reason":"direct_test_evidence" if evidence is not None else "frozen_pre_admission_evidence_unavailable"}
    if evidence_path is not None:
        evidence, evidence_meta=read_evidence_as_of(evidence_path, stage_at)
    ns["evidence_status"]=evidence_meta
    matches=match_upcoming(_live_rows(watch),list(ranking),system="footbreak",decision_stage=DECISION_STAGE).get(fixture,[]); dedupe=set(map(str,ns["dedupe_keys"])); made=[]
    for market in MARKETS:
        selected,reason=_valid_selected(stage,market)
        if selected is None: audit.append({"market":market,"status":"SKIPPED","reason":reason}); continue
        sig=selection_signature(market,_audit_selection(market,selected)); candidate=next((x for x in matches if str(x.get("market") or "")==market and selection_signature(market,x)==sig),None)
        if not candidate: audit.append({"market":market,"status":"SKIPPED","reason":"no_exact_frozen_ranking_candidate"}); continue
        estimate=hierarchical_estimate(_context(candidate,selected,watch),evidence)
        for variant,probability in (("exact_only",estimate.get("exact_only_probability")),("hierarchical_shrunk",estimate.get("hierarchical_shrunk_probability"))):
            key=f"{fixture}|{market}|{DECISION_STAGE}|{STRATEGY}|{variant}"
            if key in dedupe: audit.append({"market":market,"variant":variant,"status":"SKIPPED","reason":"research_idempotent_existing_row"}); continue
            odds=number(selected.get("odds")); be=1/odds if odds and odds>1 else None
            row={"research_id":key,"dedupe_key":key,"strategy":STRATEGY,"portfolio":"footbreak_probability_research","variant":variant,"research_only":True,"simulation_only":True,"real_betting_enabled":False,"kelly_enabled":False,"actionable_telegram":False,"status":"PENDING","match_id":fixture,"league":watch.get("league"),"home":watch.get("home"),"away":watch.get("away"),"kickoff":watch.get("kickoff") or watch.get("kickoff_hkt"),"native_stage_timestamp":stage.get("ts") or stage.get("source_snapshot_at"),"stage":DECISION_STAGE,"first_native_pre_kickoff_t5":True,"market":market,"side":selected.get("side"),"line":selected.get("line",selected.get("condition")),"odds":odds,"quote_provenance":{"source":selected.get("quote_source") or selected.get("source"),"observed_at":selected.get("observed_at")},"break_even_probability":round(be,6) if be else None,"estimated_probability":probability,"edge":round(probability-be,6) if probability is not None and be is not None else None,"prediction_status":"available" if probability is not None else "unavailable","probability_estimate":estimate,"condition_signature_version":estimate.get("version"),"evidence_snapshot":dict(evidence_meta),"admission_boundary_at":boundary.isoformat(),"created_at":_now()}
            ns["rows"].append(row); ns["dedupe_keys"].append(key); dedupe.add(key); made.append(row); audit.append({"market":market,"variant":variant,"status":"CREATED","reason":"probability_research_row_created"})
    ns["dedupe_keys"]=ns["dedupe_keys"][-10000:]; ns["audit"]=(ns["audit"]+audit)[-1600:]; recompute(ns); return made,audit

def recompute(ns:dict[str,Any])->dict[str,Any]:
    rows=[r for r in ns.get("rows") or [] if isinstance(r,dict)]; report={"title":"機率驗證研究中","subtitle":"非正式推介","primary_unit":"unique_fixture_per_market","mode":"research_only","evidence":ns.get("evidence_status") or {"available":False,"reason":"evidence_not_checked"}, "markets":{}}
    for market in MARKETS:
        exact=[r|{"estimated_probability":r.get("estimated_probability")} for r in rows if r.get("market")==market and r.get("variant")=="exact_only"]
        shrunk=[r|{"estimated_probability":r.get("estimated_probability")} for r in rows if r.get("market")==market and r.get("variant")=="hierarchical_shrunk"]
        baseline=score_rows(exact,"estimated_probability"); challenger=score_rows(shrunk,"estimated_probability")
        report["markets"][market]={"exact_only":baseline,"hierarchical_shrunk":challenger,"promotion":promotion_gate(challenger,baseline,clv_coverage=None,mean_clv=None)}
    report["rows"]=len(rows); ns["stats"]=report; return report
