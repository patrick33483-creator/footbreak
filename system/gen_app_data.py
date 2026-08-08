"""把 predictions.json + sim_ledger.json 打包成儀表板用嘅 data.json。

唔會叫任何外部 API — 純粹由已有輸出重算分佈。
"""
import json
import re
import os
import sys
import datetime as dt

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import model as M
import predict as P
import staking as K

OUT = os.path.join(os.path.dirname(HERE), "hkjc-dashboard", "data.json")
HKT = dt.timezone(dt.timedelta(hours=8))
DONE_MIN = 130.0        # 開賽後幾分鐘當完場,同 settle.py 一致


def dists(r):
    f, now = r["final"], r["now"]
    phi = now.get("phi")
    if f.get("mu") and phi:
        phi = M.cap_phi(f["mu"], phi)   # 舊記錄嘅 phi 未壓過離散度下限,顯示前補回
    mat = M.score_matrix(f["lh"], f["la"], f.get("rho") or 0.0)
    gp = M.goals_pmf(mat)
    cp = M.corner_pmf(f["mu"], phi) if (f.get("mu") and phi) else None

    # 壓縮:比分矩陣只留 0-6,總入球 0-7+,角球 0-20+
    N = 7
    m2 = [[round(mat[i][j], 5) for j in range(N)] for i in range(N)]
    g2 = [round(x, 5) for x in gp[:8]]
    g2[-1] = round(1 - sum(g2[:-1]), 5)
    c2 = None
    if cp:
        c2 = [round(x, 5) for x in cp[:21]]
        c2[-1] = round(max(0.0, 1 - sum(c2[:-1])), 5)

    tops = []
    for i in range(len(mat)):
        for j in range(len(mat[i])):
            tops.append((mat[i][j], i, j))
    tops.sort(reverse=True)
    return {
        "matrix": m2, "goals_dist": g2, "corners_dist": c2, "phi": phi,
        "top_scores": [{"s": f"{i}-{j}", "p": round(p, 5)} for p, i, j in tops[:8]],
    }


def _ou(dist, line):
    """由離散分佈計「大過 line」嘅機率(line 用 .5,唔會有走水)。"""
    if not dist:
        return None
    k = int(line) + 1
    return round(sum(dist[k:]), 4)


def _band(dist, lo=0.10, hi=0.90):
    """回傳 (中位數, lo 分位, hi 分位)。"""
    if not dist:
        return None
    out, c = [], 0.0
    q = {lo: None, 0.5: None, hi: None}
    for i, p in enumerate(dist):
        c += p
        for k in q:
            if q[k] is None and c >= k:
                q[k] = i
    n = len(dist) - 1
    return [q[0.5] if q[0.5] is not None else n,
            q[lo] if q[lo] is not None else 0,
            q[hi] if q[hi] is not None else n]


def forecast(r):
    """每場一份輕量「純預測」摘要 —— 唔理有冇注,淨係講模型估咩賽果。

    同 dists() 分開:dists() 出完整矩陣(重),只有今次真正跑過嘅場先有;
    forecast() 由 final 參數重新砌,連 archived 場都有,而且細好多。
    """
    f = r.get("final") or {}
    if not f.get("lh") or not f.get("la"):
        return None
    now = r.get("now") or {}
    phi = now.get("phi")
    if f.get("mu") and phi:
        phi = M.cap_phi(f["mu"], phi)
    mat = M.score_matrix(f["lh"], f["la"], f.get("rho") or 0.0)
    gp = M.goals_pmf(mat)
    cp = M.corner_pmf(f["mu"], phi) if (f.get("mu") and phi) else None

    ph = pd_ = pa = 0.0
    btts = 0.0
    tops = []
    for i, row in enumerate(mat):
        for j, p in enumerate(row):
            if i > j:
                ph += p
            elif i == j:
                pd_ += p
            else:
                pa += p
            if i and j:
                btts += p
            tops.append((p, i, j))
    tops.sort(reverse=True)

    g = [round(x, 5) for x in gp[:9]]
    g[-1] = round(max(0.0, 1 - sum(g[:-1])), 5)
    c = None
    if cp:
        c = [round(x, 5) for x in cp[:21]]
        c[-1] = round(max(0.0, 1 - sum(c[:-1])), 5)

    return {
        "lh": round(f["lh"], 3), "la": round(f["la"], 3),
        "total": round(f["lh"] + f["la"], 3),
        "sup": round(f["lh"] - f["la"], 3),
        "mu": round(f["mu"], 2) if f.get("mu") else None,
        "p": [round(ph, 4), round(pd_, 4), round(pa, 4)],
        "btts": round(btts, 4),
        "tops": [{"s": f"{i}-{j}", "p": round(p, 4)} for p, i, j in tops[:5]],
        "goals": g, "gband": _band(gp),
        "o15": _ou(gp, 1.5), "o25": _ou(gp, 2.5), "o35": _ou(gp, 3.5),
        "corners": c, "cband": _band(cp) if cp else None,
        "c85": _ou(cp, 8.5) if cp else None,
        "c95": _ou(cp, 9.5) if cp else None,
        "c105": _ou(cp, 10.5) if cp else None,
    }


