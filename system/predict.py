"""球賽預測引擎 — 由初盤、賠率移動、天氣、疲勞、陣容傷患合成賽果預判,
然後喺讓球 / 入球大小 / 角球大小 三個市場出一個結論(或觀望)。

設計原則
--------
1. 以 Pinnacle 現價去水後擬合出嘅 λ/μ 做「市場基準」。
2. 初盤(olv)同樣擬合一次,兩者之差 = 市場已吸收嘅資訊量與方向。
3. 我自己嘅情境調整(天氣、疲勞、賽程性質、陣容)以乘數形式加喺基準之上,
   每個乘數都要寫明理由,唔准無來由亂調。
4. 調整後重算比分矩陣,得出我對場波嘅完整預判。
5. 最後才睇馬會盤價,揀一注最符合我判斷而價錢唔蝕嘅,或者觀望。
"""
from __future__ import annotations
import datetime as dt
import json
import math
from dataclasses import dataclass, field, asdict

import model as M

BANKROLL = 50000.0
CAP_PCT = 0.04          # 單場上限 4%(階段一;實際由 staking.stage() 決定)
CONF_FLOOR = 58.0       # 信心度低於此不出注


# ═══════════════ 情境調整 ═══════════════
@dataclass
class Adj:
    """一條調整。goals/corners 係乘數,supremacy 係加法(主隊 λ 佔比偏移)。"""
    tag: str
    reason: str
    goals: float = 1.0
    corners: float = 1.0
    supremacy: float = 0.0
    confidence: float = 0.0   # 對信心度嘅影響(正=更有信心)


def weather_adj(wx: dict | None) -> list[Adj]:
    """天氣調整。

    實證基礎(重要):在 75,044 場樣本嘅研究中,只有【氣溫】對入球有顯著效應,
    而且方向反直覺 —— 天氣熱反而入球多。雨、風、濕度對入球嘅效應統計上唔顯著;
    角球方面完全搵唔到任何天氣預測因子嘅可靠研究。
    所以呢度只對氣溫落乘數,其餘天氣只作場景描述,一律 ×1.00 —
    寧願唔調,都好過用自己作出嚟嘅數字。
    (Mišák 2025, IES WP 07/2025, N=75,044)
    """
    out = []
    if not wx:
        out.append(Adj("天氣", "無法取得場地天氣", confidence=-3))
        return out
    p, w, g, t, hu = (wx["precip_mm_h"], wx["wind_kmh"], wx["gust_kmh"],
                      wx["temp_c"], wx["humidity"])
    desc = f"{wx['desc']} {t}°C 濕度{hu}% 風{w}km/h(陣風{g}) 雨{p}mm/h"

    if t > 22:
        out.append(Adj("氣溫偏高", f"{desc} — 氣溫 >22°C。大樣本研究顯示高溫時段總入球"
                                  f"反而高約 2.4%(節奏慢但防線鬆、體能下降令後段易失球)",
                       goals=1.024, confidence=+1))
    elif t >= 18:
        out.append(Adj("氣溫溫和", f"{desc} — 18–22°C,入球輕微偏高約 1.1%", goals=1.011))
    elif t < 6:
        out.append(Adj("氣溫偏低", f"{desc} — <6°C,入球輕微偏低約 1.1%", goals=0.989))
    else:
        out.append(Adj("氣溫中性", f"{desc} — 6–18°C,實證上對入球無顯著影響", goals=1.0))

    notes = []
    if p >= 1.0:
        notes.append(f"降雨 {p}mm/h")
    if g >= 40:
        notes.append(f"陣風 {g}km/h")
    if hu >= 85:
        notes.append(f"高濕 {hu}%")
    if notes:
        out.append(Adj("其他天氣(不調整)",
                       "、".join(notes) + " — 現有研究對雨/風/濕度同入球或角球嘅關係"
                       "都搵唔到統計上顯著嘅效應,所以刻意唔落乘數。"
                       "但方差會大啲,信心度略扣。",
                       confidence=-2))
    return out


