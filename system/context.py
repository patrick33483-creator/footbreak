"""真實世界情境層:天氣、休息日/疲勞、賠率移動。

三個資料源:
  1. 天氣  — Open-Meteo(免費、無需 key),按場地城市 geocode 後取開賽時段預報
  2. 疲勞  — 舊 OpticOdds 結果相容路徑(可用先補充,不可用就降信心)
  3. 移動  — 本機首次 PinnAPI 觀測對比現價
"""
from __future__ import annotations
import json, os, subprocess, urllib.parse, urllib.request
import datetime as dt
from pathlib import Path

CACHE = Path(__file__).parent / "cache"
(CACHE / "wx").mkdir(parents=True, exist_ok=True)
(CACHE / "rest").mkdir(parents=True, exist_ok=True)

UA = {"User-Agent": "Mozilla/5.0"}


# ─────────────────── 通用 ───────────────────
def _get_json(url: str, timeout: int = 30):
    req = urllib.request.Request(url, headers=UA)
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.loads(r.read().decode())


def _cached(path: Path, fn, max_age_h: float = 6.0):
    if path.exists():
        age = (dt.datetime.now().timestamp() - path.stat().st_mtime) / 3600
        if age < max_age_h:
            return json.loads(path.read_text())
    v = fn()
    path.write_text(json.dumps(v, ensure_ascii=False))
    return v


def optic(path: str, params: dict) -> dict:
    arg = {"source_id": "opticodds", "tool_name": "opticodds",
           "arguments": {"path": path, "method": "GET", "params": params}}
    p = subprocess.run(["external-tool", "call", json.dumps(arg)],
                       capture_output=True, text=True)
    if p.returncode:
        return {"_err": p.stderr[:400]}
    try:
        return json.loads(p.stdout)
    except Exception as e:
        return {"_err": f"parse: {e}"}


def _rows(resp):
    """把 OpticOdds 回應剝到最內層 data list(可能巢狀 data.data)。"""
    if isinstance(resp, dict) and "_err" in resp:
        return []
    cur = resp
    for _ in range(6):
        if isinstance(cur, str):
            try:
                cur = json.loads(cur)
            except Exception:
                return []
        if isinstance(cur, list):
            return cur
        if isinstance(cur, dict):
            nxt = cur.get("result", None)
            if nxt is None:
                nxt = cur.get("data", None)
            if nxt is None:
                return []
            cur = nxt
        else:
            return []
    return cur if isinstance(cur, list) else []


# ─────────────────── 1. 天氣 ───────────────────
def geocode(city: str) -> tuple[float, float] | None:
    """venue_location 通常係 'Bochum, Germany',取第一段做查詢。"""
    if not city:
        return None
    q = city.split(",")[0].strip()
    key = "".join(c if c.isalnum() else "_" for c in q.lower())[:60]
    fp = CACHE / "wx" / f"geo_{key}.json"

    def go():
        u = ("https://geocoding-api.open-meteo.com/v1/search?"
             + urllib.parse.urlencode({"name": q, "count": 1, "language": "en", "format": "json"}))
        try:
            res = _get_json(u).get("results") or []
        except Exception:
            return None
        if not res:
            return None
        return [res[0]["latitude"], res[0]["longitude"], res[0].get("name"), res[0].get("country")]

    v = _cached(fp, go, max_age_h=24 * 30)
    if not v:
        return None
    return v[0], v[1]


WX_CODE = {
    0: "晴", 1: "大致晴", 2: "部分多雲", 3: "陰",
    45: "霧", 48: "凍霧", 51: "毛毛雨", 53: "毛毛雨", 55: "密毛雨",
    61: "小雨", 63: "中雨", 65: "大雨", 66: "凍雨", 67: "凍雨",
    71: "小雪", 73: "中雪", 75: "大雪", 77: "雪粒",
    80: "陣雨", 81: "強陣雨", 82: "暴陣雨",
    95: "雷雨", 96: "雷雨帶冰雹", 99: "雷雨帶冰雹",
}


