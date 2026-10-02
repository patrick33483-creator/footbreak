"""Presentation-only qualification; never changes accounting, locks, or sends."""
from dynamic_rules import matches
from policy import GATE_VERSION,eligible_price

VERSION="settled-or-current-qualified-v1"

def context(registry,gate_by_rule,now):
    ready=bool(registry and registry.get("gate_version")==GATE_VERSION
               and 0<=now-registry["updated_at"]<=4*3600000)
    rules=[r for r in (registry or {}).get("strategies",[])
           if r.get("active") and gate_by_rule.get(r["id"],{}).get("pass")]
    return ready,rules

def classify(payload,result,ready,rules):
    def answer(keep,reason,ids=None):
        return {"display_keep":keep,"display_reason":reason,"qualifying_rules":ids or []}
    if result is not None:
        return answer(True,"settled")
    if not ready:
        return answer(False,"qualification_unavailable")
    if not eligible_price(payload):
        return answer(False,"price_not_eligible")
    if not isinstance(payload.get("features"),dict):
        return answer(False,"saved_features_missing")
    ids=[]
    for r in rules:
        if (payload.get("market"),payload.get("side"))!=(r["market"],r["side"]):
            continue
        try:
            if matches(payload,r["clauses"]):
                ids.append(r["id"])
        except (KeyError,TypeError,ValueError):
            continue
    return answer(bool(ids),"eligible" if ids else "no_qualifying_strategy",sorted(ids))

def summary(ledger,ready):
    return {"version":VERSION,"qualification_ready":ready,"scope":"latest_100_records",
            "total":len(ledger),"completed":sum(r["result"] is not None for r in ledger),
            "qualified_pending":sum(r["display_reason"]=="eligible" for r in ledger),
            "hidden":sum(not r["display_keep"] for r in ledger)}
