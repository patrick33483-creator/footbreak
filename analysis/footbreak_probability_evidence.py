"""Build a bounded, auditable Footbreak probability-evidence artifact.

The artifact is derived from formally graded raw stage history, never from a
ranking aggregate.  It deliberately contains no betting/actionability fields.
"""
from __future__ import annotations
import hashlib, itertools, json, math
from collections import Counter, defaultdict
from datetime import datetime, timezone
from typing import Any, Iterable
from analysis.granular_conditions import (
    MARKETS, STAGES, STAGE_ORDER, _line_bucket, _relative_direction, _role,
    _selected_line, _tier, _time,
)

SCHEMA_VERSION = 2
SYSTEM = "footbreak"
MAX_ROWS = 50_000
REQUIRED = ("system", "stage", "market", "path", "odds_tier", "direction", "role", "line_bucket")
REASONS = (
    "not_native_t5", "post_hoc_or_backfill_or_excluded", "missing_fixture_or_kickoff",
    "invalid_stage_timestamp", "invalid_quote_provenance", "quote_not_pre_kickoff",
    "invalid_market_side_line_or_odds", "not_formally_graded", "undecidable_or_push",
    "missing_axis", "result_recorded_after_source_boundary",
)


def _num(value: Any) -> float | None:
    try: value = float(value)
    except (TypeError, ValueError): return None
    return value if math.isfinite(value) else None


def _stamp(value: Any) -> datetime | None: return _time(value)

def _iso(value: datetime) -> str: return value.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")

def _digest(value: Any) -> str:
    return hashlib.sha256(json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), default=str).encode()).hexdigest()

def _valid_side(market: str, side: Any) -> str | None:
    value = str(side or "").upper()
    return value if value in ({"H", "A"} if market == "HDC" else {"H", "L"}) else None

def _outcome(grade: dict[str, Any]) -> int | None:
    # The legacy grade stores boolean hit; push/refund has None. Half states
    # remain excluded from binary hit/decided evidence rather than guessed.
    hit = grade.get("hit")
    if hit is True: return 1
    if hit is False: return 0
    return None

def _stage_quote(stage: dict[str, Any], grade: dict[str, Any], market: str) -> tuple[dict[str, Any] | None, str | None]:
    candidates = [item for item in stage.get("market_grades") or [] if isinstance(item, dict) and str(item.get("code") or "").upper() == market]
    # A historical stage must have exactly one formally scored selection per market.
    if len(candidates) != 1: return None, "invalid_quote_provenance"
    selected = candidates[0]
    if selected is not grade and selected != grade: return None, "invalid_quote_provenance"
    return selected, None

def _valid_stage(stage: dict[str, Any], match: dict[str, Any], *, terminal: bool) -> tuple[dict[str, Any] | None, str | None]:
    if stage.get("post_hoc_backfill") or stage.get("post_hoc") or stage.get("backfill") or stage.get("exclude_from_simulation"):
        return None, "post_hoc_or_backfill_or_excluded"
    kickoff = _stamp(match.get("kickoff") or match.get("kickoff_hkt")); stage_at = _stamp(stage.get("predicted_at") or stage.get("ts") or stage.get("source_snapshot_at"))
    if not str(match.get("match_id") or "").strip() or kickoff is None: return None, "missing_fixture_or_kickoff"
    if stage_at is None: return None, "invalid_stage_timestamp"
    if stage_at >= kickoff: return None, "invalid_stage_timestamp"
    if terminal and str(stage.get("stage") or "") != "T-5": return None, "not_native_t5"
    output=[]
    for grade in stage.get("market_grades") or []:
        if not isinstance(grade, dict): continue
        market=str(grade.get("code") or "").upper()
        if market not in MARKETS: continue
        if grade.get("grade_status") != "GRADED": return None, "not_formally_graded" if terminal else "invalid_quote_provenance"
        side=_valid_side(market, grade.get("side")); raw_line=_num(grade.get("line",grade.get("condition"))); odds=_num(grade.get("odds"))
        source=str(grade.get("quote_source") or grade.get("source") or "").strip()
        observed=_stamp(grade.get("observed_at"))
        if not source or source.lower() in {"fallback","none","model_only","model-only","unavailable"}: return None, "invalid_quote_provenance"
        if observed is None: return None, "invalid_quote_provenance"
        if observed >= kickoff: return None, "quote_not_pre_kickoff"
        if side is None or raw_line is None or odds is None or odds <= 1: return None, "invalid_market_side_line_or_odds"
        output.append({"market":market,"side":side,"raw_line":raw_line,"selected_line":_selected_line(market,side,raw_line),"odds":odds,"stage":str(stage.get("stage")),"stage_at":stage_at,"grade":grade})
    return {"kickoff":kickoff,"stage_at":stage_at,"markets":output}, None

