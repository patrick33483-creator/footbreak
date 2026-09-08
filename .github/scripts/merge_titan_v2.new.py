#!/usr/bin/env python3
"""crownsystem-v3 · merge_titan_v2.py (crown-radar backed)

Read:
  http://127.0.0.1/crown-radar/api/matches-crown (basic auth radar:toberich)

Compute:
  V2 crown.opening_model.apply_opening_model per fixture.
  Emits initial / T-30 / T-5 forecasts by re-running the model against snapshots
  synthesised from crown-radar snapshots.initial_AH/T30_AH/live_AH triples.

Write:
  /var/www/crownsystem-v3/matches.json
  [{sid, kickoff_display, kickoff_utc_ms, league, home, away, first, t30, t5}, ...]
"""
from __future__ import annotations
import base64
import json
import os
import sys
import tempfile
import urllib.request
from datetime import datetime, timezone, timedelta

sys.path.insert(0, "/opt/footbreak")
from crown.opening_model import apply_opening_model  # type: ignore

FEED = "http://127.0.0.1/crown-radar/api/matches-crown"
AUTH = base64.b64encode(b"radar:toberich").decode()
OUT = "/var/www/crownsystem-v3/matches.json"
HKT = timezone(timedelta(hours=8))


def fetch_feed():
    req = urllib.request.Request(FEED)
    req.add_header("Authorization", f"Basic {AUTH}")
    with urllib.request.urlopen(req, timeout=30) as r:
        return json.load(r)


def _stage_prices(m: dict, stage: str) -> list[dict]:
    """Return V2-shaped prices for one stage only, so opening_cutoff aligns.

    stage in {"first","t30","t5"} → uses initial_AH/OU, T30_AH/OU, live_AH/OU.
    """
    snaps = m.get("snapshots") or {}
    ko_ms = m.get("kickoff_utc") or 0
    key_map = {
        "first": ("initial_AH", "initial_OU", -3600 * 3),
        "t30":   ("T30_AH", "T30_OU", -1800),
        "t5":    ("live_AH", "live_OU", -300),
    }
    ah_key, ou_key, offset = key_map[stage]
    source_at = datetime.fromtimestamp((ko_ms + offset * 1000) / 1000, timezone.utc).isoformat()
    rows = []
    for snap_key, code, side_map in (
        (ah_key, "HDC", {"home": "H", "away": "A"}),
        (ou_key, "HIL", {"home": "H", "away": "L"}),
    ):
        snap = snaps.get(snap_key)
        if not isinstance(snap, dict):
            continue
        h_raw = snap.get("h")
        if h_raw is None:
            continue
        try:
            line = float(h_raw)
        except (TypeError, ValueError):
            continue
        for feed_side, v2_side in side_map.items():
            odds = snap.get(feed_side)
            if odds is None:
                continue
            try:
                rows.append({
                    "market": code, "line": line, "selection": v2_side,
                    "odds": float(odds), "source_at": source_at,
                })
            except (TypeError, ValueError):
                continue
    return rows


def _stage_forecasts(m: dict, stage: str) -> list[dict]:
    key_map = {"first": ("initial_AH", "initial_OU"),
               "t30": ("T30_AH", "T30_OU"),
               "t5": ("live_AH", "live_OU")}
    ah_key, ou_key = key_map[stage]
    snaps = m.get("snapshots") or {}
    out = []
    ah = snaps.get(ah_key) or {}
    ou = snaps.get(ou_key) or {}
    for code, snap in (("HDC", ah), ("HIL", ou)):
        h = snap.get("h") if isinstance(snap, dict) else None
        if h is None:
            continue
        try:
            out.append({"code": code, "line": float(h)})
        except (TypeError, ValueError):
            pass
    return out


def _summarise(m: dict, stage: str) -> str:
    prices = _stage_prices(m, stage)
    forecasts = _stage_forecasts(m, stage)
    if not prices or not forecasts:
        return "(等候中)"
    fixture = {
        "id": str(m.get("sid") or ""),
        "league": m.get("league"),
        "home": m.get("home"),
        "away": m.get("away"),
    }
    try:
        results, _meta = apply_opening_model(
            fixture=fixture, prices=prices, forecasts=forecasts,
            learning_db_path=None,
        )
    except Exception as exc:  # pragma: no cover - defensive
        return f"(err:{type(exc).__name__})"
    if not results:
        return "(等候中)"
    # Pick highest-conviction row
    top = max(results, key=lambda r: (r.get("prob") or 0.0))
    label = top.get("label") or top.get("code") or ""
    odds = top.get("odds")
    prob = top.get("prob")
    if label and odds and prob is not None:
        return f"{label} @ {odds} · {prob*100:.1f}%"
    return label or "(等候中)"


def main() -> int:
    try:
        matches_raw = fetch_feed()
    except Exception as exc:
        print(f"[merge] feed error: {exc}", file=sys.stderr)
        return 2
    if not isinstance(matches_raw, list):
        print("[merge] feed unexpected shape", file=sys.stderr)
        return 2

    now_ms = int(datetime.now(timezone.utc).timestamp() * 1000)
    out_matches = []
    for m in matches_raw:
        if not isinstance(m, dict):
            continue
        ko_ms = m.get("kickoff_utc")
        if not ko_ms or ko_ms <= now_ms:
            continue
        ko_hkt = datetime.fromtimestamp(ko_ms / 1000, HKT)
        out_matches.append({
            "sid": str(m.get("sid") or ""),
            "kickoff_utc_ms": int(ko_ms),
            "kickoff_display": f"{ko_hkt.month}-{ko_hkt.day} {ko_hkt.strftime('%H:%M')}",
            "league": str(m.get("league") or "").strip(),
            "home": str(m.get("home") or "").strip(),
            "away": str(m.get("away") or "").strip(),
            "first": _summarise(m, "first"),
            "t30":   _summarise(m, "t30"),
            "t5":    _summarise(m, "t5"),
        })
    out_matches.sort(key=lambda x: x["kickoff_utc_ms"])
    payload = {
        "generated_utc": datetime.now(timezone.utc).isoformat(),
        "count": len(out_matches),
        "source": "crown-radar-loopback",
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
    print(f"[merge] wrote {len(out_matches)} matches to {OUT}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
