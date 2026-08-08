# -*- coding: utf-8 -*-
"""純預測報告 —— 唔理 EV、唔理落唔落注,單純講模型估個場係咩賽果。

用法:
  python3 forecast.py                 # 未開賽 + 信念 >= 58
  python3 forecast.py --min-conf 0    # 全部未開賽
  python3 forecast.py --top 10        # 只出信念最高 10 場
  python3 forecast.py --json          # 出 JSON
"""
import json, sys, argparse, datetime as dt

HKT = dt.timezone(dt.timedelta(hours=8))
DATA = "/home/user/workspace/hkjc-dashboard/data.json"


def cum(dist):
    out, s = [], 0.0
    for p in dist:
        s += p
        out.append(s)
    return out


def pct_at(dist, q):
    """返回累積機率首次 >= q 嘅索引。"""
    s = 0.0
    for i, p in enumerate(dist):
        s += p
        if s >= q:
            return i
    return len(dist) - 1


def ou_prob(dist, line):
    """大於 line 嘅機率(line 通常係 x.5)。"""
    import math
    k = math.floor(line)
    return 1.0 - sum(dist[: k + 1])


def wdl(matrix):
    h = d = a = 0.0
    for i, row in enumerate(matrix):
        for j, p in enumerate(row):
            if i > j:
                h += p
            elif i == j:
                d += p
            else:
                a += p
    return h, d, a


def rebuild(f):
    """首預場次冇存 dist,由 final 參數重建分佈。"""
    try:
        sys.path.insert(0, "/home/user/workspace/hkjc")
        import model as M
    except Exception:
        return None, None, None
    lh, la, rho = f.get("lh"), f.get("la"), f.get("rho", 0.0)
    if lh is None or la is None:
        return None, None, None
    mx = M.score_matrix(lh, la, rho)
    gd = M.goals_pmf(mx)
    cd = None
    mu = f.get("mu")
    if mu:
        try:
            cd = M.corner_pmf(mu, M.cap_phi(mu, 60.0))
        except Exception:
            cd = None
    return mx, gd, cd


def analyse(m):
    dd = m.get("dist") or {}
    mx = dd.get("matrix")
    gd = dd.get("goals_dist")
    cd = dd.get("corners_dist")
    f = m.get("final") or {}
    if not mx or not gd:
        mx, gd, cd = rebuild(f)
    if not mx or not gd:
        return None

    h, d, a = wdl(mx)
    tops = dd.get("top_scores")
    if not tops:
        flat = sorted(((i, j, p) for i, r in enumerate(mx) for j, p in enumerate(r)),
                      key=lambda t: -t[2])[:8]
        tops = [{"s": f"{i}-{j}", "p": p} for i, j, p in flat]
    # 最大機率主客走向嘅代表比分
    best_h = max(((i, j, p) for i, r in enumerate(mx) for j, p in enumerate(r) if i > j),
                 key=lambda t: t[2], default=None)
    best_a = max(((i, j, p) for i, r in enumerate(mx) for j, p in enumerate(r) if i < j),
                 key=lambda t: t[2], default=None)
    best_d = max(((i, i, mx[i][i]) for i in range(min(len(mx), len(mx[0])))),
                 key=lambda t: t[2], default=None)

    out = {
        "home": m["home"], "away": m["away"], "league": m.get("league"),
        "ko": m.get("kickoff_hkt"), "stage": m.get("stage"),
        "conf": m.get("conviction"),
        "p_home": h, "p_draw": d, "p_away": a,
        "lh": f.get("lh"), "la": f.get("la"), "total": f.get("total"),
        "supremacy": f.get("supremacy"),
        "top_scores": tops[:5],
        "best_home": best_h, "best_draw": best_d, "best_away": best_a,
        "g_med": pct_at(gd, 0.5),
        "g_p10": pct_at(gd, 0.10), "g_p90": pct_at(gd, 0.90),
        "ou25": ou_prob(gd, 2.5), "ou35": ou_prob(gd, 3.5),
        "btts": None,
    }
    # 兩隊入球
    btts = sum(mx[i][j] for i in range(1, len(mx)) for j in range(1, len(mx[0])))
    out["btts"] = btts

    if cd and f.get("mu"):
        out["c_mu"] = f.get("mu")
        out["c_med"] = pct_at(cd, 0.5)
        out["c_p10"] = pct_at(cd, 0.10)
        out["c_p90"] = pct_at(cd, 0.90)
        out["c_o95"] = ou_prob(cd, 9.5)
        out["c_o105"] = ou_prob(cd, 10.5)

    # 模型最傾向邊個市場(唔理過唔過關)
    st = m.get("stages") or []
    if st:
        lead = (st[-1] or {}).get("lead")
        if lead:
            out["lead"] = lead
    out["no_bet_reason"] = m.get("no_bet_reason")
    out["pick"] = m.get("pick")
    return out