# --------------------------------------------------------- 讓球標籤正規化
# 馬會 HDC condition 係主隊視角,舊記錄嘅 label 直接印咗原文,買客隊時
# 容易被誤讀。呢層純顯示改寫,唔會動 bet_id / condition / 結算邏輯。

_RE_HDCTXT = re.compile(r"\s*(?:(?:受讓|讓)\s*[0-9.]+|平手)\s*$")
_RE_PAREN = re.compile(r"[（(]\s*馬會盤[^）)]*[）)]\s*$")


def _fix_one(label, home, away, side=None, cond=None):
    """把讓球標籤正規化成「隊名 讓/受讓 X(馬會盤 <選邊視角盤口>)」。

    冪等:已經係新格式嘅標籤會先剝走括弧同讓球描述,再重新組裝。
    """
    if not isinstance(label, str):
        return label
    body = _RE_PAREN.sub("", label).strip()
    pre = ""
    if body.startswith("讓球"):
        pre, body = "讓球 ", body[2:].strip()
    body = _RE_HDCTXT.sub("", body).strip()      # 剝走舊有「受讓 0.25」/「平手」
    c = cond
    if c is None:
        parts = body.rsplit(" ", 1)
        if len(parts) != 2:
            return label
        body, c = parts[0].strip(), parts[1].strip()
    if cond is not None:
        parts = body.rsplit(" ", 1)
        if len(parts) == 2 and M.hdc_value(parts[1].strip()) is not None:
            body = parts[0]                      # 剝走舊有原始盤口尾巴
    team = body.strip()
    if not team or M.hdc_value(c) is None:
        return label
    s2 = side or ("H" if team == home else "A" if team == away else None)
    if s2 is None:
        return label
    return pre + M.hdc_label(team, c, s2)


def fix_hdc(obj, home=None, away=None):
    """遞歸改寫任何讓球市場嘅 label 成「隊名 讓/受讓 X(馬會盤 …)」。"""
    if isinstance(obj, list):
        for x in obj:
            fix_hdc(x, home, away)
        return obj
    if not isinstance(obj, dict):
        return obj
    h = obj.get("home") or home
    a = obj.get("away") or away
    lbl = obj.get("label")
    if isinstance(lbl, str):
        is_hdc = (obj.get("code") == "HDC" or obj.get("market") == "讓球"
                  or lbl.startswith("讓球 "))
        if is_hdc:
            obj["label"] = _fix_one(lbl, h, a, obj.get("side"),
                                    obj.get("condition"))
    for k in ("from", "to"):
        if isinstance(obj.get(k), str) and obj[k].startswith("讓球 "):
            obj[k] = _fix_one(obj[k], h, a)
    for v in obj.values():
        if isinstance(v, (dict, list)):
            fix_hdc(v, h, a)
    return obj


