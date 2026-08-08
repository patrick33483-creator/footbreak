"""三階段球賽預測執行器。

流程:HKJC 賽事 → 配對 Pinnacle → 擬合初盤/現價 → 加天氣、疲勞、場地、
陣容調整 → 重算比分矩陣 → 對比馬會盤 → 每場出一個結論。
"""
from __future__ import annotations
import json
import math
import os
import sys
import datetime as dt
from dataclasses import asdict

import hkjc_feed as H
import model as M
import sharp as S
import context as C
import predict as P
import staking as K

HERE = os.path.dirname(os.path.abspath(__file__))
HKT = dt.timezone(dt.timedelta(hours=8))

# 由人手研究(網上新聞)填入嘅陣容/傷患調整,key = HKJC matchId
NEWS_FILE = os.path.join(HERE, "news_adj.json")


def load_news_adj() -> dict:
    if os.path.exists(NEWS_FILE):
        return json.load(open(NEWS_FILE, encoding="utf-8"))
    return {}


# 三個預測時點:
#   首預  每晚 23:59 掃全板,一場只做一次
#   T-30  開賽前 30 分鐘(陣容/傷患已出,賠率漸定)
#   T-5   開賽前 5 分鐘 —— 唯一落注時點
SWEEP = "首預"
WIN_T30 = (20.0, 40.0)     # T-30 觸發窗(留返排程延遲餘裕)
WIN_T5 = (1.0, 10.0)       # T-5 觸發窗


def stage_of(mins: float, sweep: bool = False) -> str:
    if sweep:
        return SWEEP
    if WIN_T5[0] <= mins <= WIN_T5[1]:
        return "T-5"
    if WIN_T30[0] <= mins <= WIN_T30[1]:
        return "T-30"
    if mins <= WIN_T5[1]:
        return "T-5"
    if mins <= WIN_T30[1]:
        return "T-30"
    return "待入窗"


def done_stages() -> dict:
    """由模擬倉讀返每場已完成嘅預測階段,避免重複。"""
    fp = os.path.join(HERE, "sim_ledger.json")
    if not os.path.exists(fp):
        return {}
    try:
        d = json.load(open(fp, encoding="utf-8"))
    except Exception:
        return {}
    return {mid: {s["stage"] for s in (w.get("stages") or [])}
            for mid, w in (d.get("watch") or {}).items()}


def due_now(mins: float, done: set) -> str | None:
    """依家應該幫呢場做邊個階段?已做過就回 None。"""
    if WIN_T5[0] <= mins <= WIN_T5[1] and "T-5" not in done:
        return "T-5"
    if WIN_T30[0] <= mins <= WIN_T30[1] and "T-30" not in done:
        return "T-30"
    return None


def conviction(base_fit, adj_conf: float, mins: float,
               n_hk_lines: int, has_wx: bool, has_news: bool,
               hk_moved: bool | None) -> float:
    """信念強度(0-100)。以預判質素為主,唔係以 edge 為主。

    校準:一場資料齊全、擬合靚、有陣容消息、已入分析窗嘅賽事約 68-75 分;
    冇陣容消息、季初無數據嘅賽事會跌落 50 以下。上限刻意壓喺 85 —
    呢個模型本質係「銳利盤翻譯器 + 情境修正」,唔應該扮到有 90 分把握。
    """
    c = 40.0
    rmse = base_fit.get("rmse")
    if rmse is not None:
        c += max(-6, min(10, (0.05 - rmse) * 200))      # 擬合越貼越有信心
    n = base_fit.get("n") or 0
    c += min(6, n / 5.0)                                 # 銳利盤線數
    c += min(4, n_hk_lines / 6.0)                        # 馬會盤線數
    c += adj_conf                                        # 情境調整帶嚟嘅加減
    c += 2 if has_wx else 0
    c += 6 if has_news else -4
    # HKJC 真實變盤(同上一個快照比)。冇快照就唔加唔減。
    if hk_moved is False:
        c += 3          # 盤口穩定,雙方睇法一致
    elif hk_moved is True:
        c -= 2          # 臨場有郁,可能有我未掌握嘅資訊
    c += 4 if mins <= 35 else (2 if mins <= 70 else 0)
    return round(max(0.0, min(85.0, c)), 1)


