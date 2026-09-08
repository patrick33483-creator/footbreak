#!/usr/bin/env python3
"""V4 merge writing to V3 schema (V3 URL 顯 V4 資料).

- Reads crown-radar loopback for live + history.
- Locks three stages (INITIAL/T30/T5) per match on first sighting; snap frozen.
- Writes /var/www/crownsystem-v3/matches.json in V3 schema.
- Writes /var/www/crownsystem-v3/results.json cold start (only records matches
  where V4 locked >= 1 stage AND completed).
- No alpha, no model. Each stage cell = one-line free text pick from that stage's
  snap (highest decimal odds >= 1.7 across AH+OU sides), or "(等候中)"/"(未觸發)".
"""
import sys, json
from pathlib import Path
sys.path.insert(0, "/opt/crownsystem-v4")

from v4_common import (
    HKT, fetch_matches, fetch_history, now_ms, now_hkt, kickoff_hkt,
    stage_for, load_checkpoints, save_checkpoints, hk_to_decimal,
)

V3_DIR = Path("/var/www/crownsystem-v3")
V3_MATCHES = V3_DIR / "matches.json"
V3_RESULTS = V3_DIR / "results.json"

STAGES = ["INITIAL", "T30", "T5"]
STAGE_KEYS = {"INITIAL": ("initial_AH", "initial_OU"),
              "T30": ("T30_AH", "T30_OU"),
              "T5": ("T5_AH", "T5_OU")}

STAGE_LABEL = {"INITIAL": "初盤", "T30": "T-30", "T5": "T-5"}


def pick_line(stage, snap_dict):
    """Return one-line text like '皇冠初盤讓球 主 +1.5 @ 0.8' from a locked snap.

    Prefer the side whose decimal odds is highest among AH home/away + OU home/away.
    Requires decimal >= 1.7. Falls back to any valid side. If no valid, returns None.
    """
    ah_k, ou_k = STAGE_KEYS[stage]
    ah = snap_dict.get(ah_k)
    ou = snap_dict.get(ou_k)
    stage_label = STAGE_LABEL[stage]

    candidates = []  # (decimal, market_label, side_label, line_str, hk_str)
    if ah:
        h_line = str(ah.get("h", ""))
        # AH line sign: crown "h" already reflects home line; away is opposite.
        # Home line: positive means home receives; but from user's V3 samples it
        # just shows the number with sign, e.g. "+1.5" or "-0.5".
        home_line = h_line if h_line.startswith(("-", "+")) or h_line == "0" else "+" + h_line
        # Away line = numerically negated:
        try:
            away_line_num = -float(h_line)
            away_line = f"{'+' if away_line_num >= 0 else ''}{away_line_num:g}"
        except Exception:
            away_line = h_line
        for side, hk, line in [("主", ah.get("home"), home_line),
                                ("客", ah.get("away"), away_line)]:
            dec = hk_to_decimal(hk)
            if dec:
                candidates.append((dec, "讓球", side, line, str(hk)))
    if ou:
        o_line = str(ou.get("h", ""))
        for side, hk in [("大", ou.get("home")), ("細", ou.get("away"))]:
            dec = hk_to_decimal(hk)
            if dec:
                candidates.append((dec, "入球大細", side, o_line, str(hk)))
    if not candidates:
        return None
    # Prefer decimal >= 1.7 with highest decimal; else highest decimal overall.
    over = [c for c in candidates if c[0] >= 1.7]
    pool = over if over else candidates
    pool.sort(key=lambda c: -c[0])
    dec, mkt, side, line, hk = pool[0]
    return f"皇冠{stage_label}{mkt} {side} {line} @ {hk}"


def stage_snap_for(snapshots, stage):
    ah_k, ou_k = STAGE_KEYS[stage]
    return {ah_k: snapshots.get(ah_k), ou_k: snapshots.get(ou_k)}


def maybe_lock(cp_match, stage, snapshots):
    if stage in cp_match:
        return False
    ah_k, ou_k = STAGE_KEYS[stage]
    if not snapshots.get(ah_k) and not snapshots.get(ou_k):
        return False
    cp_match[stage] = {
        "locked_at_ms": now_ms(),
        "locked_at_hkt": now_hkt().strftime("%Y-%m-%d %H:%M:%S"),
        "snap": stage_snap_for(snapshots, stage),
    }
    return True


