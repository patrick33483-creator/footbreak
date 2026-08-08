"""銳利參考盤 (Pinnacle) 取數 + 與 HKJC 賽事配對。"""
import json
import os
import re
import subprocess
import difflib
import unicodedata
from datetime import datetime, timedelta, timezone

HKT = timezone(timedelta(hours=8))
CACHE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "cache")
os.makedirs(CACHE, exist_ok=True)

BOOKS = ["Pinnacle"]
MARKETS = ["Moneyline", "Asian Handicap", "Total Goals", "Total Corners"]


def _call(path, params):
    payload = json.dumps({
        "source_id": "opticodds", "tool_name": "opticodds",
        "arguments": {"path": path, "params": params},
    })
    p = subprocess.run(["external-tool", "call", payload],
                       capture_output=True, text=True, timeout=180)
    if p.returncode != 0:
        raise RuntimeError(p.stderr[:400])
    d = json.loads(p.stdout)
    r = d.get("result", d)
    if "data" not in r:
        raise RuntimeError(str(r)[:300])
    return r["data"]


# ---------------------------------------------------------------- 賽事清單

def list_fixtures(max_pages=25, refresh=False):
    """列出所有有 Pinnacle 盤的足球賽事。快取 20 分鐘。"""
    f = os.path.join(CACHE, "sharp_fixtures.json")
    if not refresh and os.path.exists(f):
        age = datetime.now().timestamp() - os.path.getmtime(f)
        if age < 1200:
            with open(f, encoding="utf8") as fh:
                return json.load(fh)
    out, cursor = [], None
    for _ in range(max_pages):
        params = {"sport": "soccer", "sportsbook": "Pinnacle"}
        if cursor:
            params["cursor"] = cursor
        d = _call("/fixtures/active", params)
        rows = d.get("data") or []
        out.extend(rows)
        cursor = d.get("cursor")
        if not rows or not cursor:
            break
    with open(f, "w", encoding="utf8") as fh:
        json.dump(out, fh)
    return out


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
    global _LEAGUE_META
    if _LEAGUE_META:
        return _LEAGUE_META
    f = os.path.join(CACHE, "league_meta.json")
    if os.path.exists(f):
        with open(f, encoding="utf8") as fh:
            _LEAGUE_META = json.load(fh)
    else:
        rows = _call("/leagues", {"sport": "soccer"})["data"]
        _LEAGUE_META = {r["id"]: {"gender": r.get("gender"), "name": r.get("name")}
                        for r in rows}
        with open(f, "w", encoding="utf8") as fh:
            json.dump(_LEAGUE_META, fh, ensure_ascii=False)
    return _LEAGUE_META


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
    # 聯賽元資料的性別比隊名可靠
    g = (league_meta().get(lg.get("id")) or {}).get("gender")
    if g == "women":
        q.add("women")
    elif g == "men":
        q.discard("women")
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
        s = (sh + sa) / 2
        if _sim(hh, fa) + _sim(ha, fh) > sh + sa:
            continue          # 主客倒轉,唔接受(避免錯邊)
        scored.append((s, min(sh, sa), fx))
    if not scored:
        return None, 0.0
    scored.sort(key=lambda x: -x[0])
    s, lo, fx = scored[0]
    runner = scored[1][0] if len(scored) > 1 else 0.0
    # 三重閘門:平均分、兩邊都不能太低、與次佳選項要有明顯距離
    if s < 0.66 or lo < 0.42 or (s - runner) < 0.12:
        return None, s
    return fx, s


# ---------------------------------------------------------------- 賠率

def american_to_dec(p):
    p = float(p)
    return 1 + (p / 100 if p > 0 else 100 / -p)


def fetch_odds(fixture_ids):
    """批次取 Pinnacle 賠率 (每次最多 5 場)。回傳 {fixture_id: [odds...]}"""
    out = {}
    ids = list(fixture_ids)
    for i in range(0, len(ids), 5):
        chunk = ids[i:i + 5]
        d = _call("/fixtures/odds", {
            "fixture_id": chunk, "sportsbook": BOOKS, "market": MARKETS,
        })
        for fx in d.get("data") or []:
            out[fx["id"]] = fx.get("odds") or []
    return out


def structure(odds, home_name, away_name):
    """整理 Pinnacle 賠率成模型輸入格式。

    回傳 {"HAD": [...], "HDC": [...], "HIL": [...], "CHL": [...]},
    與 hkjc_feed.flatten_odds 同一格式,方便共用擬合器。
    HDC condition 一律轉換成「主隊角度」。
    """
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
