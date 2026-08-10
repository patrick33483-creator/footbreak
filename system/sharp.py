"""PinnAPI Edge 銳利參考盤 + 與 HKJC 賽事配對。

The public Footbreak interface remains unchanged:
  list_fixtures() -> Optic-compatible fixture dictionaries
  fetch_odds([fixture_id]) -> {fixture_id: price rows}
  structure(rows, home, away) -> HAD/HDC/HIL/CHL model structure

Internally it uses PinnAPI Edge only.  There is no OpticOdds fallback and no
disk-cache fallback for a failed live provider request: a provider failure must
propagate to the systemd service instead of silently rebuilding stale output.
"""
import json
import os
import re
import difflib
import unicodedata
import sys
from pathlib import Path
from datetime import datetime, timedelta, timezone

HKT = timezone(timedelta(hours=8))
CACHE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "cache")
os.makedirs(CACHE, exist_ok=True)

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from crown.config import settings as crown_settings
from crown.pinnapi import PinnapiClient


class ProviderError(RuntimeError):
    """A live sharp-provider failure.  Callers must not downgrade to stale data."""


def _client():
    config = crown_settings()
    if not config.pinnapi_configured:
        raise ProviderError("PinnAPI Edge credentials are not configured")
    return PinnapiClient(config)


# ---------------------------------------------------------------- 賽事清單

def _fixture_from_pinnapi(row):
    """Present a PinnAPI fixture through the legacy Footbreak fixture contract."""
    kickoff = datetime.fromtimestamp(float(row["kickoff"]), timezone.utc)
    league = str(row["league"])
    return {
        "id": str(row["id"]),
        "start_date": kickoff.isoformat().replace("+00:00", "Z"),
        "home_team_display": str(row["home"]),
        "away_team_display": str(row["away"]),
        "league": {"id": f"pinnapi:{league}", "name": league},
        "venue_name": None, "venue_location": None, "venue_neutral": False,
        "home_competitors": [], "away_competitors": [],
        "_provider": "pinnapi",
    }


def list_fixtures(max_pages=25, refresh=False):
    """Fetch current PinnAPI prematch fixtures; never use stale file fallback."""
    del max_pages, refresh
    try:
        rows = _client().fixtures()
    except Exception as exc:
        raise ProviderError(f"PinnAPI fixtures unavailable ({type(exc).__name__})") from exc
    # PinnAPI's fixture payload also contains child events such as
    # ``Team A (Corners) v Team B (Corners)``.  They share the parent kickoff
    # and almost identical team names, so keeping them in the normal fixture
    # universe makes an otherwise exact match look ambiguous.  Corners are
    # fetched separately through ``corner_lines(parent_id)`` below.
    rows = [row for row in rows if not row.get("parent_id")]
    if not rows:
        raise ProviderError("PinnAPI returned no eligible soccer prematch fixtures")
    return [_fixture_from_pinnapi(row) for row in rows]


# ---------------------------------------------------------------- 隊名配對

_STOP = {"fc", "cf", "sc", "ac", "afc", "club", "de", "the", "cd", "ca",
         "if", "ik", "bk", "sk", "fk", "vv", "sv", "rb"}

# HKJC 縮寫 -> 標準寫法
_ALIAS = {
    "utd": "united", "st": "saint", "qpr": "queens park rangers",
    "psv": "psv eindhoven", "kups": "kuopion palloseura",
    "tps": "turun palloseura", "nec": "nijmegen", "az": "az alkmaar",
    "m gladbach": "borussia monchengladbach", "wolves": "wolverhampton",
    "spurs": "tottenham", "inter": "internazionale",
    "mvv": "maastricht", "hjk": "helsingin jalkapalloklubi",
    "sjk": "seinajoen jalkapallokerho", "kupsx": "kuopion palloseura",
    "vps": "vaasan palloseura", "amadora": "estrela amadora",
}


# 需要保留的區別詞 (女足/青年隊/二隊) —— 錯配會直接導致錯盤
_QUAL = {"women", "w", "u17", "u19", "u20", "u21", "u23", "ii", "b", "reserves",
         "youth", "academy", "jong"}
_QUAL_CANON = {"w": "women", "ii": "res", "b": "res", "reserves": "res",
               "jong": "res", "youth": "res", "academy": "res",
               "u21": "res", "u23": "res"}
_AGE = {"u17", "u19", "u20"}

_LEAGUE_META = {}


def league_meta():
    """Compatibility stub: PinnAPI fixture rows do not expose Optic league metadata."""
    return {}


