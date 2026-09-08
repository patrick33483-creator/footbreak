#!/usr/bin/env python3
"""V4 merge — every 5 min:

1. fetch crown-radar live + history
2. for each live match, determine current stage
3. if the stage checkpoint is not locked, generate predictions using current snap and LOCK it
4. write matches.json (list view for UI)
5. write results.json (append newly-completed matches with their locked predictions)

Cold start: no backtest. All predictions accumulate from now.
"""
import sys
sys.path.insert(0, "/opt/crownsystem-v4")

import json
import datetime as dt
from pathlib import Path

from v4_common import (
    HKT, fetch_matches, fetch_history, now_ms, now_hkt, kickoff_hkt,
    stage_for, load_checkpoints, save_checkpoints, load_results_history,
    V4_DIR, V4_MATCHES, V4_RESULTS, V4_META,
)
from v4_model_p1 import predict_market_bundle


STAGES = ["INITIAL", "T30", "T5"]

def stage_snap_key_for(stage):
    return {"INITIAL": ("initial_AH", "initial_OU"),
            "T30": ("T30_AH", "T30_OU"),
            "T5": ("T5_AH", "T5_OU")}[stage]


def snapshot_snapshot(snaps, stage):
    """Snap only the two keys relevant to a stage into a small dict."""
    ah_k, ou_k = stage_snap_key_for(stage)
    return {ah_k: snaps.get(ah_k), ou_k: snaps.get(ou_k)}


def maybe_lock_stage(cp_match, stage, snapshots):
    """If stage not yet locked and current snap is available, lock it now."""
    if stage in cp_match:
        return False
    ah_k, ou_k = stage_snap_key_for(stage)
    ah = snapshots.get(ah_k)
    ou = snapshots.get(ou_k)
    if not ah and not ou:
        return False  # nothing to lock yet
    bundle = predict_market_bundle(snapshots, stage)
    cp_match[stage] = {
        "locked_at_ms": now_ms(),
        "locked_at_hkt": now_hkt().strftime("%Y-%m-%d %H:%M:%S"),
        "snap": snapshot_snapshot(snapshots, stage),
        "predictions": bundle,
    }
    return True


def project_row(match, cp_match, current_stage):
    """Build one UI row for matches.json."""
    sid = match["sid"]
    ko_ms = match["kickoff_utc"]
    ko = kickoff_hkt(ko_ms).strftime("%Y-%m-%d %H:%M")
    row = {
        "sid": sid,
        "kickoff": ko,
        "kickoff_ms": ko_ms,
        "league": match.get("league"),
        "home": match.get("home"),
        "away": match.get("away"),
        "current_stage": current_stage,
        "stages": {},
    }
    for st in STAGES:
        info = cp_match.get(st)
        if info:
            row["stages"][st] = {
                "locked_at": info.get("locked_at_hkt"),
                "predictions": info.get("predictions"),
                "snap": info.get("snap"),
                "status": "已鎖",
            }
        else:
            if current_stage == "FUTURE" and STAGES.index(st) > 0:
                row["stages"][st] = {"status": "等候中"}
            elif current_stage in STAGES and STAGES.index(st) > STAGES.index(current_stage):
                row["stages"][st] = {"status": "等候中"}
            elif current_stage in ("LIVE", "POST"):
                row["stages"][st] = {"status": "已錯過"}
            else:
                row["stages"][st] = {"status": "等候中"}
    return row


def result_row(match, cp_match):
    """When a match reaches POST/completed, freeze the row for results.json."""
    ko_ms = match["kickoff_utc"]
    ko = kickoff_hkt(ko_ms).strftime("%Y-%m-%d %H:%M")
    return {
        "sid": match["sid"],
        "kickoff": ko,
        "kickoff_ms": ko_ms,
        "league": match.get("league"),
        "home": match.get("home"),
        "away": match.get("away"),
        "stages": {st: cp_match.get(st, {"status": "未觸發"}) for st in STAGES},
        "result": match.get("result"),
        "archived_at": now_hkt().strftime("%Y-%m-%d %H:%M:%S"),
    }


def main():
    V4_DIR.mkdir(parents=True, exist_ok=True)

    cp = load_checkpoints()  # {sid: {INITIAL:{...}, T30:{...}, T5:{...}}}

    live = fetch_matches()
    hist = fetch_history()
    hist_by_sid = {m["sid"]: m for m in hist if isinstance(m, dict)}

    now = now_ms()
    ui_rows = []
    stats = {"locked_this_tick": 0, "waiting": 0, "total_live": len(live)}

    for m in live:
        sid = m["sid"]
        stage = stage_for(m["kickoff_utc"], now)
        cp.setdefault(sid, {})
        # Lock stages as they become due. Also lock stages the match already
        # passed (best-effort fallback: use current live snap if we missed it).
        for st in STAGES:
            if st == stage or (STAGES.index(st) < STAGES.index(stage) if stage in STAGES else False):
                changed = maybe_lock_stage(cp[sid], st, m.get("snapshots", {}))
                if changed:
                    stats["locked_this_tick"] += 1
        if stage in STAGES and not cp[sid].get(stage):
            stats["waiting"] += 1
        ui_rows.append(project_row(m, cp[sid], stage))

    # Detect newly completed: history contains a match not already in results file.
    # Cold start rule: only record matches where V4 actually locked >=1 stage.
    results = load_results_history()
    known = {r["sid"] for r in results}
    for sid, hm in hist_by_sid.items():
        if sid in known:
            continue
        r = hm.get("result") or {}
        if r.get("status") not in ("完", "完場"):
            continue
        cp_m = cp.get(sid, {})
        if not any(st in cp_m for st in STAGES):
            continue  # V4 didn't lock anything for this match — skip (cold start)
        results.append(result_row(hm, cp_m))

    # Sort UI: soonest kickoff first
    ui_rows.sort(key=lambda r: r["kickoff_ms"])

    # Write outputs
    meta = {
        "generated_at": now_hkt().strftime("%Y-%m-%d %H:%M:%S"),
        "generated_at_ms": now,
        "version": "v4-p1-mvp",
        "stats": stats,
        "total_live": len(live),
        "total_completed_recorded": len(results),
    }
    payload = {"meta": meta, "matches": ui_rows}
    V4_MATCHES.write_text(json.dumps(payload, ensure_ascii=False, indent=2))
    V4_RESULTS.write_text(json.dumps(results, ensure_ascii=False, indent=2))
    V4_META.parent.mkdir(parents=True, exist_ok=True)
    V4_META.write_text(json.dumps(meta, ensure_ascii=False, indent=2))
    save_checkpoints(cp)
    print(json.dumps(meta, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
