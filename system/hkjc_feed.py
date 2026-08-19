"""HKJC 足球賽事 + 賠率取數模組 (公開 GraphQL 端點)."""
import json
import urllib.request
from datetime import datetime, timezone, timedelta

GQL = "https://info.cld.hkjc.com/graphql/base/"
HKT = timezone(timedelta(hours=8))

# HKJC 盤口代碼
# HAD 主客和 / HDC 讓球 / HIL 入球大小 / CHL 角球大小 / CHD 角球讓球 / TTG 總入球
# 端點每次最多接受 4 個盤口代碼
ODDS_TYPES = ["HAD", "HDC", "HIL", "CHL"]

import os

_QF = os.path.join(os.path.dirname(os.path.abspath(__file__)), "match_query.graphql")
with open(_QF, encoding="utf8") as _f:
    MATCH_QUERY = _f.read()


def _post(query, variables):
    req = urllib.request.Request(
        GQL,
        data=json.dumps({"query": query, "variables": variables}).encode(),
        headers={
            "Content-Type": "application/json",
            "User-Agent": "Mozilla/5.0",
            "Referer": "https://bet.hkjc.com/",
        },
    )
    try:
        timeout = min(20.0, max(1.0, float(os.getenv("FOOTBREAK_REMOTE_TIMEOUT_SECONDS", "8"))))
    except ValueError:
        timeout = 8.0
    with urllib.request.urlopen(req, timeout=timeout) as r:
        raw = r.read()
    if raw[:2] == b"\x1f\x8b":
        import gzip
        raw = gzip.decompress(raw)
    out = json.loads(raw.decode("utf8"))
    if out.get("errors"):
        raise RuntimeError(out["errors"])
    return out["data"]


def fetch_matches(start_date=None, end_date=None, match_ids=None, odds_types=None):
    """回傳 HKJC 賽事清單(含賠率)。日期格式 YYYY-MM-DD (HKT)。"""
    ot = odds_types or ODDS_TYPES
    v = {
        "fbOddsTypes": ot,
        "fbOddsTypesM": ["ALL"],
        "inplayOnly": False,
        "featuredMatchesOnly": False,
        "earlySettlementOnly": False,
        "showAllMatch": True,
    }
    if match_ids:
        v["matchIds"] = match_ids
    ms = _post(MATCH_QUERY, v)["matches"]
    # 端點的 startDate/endDate 過濾不穩定,改為本地按 HKT 日期過濾
    if start_date or end_date:
        keep = []
        for m in ms:
            ko = parse_kickoff(m)
            if not ko:
                continue
            d = ko.strftime("%Y-%m-%d")
            if start_date and d < start_date:
                continue
            if end_date and d > end_date:
                continue
            keep.append(m)
        ms = keep
    return ms


def parse_kickoff(m):
    """賽事開賽時間 (HKT aware datetime)。"""
    ts = m.get("kickOffTime") or m.get("matchDate")
    if not ts:
        return None
    ts = ts.replace("Z", "+00:00")
    dt = datetime.fromisoformat(ts)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=HKT)
    return dt.astimezone(HKT)


def flatten_odds(m):
    """整理成 {盤口: [{line, condition, main, selections:{str: odds}}]}"""
    out = {}
    for p in m.get("foPools") or []:
        if p.get("status") not in ("SELLINGSTARTED", "SELLING", "AVAILABLE"):
            # 保留但標記
            pass
        lines = []
        for ln in p.get("lines") or []:
            sels = {}
            for c in ln.get("combinations") or []:
                key = (c.get("selections") or [{}])[0].get("str") or c.get("str")
                try:
                    sels[key] = float(c["currentOdds"])
                except (TypeError, ValueError, KeyError):
                    sels[key] = None
            lines.append({
                "lineId": ln.get("lineId"),
                "condition": ln.get("condition"),
                "main": ln.get("main"),
                "status": ln.get("status"),
                "odds": sels,
            })
        out.setdefault(p["oddsType"], []).extend(lines)
    return out


def now_hkt():
    return datetime.now(HKT)


if __name__ == "__main__":
    today = now_hkt().strftime("%Y-%m-%d")
    tmr = (now_hkt() + timedelta(days=1)).strftime("%Y-%m-%d")
    ms = fetch_matches(today, tmr)
    print(f"共 {len(ms)} 場 ({today} ~ {tmr})")
    for m in ms[:6]:
        ko = parse_kickoff(m)
        od = flatten_odds(m)
        print("-", m["frontEndId"], m["tournament"]["name_ch"],
              m["homeTeam"]["name_ch"], "vs", m["awayTeam"]["name_ch"],
              ko.strftime("%m-%d %H:%M") if ko else "?", m["status"])
        for k, v in od.items():
            for ln in v[:2]:
                print("   ", k, ln["condition"], ln["main"], ln["odds"])
