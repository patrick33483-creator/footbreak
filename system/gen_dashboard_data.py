"""產生儀表板資料 JSON。

每場輸出:
  - Dixon-Coles 擬合參數 (λ主/λ客/ρ) — 基準為 Pinnacle 去水盤
  - 勝平負機率、最可能比分、總入球分佈
  - 角球負二項參數與分佈
  - 資料品質旗標:HKJC 盤口滯後、Pinnacle 開盤→現價移動、獨立近況可用性
"""
import json
import math
import os
import sys
import time
from datetime import datetime

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import hkjc_feed as H
import model as M
import sharp as S

HORIZON_MIN = 2200          # 只做未來 ~36 小時
OUT = os.path.join(os.path.dirname(os.path.abspath(__file__)), "dashboard", "data.json")

# 由賽果數據體檢得出:賽果分數異常(疑似累計/加時分數)的聯賽
BAD_RESULTS_LEAGUES = {
    "japan_-_j1_league", "netherlands_-_eredivisie", "norway_-_eliteserien",
    "england_-_efl_cup", "north_america_-_leagues_cup",
}


def american_to_dec(p):
    return S.american_to_dec(p)


def result_probs(mat):
    h = d = a = 0.0
    for i, row in enumerate(mat):
        for j, p in enumerate(row):
            if i > j:
                h += p
            elif i == j:
                d += p
            else:
                a += p
    return h, d, a


def top_scores(mat, k=6):
    out = []
    for i, row in enumerate(mat):
        for j, p in enumerate(row):
            out.append((p, i, j))
    out.sort(reverse=True)
    return [{"h": i, "a": j, "p": p} for p, i, j in out[:k]]


def goals_dist(mat, kmax=8):
    d = [0.0] * (kmax + 1)
    for i, row in enumerate(mat):
        for j, p in enumerate(row):
            t = i + j
            d[min(t, kmax)] += p
    return d


def corner_dist(pmf, kmax=20):
    d = [0.0] * (kmax + 1)
    for k, p in enumerate(pmf):
        d[min(k, kmax)] += p
    return d


def ou_table(pmf, lines):
    """回傳每條線嘅大/細機率。pmf 必須係一維總數分佈。"""
    out = []
    for ln in lines:
        w, pu = M.ou_prob(pmf, str(ln), "H")
        wl, _ = M.ou_prob(pmf, str(ln), "L")
        out.append({"line": ln, "over": w, "under": wl, "push": pu})
    return out


def ah_table(mat, lines):
    out = []
    for ln in lines:
        w, pu = M.ah_prob(mat, ln, "H")
        wa, pa = M.ah_prob(mat, ln, "A")
        out.append({"line": ln, "home": w, "home_push": pu,
                    "away": wa, "away_push": pa})
    return out


def pool_staleness(hk_match, now):
    out = {}
    for p in (hk_match.get("foPools") or []):
        try:
            ua = datetime.fromisoformat(p["updateAt"])
            out[p["oddsType"]] = round((now - ua).total_seconds() / 3600.0, 1)
        except Exception:
            pass
    return out


def pinnacle_movement(fixture_id):
    """由 /fixtures/odds/historical 取開盤價 (olv),對比現價。"""
    try:
        h = S._call("/fixtures/odds/historical",
                    {"fixture_id": [fixture_id], "sportsbook": ["Pinnacle"]})
    except Exception:
        return {}
    rows = (h.get("data") or [])
    if not rows:
        return {}
    out = {}
    for o in (rows[0].get("odds") or []):
        mid = o.get("market_id")
        if mid not in ("asian_handicap", "total_goals", "total_corners", "moneyline"):
            continue
        if not o.get("is_main"):
            continue
        olv = o.get("olv") or {}
        if olv.get("price") is None:
            continue
        out.setdefault(mid, []).append({
            "sel": o.get("selection"), "name": o.get("name"),
            "open_price": american_to_dec(olv["price"]),
            "open_points": olv.get("points"),
        })
    return out


def stage_of(mins):
    if mins < 0:
        return "已開賽"
    if mins <= 7.5:
        return "T-5"
    if mins <= 32.5:
        return "T-30"
    if mins <= 62.5:
        return "T-60"
    return "待入窗"