def qualifiers(team, league=""):
    """得出隊伍區別標記。二隊標記只看隊名,避免被聯賽名 (Serie B) 誤導。"""
    tt = set(norm(team).split())
    lt = set(norm(league or "").split())
    out = set()
    for w in tt & _QUAL:
        out.add(_QUAL_CANON.get(w, w))
    for w in lt & (_AGE | {"women"}):
        out.add(w)
    return out


def hk_qual(hk_match):
    hl = (hk_match.get("tournament") or {}).get("name_en") or ""
    return (qualifiers(hk_match["homeTeam"]["name_en"], hl)
            | qualifiers(hk_match["awayTeam"]["name_en"], hl))


def fx_qual(fx):
    lg = fx.get("league") or {}
    lname = lg.get("name") or ""
    q = (qualifiers(fx.get("home_team_display") or "", lname)
         | qualifiers(fx.get("away_team_display") or "", lname))
    return q


def norm(s):
    if not s:
        return ""
    s = unicodedata.normalize("NFKD", s)
    s = "".join(c for c in s if not unicodedata.combining(c))
    s = re.sub(r"[^a-z0-9 ]", " ", s.lower())
    words = []
    for w in s.split():
        w = _ALIAS.get(w, w)
        words.extend(w.split())
    return " ".join(w for w in words if w and w not in _STOP)


def toks(s):
    return set(norm(s).split())


def _sim(a, b):
    ta, tb = toks(a), toks(b)
    if not ta or not tb:
        return 0.0
    ca, cb = ta - _QUAL, tb - _QUAL
    if not ca or not cb:
        return 0.0
    inter = len(ca & cb)
    base = inter / max(1, min(len(ca), len(cb)))
    # 詞層模糊比對 (Yokohama vs Yokohama F Marinos, Utd vs United)
    fuzz = 0.0
    for x in ca:
        best = 0.0
        for y in cb:
            best = max(best, difflib.SequenceMatcher(None, x, y).ratio())
        fuzz += best
    fuzz /= len(ca)
    # 整串比對
    whole = difflib.SequenceMatcher(None, " ".join(sorted(ca)),
                                    " ".join(sorted(cb))).ratio()
    return max(base, 0.5 * fuzz + 0.5 * whole, 0.75 * fuzz)


def match_fixture(hk_match, fixtures, kickoff, tol_min=30):
    """把 HKJC 賽事對應到 Pinnacle fixture。回傳 (fixture, score) 或 (None, 0)。"""
    hh = hk_match["homeTeam"]["name_en"]
    ha = hk_match["awayTeam"]["name_en"]
    hq = hk_qual(hk_match)
    scored = []
    for fx in fixtures:
        try:
            sd = datetime.fromisoformat(fx["start_date"].replace("Z", "+00:00"))
        except Exception:
            continue
        if abs((sd - kickoff).total_seconds()) > tol_min * 60:
            continue
        fh = fx.get("home_team_display") or ""
        fa = fx.get("away_team_display") or ""
        # 女足 / 青年組 / 二隊 區別必須一致
        if hq != fx_qual(fx):
            continue
        sh, sa = _sim(hh, fh), _sim(ha, fa)
        rh, ra = _sim(hh, fa), _sim(ha, fh)
        direct = (sh + sa) / 2
        reverse = (rh + ra) / 2
        reversed_orientation = reverse > direct
        score = reverse if reversed_orientation else direct
        floor = min(rh, ra) if reversed_orientation else min(sh, sa)
        scored.append((score, floor, reversed_orientation, fx))
    if not scored:
        return None, 0.0
    scored.sort(key=lambda x: -x[0])
    s, lo, reversed_orientation, fx = scored[0]
    runner = scored[1][0] if len(scored) > 1 else 0.0
    # 三重閘門:平均分、兩邊都不能太低、與次佳選項要有明顯距離
    if s < 0.66 or lo < 0.42 or (s - runner) < 0.12:
        return None, s
    matched = dict(fx)
    matched["_orientation_reversed"] = reversed_orientation
    return matched, s