HK_SNAP = os.path.join(HERE, "hk_snapshots.json")


def hk_odds_fingerprint(m) -> dict:
    """記低馬會每條線嘅現價,用嚟同下一階段比較,量度真實變盤。
    (HKJC feed 冇提供最後改盤時間 —— updateAt 只係開賣時間,唔可以當滯後訊號。)"""
    hk = H.flatten_odds(m)
    out = {}
    for code in ("HAD", "HDC", "HIL", "CHL"):
        for ln in (hk.get(code) or []):
            for side, o in (ln.get("odds") or {}).items():
                out[f"{code}|{ln.get('condition')}|{side}"] = o
    return out


def load_hk_snaps() -> dict:
    if os.path.exists(HK_SNAP):
        return json.load(open(HK_SNAP, encoding="utf-8"))
    return {}


def hk_movement(prev: dict | None, cur: dict):
    """回傳 (有無郁, 最大變幅%, 變咗幾多條線)。"""
    if not prev:
        return None, None, None
    keys = set(prev) & set(cur)
    if not keys:
        return None, None, None
    diffs = [(cur[k] - prev[k]) / prev[k] for k in keys if prev[k]]
    if not diffs:
        return None, None, None
    n_moved = sum(1 for d in diffs if abs(d) > 0.005)
    mx = max(diffs, key=abs)
    return (n_moved > 0), round(mx * 100, 2), n_moved


def analyse_match(m, fx, wx_city_override=None, news=None, prev_snap=None,
                  stage_override=None):
    """對單場做完整預判。回傳 dict。"""
    ko = H.parse_kickoff(m)
    mins = (ko - dt.datetime.now(dt.timezone.utc)).total_seconds() / 60
    home_ch = m["homeTeam"]["name_ch"]
    away_ch = m["awayTeam"]["name_ch"]
    hname, aname = fx["home_team_display"], fx["away_team_display"]

    cur_st = S.structure(S.fetch_odds([fx["id"]]).get(fx["id"], []), hname, aname)
    now = P.fit_view(cur_st)
    if not now:
        return {"skip": "無法由銳利盤擬合模型"}
    op = P.fit_view(C.opening_structure(fx["id"], hname, aname))

    # ── 情境調整 ──
    adjs: list[P.Adj] = []
    mv_adj, mv_sig = P.movement_adj(op, now)
    adjs += mv_adj

    city = wx_city_override or (fx.get("venue_location") or "").split(",")[0].strip()
    wx = C.weather_at(city, ko) if city else None
    adjs += P.weather_adj(wx)

    th = (fx.get("home_competitors") or [{}])[0].get("id")
    ta = (fx.get("away_competitors") or [{}])[0].get("id")
    fh = C.fatigue(th, ko) if th else {}
    fa = C.fatigue(ta, ko) if ta else {}
    adjs += P.fatigue_adj(fh, fa, fx.get("league", {}).get("id", ""))
    adjs += P.venue_adj(bool(fx.get("venue_neutral")), "")

    if news:
        for n in news.get("adjustments", []):
            adjs.append(P.Adj(n.get("tag", "陣容"), n.get("reason", ""),
                              goals=n.get("goals", 1.0),
                              corners=n.get("corners", 1.0),
                              supremacy=n.get("supremacy", 0.0),
                              confidence=n.get("confidence", 0.0)))

    lh2, la2, mu2, mults = P.apply(now["lh"], now["la"], now.get("mu"), adjs)
    rho = now["rho"]
    phi = now.get("phi")

    mat, gp, cp, ph, pd, pa = P.outcome_probs(lh2, la2, rho, mu2, phi)

    # ── 對比馬會盤 ──
    hk = H.flatten_odds(m)
    n_hk = sum(len(hk.get(k) or []) for k in ("HAD", "HDC", "HIL", "CHL"))
    fg = (lh2, la2, rho, now["rmse"], now["n"])
    fc = (mu2, phi, now.get("c_rmse", 0), now.get("c_n", 0)) if (mu2 and phi) else None
    cands = M.evaluate(hk, fg, fc, home_ch, away_ch)

    fp = hk_odds_fingerprint(m)
    prev_fp = (prev_snap or {}).get("fingerprint")
    moved, max_move, n_moved = hk_movement(prev_fp, fp)

    adj_conf = sum(a.confidence for a in adjs)
    conv = conviction(now, adj_conf, mins, n_hk, wx is not None, bool(news), moved)

    # ── 每條候選:EV + 凱利 ──
    rows = []
    for c in cands:
        ev = P.ev_pct(c.prob, c.push, c.odds)
        kf = P.kelly_fraction(c.prob, c.push, c.odds)
        rows.append({"market": c.market, "code": c.code, "condition": c.condition,
                     "side": c.side, "label": c.label, "odds": c.odds,
                     "fair": round(c.fair, 3), "prob": round(c.prob, 4),
                     "push": round(c.push, 4), "ev": round(ev, 4),
                     "kelly_raw": round(kf, 4), "is_main": c.is_main})

    return {
        "match_id": m.get("id"), "home": home_ch, "away": away_ch,
        "home_en": hname, "away_en": aname,
        "fixture_id": fx.get("id"), "league_id": fx.get("league", {}).get("id"),
        "league": m.get("tournament", {}).get("nameCH") or fx.get("league", {}).get("name"),
        "kickoff_hkt": ko.astimezone(HKT).strftime("%Y-%m-%d %H:%M"),
        "mins_to_ko": round(mins, 1),
        "stage": stage_override or stage_of(mins),
        "venue": fx.get("venue_name"), "venue_city": city,
        "neutral": bool(fx.get("venue_neutral")),
        "hk_pool_opened": (m.get("foPools") or [{}])[0].get("updateAt"),
        "hk_moved_since_last": moved, "hk_max_move_pct": max_move,
        "hk_n_lines_moved": n_moved, "hk_fingerprint": fp,
        "open": ({k: round(v, 4) for k, v in op.items()} if op else None),
        "now": {k: round(v, 4) for k, v in now.items()},
        "movement": mv_sig,
        "weather": wx,
        "fatigue": {"home": fh, "away": fa},
        "adjustments": [asdict(a) for a in adjs],
        "mults": mults,
        "final": {"lh": round(lh2, 3), "la": round(la2, 3),
                  "total": round(lh2 + la2, 3), "supremacy": round(lh2 - la2, 3),
                  "mu": (round(mu2, 2) if mu2 else None), "rho": round(rho, 4)},
        "outcome": {"home": round(ph, 4), "draw": round(pd, 4), "away": round(pa, 4)},
        "top_scores": sorted(
            [{"s": f"{i}-{j}", "p": round(mat[i][j], 4)}
             for i in range(6) for j in range(6)],
            key=lambda x: -x["p"])[:6],
        "conviction": conv,
        "candidates": rows,
        "n_hk_lines": n_hk,
    }