def main(limit=None):
    now = H.now_hkt()
    fixtures = S.list_fixtures()
    matches = [m for m in H.fetch_matches() if m["status"] == "PREEVENT"]
    sel = []
    for m in matches:
        ko = H.parse_kickoff(m)
        mins = (ko - now).total_seconds() / 60.0
        if -10 <= mins <= HORIZON_MIN:
            sel.append((mins, ko, m))
    sel.sort(key=lambda x: x[0])
    if limit:
        sel = sel[:limit]

    # 配對
    paired = []
    for mins, ko, m in sel:
        f, sc = S.match_fixture(m, fixtures, ko)
        paired.append((mins, ko, m, f, sc))

    # 批量取賠率
    fids = [p[3]["id"] for p in paired if p[3]]
    odds_map = S.fetch_odds(fids) if fids else {}

    out = {"generated_at": now.isoformat(), "matches": [],
           "meta": {"n_hkjc": len(matches), "n_window": len(sel),
                    "bad_results_leagues": sorted(BAD_RESULTS_LEAGUES)}}

    for mins, ko, m, f, sc in paired:
        hk = H.flatten_odds(m)
        rec = {
            "hk_id": m["id"],
            "home": m["homeTeam"]["name_ch"], "away": m["awayTeam"]["name_ch"],
            "home_en": m["homeTeam"]["name_en"], "away_en": m["awayTeam"]["name_en"],
            "league": m["tournament"]["name_ch"], "league_en": m["tournament"]["name_en"],
            "kickoff": ko.isoformat(), "mins_to_ko": round(mins, 1),
            "stage": stage_of(mins),
            "hk_staleness_h": pool_staleness(m, now),
            "hk_lines": {k: [{"condition": l["condition"], "main": l["main"],
                              "odds": l["odds"]} for l in v]
                         for k, v in hk.items() if not k.startswith("_")},
            "matched": bool(f), "match_score": round(sc, 3) if f else None,
        }
        if not f:
            rec["status"] = "無銳利盤參考 — 無法預測"
            out["matches"].append(rec)
            continue

        rec["pin_home"] = f["home_team_display"]
        rec["pin_away"] = f["away_team_display"]
        rec["league_id"] = (f.get("league") or {}).get("id")
        rec["venue"] = f.get("venue_name")
        rec["venue_city"] = f.get("venue_location")
        rec["neutral"] = bool(f.get("venue_neutral"))
        rec["indep_form_ok"] = rec["league_id"] not in BAD_RESULTS_LEAGUES

        pin = S.structure(odds_map.get(f["id"], []), f["home_team_display"],
                          f["away_team_display"])
        rec["pin_lines"] = {k: [{"condition": l["condition"], "main": l["main"],
                                 "odds": l["odds"]} for l in v]
                            for k, v in pin.items() if not k.startswith("_")}
        if pin.get("_ts"):
            rec["pin_ts"] = pin["_ts"]

        # 入球模型
        try:
            lh, la, rho, rmse, n = M.fit_goals(pin)
        except Exception as e:
            rec["status"] = f"入球擬合失敗: {e}"
            out["matches"].append(rec)
            continue
        if n < 3:
            rec["status"] = f"銳利盤線數不足 ({n})"
            out["matches"].append(rec)
            continue

        mat = M.score_matrix(lh, la, rho)
        ph, pd_, pa = result_probs(mat)
        rec["goals"] = {
            "lh": lh, "la": la, "rho": rho, "rmse": rmse, "n_targets": n,
            "total": lh + la, "supremacy": lh - la,
            "p_home": ph, "p_draw": pd_, "p_away": pa,
            "top_scores": top_scores(mat, 8),
            "dist": goals_dist(mat, 8),
            "matrix": [[round(mat[i][j], 6) for j in range(7)] for i in range(7)],
            "ou": ou_table(M.goals_pmf(mat), [1.5, 2.0, 2.5, 3.0, 3.5, 4.5]),
            "ah": ah_table(mat, ["-2.0", "-1.5", "-1.0", "-0.5", "0.0",
                                 "+0.5", "+1.0", "+1.5", "+2.0"]),
        }

        # 角球模型
        try:
            cm, cphi, crmse, cn = M.fit_corners(pin)
            if cn >= 2:
                cp = M.corner_pmf(cm, cphi)
                rec["corners"] = {
                    "mu": cm, "phi": cphi, "rmse": crmse, "n_targets": cn,
                    "dist": corner_dist(cp, 20),
                    "ou": ou_table(cp, [8.5, 9.5, 10.5, 11.5, 12.5]),
                }
        except Exception:
            pass

        out["matches"].append(rec)

    os.makedirs(os.path.dirname(OUT), exist_ok=True)
    with open(OUT, "w", encoding="utf8") as fh:
        json.dump(out, fh, ensure_ascii=False)
    ok = sum(1 for r in out["matches"] if r.get("goals"))
    print(f"寫出 {OUT}")
    print(f"  窗內賽事 {len(out['matches'])} 場,成功預測 {ok} 場")
    return out


if __name__ == "__main__":
    lim = int(sys.argv[1]) if len(sys.argv) > 1 else None
    main(lim)