def orient_prices(prices, reversed_orientation=False):
    """Convert PinnAPI selections into HKJC's home/away orientation.

    Totals and corners are orientation-independent.  For a reversed fixture,
    1X2 home/away selections swap; Asian handicap selections swap and the
    home-perspective line changes sign.
    """
    if not reversed_orientation:
        return list(prices or [])
    converted = []
    for source in prices or []:
        row = dict(source)
        market = row.get("market")
        selection = row.get("selection")
        if market == "1X2" and selection in {"H", "A"}:
            row["selection"] = "A" if selection == "H" else "H"
        elif market == "HDC" and selection in {"H", "A"}:
            row["selection"] = "A" if selection == "H" else "H"
            try:
                row["line"] = -float(row.get("line"))
            except (TypeError, ValueError):
                continue
        converted.append(row)
    return converted


# ---------------------------------------------------------------- PinnAPI prices

def american_to_dec(p):
    p = float(p)
    return 1 + (p / 100 if p > 0 else 100 / -p)


def fetch_odds(fixture_ids):
    """Fetch full-match PinnAPI prices for every requested event or raise.

    PinnAPI exposes per-event prematch lines rather than Optic's batch endpoint.
    Returning an incomplete map would let downstream code quietly use partial
    data, so a failure of the normal full-match market fails this prediction
    pass.  Corners are a separate special-event feed: an absent, ambiguous, or
    temporarily failed corner response deliberately contributes no CHL rows,
    but must not discard otherwise usable HDC/HIL/1X2 data.
    """
    out = {}
    client = _client()
    for fixture_id in dict.fromkeys(str(item) for item in fixture_ids):
        try:
            parsed = client.lines(fixture_id)
        except Exception as exc:
            raise ProviderError(f"PinnAPI lines unavailable for {fixture_id} ({type(exc).__name__})") from exc
        prices = parsed.get("prices") or []
        if not prices:
            raise ProviderError(f"PinnAPI returned no full-match prices for {fixture_id}")
        merged = [
            dict(price, provider="pinnapi", event_id=fixture_id,
                 timestamp_inferred=bool(parsed.get("timestamp_inferred")))
            for price in prices
        ]
        try:
            corners = client.corner_lines(fixture_id)
        except Exception:
            # CHL is optional and fail-closed.  The normal match markets above
            # remain valid rather than turning a special-market outage into a
            # whole-fixture provider failure.
            corners = None
        if corners:
            for price in corners.get("prices") or []:
                # The parser accepts only one verified full-match corner child.
                # Keep CHL only; CHDC has no Footbreak model/settlement path.
                if price.get("market") != "CHL":
                    continue
                merged.append(dict(
                    price,
                    provider="pinnapi",
                    event_id=fixture_id,
                    corner_event_id=corners.get("corner_event_id"),
                    timestamp_inferred=bool(corners.get("timestamp_inferred")),
                ))
        out[fixture_id] = merged
    return out


def _native_structure(prices):
    """PinnAPI decimal prices -> Footbreak's existing model input structure."""
    had = {}
    hdc, hil, chl = {}, {}, {}
    ts = 0.0
    for price in prices:
        market = price.get("market")
        selection = price.get("selection")
        odds = price.get("odds")
        try:
            odds = float(odds)
            ts = max(ts, float(price.get("source_at") or 0))
        except (TypeError, ValueError):
            continue
        if odds <= 1:
            continue
        if market == "1X2" and selection in {"H", "D", "A"}:
            had[selection] = odds
            continue
        if market not in {"HDC", "HIL", "CHL"}:
            continue
        if market == "HDC" and selection not in {"H", "A"}:
            continue
        if market in {"HIL", "CHL"} and selection not in {"H", "L"}:
            continue
        try:
            line = float(price.get("line"))
        except (TypeError, ValueError):
            continue
        book = hdc if market == "HDC" else hil if market == "HIL" else chl
        row = book.setdefault(line, {"odds": {}, "main": False})
        row["odds"][selection] = odds
        row["main"] = bool(row["main"] or price.get("main"))

    def as_lines(book, keys):
        rows = []
        for line, row in sorted(book.items()):
            if all(row["odds"].get(key) for key in keys):
                rows.append({"lineId": None, "condition": f"{line:g}", "main": bool(row["main"]),
                             "status": "AVAILABLE", "odds": row["odds"]})
        if rows and not any(row["main"] for row in rows):
            midpoint = min(rows, key=lambda row: abs(row["odds"][keys[0]] - row["odds"][keys[1]]))
            midpoint["main"] = True
        return rows

    result = {
        "HDC": as_lines(hdc, ("H", "A")),
        "HIL": as_lines(hil, ("H", "L")),
        # Special-market parsing emits CHL only for one verified full-match
        # corner child.  Missing/ambiguous special markets therefore remain
        # empty without affecting standard markets.
        "CHL": as_lines(chl, ("H", "L")),
        "_ts": ts,
        "_provider": "pinnapi",
    }
    if len(had) == 3:
        result["HAD"] = [{"lineId": None, "condition": "0.0", "main": True,
                          "status": "AVAILABLE", "odds": had}]
    return result


