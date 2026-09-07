#!/usr/bin/env python3
"""V2 results sync — 方案 E.

Primary source: titan007 Over_YYYYMMDD.htm (via footbreak crown.titan.TitanClient).
Fallback: footbreak prediction_history.json + automatic_results.json.

Output: /var/www/stage_engine_v2/results.json
Schema: {"results": {match_id: {score, home, away, source, verified_at}},
         "updated_at_utc": "...", "stats": {...}}
"""
from __future__ import annotations

import json
import os
import sys
import tempfile
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

# Import footbreak modules
sys.path.insert(0, "/opt/footbreak")

from crown.config import settings as _settings
from crown.titan import TitanClient
from system.titan_results import (
    fetch_titan_result_rows,
    load_crown_titan_match_map,
)

HKT = timezone(timedelta(hours=8))
OUT_PATH = Path("/var/www/stage_engine_v2/results.json")
V2_DATA = Path("/var/www/stage_engine_v2/data.json")
CONFIG = _settings()
FB_HISTORY = CONFIG.state_dir / "prediction_history.json"
FB_AUTO_RESULTS = Path("/var/lib/footbreak/stage_engine_v2/automatic_results.json")


def _log(msg: str) -> None:
    print(f"[{datetime.now(timezone.utc).isoformat()}] {msg}", flush=True)


def _fetch_titan_results() -> tuple[dict[str, dict], int, list[str]]:
    """Fetch titan007 Over pages for today + yesterday + 2 days ago (HKT).

    Returns (results_by_titan_id, total_rows, dates_fetched).
    """
    now = datetime.now(HKT)
    dates = {
        (now - timedelta(days=d)).strftime("%Y%m%d")
        for d in range(0, 3)  # today, yesterday, day-before
    }
    _log(f"titan007 fetch dates: {sorted(dates)}")
    try:
        _client, rows = fetch_titan_result_rows(dates=dates)
    except Exception as e:
        _log(f"titan fetch ERROR: {type(e).__name__}: {e}")
        return {}, 0, sorted(dates)

    _log(f"titan007 returned {len(rows)} rows")
    out: dict[str, dict] = {}
    for row in rows:
        tid = str(row.get("id") or "").strip()
        hs = row.get("home_score")
        as_ = row.get("away_score")
        if not tid or hs is None or as_ is None:
            continue
        kickoff = row.get("kickoff")
        kickoff_iso = (
            kickoff.astimezone(timezone.utc).isoformat()
            if hasattr(kickoff, "isoformat")
            else None
        )
        out[tid] = {
            "score": f"{hs}-{as_}",
            "home_score": int(hs),
            "away_score": int(as_),
            "home": row.get("home") or "",
            "away": row.get("away") or "",
            "league": row.get("league") or "",
            "kickoff_utc": kickoff_iso,
            "source": "titan007_over",
            "verified_at": datetime.now(timezone.utc).isoformat(),
        }
    return out, len(rows), sorted(dates)


def _load_hkjc_to_titan_map() -> dict[str, str]:
    """HKJC match_id → titan_match_id (from footbreak prediction_history)."""
    try:
        m = load_crown_titan_match_map()
        _log(f"HKJC→titan map: {len(m)} entries")
        return m
    except Exception as e:
        _log(f"hkjc map ERROR: {type(e).__name__}: {e}")
        return {}


def _load_prediction_history_scores() -> dict[str, dict]:
    """Fallback: prediction_history.json rows[].score, keyed by match_id + titan_match_id."""
    if not FB_HISTORY.exists():
        _log(f"fb history missing: {FB_HISTORY}")
        return {}
    try:
        payload = json.loads(FB_HISTORY.read_text(encoding="utf-8"))
    except Exception as e:
        _log(f"fb history parse ERROR: {e}")
        return {}
    rows = payload.get("rows") or []
    out: dict[str, dict] = {}
    for r in rows:
        if not isinstance(r, dict):
            continue
        score = r.get("score")
        if not score:
            continue
        try:
            hs, as_ = str(score).split("-", 1)
            hs, as_ = int(hs.strip()), int(as_.strip())
        except (ValueError, AttributeError):
            continue
        payload_common = {
            "score": f"{hs}-{as_}",
            "home_score": hs,
            "away_score": as_,
            "home": r.get("home") or "",
            "away": r.get("away") or "",
            "league": r.get("league") or "",
            "kickoff_utc": r.get("kickoff"),
            "source": "footbreak_history",
            "verified_at": r.get("verified_at") or datetime.now(timezone.utc).isoformat(),
        }
        for key in (r.get("match_id"), r.get("titan_match_id"), r.get("hkjc_match_id")):
            if key:
                out[str(key)] = payload_common
    _log(f"fb history fallback: {len(out)} entries")
    return out