def pick_one(res: dict, min_ev=0.015, conf_floor=P.CONF_FLOOR):
    """每場揀一個結論。回傳 (pick|None, reason)。"""
    conv = res["conviction"]
    if conv < conf_floor:
        return None, f"信念強度 {conv} 低於門檻 {conf_floor:g} — 資訊唔夠,觀望"
    ok = [c for c in res["candidates"] if c["ev"] >= min_ev]
    if not ok:
        best = max(res["candidates"], key=lambda c: c["ev"]) if res["candidates"] else None
        b = f"(最佳僅 {best['label']} EV {best['ev']:+.2%})" if best else ""
        return None, f"我嘅預判同馬會盤價基本一致,冇一注值博 {b} — 觀望"
    # 排序:信念 × 價值。主線優先(流通量高、更可信)
    for c in ok:
        c["score"] = c["ev"] * (1.0 + (0.15 if c["is_main"] else 0))
    best = max(ok, key=lambda c: c["score"])
    # ── 分數凱利:階段控制 + 市場折讓 + 信念縮放,最後才套單場上限 ──
    st = K.stage()
    mkt_mult = float(st["market_mult"].get(best["code"], 1.0))
    frac = best["kelly_raw"] * st["fraction"] * mkt_mult
    frac *= min(1.0, conv / 75.0)          # 信念未夠 75 分按比例縮注
    frac = min(frac, st["cap"])            # 單場硬上限
    best["stake"] = round(P.BANKROLL * frac / 10) * 10
    best["kelly_used"] = round(frac, 4)
    best["stake_stage"] = {
        "level": st["level"], "label": st["label"],
        "fraction": round(st["fraction"], 4), "cap": st["cap"],
        "market_mult": mkt_mult, "n_settled": st["n_settled"],
        "slope": (round(st["slope"], 3) if st["slope"] is not None else None),
    }
    return best, ""


