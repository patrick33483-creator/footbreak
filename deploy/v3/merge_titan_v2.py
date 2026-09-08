#!/usr/bin/env python3
"""crownsystem-v3 · Crown 皇冠盤場 + V2 三階段預測

Read:
  /var/www/crown/data.json  (Crown scraper + V2 predict 已寫入嘅完整 137 場)

Write:
  /var/www/crownsystem-v3/matches.json
  [{sid, kickoff_display, kickoff_utc, league, home, away, first, t30, t5}, ...]

Filter: 只保留未開波 (kickoff_utc > now).
"""
from __future__ import annotations
import json
import os
import tempfile
from datetime import datetime, timezone, timedelta

CROWN = "/var/www/crown/data.json"
OUT = "/var/www/crownsystem-v3/matches.json"

HKT = timezone(timedelta(hours=8))


def load_json(path: str, default):
    try:
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    except (OSError, json.JSONDecodeError):
        return default


def parse_ko(match: dict) -> datetime | None:
    """Parse kickoff to UTC datetime. Crown match uses kickoff_hkt ISO."""
    for k in ("kickoff_utc", "kickoff_hkt", "kickoff"):
        v = match.get(k)
        if not v:
            continue
        try:
            dt = datetime.fromisoformat(str(v).replace(" ", "T"))
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=HKT)
            return dt.astimezone(timezone.utc)
        except ValueError:
            continue
    return None


def _stage_summary(match: dict, stage_name: str) -> str:
    """Return a display string for the requested stage.

    Crown data.json has:
      - match['stages'] (list of stage rows), each with 'stage' and 'market_predictions'
      - match['lead_view'] (top-level lead if stage matches current)
      - match['pick'] / match['forecast'] fields

    We try, in priority: stages[stage_name].market_predictions[0] lead label + odds.
    """
    stages = match.get("stages")
    if isinstance(stages, list):
        for row in stages:
            if not isinstance(row, dict):
                continue
            if str(row.get("stage") or "") != stage_name:
                continue
            preds = row.get("market_predictions")
            if isinstance(preds, list) and preds:
                p = preds[0] if isinstance(preds[0], dict) else {}
                label = p.get("label") or p.get("selection") or ""
                odds = p.get("odds") or p.get("decimal_odds")
                if label and odds:
                    return f"{label} @ {odds}"
                if label:
                    return str(label)
            # if stage exists but no prediction, mark waiting
            status = row.get("status") or row.get("source_status")
            if status:
                return f"({status})"
            return "(等候中)"
    return "-"


def main() -> int:
    crown = load_json(CROWN, {})
    matches_raw = crown.get("matches") or []

    now_utc = datetime.now(timezone.utc)
    out_matches = []
    for m in matches_raw:
        if not isinstance(m, dict):
            continue
        ko = parse_ko(m)
        if ko is None:
            continue
        if ko <= now_utc:
            continue  # skip past
        sid = str(
            m.get("titan_match_id")
            or m.get("match_id")
            or m.get("hkjc_match_id")
            or m.get("id")
            or ""
        ).strip()
        ko_hkt = ko.astimezone(HKT)
        out_matches.append({
            "sid": sid,
            "kickoff_utc_ms": int(ko.timestamp() * 1000),
            "kickoff_display": f"{ko_hkt.month}-{ko_hkt.day} {ko_hkt.strftime('%H:%M')}",
            "league": str(m.get("league") or "").strip(),
            "home": str(m.get("home") or "").strip(),
            "away": str(m.get("away") or "").strip(),
            "first": _stage_summary(m, "首預"),
            "t30": _stage_summary(m, "T-30"),
            "t5": _stage_summary(m, "T-5"),
        })

    out_matches.sort(key=lambda x: x["kickoff_utc_ms"])

    payload = {
        "generated_utc": datetime.now(timezone.utc).isoformat(),
        "count": len(out_matches),
        "matches": out_matches,
    }

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