def build(matches: Iterable[dict[str, Any]], *, generated_at: str, source_boundary_at: str | None = None, max_rows: int = MAX_ROWS) -> dict[str, Any]:
    """Build deterministically; rejected raw rows appear only in bounded counts."""
    generated=_stamp(generated_at)
    if generated is None: raise ValueError("generated_at must be a valid timestamp")
    boundary=_stamp(source_boundary_at) or generated
    diagnostics=Counter(); entries=[]
    for match in sorted((m for m in matches if isinstance(m,dict)), key=lambda m: str(m.get("match_id") or "")):
        by_market: dict[str, dict[str, dict[str, Any]]] = defaultdict(dict)
        terminal_by_market: dict[str, dict[str, Any]] = {}
        for stage in sorted((s for s in match.get("stages") or [] if isinstance(s,dict)), key=lambda s: STAGE_ORDER.get(str(s.get("stage")),99)):
            parsed, reason=_valid_stage(stage,match,terminal=str(stage.get("stage"))=="T-5")
            if reason:
                diagnostics[reason]+=1; continue
            assert parsed is not None
            for item in parsed["markets"]:
                by_market[item["market"]][item["stage"]]=item | {"kickoff":parsed["kickoff"]}
                if item["stage"]=="T-5": terminal_by_market[item["market"]]=item | {"kickoff":parsed["kickoff"]}
        for market, terminal in terminal_by_market.items():
            outcome=_outcome(terminal["grade"])
            if outcome is None:
                diagnostics["undecidable_or_push"]+=1; continue
            decided_at=_stamp(terminal["grade"].get("result_recorded_at") or terminal["grade"].get("settled_at") or generated_at)
            if decided_at is None or decided_at > boundary:
                diagnostics["result_recorded_after_source_boundary"]+=1; continue
            available=[by_market[market][name] for name in STAGES if name in by_market[market] and STAGE_ORDER[name] <= STAGE_ORDER["T-5"]]
            # Keep exactly the same stage-combination semantics as
            # granular_conditions._paths(): every available combination whose
            # terminal stage is T-5, not merely suffixes.  For three stages
            # this is T-5, 首預→T-5, T-30→T-5 and 首預→T-30→T-5.
            for size in range(1, len(available) + 1):
                for path in itertools.combinations(available, size):
                    if path[-1]["stage"]!="T-5": continue
                    direction=_relative_direction(market,(item["side"] for item in path)); role=_role(market,terminal["side"],terminal["raw_line"]); bucket=_line_bucket(market,terminal["selected_line"]); tier=_tier(terminal["odds"])
                    axis={"system":SYSTEM,"stage":"T-5","market":market,"path":"→".join(item["stage"] for item in path),"odds_tier":tier,"direction":direction,"role":role,"line_bucket":bucket,"league":str(match.get("league") or "").strip() or None}
                    if any(axis.get(key) in (None,"") for key in REQUIRED): diagnostics["missing_axis"]+=1; continue
                    # Stable but non-reversible browser-safe identity: used only
                    # for cohort de-duplication and never emitted to Dashboard.
                    fixture_market_key=_digest([SYSTEM,str(match.get("match_id")),market])[:32]
                    entry={**axis,"fixture_market_key":fixture_market_key,"outcome":"Won" if outcome else "Lost","decided_at":_iso(decided_at),"kickoff":_iso(terminal["kickoff"]),"native_stage_at":_iso(terminal["stage_at"]),"quote_observed_at":_iso(_stamp(terminal["grade"].get("observed_at"))),"quote_source":str(terminal["grade"].get("quote_source") or terminal["grade"].get("source")),"odds":round(terminal["odds"],8)}
                    # Include fixture-market identity to avoid a collision when
                    # different fixtures happen to share timestamps and axes.
                    entry["evidence_id"]=_digest(entry)[:32]; entries.append(entry)
    entries.sort(key=lambda row:(row["decided_at"],row["evidence_id"]))
    if len(entries)>max_rows:
        diagnostics["bounded_rows_dropped"]=len(entries)-max_rows; entries=entries[-max_rows:]
    coverage={"accepted_rows":len(entries),"by_market":dict(sorted(Counter(row["market"] for row in entries).items())),"by_path":dict(sorted(Counter(row["path"] for row in entries).items())),"excluded":{key:int(diagnostics[key]) for key in (*REASONS,"bounded_rows_dropped") if diagnostics.get(key)}}
    entries_hash=_digest(entries)
    return {"schema_version":SCHEMA_VERSION,"system":SYSTEM,"generated_at":_iso(generated),"source_boundary_at":_iso(boundary),"source":"formally_graded_accuracy_history_raw_stages","max_rows":max_rows,"entries_sha256":entries_hash,"coverage":coverage,"entries":entries}