def fmt(r):
    L = []
    ko = (r["ko"] or "")[11:16]
    L.append(f"【{ko}】{r['home']} v {r['away']}  ·  {r['league']}  ·  信念 {r['conf']:.1f}  ·  {r['stage']}")
    L.append(f"  主勝 {r['p_home']*100:.1f}%  和 {r['p_draw']*100:.1f}%  客勝 {r['p_away']*100:.1f}%"
             f"   |  預期入球 {r['lh']:.2f} - {r['la']:.2f}(合共 {r['total']:.2f},主客差 {r['supremacy']:+.2f})")
    ts = "  ".join(f"{s['s']} {s['p']*100:.1f}%" for s in r["top_scores"])
    L.append(f"  最可能比分  {ts}")
    bh, bd, ba = r["best_home"], r["best_draw"], r["best_away"]
    L.append(f"  各走向代表比分  主勝 {bh[0]}-{bh[1]} ({bh[2]*100:.1f}%)  ·  和 {bd[0]}-{bd[1]} ({bd[2]*100:.1f}%)"
             f"  ·  客勝 {ba[0]}-{ba[1]} ({ba[2]*100:.1f}%)")
    L.append(f"  總入球  中位 {r['g_med']}  ·  八成落喺 {r['g_p10']}–{r['g_p90']} 球"
             f"  ·  大2.5 {r['ou25']*100:.0f}%  大3.5 {r['ou35']*100:.0f}%  兩隊入球 {r['btts']*100:.0f}%")
    if r.get("c_med") is not None:
        L.append(f"  總角球  期望 {r['c_mu']:.1f}  ·  中位 {r['c_med']}  ·  八成落喺 {r['c_p10']}–{r['c_p90']} 個"
                 f"  ·  大9.5 {r['c_o95']*100:.0f}%  大10.5 {r['c_o105']*100:.0f}%")
    ld = r.get("lead")
    if ld:
        L.append(f"  模型最傾向  {ld['market']} {ld['label']} @{ld['odds']}  模型 {ld['prob']*100:.1f}%  EV {ld['ev']*100:+.2f}%")
    if r.get("pick"):
        p = r["pick"]
        L.append(f"  ★ 已落注  {p.get('label')} @{p.get('odds')} ${p.get('stake')}")
    elif r.get("no_bet_reason"):
        L.append(f"  ✗ 唔買原因  {r['no_bet_reason']}")
    return "\n".join(L)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--min-conf", type=float, default=58.0)
    ap.add_argument("--top", type=int, default=0)
    ap.add_argument("--json", action="store_true")
    ap.add_argument("--all", action="store_true", help="包括已開賽")
    a = ap.parse_args()

    d = json.load(open(DATA, encoding="utf8"))
    now = dt.datetime.now(HKT)
    rows = []
    for m in d["matches"]:
        ko = m.get("kickoff_hkt")
        if not a.all and ko:
            try:
                if dt.datetime.fromisoformat(ko) <= now:
                    continue
            except Exception:
                pass
        if (m.get("conviction") or 0) < a.min_conf:
            continue
        r = analyse(m)
        if r:
            rows.append(r)

    rows.sort(key=lambda r: -(r["conf"] or 0))
    if a.top:
        rows = rows[: a.top]

    if a.json:
        print(json.dumps(rows, ensure_ascii=False, indent=1))
        return
    print(f"═══ 純預測報告 · {now:%Y-%m-%d %H:%M} HKT · {len(rows)} 場(信念 ≥ {a.min_conf:g},未開賽) ═══\n")
    for r in rows:
        print(fmt(r))
        print()


if __name__ == "__main__":
    main()