def fatigue_adj(fh: dict, fa: dict, league: str) -> list[Adj]:
    """疲勞 / 賽程調整。

    實證基礎:休息日效應只喺【≤3 日】嘅極短休息區間先出現,而且係影響
    勝負機率(每多一日休息約 +4 個百分點),唔係直接影響入球數。
    ≥4 日休息嘅差異係確認嘅零效應 —— 所以唔會亂調。
    上仗加時嘅效應方向啱但統計上唔顯著(p≈0.13),只作軟性提示。
    (Scoppa 2013, IZA DP 7519)
    """
    out = []
    rh, ra = fh.get("rest_days"), fa.get("rest_days")
    if rh is None or ra is None:
        out.append(Adj("賽程", "無賽果資料,無法推算休息日", confidence=-4))
        return out
    if rh > 40 and ra > 40:
        if 60 <= rh <= 130 and 60 <= ra <= 130:
            out.append(Adj("開季首輪",
                           f"雙方上仗分別為 {fh['prev_date']} / {fa['prev_date']},"
                           f"即今仗係新季首輪。季初陣容磨合未成、新援未入位、"
                           f"教練戰術未定型,賽果方差明顯大於常態,而且我手上冇本季"
                           f"任何比賽數據可以驗證 —— 信心度大幅扣。",
                           confidence=-12))
        else:
            out.append(Adj("賽程資料殘缺",
                           f"推算休息日 {rh:.0f}/{ra:.0f} 日不合理,該聯賽賽果資料"
                           f"明顯不完整(僅 {fh.get('n')}/{fa.get('n')} 場),疲勞層不可用",
                           confidence=-8))
        return out

    out.append(Adj("休息日", f"主隊 {rh:.1f} 日(上仗 {fh['prev_date']})、"
                            f"客隊 {ra:.1f} 日(上仗 {fa['prev_date']});"
                            f"近 14 日場數 {fh['games_14d']} / {fa['games_14d']}"))

    # 只有其中一方進入 ≤3 日極短休息區間先計效應
    if min(rh, ra) <= 3.2 and abs(rh - ra) >= 1.0:
        days = min(2.0, abs(rh - ra))
        who = "主隊" if rh > ra else "客隊"
        shift = 0.08 * days * (1 if rh > ra else -1)
        out.append(Adj("極短休息劣勢",
                       f"{'客隊' if rh > ra else '主隊'}只休 {min(rh,ra):.1f} 日,"
                       f"進入 ≤3 日極短休息區間;{who}多休 {days:.1f} 日。"
                       f"實證上呢個區間每多一日休息約值 +4 個百分點勝率,"
                       f"折算約 {abs(shift):.2f} 球主客差,傾向{who}。",
                       supremacy=shift, confidence=+2))
    elif abs(rh - ra) >= 2.0:
        out.append(Adj("休息不均(不調整)",
                       f"休息日差 {abs(rh-ra):.1f} 日,但雙方都 ≥4 日。"
                       f"實證上 ≥4 日區間嘅休息差異係確認嘅零效應,故意唔調。"))

    if fh.get("prev_extra_time"):
        out.append(Adj("主隊上仗加時",
                       "主隊上仗打足 120 分鐘。此效應方向合理但統計上唔顯著(p≈0.13),"
                       "只落一個好細嘅軟性調整。", supremacy=-0.04, confidence=-2))
    if fa.get("prev_extra_time"):
        out.append(Adj("客隊上仗加時",
                       "客隊上仗打足 120 分鐘。此效應方向合理但統計上唔顯著(p≈0.13),"
                       "只落一個好細嘅軟性調整。", supremacy=0.04, confidence=-2))
    if (fh.get("games_14d") or 0) >= 4 or (fa.get("games_14d") or 0) >= 4:
        out.append(Adj("賽程密集",
                       f"14 日內場數 主{fh['games_14d']} / 客{fa['games_14d']} — "
                       f"輪換風險上升,首發難料,信心度扣。", confidence=-4))
    return out


def venue_adj(neutral: bool, league: str = "") -> list[Adj]:
    """中立場。實證基礎:疫情空場嘅自然實驗確認主場優勢實質存在且可量度,
    中立場應該把主場優勢抹掉。但莊家盤口通常已經反映咗一部分,
    所以只落一個中度偏移。(PMC8724651 系統性綜述, 26 項研究)"""
    if not neutral:
        return []
    return [Adj("中立場", "非主隊真正主場。實證上主場優勢實質存在,中立場應該大幅打折;"
                       "盤口未必完全反映",
                supremacy=-0.10, confidence=-4)]


def movement_adj(op: dict | None, now: dict) -> tuple[list[Adj], dict]:
    """初盤 → 現價 嘅移動,作為市場資訊增量。"""
    out, sig = [], {}
    if not op:
        out.append(Adj("賠率移動", "無初盤紀錄,無法量度市場移動", confidence=-5))
        return out, sig
    d_tot = now["total"] - op["total"]
    d_sup = now["supremacy"] - op["supremacy"]
    d_cnr = ((now.get("mu") or 0) - (op.get("mu") or 0)) if (now.get("mu") and op.get("mu")) else None
    sig = {"d_total": round(d_tot, 3), "d_sup": round(d_sup, 3),
           "d_corners": (round(d_cnr, 3) if d_cnr is not None else None),
           "open_total": round(op["total"], 3), "open_sup": round(op["supremacy"], 3)}

    def dirtxt(x, up, dn, unit="球"):
        return f"{up} {abs(x):.2f}{unit}" if x > 0 else f"{dn} {abs(x):.2f}{unit}"

    if abs(d_tot) >= 0.15:
        out.append(Adj("大盤被推動",
                       f"總入球初盤 {op['total']:.2f} → 現價 {now['total']:.2f}"
                       f"({dirtxt(d_tot,'升','跌')})。"
                       f"銳利盤大幅移動通常代表有實質資訊(陣容、傷患、天氣)入市,"
                       f"應該跟隨方向而非逆向。",
                       confidence=+5))
    elif abs(d_tot) >= 0.06:
        out.append(Adj("大盤微動",
                       f"總入球 {op['total']:.2f} → {now['total']:.2f}({dirtxt(d_tot,'升','跌')})",
                       confidence=+2))
    else:
        out.append(Adj("大盤平穩",
                       f"總入球 {op['total']:.2f} → {now['total']:.2f},幾乎無移動 — "
                       f"市場對入球預期穩定,無新資訊",
                       confidence=+3))

    if abs(d_sup) >= 0.15:
        who = "主隊" if d_sup > 0 else "客隊"
        out.append(Adj("讓球盤被推動",
                       f"主客差初盤 {op['supremacy']:+.2f} → 現價 {now['supremacy']:+.2f},"
                       f"錢明顯落{who}。銳利盤讓球移動 ≥0.15 球屬強信號。",
                       confidence=+5))
    elif abs(d_sup) >= 0.06:
        who = "主隊" if d_sup > 0 else "客隊"
        out.append(Adj("讓球微動", f"主客差 {op['supremacy']:+.2f} → {now['supremacy']:+.2f},略偏{who}",
                       confidence=+2))

    if d_cnr is not None and abs(d_cnr) >= 0.4:
        out.append(Adj("角球盤移動",
                       f"總角球初盤 {op['mu']:.2f} → 現價 {now['mu']:.2f}"
                       f"({dirtxt(d_cnr,'升','跌','個')})",
                       confidence=+3))
    return out, sig