def weather_at(city: str, kickoff_utc: dt.datetime) -> dict | None:
    """取開賽時 + 開賽後一小時的平均條件(覆蓋比賽時段)。"""
    ll = geocode(city)
    if not ll:
        return None
    lat, lon = ll
    fp = CACHE / "wx" / f"fc_{lat:.2f}_{lon:.2f}.json"

    def go():
        u = ("https://api.open-meteo.com/v1/forecast?" + urllib.parse.urlencode({
            "latitude": lat, "longitude": lon,
            "hourly": ("temperature_2m,relative_humidity_2m,precipitation,"
                       "wind_speed_10m,wind_gusts_10m,weather_code"),
            "forecast_days": 4, "timezone": "UTC"}))
        try:
            return _get_json(u)["hourly"]
        except Exception:
            return None

    h = _cached(fp, go, max_age_h=3)
    if not h:
        return None
    want = [kickoff_utc.replace(minute=0, second=0, microsecond=0),
            (kickoff_utc + dt.timedelta(hours=1)).replace(minute=0, second=0, microsecond=0)]
    idx = []
    for w in want:
        s = w.strftime("%Y-%m-%dT%H:00")
        if s in h["time"]:
            idx.append(h["time"].index(s))
    if not idx:
        return None
    mean = lambda k: sum(h[k][i] for i in idx) / len(idx)
    codes = [h["weather_code"][i] for i in idx]
    return {
        "temp_c": round(mean("temperature_2m"), 1),
        "humidity": round(mean("relative_humidity_2m")),
        "precip_mm_h": round(mean("precipitation"), 2),
        "wind_kmh": round(mean("wind_speed_10m"), 1),
        "gust_kmh": round(max(h["wind_gusts_10m"][i] for i in idx), 1),
        "code": max(codes),
        "desc": WX_CODE.get(max(codes), f"code {max(codes)}"),
        "lat": lat, "lon": lon,
    }


# ─────────────────── 2. 疲勞 / 休息日 ───────────────────
def team_recent(team_id: str) -> list[dict]:
    """近 10 場賽果(端點硬上限)。回傳 [{date, is_home, gf, ga, extra_time}]。"""
    fp = CACHE / "rest" / f"{team_id}.json"

    def go():
        rows = _rows(optic("/fixtures/results", {"team_id": team_id}))
        out = []
        for r in rows:
            fx = r.get("fixture") or {}
            sd = fx.get("start_date") or r.get("start_date")
            if not sd:
                continue
            sc = r.get("scores") or {}
            hs, as_ = sc.get("home") or {}, sc.get("away") or {}
            ht = hs.get("total")
            at = as_.get("total")
            if ht is None or at is None:
                continue
            hn = ((fx.get("home_competitors") or [{}])[0]).get("id")
            is_home = (hn == team_id)
            # 加時偵測:出現第 3/4 節或總分節數 > 2
            per = hs.get("periods") or {}
            et = any(k for k in per if any(t in str(k).lower()
                                           for t in ("3", "4", "ot", "extra")))
            out.append({"date": sd, "is_home": is_home,
                        "gf": ht if is_home else at,
                        "ga": at if is_home else ht,
                        "extra_time": et})
        out.sort(key=lambda x: x["date"], reverse=True)
        return out

    return _cached(fp, go, max_age_h=12)


def fatigue(team_id: str, kickoff_utc: dt.datetime) -> dict:
    """休息日數、近 14 日場數、上場是否加時。"""
    rec = team_recent(team_id)
    if not rec:
        return {"rest_days": None, "games_14d": None, "prev_extra_time": None, "n": 0}
    prev = None
    for r in rec:
        d = dt.datetime.fromisoformat(r["date"].replace("Z", "+00:00"))
        if d < kickoff_utc:
            prev = (d, r)
            break
    if not prev:
        return {"rest_days": None, "games_14d": None, "prev_extra_time": None, "n": len(rec)}
    rest = (kickoff_utc - prev[0]).total_seconds() / 86400
    g14 = sum(1 for r in rec
              if 0 <= (kickoff_utc - dt.datetime.fromisoformat(
                  r["date"].replace("Z", "+00:00"))).days <= 14)
    return {"rest_days": round(rest, 1), "games_14d": g14,
            "prev_extra_time": prev[1]["extra_time"],
            "prev_date": prev[0].date().isoformat(), "n": len(rec)}


# ─────────────────── 3. 賠率移動 ───────────────────
def opening_structure(fixture_id: str, home_name: str, away_name: str) -> dict | None:
    """Compatibility wrapper for the locally observed PinnAPI first quote.

    PinnAPI Edge has no Optic-style `/fixtures/odds/historical` API in this
    deployment.  The sharp provider records the first complete structure and
    returns it here; no OpticOdds call or stale historical fallback occurs.
    """
    import sharp as S
    del home_name, away_name
    return S.opening_structure(fixture_id)


def opening_vs_now(fixture_id: str, odds_ids: list[str]) -> dict:
    """Legacy API retained as an explicit unsupported empty result."""
    del fixture_id, odds_ids
    return {}


if __name__ == "__main__":
    ko = dt.datetime(2026, 8, 7, 18, 30, tzinfo=dt.timezone.utc)
    print("天氣 Bochum:", weather_at("Bochum, Germany", ko))
    print("天氣 Wolverhampton:", weather_at("Wolverhampton, England", ko))
