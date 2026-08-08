"""新規則影響評估:對 picks_all.json 重新評分,比較舊規則 vs 新規則。

新規則兩項:
  1. MIN_ODDS = 1.60  —— 低過此賠率一律唔落注
  2. DISP_MIN = 1.35  —— 角球負二項離散度下限,重算角球機率
只做評估,唔會寫任何檔案、唔會建立注單。
"""
import json
import os

import model as M

HERE = os.path.dirname(os.path.abspath(__file__))
BANKROLL = 50000.0


def cond_of(lbl):
    """'角球大 9.5' -> '9.5' ;'角球細 13.5' -> '13.5'"""
    return lbl.split()[-1]


def side_of(lbl):
    return "H" if "大" in lbl else "L"


def recompute_corner(p):
    """用壓咗離散度下限嘅 phi 重算角球機率同 EV。"""
    mu, phi = p["fc"][0], p["fc"][1]
    phi2 = M.cap_phi(mu, phi)
    pmf = M.corner_pmf(mu, phi2)
    w, pu = M.ou_prob(pmf, cond_of(p["lbl"]), side_of(p["lbl"]))
    return w, pu, phi, phi2


def main():
    picks = json.load(open(os.path.join(HERE, "picks_all.json"), encoding="utf8"))
    rows = []
    for p in picks:
        odds, code = p["odds"], p["mk"]
        prob, push = p["prob"], p.get("push", 0.0)
        phi_old = phi_new = None
        if code == "角球大小":
            prob, push, phi_old, phi_new = recompute_corner(p)
        # 同 model.evaluate() 一致:edge = 賠率 / 模型公平賠率 - 1
        edge = odds / M.fair_odds(prob, push) - 1.0 if code == "角球大小" else p["edge"]
        drop = []
        if edge < 0.02:
            drop.append(f"優勢 {edge*100:+.1f}% < 2.0%")
        rows.append({
            "mk": code, "match": f"{p['h']} v {p['a']}", "lbl": p["lbl"],
            "odds": odds, "conf": p["conf"], "stake_old": p["stake"],
            "edge_old": p["edge"], "edge_new": edge,
            "prob_old": p["prob"], "prob_new": prob,
            "phi_old": phi_old, "phi_new": phi_new,
            "cap_mult": M.odds_cap_mult(odds),
            "keep": not drop, "why": " · ".join(drop),
        })
    return rows


if __name__ == "__main__":
    rows = main()
    for mk in ("讓球", "入球大小", "角球大小"):
        sub = [r for r in rows if r["mk"] == mk]
        if not sub:
            continue
        keep = [r for r in sub if r["keep"]]
        print(f"\n══ {mk} —— 舊 {len(sub)} 條 → 新 {len(keep)} 條")
        for r in sorted(sub, key=lambda x: -x["edge_old"]):
            tag = "保留" if r["keep"] else "剔除"
            extra = ""
            if r["phi_old"]:
                extra = (f"  φ {r['phi_old']:.1f}→{r['phi_new']:.1f}"
                         f"  機率 {r['prob_old']:.4f}→{r['prob_new']:.4f}")
            print(f"  [{tag}] {r['lbl']:<12} @{r['odds']:.2f}  "
                  f"優勢 {r['edge_old']*100:+.1f}%→{r['edge_new']*100:+.1f}%  "
                  f"信念 {r['conf']:.1f}  舊注 ${r['stake_old']:,}  "
                  f"{r['match']}{extra}")
            if not r["keep"]:
                print(f"          理由:{r['why']}")
    keep = [r for r in rows if r["keep"]]
    print(f"\n總計:舊規則 {len(rows)} 條 → 新規則 {len(keep)} 條 "
          f"(剔除 {len(rows) - len(keep)} 條)")
    print(f"舊注碼合計 ${sum(r['stake_old'] for r in rows):,.0f} · "
          f"保留部分 ${sum(r['stake_old'] for r in keep):,.0f}")
