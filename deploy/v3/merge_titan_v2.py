#!/usr/bin/env python3
"""crownsystem-v3 · Titan upcoming + V2 predict merge

Read:
  /var/www/stage_engine_v2/titan_today.json   (Titan 今日皇冠場列表)
  /var/www/stage_engine_v2/data.json          (V2 預測結果, join by sid)

Write:
  /var/www/crownsystem-v3/matches.json
  [{sid, kickoff, league, home, away, first, t30, t5}, ...]

Filter: 只保留 score == "-" (未開波) 嘅場.
"""
from __future__ import annotations
import json
import os
import tempfile
from datetime import datetime, timezone

TITAN = "/var/www/stage_engine_v2/titan_today.json"
V2 = "/var/www/stage_engine_v2/data.json"
OUT = "/var/www/crownsystem-v3/matches.json"


def load_json(path: str, default):
    try:
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    except (OSError, json.JSONDecodeError):
        return default


def build_v2_lookup(v2_data: dict) -> dict[str, dict]:
    lookup: dict[str, dict] = {}
    for f in v2_data.get("fixtures", []) or []:
        sid = str(f.get("id") or "").strip()
        if not sid:
            continue
        stages = f.get("stages") or {}
        # Stage keys are Chinese: 首預 / T-30 / T-5
        lookup[sid] = {
            "first": (stages.get("首預") or {}).get("summary") or "-",
            "t30": (stages.get("T-30") or {}).get("summary") or "-",
            "t5": (stages.get("T-5") or {}).get("summary") or "-",
        }
    return lookup


def main() -> int:
    titan = load_json(TITAN, {})
    v2 = load_json(V2, {})
    v2_lookup = build_v2_lookup(v2)

    matches_raw = titan.get("matches") or titan.get("fixtures") or titan
    if not isinstance(matches_raw, list):
        matches_raw = []

    out_matches = []
    for m in matches_raw:
        score = str(m.get("score", "")).strip()
        # 只要未開波
        if score not in ("", "-"):
            continue
        sid = str(m.get("sid") or "").strip()
        v2p = v2_lookup.get(sid, {"first": "-", "t30": "-", "t5": "-"})
        out_matches.append({
            "sid": sid,
            "kickoff": str(m.get("kickoff", "")).strip(),
            "league": str(m.get("league", "")).strip(),
            "home": str(m.get("home", "")).strip(),
            "away": str(m.get("away", "")).strip(),
            "first": v2p["first"],
            "t30": v2p["t30"],
            "t5": v2p["t5"],
        })

    # sort by kickoff HH:MM
    def sort_key(m):
        k = m.get("kickoff", "")
        try:
            hh, mm = k.split(":")
            return int(hh) * 60 + int(mm)
        except Exception:
            return 9999
    out_matches.sort(key=sort_key)

    payload = {
        "generated_utc": datetime.now(timezone.utc).isoformat(),
        "count": len(out_matches),
        "matches": out_matches,
    }

    # atomic write
    os.makedirs(os.path.dirname(OUT), exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=os.path.dirname(OUT), prefix=".matches.", suffix=".json")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(payload, f, ensure_ascii=False, indent=2)
        os.chmod(tmp, 0o644)
        os.replace(tmp, OUT)
    except Exception:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise

    print(f"[merge_titan_v2] wrote {len(out_matches)} matches to {OUT}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