# ═══════════════ 合成 ═══════════════
def apply(lh: float, la: float, mu: float | None, adjs: list[Adj]):
    """把所有調整乘/加落基準 λ、μ。supremacy 用等總量重分配。"""
    gm = 1.0
    cm = 1.0
    sup = 0.0
    for a in adjs:
        gm *= a.goals
        cm *= a.corners
        sup += a.supremacy
    base_total = lh + la
    total = base_total * gm
    # supremacy 偏移:保持總入球不變,調整主客分配
    base_share = lh / base_total if base_total > 0 else 0.5
    share = min(0.85, max(0.15, base_share + sup / 2))

    # 角球:實證上唔受天氣、讓球盤大細、控球率影響,只跟總入球期望走,
    # 彈性 ≈ 0.16 (arXiv 2112.13001, N=20,190,用嘅正正係 HKJC 數據)。
    # 所以角球唔可以獨立亂調 —— 佢係由入球調整推導出嚟。
    mu2 = None
    if mu is not None:
        elast = (total / base_total) ** 0.16 if base_total > 0 else 1.0
        mu2 = mu * elast * cm
    return (total * share, total * (1 - share), mu2,
            {"goals_mult": round(gm, 4), "corners_direct_mult": round(cm, 4),
             "corners_elasticity": round((total / base_total) ** 0.16, 4) if base_total else 1.0,
             "sup_shift": round(sup, 4)})


def fit_view(struct: dict) -> dict | None:
    """由一份賠率結構擬合出模型觀點。"""
    if not struct:
        return None
    try:
        lh, la, rho, rmse, n = M.fit_goals(struct)
    except Exception:
        return None
    if not lh or not la:
        return None
    v = {"lh": lh, "la": la, "rho": rho, "rmse": rmse, "n": n,
         "total": lh + la, "supremacy": lh - la}
    try:
        c = M.fit_corners(struct)
        if c and c[0]:
            v["mu"], v["phi"], v["c_rmse"], v["c_n"] = c[0], c[1], c[2], c[3]
    except Exception:
        pass
    return v


def outcome_probs(lh: float, la: float, rho: float, mu: float | None, phi: float | None):
    mat = M.score_matrix(lh, la, rho)
    gp = M.goals_pmf(mat)
    ph = sum(mat[i][j] for i in range(len(mat)) for j in range(len(mat)) if i > j)
    pd = sum(mat[i][i] for i in range(len(mat)))
    pa = 1 - ph - pd
    cp = M.corner_pmf(mu, phi) if (mu and phi) else None
    return mat, gp, cp, ph, pd, pa


# ═══════════════ 凱利(修正版:用 EV 定義) ═══════════════
def kelly_fraction(p_win: float, p_push: float, dec_odds: float) -> float:
    """走水盤正確凱利。b = dec_odds - 1,輸嘅機率 = 1 - p_win - p_push。
    最大化 E[log]:對 f 求導 → p*b/(1+f*b) - l/(1-f) = 0
    (走水部分本金不變,對 log 增長無貢獻,但要計入分母。)"""
    b = dec_odds - 1.0
    p, q = p_win, 1.0 - p_win - p_push
    if b <= 0 or p <= 0:
        return 0.0
    ev = p * b - q
    if ev <= 0:
        return 0.0
    lo, hi = 0.0, 0.999
    for _ in range(80):
        f = (lo + hi) / 2
        # d/df E[log] = p*b/(1+f*b) - q/(1-f)
        d = p * b / (1 + f * b) - q / (1 - f)
        if d > 0:
            lo = f
        else:
            hi = f
    return (lo + hi) / 2


def ev_pct(p_win: float, p_push: float, dec_odds: float) -> float:
    return p_win * (dec_odds - 1.0) - (1.0 - p_win - p_push)


if __name__ == "__main__":
    print("預測引擎模組 — 由 run_predict.py 驅動")
