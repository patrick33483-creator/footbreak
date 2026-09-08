"""球賽預測 V4 — 共用模塊 (data source, schema, odds utils, checkpoint)"""
import json
import os
import urllib.request
import base64
import datetime as dt
from pathlib import Path

HKT = dt.timezone(dt.timedelta(hours=8))
AUTH = "Basic " + base64.b64encode(b"radar:toberich").decode()

V4_DIR = Path("/var/www/crownsystem-v4")
V4_STATE_DIR = Path("/var/lib/crownsystem-v4")
V4_MATCHES = V4_DIR / "matches.json"
V4_RESULTS = V4_DIR / "results.json"
V4_BRIEFINGS = V4_DIR / "briefings"
V4_CHECKPOINTS = V4_STATE_DIR / "checkpoints.json"  # sid -> {stage: {locked_at, snap, predictions}}
V4_META = V4_STATE_DIR / "meta.json"

CROWN_RADAR = "http://127.0.0.1"

def fetch_crown(path, timeout=20):
    req = urllib.request.Request(CROWN_RADAR + path, headers={"Authorization": AUTH})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.loads(r.read())

def fetch_matches():
    """Returns list of live matches with snapshots."""
    d = fetch_crown("/crown-radar/api/matches-crown")
    return d if isinstance(d, list) else d.get("matches", [])

def fetch_history():
    """Returns list of past matches with result."""
    d = fetch_crown("/crown-radar/api/history")
    return d if isinstance(d, list) else d.get("matches", [])

def now_hkt():
    return dt.datetime.now(HKT)

def now_ms():
    return int(dt.datetime.now(dt.timezone.utc).timestamp() * 1000)

def kickoff_hkt(ms):
    return dt.datetime.fromtimestamp(ms / 1000, tz=HKT)

# --- HK odds <-> Decimal ---
def hk_to_decimal(hk):
    """Hong Kong odds -> decimal odds. HK 0.86 -> decimal 1.86."""
    if hk is None:
        return None
    return round(float(hk) + 1.0, 3)

def implied_prob_from_hk(hk):
    """HK odds -> implied probability (raw, with vig)."""
    dec = hk_to_decimal(hk)
    if not dec or dec <= 1:
        return None
    return round(1.0 / dec, 4)

def no_vig_two_way(p_home_raw, p_away_raw):
    """De-vig probs from two-way market (returns no-vig p_home, p_away)."""
    if p_home_raw is None or p_away_raw is None:
        return None, None
    total = p_home_raw + p_away_raw
    if total <= 0:
        return None, None
    return round(p_home_raw / total, 4), round(p_away_raw / total, 4)

# --- Stage decision ---
def stage_for(kickoff_ms, now_ms_=None):
    """Which stage window we are in relative to kickoff.

    Returns one of: 'INITIAL', 'T30', 'T5', 'LIVE', 'POST', 'FUTURE'.
    - FUTURE: >4h before kickoff
    - INITIAL: 4h > delta > 30min before kickoff
    - T30: 30min > delta > 5min before kickoff
    - T5: 5min > delta > 0 before kickoff
    - LIVE: kickoff passed but <150min
    - POST: >150min after kickoff
    """
    if now_ms_ is None:
        now_ms_ = now_ms()
    delta_min = (kickoff_ms - now_ms_) / 60000.0
    if delta_min > 240:
        return "FUTURE"
    if delta_min > 30:
        return "INITIAL"
    if delta_min > 5:
        return "T30"
    if delta_min > 0:
        return "T5"
    if delta_min > -150:
        return "LIVE"
    return "POST"

def load_checkpoints():
    V4_STATE_DIR.mkdir(parents=True, exist_ok=True)
    if V4_CHECKPOINTS.exists():
        try:
            return json.loads(V4_CHECKPOINTS.read_text())
        except Exception:
            return {}
    return {}

def save_checkpoints(cp):
    V4_STATE_DIR.mkdir(parents=True, exist_ok=True)
    tmp = V4_CHECKPOINTS.with_suffix(".tmp")
    tmp.write_text(json.dumps(cp, ensure_ascii=False, indent=2))
    tmp.replace(V4_CHECKPOINTS)

def load_results_history():
    if V4_RESULTS.exists():
        try:
            return json.loads(V4_RESULTS.read_text())
        except Exception:
            return []
    return []