def cell_text(stage, current_stage, cp_match):
    if stage in cp_match:
        line = pick_line(stage, cp_match[stage]["snap"])
        return line if line else "(冇合資格盤)"
    if current_stage == "FUTURE":
        return "(等候中)"
    if current_stage in STAGES:
        # current or earlier?
        if STAGES.index(stage) > STAGES.index(current_stage):
            return "(等候中)"
        else:
            return "(未觸發)"
    # LIVE/POST
    return "(未觸發)"


def build_row(m, cp_match, current_stage):
    ko_ms = m["kickoff_utc"]
    ko_disp = kickoff_hkt(ko_ms).strftime("%-m-%-d %H:%M")
    return {
        "sid": m["sid"],
        "kickoff_utc_ms": ko_ms,
        "kickoff_display": ko_disp,
        "league": m.get("league"),
        "home": m.get("home"),
        "away": m.get("away"),
        "first": cell_text("INITIAL", current_stage, cp_match),
        "t30": cell_text("T30", current_stage, cp_match),
        "t5": cell_text("T5", current_stage, cp_match),
    }


def build_result_row(hm, cp_match):
    ko_ms = hm["kickoff_utc"]
    ko_disp = kickoff_hkt(ko_ms).strftime("%-m-%-d %H:%M")
    r = hm.get("result") or {}
    return {
        "sid": hm["sid"],
        "kickoff_utc_ms": ko_ms,
        "kickoff_display": ko_disp,
        "league": hm.get("league"),
        "home": hm.get("home"),
        "away": hm.get("away"),
        "first": cell_text("INITIAL", "POST", cp_match),
        "t30": cell_text("T30", "POST", cp_match),
        "t5": cell_text("T5", "POST", cp_match),
        "score": f"{r.get('home_score')}-{r.get('away_score')}"
                 if r.get("home_score") is not None else None,
        "ht_score": f"{r.get('ht_home_score')}-{r.get('ht_away_score')}"
                    if r.get("ht_home_score") is not None else None,
        "archived_at": now_hkt().strftime("%Y-%m-%d %H:%M:%S"),
    }


def main():
    V3_DIR.mkdir(parents=True, exist_ok=True)
    cp = load_checkpoints()  # per-sid stage lock

    live = fetch_matches()
    hist = fetch_history()
    hist_by_sid = {m["sid"]: m for m in hist if isinstance(m, dict)}

    now = now_ms()
    stats = {"locked_this_tick": 0, "total_live": len(live)}
    rows = []
    for m in live:
        sid = m["sid"]
        stage = stage_for(m["kickoff_utc"], now)
        cp.setdefault(sid, {})
        # Try to lock stages up to and including the current one
        for st in STAGES:
            allowed = (st == stage) or (stage in STAGES and STAGES.index(st) <= STAGES.index(stage))
            if allowed:
                if maybe_lock(cp[sid], st, m.get("snapshots", {})):
                    stats["locked_this_tick"] += 1
        rows.append(build_row(m, cp[sid], stage))

    # Cold-start results: only for matches V4 locked >= 1 stage
    try:
        prev = json.load(open(V3_RESULTS))
        prev_matches = prev if isinstance(prev, list) else prev.get("matches", [])
    except Exception:
        prev_matches = []
    known = {r["sid"] for r in prev_matches}
    for sid, hm in hist_by_sid.items():
        if sid in known:
            continue
        r = hm.get("result") or {}
        if r.get("status") not in ("完", "完場"):
            continue
        cp_m = cp.get(sid, {})
        if not any(st in cp_m for st in STAGES):
            continue
        prev_matches.append(build_result_row(hm, cp_m))

    rows.sort(key=lambda r: r["kickoff_utc_ms"])
    prev_matches.sort(key=lambda r: -r["kickoff_utc_ms"])

    matches_out = {
        "generated_utc": now_hkt().astimezone().isoformat(),
        "count": len(rows),
        "source": "crown-radar-loopback (v4-logic)",
        "matches": rows,
    }
    results_out = {
        "generated_utc": now_hkt().astimezone().isoformat(),
        "count": len(prev_matches),
        "source": "crown-radar-loopback (v4-logic, cold start)",
        "matches": prev_matches,
    }
    V3_MATCHES.write_text(json.dumps(matches_out, ensure_ascii=False, indent=2))
    V3_RESULTS.write_text(json.dumps(results_out, ensure_ascii=False, indent=2))
    save_checkpoints(cp)

    print(json.dumps({
        "generated": matches_out["generated_utc"],
        "live": len(rows),
        "locked_this_tick": stats["locked_this_tick"],
        "completed_recorded": len(prev_matches),
    }, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