def _load_automatic_results() -> dict[str, dict]:
    """Fallback 2: automatic_results.json (stage_engine_v2 own settle)."""
    if not FB_AUTO_RESULTS.exists():
        return {}
    try:
        payload = json.loads(FB_AUTO_RESULTS.read_text(encoding="utf-8"))
    except Exception as e:
        _log(f"auto results parse ERROR: {e}")
        return {}
    rs = payload.get("results") or {}
    out: dict[str, dict] = {}
    for mid, v in rs.items():
        if not isinstance(v, dict):
            continue
        hs, as_ = v.get("home_score"), v.get("away_score")
        if hs is None or as_ is None:
            continue
        out[str(mid)] = {
            "score": f"{hs}-{as_}",
            "home_score": int(hs),
            "away_score": int(as_),
            "home": v.get("home") or "",
            "away": v.get("away") or "",
            "league": v.get("league") or "",
            "kickoff_utc": v.get("kickoff_utc"),
            "source": "automatic_results",
            "verified_at": v.get("verified_at_utc") or datetime.now(timezone.utc).isoformat(),
        }
    _log(f"automatic_results fallback: {len(out)} entries")
    return out


def _load_v2_fixtures() -> list[dict]:
    if not V2_DATA.exists():
        _log(f"v2 data.json missing: {V2_DATA}")
        return []
    try:
        d = json.loads(V2_DATA.read_text(encoding="utf-8"))
    except Exception as e:
        _log(f"v2 data.json parse ERROR: {e}")
        return []
    return d.get("fixtures") or []


def build_results() -> dict:
    started = time.time()

    # 1. Primary: titan007 Over pages
    titan_results, titan_rows, dates = _fetch_titan_results()

    # 2. HKJC → titan mapping (from prediction_history)
    hkjc_to_titan = _load_hkjc_to_titan_map()

    # 3. Fallbacks
    fb_history = _load_prediction_history_scores()
    fb_auto = _load_automatic_results()

    # 4. Load v2 fixtures
    fixtures = _load_v2_fixtures()

    # 5. For each fixture, resolve score
    results: dict[str, dict] = {}
    stats = {
        "total_fixtures": len(fixtures),
        "matched_titan_direct": 0,
        "matched_hkjc_via_map": 0,
        "matched_history_fallback": 0,
        "matched_auto_fallback": 0,
        "no_result": 0,
    }

    for fx in fixtures:
        fid = str(fx.get("id") or "").strip()
        src = fx.get("source") or ""
        if not fid:
            continue

        # 5a. titan source: direct join
        if src == "titan" and fid in titan_results:
            results[fid] = titan_results[fid]
            stats["matched_titan_direct"] += 1
            continue

        # 5b. hkjc source: via mapping
        if src == "hkjc" and fid in hkjc_to_titan:
            titan_id = hkjc_to_titan[fid]
            if titan_id in titan_results:
                r = dict(titan_results[titan_id])
                r["source"] = "titan007_over_via_hkjc_map"
                results[fid] = r
                stats["matched_hkjc_via_map"] += 1
                continue

        # 5c. Fallback: prediction_history by fixture id (may be titan or hkjc id)
        if fid in fb_history:
            results[fid] = fb_history[fid]
            stats["matched_history_fallback"] += 1
            continue

        # 5d. Fallback: automatic_results
        if fid in fb_auto:
            results[fid] = fb_auto[fid]
            stats["matched_auto_fallback"] += 1
            continue

        stats["no_result"] += 1

    payload = {
        "results": results,
        "updated_at_utc": datetime.now(timezone.utc).isoformat(),
        "stats": stats,
        "meta": {
            "titan_dates_fetched": dates,
            "titan_rows_seen": titan_rows,
            "titan_results_indexed": len(titan_results),
            "hkjc_to_titan_map_size": len(hkjc_to_titan),
            "history_fallback_size": len(fb_history),
            "auto_fallback_size": len(fb_auto),
            "build_elapsed_s": round(time.time() - started, 2),
        },
    }
    return payload


def atomic_write(path: Path, data: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(
        prefix=".results.", suffix=".tmp", dir=str(path.parent)
    )
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, separators=(",", ":"))
        os.chmod(tmp, 0o644)
        os.replace(tmp, path)
    finally:
        if os.path.exists(tmp):
            os.unlink(tmp)


def main() -> int:
    payload = build_results()
    atomic_write(OUT_PATH, payload)
    stats = payload["stats"]
    meta = payload["meta"]
    _log(
        f"wrote {OUT_PATH} results={len(payload['results'])} "
        f"titan={stats['matched_titan_direct']} "
        f"hkjc_map={stats['matched_hkjc_via_map']} "
        f"history_fb={stats['matched_history_fallback']} "
        f"auto_fb={stats['matched_auto_fallback']} "
        f"no_result={stats['no_result']} / total={stats['total_fixtures']} "
        f"elapsed={meta['build_elapsed_s']}s"
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