def structure(odds, home_name, away_name):
    """整理 Pinnacle 賠率成模型輸入格式。

    回傳 {"HAD": [...], "HDC": [...], "HIL": [...], "CHL": [...]},
    與 hkjc_feed.flatten_odds 同一格式,方便共用擬合器。
    HDC condition 一律轉換成「主隊角度」。
    """
    if any(row.get("provider") == "pinnapi" for row in (odds or []) if isinstance(row, dict)):
        return _native_structure(odds)

    # Legacy converter retained only for offline historic fixture artifacts.
    # Live Footbreak paths call the PinnAPI branch above.
    ml, ah, tg, tc = {}, {}, {}, {}
    ts = 0.0
    for o in odds:
        try:
            price = american_to_dec(o["price"])
        except Exception:
            continue
        ts = max(ts, float(o.get("timestamp") or 0))
        mk, nm = o.get("market"), (o.get("name") or "")
        pts = o.get("points")
        if mk == "Moneyline":
            if _sim(nm, home_name) > 0.6:
                ml["H"] = price
            elif _sim(nm, away_name) > 0.6:
                ml["A"] = price
            elif nm.strip().lower() == "draw":
                ml["D"] = price
        elif mk == "Asian Handicap" and pts is not None:
            pts = float(pts)
            if _sim(nm, home_name) > 0.6:
                ah.setdefault(pts, {})["H"] = price       # 主隊 line = pts
            elif _sim(nm, away_name) > 0.6:
                ah.setdefault(-pts, {})["A"] = price      # 客隊 +x -> 主隊 -x
        elif mk == "Total Goals" and pts is not None:
            side = "H" if nm.lower().startswith("over") else "L"
            tg.setdefault(float(pts), {})[side] = price
        elif mk == "Total Corners" and pts is not None:
            side = "H" if nm.lower().startswith("over") else "L"
            tc.setdefault(float(pts), {})[side] = price

    def as_lines(d, keys):
        rows = []
        for cond in sorted(d):
            o = d[cond]
            if not all(k in o for k in keys):
                continue
            rows.append({"lineId": None, "condition": f"{cond:g}",
                         "main": False, "status": "AVAILABLE", "odds": o})
        if rows:
            # 最接近 2 邊平手的當主線
            mid = min(rows, key=lambda r: abs(r["odds"][keys[0]] - r["odds"][keys[1]]))
            mid["main"] = True
        return rows

    res = {}
    if len(ml) == 3:
        res["HAD"] = [{"lineId": None, "condition": "0.0", "main": True,
                       "status": "AVAILABLE", "odds": ml}]
    res["HDC"] = as_lines(ah, ["H", "A"])
    res["HIL"] = as_lines(tg, ["H", "L"])
    res["CHL"] = as_lines(tc, ["H", "L"])
    res["_ts"] = ts
    return res


def _opening_path(fixture_id):
    return os.path.join(CACHE, "pinnapi_open", f"{fixture_id}.json")


def opening_structure(fixture_id):
    """Return a locally observed PinnAPI first quote, never Optic historical data."""
    path = _opening_path(fixture_id)
    try:
        with open(path, encoding="utf-8") as handle:
            value = json.load(handle)
    except (OSError, ValueError, json.JSONDecodeError):
        return None
    return value if isinstance(value, dict) else None


def remember_opening(fixture_id, structure_value):
    """Persist the first valid PinnAPI structure only; later calls cannot overwrite it."""
    if not structure_value or not (
        structure_value.get("HAD")
        or structure_value.get("HDC")
        or structure_value.get("HIL")
        or structure_value.get("CHL")
    ):
        return
    path = _opening_path(fixture_id)
    if os.path.exists(path):
        return
    os.makedirs(os.path.dirname(path), exist_ok=True)
    temp = f"{path}.tmp-{os.getpid()}"
    with open(temp, "w", encoding="utf-8") as handle:
        json.dump(structure_value, handle, ensure_ascii=False)
        handle.flush()
        os.fsync(handle.fileno())
    try:
        os.link(temp, path)
    except FileExistsError:
        pass
    finally:
        try:
            os.unlink(temp)
        except FileNotFoundError:
            pass