def main():
    preds = json.load(open(os.path.join(HERE, "predictions.json"), encoding="utf-8"))
    lp = os.path.join(HERE, "sim_ledger.json")
    led = json.load(open(lp, encoding="utf-8")) if os.path.exists(lp) else {"bets": [], "log": []}

    for r in preds:
        try:
            r["dist"] = dists(r)
        except Exception as e:
            r["dist"] = None
            r["dist_error"] = str(e)
        # 瘦身:候選只留 EV 頭 12 個
        r["candidates"] = sorted(r.get("candidates") or [],
                                 key=lambda c: -c["ev"])[:12]

    # 把三段預測記錄掛返落每一場
    watch = led.get("watch", {})
    for r in preds:
        w = watch.get(str(r["match_id"]))
        r["stages"] = (w or {}).get("stages", [])
    # 只出現喺 watch 但今次冇跑到嘅場,也要保留(例如已過 T-5)
    have = {str(r["match_id"]) for r in preds}
    for mid, w in watch.items():
        if mid in have or not w.get("stages"):
            continue
        last = w["stages"][-1]
        preds.append({
            "match_id": mid, "home": w["home"], "away": w["away"],
            "home_en": w.get("home_en"), "away_en": w.get("away_en"),
            "league": w["league"], "kickoff_hkt": w["kickoff"],
            "venue": w.get("venue"), "venue_city": w.get("venue_city"),
            "stage": last["stage"], "conviction": last.get("conviction"),
            "final": last.get("final"), "open": last.get("open"),
            "now": last.get("now"), "movement": last.get("movement"),
            "adjustments": last.get("adjustments") or [],
            "mults": last.get("mults"), "outcome": last.get("outcome"),
            "candidates": [], "pick": last.get("pick") if last.get("can_bet") else None,
            "no_bet_reason": last.get("no_bet_reason"),
            "stages": w["stages"], "archived": True, "dist": None,
        })
    # 已完結嘅賽事唔擺喺面板(開賽 + DONE_MIN 分鐘後當完場)。
    # 記錄仍然留喺 predictions.json / sim_ledger.json,只係唔顯示。
    _now = dt.datetime.now(HKT)
    def _ended(r):
        try:
            ko = dt.datetime.fromisoformat(str(r.get("kickoff_hkt")))
        except Exception:
            return False
        if ko.tzinfo is None:
            ko = ko.replace(tzinfo=HKT)
        return (_now - ko).total_seconds() / 60.0 >= DONE_MIN
    n_all = len(preds)
    preds = [r for r in preds if not _ended(r)]
    n_hidden = n_all - len(preds)
    preds.sort(key=lambda r: r["kickoff_hkt"])

    # 純預測摘要 —— 每場都有(包括 archived),同有冇注完全無關
    n_fc = 0
    for r in preds:
        try:
            r["fc"] = forecast(r)
            n_fc += 1 if r["fc"] else 0
        except Exception:
            r["fc"] = None

    bets = led.get("bets", [])
    pend = [b for b in bets if b["status"] == "PENDING"]
    done = [b for b in bets if b["status"] == "SETTLED"]
    bank = led.get("bankroll", P.BANKROLL)
    S = led.get("stats") or {}
    _STK = K.stage()
    _NTF = {"last_sent": None, "n_bets": 0, "n_settled": 0,
            "n_queue": 0, "n_sweeps": 0, "last_sweep": None}
    _nf = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                       "notify_state.json")
    if os.path.exists(_nf):
        try:
            with open(_nf, encoding="utf-8") as _f:
                _ns = json.load(_f)
            _NTF = {"last_sent": _ns.get("last_sent"),
                    "n_bets": len(_ns.get("bets") or []),
                    "n_settled": len(_ns.get("settled") or []),
                    "n_queue": len(_ns.get("queue") or []),
                    "n_sweeps": len(_ns.get("sweeps") or []),
                    "last_sweep": (_ns.get("sweeps") or [None])[-1]}
        except Exception:
            pass
    ledger = {
        "bankroll": bank,
        "bets": sorted(bets, key=lambda b: b["kickoff"]),
        "log": led.get("log", [])[-30:],
        "stats": {
            "n_pending": len(pend),
            "n_voided": sum(1 for b in bets if b["status"] == "VOIDED"),
            "n_settled": len(done),
            "open_stake": sum(b["stake"] for b in pend),
            "open_pct": (sum(b["stake"] for b in pend) / bank) if bank else 0,
            "pnl": sum(b.get("pnl") or 0 for b in done),
            "turnover": sum(b["stake"] for b in done),
            "roi": (sum(b.get("pnl") or 0 for b in done)
                    / sum(b["stake"] for b in done)) if done else None,
            "n_won": sum(1 for b in done if (b.get("pnl") or 0) > 0),
            "n_lost": sum(1 for b in done if (b.get("pnl") or 0) < 0),
            "n_decided": S.get("n_decided", 0),
            "hits": S.get("hits", 0),
            "hit_rate": S.get("hit_rate"),
            "equity": S.get("equity", bank),
            "by_market": S.get("by_market", {}),
            "curve": S.get("curve", []),
            "res_counts": {k: sum(1 for b in done if b.get("result") == k)
                           for k in ("Won", "Half Won", "Refunded",
                                     "Half Lost", "Lost")},
            "daily_cap": bank * 1.00,
            "open_cap": bank * 1.00,
            "single_cap_pct": _STK["cap"],
            "conf_floor": P.CONF_FLOOR,
            "staking": _STK,
            "notify": _NTF,
            "bet_stage": "T-5",
            "n_watch": len(watch),
            "n_stage_preds": sum(len(w.get("stages") or []) for w in watch.values()),
        },
    }

    # 純預測準繩度記分板(accuracy.py 出,冇就當冇)
    acc = None
    _ap = os.path.join(HERE, "accuracy.json")
    if os.path.exists(_ap):
        try:
            with open(_ap, encoding="utf-8") as _f:
                acc = json.load(_f)
        except Exception:
            acc = None

    out = {
        "generated_at": dt.datetime.now(HKT).isoformat(timespec="seconds"),
        "n_hidden_ended": n_hidden,
        "matches": fix_hdc(preds),
        "ledger": fix_hdc(ledger),
        "accuracy": acc,
    }
    os.makedirs(os.path.dirname(OUT), exist_ok=True)
    json.dump(out, open(OUT, "w", encoding="utf-8"), ensure_ascii=False,
              separators=(",", ":"))
    kb = os.path.getsize(OUT) / 1024
    _an = f" · 準繩度 {acc['n_matches']} 場" if acc else ""
    print(f"{len(preds)} 場(隱藏已完結 {n_hidden} 場) · 純預測 {n_fc} 場{_an} · "
          f"{len(bets)} 注 → {OUT} ({kb:.0f} KB)")


if __name__ == "__main__":
    main()
