"""Read-only, signature-keyed Wilson frozen-registry manifest.

This tool never repairs, migrates, admits, settles, or recovers rows.  It exists
so a production ledger can prove the actual terminal decision stage before any
matcher or recovery semantics are changed.
"""
from __future__ import annotations
import argparse, hashlib, json
from collections import Counter
from pathlib import Path
from typing import Any

SYSTEMS = ("footbreak", "crown")
T5 = "T-5"

def canonical_hash(value: Any) -> str:
    return hashlib.sha256(json.dumps(value, ensure_ascii=False, sort_keys=True,
        separators=(",", ":")).encode()).hexdigest()

def definition_signature(definition: dict[str, Any]) -> str:
    return canonical_hash(definition)[:24]

def _terminal(path: Any) -> str | None:
    parts = [p.strip() for p in str(path or "").split("→") if p.strip()]
    return parts[-1] if parts else None

def _sig(row: Any) -> str | None:
    if not isinstance(row, dict): return None
    for key in ("frozen_condition_signature", "condition_signature", "signature"):
        if row.get(key): return str(row[key])
    return None

def build_manifest(ledger: dict[str, Any], system: str) -> dict[str, Any]:
    reasons = Counter(); ns = ledger.get("wilson_validation")
    if system not in SYSTEMS: raise ValueError("system must be footbreak or crown")
    if not isinstance(ns, dict):
        return {"system": system, "valid": False, "conditions": [],
                "rejection_reasons": {"missing_wilson_namespace": 1},
                "recovery": {"implemented": False, "safely_recoverable_missed_rows": 0}}
    if ns.get("system") != system: reasons["system_mismatch"] += 1
    conditions = ns.get("conditions") if isinstance(ns.get("conditions"), dict) else {}
    order = ns.get("condition_order") if isinstance(ns.get("condition_order"), list) else []
    if len(order) != len(set(map(str, order))): reasons["duplicate_condition_order"] += 1
    unknown = set(map(str, order)) - set(map(str, conditions))
    if unknown: reasons["ordered_signature_missing_condition"] += len(unknown)
    extra = set(map(str, conditions)) - set(map(str, order))
    if extra: reasons["condition_missing_from_order"] += len(extra)
    rows = []
    all_activity = []
    for key in ("bets",): all_activity.extend(x for x in ledger.get(key, []) if isinstance(x, dict))
    for key in ("observations", "audit"):
        all_activity.extend(x for x in ns.get(key, []) if isinstance(x, dict))
    for number, rawsig in enumerate(order, 1):
        sig = str(rawsig); frozen = conditions.get(sig); row_reasons=[]
        if not isinstance(frozen, dict): continue
        definition = frozen.get("definition")
        if not isinstance(definition, dict):
            row_reasons.append("missing_definition"); definition={}
        computed = definition_signature(definition) if definition else None
        if frozen.get("signature") != sig: row_reasons.append("stored_signature_key_mismatch")
        if computed != sig: row_reasons.append("definition_signature_drift")
        persisted_number = frozen.get("condition_number")
        if persisted_number != number: row_reasons.append("condition_number_order_mismatch")
        if definition.get("system") != system: row_reasons.append("definition_system_mismatch")
        path_terminal = _terminal(definition.get("path")); stage = definition.get("stage")
        if not stage: row_reasons.append("missing_decision_stage")
        if not path_terminal: row_reasons.append("missing_path_terminal")
        if stage and path_terminal and stage != path_terminal: row_reasons.append("stage_path_terminal_mismatch")
        versions=frozen.get("evidence_versions")
        if not isinstance(versions,list) or not versions: row_reasons.append("missing_evidence_versions"); versions=[]
        active=versions[-1] if versions and isinstance(versions[-1],dict) else {}
        for i,v in enumerate(versions,1):
            if not isinstance(v,dict): row_reasons.append("malformed_evidence_version"); continue
            if v.get("condition_signature") != sig: row_reasons.append("evidence_signature_mismatch")
            if v.get("version") != i: row_reasons.append("evidence_version_sequence_mismatch")
            payload={k:v.get(k) for k in ("condition_signature","version","prior_version","prior_evidence_hash","batch_fixture_market_hashes","batch_hits","batch_decided","cumulative_hits","cumulative_decided","wilson95_lower_raw","minimum_acceptable_odds_raw","activation_boundary_at")}
            if v.get("evidence_hash") != canonical_hash(payload): row_reasons.append("evidence_hash_drift")
            if i>1 and v.get("prior_evidence_hash") != versions[i-2].get("evidence_hash"): row_reasons.append("evidence_chain_broken")
        matched=[x for x in all_activity if _sig(x)==sig]
        statuses=Counter(str(x.get("status") or x.get("outcome") or "UNKNOWN") for x in matched)
        market=definition.get("market")
        current_matcher = bool(stage==T5 and path_terminal==T5)
        for reason in set(row_reasons): reasons[reason]+=1
        rows.append({
          "condition_number": persisted_number, "order_position": number, "signature": sig,
          "definition": definition, "definition_hash": canonical_hash(definition) if definition else None,
          "market": market, "path": definition.get("path"), "decision_stage": stage,
          "path_terminal_stage": path_terminal, "active_evidence_version": active.get("version"),
          "active_evidence_hash": active.get("evidence_hash"),
          "activation_boundary": active.get("activation_boundary_at"),
          "prospective_x20": {"hits": active.get("batch_hits",0), "decided": active.get("batch_decided",0), "target":20},
          "formal_rows": len(matched), "formal_status_counts": dict(sorted(statuses.items())),
          "current_matcher_can_structurally_admit": current_matcher,
          "safely_recoverable_missed_rows": 0,
          "recovery_status": "NOT_PROVEN_READ_ONLY_AUDIT",
          "rejection_reasons": sorted(set(row_reasons)), "valid": not row_reasons,
        })
    terminal=Counter(str(r.get("decision_stage") or "MISSING") for r in rows)
    out={"schema":"wilson-registry-manifest-v1","system":system,"namespace_schema_version":ns.get("schema_version"),
      "namespace_activation_at":ns.get("activation_at"),"condition_count":len(rows),
      "decision_stage_counts":dict(sorted(terminal.items())),"conditions":rows,
      "rejection_reasons":dict(sorted(reasons.items())),"valid":not reasons,
      "recovery":{"implemented":False,"mode":"audit-only","safely_recoverable_missed_rows":0,
       "reason":"No immutable non-T5 production condition and intended semantics have been independently proven; no historical rows are reconstructed."}}
    out["manifest_hash"]=canonical_hash(out)
    return out

def main() -> int:
    p=argparse.ArgumentParser(); p.add_argument("--system",required=True,choices=SYSTEMS); p.add_argument("--ledger",required=True,type=Path); p.add_argument("--output",type=Path); p.add_argument("--require-valid",action="store_true")
    a=p.parse_args(); before=a.ledger.read_bytes(); ledger=json.loads(before); result=build_manifest(ledger,a.system)
    if a.ledger.read_bytes()!=before: raise RuntimeError("ledger mutated during read-only audit")
    text=json.dumps(result,ensure_ascii=False,sort_keys=True,indent=2)+"\n"
    if a.output: a.output.write_text(text,encoding="utf-8")
    else: print(text,end="")
    return 2 if a.require_valid and not result["valid"] else 0
if __name__=="__main__": raise SystemExit(main())
