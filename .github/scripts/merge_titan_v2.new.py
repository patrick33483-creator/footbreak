#!/usr/bin/env python3
"""crownsystem-v3 · merge_titan_v2.py (crown-radar backed, with persistent history)

Reads:
  http://127.0.0.1/crown-radar/api/matches-crown  (live)
  http://127.0.0.1/crown-radar/api/history        (finished)

Writes:
  /var/www/crownsystem-v3/matches.json  — live board (未開場)
  /var/www/crownsystem-v3/results.json  — 累積歷史 (kickoff >= EPOCH)

History rule:
  Only accept sid whose kickoff_utc >= EPOCH_MS (今日 2026-09-08 16:45 HKT).
  Once appended, never overwrite (idempotent by sid).
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

FEED_LIVE = "http://127.0.0.1/crown-radar/api/matches-crown"
FEED_HIST = "http://127.0.0.1/crown-radar/api/history"
AUTH = base64.b64encode(b"radar:toberich").decode()

OUT_MATCHES = "/var/www/crownsystem-v3/matches.json"
OUT_RESULTS = "/var/www/crownsystem-v3/results.json"

HKT = timezone(timedelta(hours=8))
# Epoch: 2026-09-08 16:45 HKT (fixed cut-off; nothing before this counts)
EPOCH_MS = int(datetime(2026, 9, 8, 16, 45, tzinfo=HKT).timestamp() * 1000)


def fetch(url: str):
    req = urllib.request.Request(url)
    req.add_header("Authorization", f"Basic {AUTH}")
    with urllib.request.urlopen(req, timeout=30) as r:
        return json.load(r)


def _stage_prices(m: dict, stage: str) -> list[dict]:
    snaps = m.get("snapshots") or {}
    ko_ms = m.get("kickoff_utc") or 0
    key_map = {
        "first": ("initial_AH", "initial_OU", -3600 * 3),
        "t30":   ("T30_AH",     "T30_OU",     -1800),
        "t5":    ("T5_AH",      "T5_OU",      -300),
    }
    ah_key, ou_key, offset = key_map[stage]
    # Fallback: some feeds only have `live_*` for the T-5 snapshot.
    if stage == "t5" and not isinstance(snaps.get(ah_key), dict):
        ah_key, ou_key = "live_AH", "live_OU"
    source_at = datetime.fromtimestamp((ko_ms + offset * 1000) / 1000, timezone.utc).isoformat()
    rows: list[dict] = []
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
    key_map = {
        "first": ("initial_AH", "initial_OU"),
        "t30":   ("T30_AH", "T30_OU"),
        "t5":    ("T5_AH", "T5_OU"),
    }
    snaps = m.get("snapshots") or {}
    ah_key, ou_key = key_map[stage]
    if stage == "t5" and not isinstance(snaps.get(ah_key), dict):
        ah_key, ou_key = "live_AH", "live_OU"
    out: list[dict] = []
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


_STAGE_GATE_MIN = {"first": None, "t30": 30, "t5": 5}


def _forecast_for(m: dict, stage: str) -> str:
    """Return a display string for a stage forecast, ignoring time gates."""
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
    except Exception as exc:
        return f"(err:{type(exc).__name__})"
    if not results:
        return "(等候中)"
    top = max(results, key=lambda r: (r.get("prob") or 0.0))
    label = top.get("label") or top.get("code") or ""
    odds = top.get("odds")
    if label and odds is not None:
        return f"{label} @ {odds}"
    return label or "(等候中)"


def _summarise_live(m: dict, stage: str, now_ms: int) -> str:
    ko_ms = m.get("kickoff_utc") or 0
    mins_to_ko = (ko_ms - now_ms) / 60000.0
    gate = _STAGE_GATE_MIN.get(stage)
    if gate is not None and mins_to_ko > gate:
        return "(等候中)"
    return _forecast_for(m, stage)


def _atomic_write(path: str, payload):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=os.path.dirname(path), prefix=".tmp.", suffix=".json")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(payload, f, ensure_ascii=False, indent=2)
        os.chmod(tmp, 0o644)
        os.replace(tmp, path)
    finally:
        if os.path.exists(tmp):
            try:
                os.unlink(tmp)
            except OSError:
                pass


def build_live() -> int:
    try:
        raw = fetch(FEED_LIVE)
    except Exception as exc:
        print(f"[live] feed error: {exc}", file=sys.stderr)
        return 2
    if not isinstance(raw, list):
        print("[live] unexpected shape", file=sys.stderr)
        return 2
    now_ms = int(datetime.now(timezone.utc).timestamp() * 1000)
    out = []
    for m in raw:
        if not isinstance(m, dict):
            continue
        ko_ms = m.get("kickoff_utc")
        if not ko_ms or ko_ms <= now_ms:
            continue
        ko_hkt = datetime.fromtimestamp(ko_ms / 1000, HKT)
        out.append({
            "sid": str(m.get("sid") or ""),
            "kickoff_utc_ms": int(ko_ms),
            "kickoff_display": f"{ko_hkt.month}-{ko_hkt.day} {ko_hkt.strftime('%H:%M')}",
            "league": str(m.get("league") or "").strip(),
            "home": str(m.get("home") or "").strip(),
            "away": str(m.get("away") or "").strip(),
            "first": _summarise_live(m, "first", now_ms),
            "t30":   _summarise_live(m, "t30", now_ms),
            "t5":    _summarise_live(m, "t5", now_ms),
        })
    out.sort(key=lambda x: x["kickoff_utc_ms"])
    _atomic_write(OUT_MATCHES, {
        "generated_utc": datetime.now(timezone.utc).isoformat(),
        "count": len(out),
        "source": "crown-radar-loopback",
        "matches": out,
    })
    print(f"[live] wrote {len(out)} → {OUT_MATCHES}")
    return 0


def _existing_results() -> dict:
    if os.path.exists(OUT_RESULTS):
        try:
            with open(OUT_RESULTS, encoding="utf-8") as f:
                d = json.load(f)
            if isinstance(d, dict) and isinstance(d.get("matches"), list):
                return d
        except Exception:
            pass
    return {"epoch_ms": EPOCH_MS, "matches": []}


def build_history() -> int:
    try:
        raw = fetch(FEED_HIST)
    except Exception as exc:
        print(f"[hist] feed error: {exc}", file=sys.stderr)
        return 2
    if not isinstance(raw, list):
        print("[hist] unexpected shape", file=sys.stderr)
        return 2

    existing = _existing_results()
    by_sid = {m["sid"]: m for m in existing["matches"] if isinstance(m, dict) and m.get("sid")}

    added = 0
    updated = 0
    for m in raw:
        if not isinstance(m, dict):
            continue
        ko_ms = m.get("kickoff_utc") or 0
        if ko_ms < EPOCH_MS:
            continue
        sid = str(m.get("sid") or "")
        if not sid:
            continue

        result = m.get("result") or {}
        if not isinstance(result, dict):
            result = {}

        ko_hkt = datetime.fromtimestamp(ko_ms / 1000, HKT)
        # Compute forecasts once; keep whatever exists in snapshots.
        first_pred = _forecast_for(m, "first")
        t30_pred = _forecast_for(m, "t30")
        t5_pred = _forecast_for(m, "t5")

        hs = result.get("home_score")
        as_ = result.get("away_score")
        ht_hs = result.get("ht_home_score")
        ht_as = result.get("ht_away_score")
        status = result.get("status") or ""
        if hs is not None and as_ is not None:
            score_display = f"{hs}-{as_}"
            if ht_hs is not None and ht_as is not None:
                score_display += f" (半 {ht_hs}-{ht_as})"
            if status:
                score_display += f" [{status}]"
        else:
            score_display = ""

        entry = {
            "sid": sid,
            "kickoff_utc_ms": int(ko_ms),
            "kickoff_display": f"{ko_hkt.month}-{ko_hkt.day} {ko_hkt.strftime('%H:%M')}",
            "league": str(m.get("league") or "").strip(),
            "home": str(m.get("home") or "").strip(),
            "away": str(m.get("away") or "").strip(),
            "score": score_display,
            "home_score": hs,
            "away_score": as_,
            "ht_home_score": ht_hs,
            "ht_away_score": ht_as,
            "status": status,
            "first": first_pred,
            "t30":   t30_pred,
            "t5":    t5_pred,
        }
        if sid in by_sid:
            # Refresh score/status only if changed (predictions stay frozen once real).
            old = by_sid[sid]
            changed = False
            for k in ("score", "home_score", "away_score",
                      "ht_home_score", "ht_away_score", "status"):
                if old.get(k) != entry[k]:
                    old[k] = entry[k]
                    changed = True
            # Predictions: only overwrite "(等候中)" or "(err:…)" placeholders.
            for k in ("first", "t30", "t5"):
                v = old.get(k) or ""
                if v.startswith("(") and not entry[k].startswith("("):
                    old[k] = entry[k]
                    changed = True
            if changed:
                updated += 1
        else:
            by_sid[sid] = entry
            added += 1

    all_matches = list(by_sid.values())
    all_matches.sort(key=lambda x: x.get("kickoff_utc_ms") or 0, reverse=True)
    _atomic_write(OUT_RESULTS, {
        "generated_utc": datetime.now(timezone.utc).isoformat(),
        "epoch_ms": EPOCH_MS,
        "epoch_display": "2026-09-08 16:45 HKT",
        "count": len(all_matches),
        "added_this_run": added,
        "updated_this_run": updated,
        "source": "crown-radar-history",
        "matches": all_matches,
    })
    print(f"[hist] total={len(all_matches)} +new={added} ~upd={updated} → {OUT_RESULTS}")
    return 0


def main() -> int:
    a = build_live()
    b = build_history()
    return a or b


if __name__ == "__main__":
    raise SystemExit(main())