def main(match_ids=None, horizon_min=700, out="predictions.json",
         mode="due", force=False):
    """mode:
         sweep — 掃全板,每場做一次「首預」(已做過就跳過)
         due   — 只做啱啱踏入 T-30 / T-5 窗口而又未做過嘅場
         all   — 唔理窗口,horizon 內全部重跑(除錯用)
    """
    matches = [m for m in H.fetch_matches() if m.get("status") == "PREEVENT"]
    fixtures = S.list_fixtures()
    news_all = load_news_adj()
    snaps = load_hk_snaps()
    done = {} if force else done_stages()
    results, skipped = [], 0
    for m in matches:
        mid = str(m.get("id"))
        if match_ids and mid not in match_ids:
            continue
        ko = H.parse_kickoff(m)
        if not ko:
            continue
        mins = (ko - dt.datetime.now(dt.timezone.utc)).total_seconds() / 60
        if mins < 2 or mins > horizon_min:
            continue

        seen = done.get(mid, set())
        if mode == "sweep":
            if SWEEP in seen:
                skipped += 1
                continue
            stage = SWEEP
        elif mode == "due":
            stage = due_now(mins, seen)
            if stage is None:
                skipped += 1
                continue
        else:
            stage = stage_of(mins)

        fx, sc = S.match_fixture(m, fixtures, ko)
        if not fx:
            continue
        try:
            r = analyse_match(m, fx, news=news_all.get(mid),
                              prev_snap=snaps.get(mid), stage_override=stage)
        except Exception as e:
            print(f"  ! {m['homeTeam']['name_ch']}: {e}", file=sys.stderr)
            continue
        if r.get("skip"):
            continue
        pick, reason = pick_one(r)
        # 只有 T-5 先真係落注。首預 / T-30 只作預測記錄。
        r["can_bet"] = (stage == "T-5")
        r["pick"] = pick if r["can_bet"] else None
        r["lead_view"] = pick          # 前兩段:模型傾向,但唔落注
        r["no_bet_reason"] = (reason if r["can_bet"]
                              else (f"{stage} 只作預測,落注統一喺 T-5"
                                    if pick else reason))
        results.append(r)
        tag = (f"{pick['label']} @{pick['odds']} ${pick['stake']:,.0f}"
               if pick else "觀望")
        flag = "落注" if r["can_bet"] and pick else "預測"
        print(f"{r['mins_to_ko']:6.0f}m {stage:4s} {flag} "
              f"{r['home'][:8]:9s}v {r['away'][:8]:9s} "
              f"信念{r['conviction']:4.1f}  {tag}")

    for r in results:
        snaps[str(r["match_id"])] = {
            "ts": dt.datetime.now(HKT).isoformat(timespec="seconds"),
            "stage": r["stage"], "fingerprint": r.pop("hk_fingerprint"),
            "final": r["final"], "conviction": r["conviction"],
            "pick": (r["pick"] or r.get("lead_view") or {}).get("label"),
        }
    json.dump(snaps, open(HK_SNAP, "w", encoding="utf-8"),
              ensure_ascii=False, indent=1)
    results.sort(key=lambda r: r["mins_to_ko"])
    json.dump(results, open(os.path.join(HERE, out), "w", encoding="utf-8"),
              ensure_ascii=False, indent=1)
    print(f"\n{len(results)} 場已處理 · {skipped} 場跳過(未到時點或已做過) → {out}")
    return results


if __name__ == "__main__":
    a = sys.argv[1:]
    mode = "due"
    if "--sweep" in a:
        mode = "sweep"
    elif "--all" in a:
        mode = "all"
    nums = [x for x in a if x.lstrip("-").isdigit()]
    hz = int(nums[0]) if nums else (2160 if mode == "sweep" else 60)
    main(horizon_min=hz, mode=mode, force="--force" in a)
