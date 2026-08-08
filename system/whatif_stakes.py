"""比較舊規則(硬性最低賠率 1.50 + 4% 平頭上限)同新規則(無賠率門檻 +
賠率遞減上限)喺全板嘅注碼分佈差異。唯讀,唔會改帳本。"""
import model as M
import whatif_rules as W

OLD_MIN_ODDS = 1.50
BANK = 50000.0
FRAC = 1.0 / 3.0
CAP = 0.04
MULT = {"CHL": 0.5}
CODE = {"讓球": "HDC", "入球大小": "HIL", "角球大小": "CHL"}


class C:
    pass


def stake(odds, prob, push, conf, code, cap_mult):
    c = C()
    c.odds, c.prob, c.push, c.confidence, c.code = odds, prob, push, conf, code
    c.edge = 1.0
    shrink = 0.35 + 0.65 * (conf / 100.0)
    p = prob * shrink + (1 / odds) * (1 - shrink)
    denom = 1 - push
    if denom <= 1e-6:
        return 0.0, 0.0
    pe = min(0.98, p / denom)
    b = odds - 1
    f_full = max(0.0, (pe * b - (1 - pe)) / b) * denom
    f = f_full * FRAC * MULT.get(code, 1.0)
    f = min(f, CAP * cap_mult)
    return round(BANK * f / 10) * 10, f


def main():
    import json
    import os
    picks = json.load(open(os.path.join(W.HERE, "picks_all.json"),
                          encoding="utf8"))
    rows = W.main()
    print(f"{'市場':<6}{'選項':<16}{'賠率':>6}{'優勢':>8}{'信念':>7}"
          f"{'舊注':>9}{'新注':>9}{'上限乘數':>9}  賽事")
    to, tn, n_old, n_new = 0, 0, 0, 0
    for r in sorted(rows, key=lambda x: -x["edge_new"]):
        code = CODE.get(r["mk"], "HDC")
        prob = r["prob_new"]
        push = 0.0
        for p in picks:
            if p["lbl"] == r["lbl"] and abs(p["odds"] - r["odds"]) < 1e-9:
                push = p.get("push", 0.0)
                break
        edge, odds = r["edge_new"], r["odds"]
        # 舊規則:賠率門檻 1.50 + 平頭 4% 上限
        keep_old = edge >= 0.02 and odds >= OLD_MIN_ODDS
        s_old = stake(odds, prob, push, r["conf"], code, 1.0)[0] if keep_old else 0
        # 新規則:無賠率門檻 + 遞減上限
        keep_new = edge >= 0.02
        cm = M.odds_cap_mult(odds)
        s_new = stake(odds, prob, push, r["conf"], code, cm)[0] if keep_new else 0
        if s_new and s_new < 200:
            s_new = 0     # 最低注碼 $200
        if not (s_old or s_new):
            continue
        n_old += bool(s_old)
        n_new += bool(s_new)
        to += s_old
        tn += s_new
        print(f"{r['mk']:<6}{r['lbl'][:15]:<16}{odds:>6.2f}{edge*100:>7.1f}%"
              f"{r['conf']:>7.1f}{s_old:>9,.0f}{s_new:>9,.0f}{cm*100:>8.0f}%  "
              f"{r['match']}")
    print(f"\n舊規則:{n_old} 條 · 合計 ${to:,.0f} ({to/BANK*100:.1f}% 本金)")
    print(f"新規則:{n_new} 條 · 合計 ${tn:,.0f} ({tn/BANK*100:.1f}% 本金)")
    print(f"差異:{n_new-n_old:+d} 條 · ${tn-to:+,.0f}")


if __name__ == "__main__":
    main()
