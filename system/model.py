"""HKJC 足球預測引擎.

核心思路
--------
1. 以 Dixon-Coles 修正的雙變量 Poisson 為底層入球模型。
2. 用 HKJC 自身的「整個賠率面」(主客和 HAD + 全部讓球線 HDC + 全部大小線 HIL)
   反解出一組 (lambda_home, lambda_away, rho) —— 等於做一次「跨盤口去水佰」。
   單一盤口去水佰只能得出該盤的市場機率;用整個賠率面聯合擬合,可以找出
   個別線與整體分佈不一致的地方,這些就是內部錯價 (stale alternate lines)。
3. 角球用負二項分佈 (Poisson 過度離散) 擬合 CHL 全部線。
4. Edge = HKJC 賠率 / 模型公平賠率 - 1,再按信心度收縮後計凱利注碼。
"""
import math
from dataclasses import dataclass, field, asdict

MAXG = 12          # 入球矩陣上限
MAXC = 30          # 角球上限


# ---------------------------------------------------------------- 分佈

def _lfact(n, _c={}):
    if n not in _c:
        _c[n] = math.lgamma(n + 1)
    return _c[n]


def pois(k, lam):
    if lam <= 0:
        return 1.0 if k == 0 else 0.0
    return math.exp(k * math.log(lam) - lam - _lfact(k))


def dc_tau(i, j, lh, la, rho):
    """Dixon-Coles 低比數修正。"""
    if i == 0 and j == 0:
        return 1 - lh * la * rho
    if i == 0 and j == 1:
        return 1 + lh * rho
    if i == 1 and j == 0:
        return 1 + la * rho
    if i == 1 and j == 1:
        return 1 - rho
    return 1.0


def score_matrix(lh, la, rho=0.0):
    m = [[0.0] * (MAXG + 1) for _ in range(MAXG + 1)]
    ph = [pois(i, lh) for i in range(MAXG + 1)]
    pa = [pois(j, la) for j in range(MAXG + 1)]
    tot = 0.0
    for i in range(MAXG + 1):
        for j in range(MAXG + 1):
            v = ph[i] * pa[j] * dc_tau(i, j, lh, la, rho)
            v = max(v, 0.0)
            m[i][j] = v
            tot += v
    if tot > 0:
        for i in range(MAXG + 1):
            for j in range(MAXG + 1):
                m[i][j] /= tot
    return m


def negbin(k, mu, phi):
    """負二項 (mu 平均, phi 離散度; phi -> inf 即 Poisson)。"""
    if phi > 1e6:
        return pois(k, mu)
    r = phi
    p = r / (r + mu)
    return math.exp(math.lgamma(k + r) - math.lgamma(r) - _lfact(k)
                    + r * math.log(p) + k * math.log(1 - p))


DISP_MIN = 1.35   # 角球最低離散度 (variance / mean)。實證足球角球過度離散,
                  # 盤口反推出嚟嘅 phi 經常太樂觀,呢度設下限令尾部加厚。


def cap_phi(mu, phi):
    """把負二項 phi 壓到令 var/mean >= DISP_MIN。

    負二項:var = mu + mu^2/phi,故 var/mean = 1 + mu/phi。
    要 1 + mu/phi >= DISP_MIN,即 phi <= mu / (DISP_MIN - 1)。
    """
    if not mu or mu <= 0 or DISP_MIN <= 1.0:
        return phi
    hi = mu / (DISP_MIN - 1.0)
    return min(phi, hi) if phi else hi


def corner_pmf(mu, phi=8.0):
    v = [negbin(k, mu, phi) for k in range(MAXC + 1)]
    s = sum(v)
    return [x / s for x in v]


# ---------------------------------------------------------------- 盤口結算

def parse_condition(cond):
    """'0.0/-0.5' -> [0.0, -0.5];  '2.5' -> [2.5]"""
    if cond is None:
        return []
    out = []
    for part in str(cond).split("/"):
        part = part.strip().replace("+", "")
        if part in ("", "-"):
            continue
        try:
            out.append(float(part))
        except ValueError:
            return []
    # OpticOdds 用小數表示四分盤 (-0.75),HKJC 用斜線 (-0.5/-1.0)。
    # 單一值而小數部分是 .25/.75 時,展開成兩道半盤。
    if len(out) == 1:
        v = out[0]
        frac = abs(v) - math.floor(abs(v))
        if abs(frac - 0.25) < 1e-6 or abs(frac - 0.75) < 1e-6:
            return [v - 0.25, v + 0.25]
    return out


def ah_prob(mat, hcap, side):
    """亞洲讓球贏面。hcap 為主隊角度(負數=主隊讓)。
    回傳 (win, push, half_win, half_loss) 摺算成期望回報係數用的 P。
    這裡直接回傳「等值勝出機率」:全贏=1, 走水=退本(以剔除計), 半贏=0.5。
    """
    legs = parse_condition(hcap) or [0.0]
    w = p = 0.0
    for h in legs:
        for i in range(MAXG + 1):
            for j in range(MAXG + 1):
                margin = (i - j) + h if side == "H" else (j - i) - h
                pr = mat[i][j]
                if margin > 1e-9:
                    w += pr / len(legs)
                elif abs(margin) < 1e-9:
                    p += pr / len(legs)
    return w, p


def ou_prob(pmf, line, side):
    """大小球。side 'H'=大(over) 'L'=小(under)。"""
    legs = parse_condition(line) or [2.5]
    w = p = 0.0
    for L in legs:
        for k, pr in enumerate(pmf):
            d = k - L
            if (d > 0 and side == "H") or (d < 0 and side == "L"):
                w += pr / len(legs)
            elif abs(d) < 1e-9:
                p += pr / len(legs)
    return w, p


def goals_pmf(mat):
    v = [0.0] * (2 * MAXG + 1)
    for i in range(MAXG + 1):
        for j in range(MAXG + 1):
            v[i + j] += mat[i][j]
    return v


def fair_odds(win, push):
    """走水部分退本 => 公平賠率 o 滿足 win*(o-1) + push*0 - (1-win-push) = 0"""
    lose = 1 - win - push
    if win <= 1e-9:
        return 999.0
    return 1 + lose / win


# ---------------------------------------------------------------- 擬合

def _lines_of(odds, key):
    return [ln for ln in odds.get(key, []) if ln.get("odds")]


def devig_power(vals):
    """Power 去水法:找 k 使 sum(q_i**k)=1,q_i=1/o_i。

    比等比例歸一化更能處理熱門-冷門偏誤,適用於 Pinnacle 副線。
    """
    q = [1.0 / v for v in vals]
    lo, hi = 0.5, 4.0
    for _ in range(60):
        k = (lo + hi) / 2
        s = sum(x ** k for x in q)
        if s > 1.0:
            lo = k
        else:
            hi = k
    k = (lo + hi) / 2
    out = [x ** k for x in q]
    t = sum(out)
    return [x / t for x in out]


def _cond_mid(cond):
    """取條件中間值 (quarter line 取平均)。解不到回傳 None。"""
    parts = parse_condition(cond)
    if not parts:
        return None
    return sum(parts) / len(parts)


def _central(lines, span):
    """只保留距離平手線 span 以内的盤口,避免極端副線拉歪擬合。"""
    if not lines:
        return []
    usable = [(ln, _cond_mid(ln.get("condition"))) for ln in lines]
    usable = [(ln, m) for ln, m in usable if m is not None]
    if not usable:
        return []
    anchor = next((m for ln, m in usable if ln.get("main")), None)
    if anchor is None:
        mids = sorted(m for _, m in usable)
        anchor = mids[len(mids) // 2]
    return [ln for ln, m in usable if abs(m - anchor) <= span + 1e-9]


def fit_goals(odds):
    """由 HAD + HDC + HIL 全部線,擬合 (lh, la, rho)。回傧 (lh, la, rho, rmse, n_lines)"""
    targets = []   # (kind, cond, side, market_prob)

    def add_book(lines, kind, sides):
        for ln in lines:
            o = ln["odds"]
            vals = [o.get(s) for s in sides]
            if any(v is None or v <= 1.0 for v in vals):
                continue
            tot = sum(1.0 / v for v in vals)
            if not (0.9 < tot < 1.6):
                continue
            probs = devig_power(vals)
            # 權重:越接近平手線越可靠
            wgt = 1.0 if ln.get("main") else 0.5
            for s, q in zip(sides, probs):
                targets.append((kind, ln.get("condition"), s, q, wgt))

    add_book(_lines_of(odds, "HAD"), "HAD", ["H", "D", "A"])
    add_book(_central(_lines_of(odds, "HDC"), 0.75), "HDC", ["H", "A"])
    add_book(_central(_lines_of(odds, "HIL"), 1.0), "HIL", ["H", "L"])

    if not targets:
        return None

    def model_prob(lh, la, rho):
        mat = score_matrix(lh, la, rho)
        gp = goals_pmf(mat)
        out = []
        for kind, cond, side, q, wgt in targets:
            if kind == "HAD":
                if side == "H":
                    p = sum(mat[i][j] for i in range(MAXG + 1) for j in range(i))
                elif side == "A":
                    p = sum(mat[i][j] for i in range(MAXG + 1) for j in range(i + 1, MAXG + 1))
                else:
                    p = sum(mat[i][i] for i in range(MAXG + 1))
            elif kind == "HDC":
                w, pu = ah_prob(mat, cond, side)
                p = w / (1 - pu) if pu < 0.999 else 0.5
            else:
                w, pu = ou_prob(gp, cond, side)
                p = w / (1 - pu) if pu < 0.999 else 0.5
            out.append(p)
        return out

    def loss(params):
        lh, la, rho = params
        if lh <= 0.05 or la <= 0.05 or lh > 6 or la > 6 or abs(rho) > 0.25:
            return 1e9
        pm = model_prob(lh, la, rho)
        s = wt = 0.0
        for (kind, cond, side, q, wgt), p in zip(targets, pm):
            p = min(max(p, 1e-6), 1 - 1e-6)
            s += wgt * (math.log(p / (1 - p)) - math.log(q / (1 - q))) ** 2
            wt += wgt
        return s / wt

    # Nelder-Mead 簡易實作 (避免依賴 scipy)
    best = _nelder_mead(loss, [1.35, 1.15, -0.03],
                        steps=[0.35, 0.35, 0.05], iters=260)
    lh, la, rho = best
    rmse = math.sqrt(loss(best))
    return lh, la, rho, rmse, len(targets)


def fit_corners(odds):
    """由 CHL 全部線擬合角球平均數 mu。"""
    lines = _central(_lines_of(odds, "CHL"), 2.0)
    obs = []
    for ln in lines:
        o = ln["odds"]
        h, l = o.get("H"), o.get("L")
        if not h or not l or h <= 1 or l <= 1:
            continue
        tot = 1 / h + 1 / l
        if not (0.9 < tot < 1.6):
            continue
        wgt = 1.0 if ln.get("main") else 0.5
        obs.append((ln.get("condition"), devig_power([h, l])[0], wgt))
    if not obs:
        return None

    def loss(params):
        mu, phi = params
        if mu < 3 or mu > 20 or phi < 2 or phi > 60:
            return 1e9
        pmf = corner_pmf(mu, phi)
        s = wt = 0.0
        for cond, q, wgt in obs:
            w, pu = ou_prob(pmf, cond, "H")
            p = w / (1 - pu) if pu < 0.999 else 0.5
            p = min(max(p, 1e-6), 1 - 1e-6)
            s += wgt * (math.log(p / (1 - p)) - math.log(q / (1 - q))) ** 2
            wt += wgt
        return s / wt

    if len(obs) == 1:
        best = _nelder_mead(lambda x: loss([x[0], 9.0]), [10.0], [1.5], 120)
        mu, phi = best[0], 9.0
    else:
        mu, phi = _nelder_mead(loss, [10.0, 9.0], [1.5, 3.0], 220)
    rmse = math.sqrt(loss([mu, phi]))
    # 擬合完先壓離散度下限,唔喺 loss 入面壓,以免扭曲 mu 嘅擬合。
    phi = cap_phi(mu, phi)
    return mu, phi, rmse, len(obs)


def _nelder_mead(f, x0, steps, iters=200):
    n = len(x0)
    pts = [list(x0)]
    for i in range(n):
        p = list(x0)
        p[i] += steps[i]
        pts.append(p)
    vals = [f(p) for p in pts]
    for _ in range(iters):
        order = sorted(range(n + 1), key=lambda k: vals[k])
        pts = [pts[k] for k in order]
        vals = [vals[k] for k in order]
        if abs(vals[-1] - vals[0]) < 1e-12:
            break
        cen = [sum(p[i] for p in pts[:-1]) / n for i in range(n)]
        ref = [cen[i] + 1.0 * (cen[i] - pts[-1][i]) for i in range(n)]
        fr = f(ref)
        if fr < vals[0]:
            exp = [cen[i] + 2.0 * (cen[i] - pts[-1][i]) for i in range(n)]
            fe = f(exp)
            pts[-1], vals[-1] = (exp, fe) if fe < fr else (ref, fr)
        elif fr < vals[-2]:
            pts[-1], vals[-1] = ref, fr
        else:
            con = [cen[i] + 0.5 * (pts[-1][i] - cen[i]) for i in range(n)]
            fc = f(con)
            if fc < vals[-1]:
                pts[-1], vals[-1] = con, fc
            else:
                for k in range(1, n + 1):
                    pts[k] = [(pts[k][i] + pts[0][i]) / 2 for i in range(n)]
                    vals[k] = f(pts[k])
    return pts[0]


# ---------------------------------------------------------------- 估值

@dataclass
class Candidate:
    market: str          # 讓球 / 大小 / 角球大小
    code: str            # HDC / HIL / CHL
    condition: str
    side: str            # H / A / L
    label: str           # 中文描述
    odds: float
    fair: float
    prob: float
    push: float
    edge: float
    confidence: float = 0.0
    kelly_frac: float = 0.0
    kelly_full: float = 0.0
    stake: float = 0.0
    is_main: bool = False
    note: str = ""


SIDE_CN = {"H": "主", "A": "客", "L": "細"}


def _fmt_cond(c):
    return str(c).replace("+", "+")


# --- 讓球視角轉換 -------------------------------------------------------
# 馬會 feed 嘅 HDC condition 一律係「主隊視角」:正數 = 主隊受讓,
# 負數 = 主隊讓。買客隊時條件唔會自動反號,所以顯示要自己轉,
# 否則「客隊 +0.5/+1.0」會被誤讀成客隊受讓(實際係客隊讓 0.75)。

def hdc_value(condition, side="H"):
    """回傳指定一方嘅讓球值:正數 = 受讓,負數 = 讓。無法解析回 None。"""
    vals = []
    for p in str(condition).replace("＋", "+").split("/"):
        p = p.strip()
        if not p:
            continue
        try:
            vals.append(float(p))
        except ValueError:
            return None
    if not vals:
        return None
    v = sum(vals) / len(vals)          # 主隊視角
    return v if side == "H" else -v


def hdc_text(condition, side="H"):
    """人話讓球描述,例如「受讓 0.25」/「讓 0.75」/「平手」。"""
    v = hdc_value(condition, side)
    if v is None:
        return _fmt_cond(condition)
    if abs(v) < 1e-9:
        return "平手"
    return ("受讓 " if v > 0 else "讓 ") + f"{abs(v):g}"


def hdc_cond_display(condition, side="H"):
    """把馬會盤口(主隊視角)翻成指定一方嘅視角。

    主隊: 原樣。客隊: 逐個數字反號並強制帶正負號,
    例如 "0.0/-0.5" -> "+0.0/+0.5"、"+0.5/+1.0" -> "-0.5/-1.0"。
    """
    raw = str(condition).replace("＋", "+").strip()
    if side == "H":
        return _fmt_cond(condition)
    out = []
    for p in raw.split("/"):
        p = p.strip()
        if not p:
            continue
        try:
            v = float(p)
        except ValueError:
            return _fmt_cond(condition)
        dec = len(p.split(".")[1]) if "." in p else 0
        v = -v
        sign = "-" if v < -1e-12 else "+"
        out.append(f"{sign}{abs(v):.{dec}f}")
    return "/".join(out) if out else _fmt_cond(condition)


def hdc_label(team, condition, side="H"):
    """完整顯示標籤:「格尼斯坦 受讓 0.25(馬會盤 +0.0/+0.5)」。"""
    return (f"{team} {hdc_text(condition, side)}"
            f"(馬會盤 {hdc_cond_display(condition, side)})")


def evaluate(odds, fit_g, fit_c, home="主", away="客"):
    """對每條線計算模型公平賠率與 Edge。"""
    cands = []
    if fit_g:
        lh, la, rho, rmse, nl = fit_g
        mat = score_matrix(lh, la, rho)
        gp = goals_pmf(mat)
        for ln in _lines_of(odds, "HDC"):
            if ln.get("status") not in (None, "AVAILABLE"):
                continue
            for side in ("H", "A"):
                o = ln["odds"].get(side)
                if not o or o <= 1.01:
                    continue
                w, pu = ah_prob(mat, ln.get("condition"), side)
                fo = fair_odds(w, pu)
                team = home if side == "H" else away
                cands.append(Candidate(
                    "讓球", "HDC", _fmt_cond(ln.get("condition")), side,
                    hdc_label(team, ln.get("condition"), side),
                    o, fo, w, pu, o / fo - 1, is_main=bool(ln.get("main"))))
        for ln in _lines_of(odds, "HIL"):
            if ln.get("status") not in (None, "AVAILABLE"):
                continue
            for side in ("H", "L"):
                o = ln["odds"].get(side)
                if not o or o <= 1.01:
                    continue
                w, pu = ou_prob(gp, ln.get("condition"), side)
                fo = fair_odds(w, pu)
                cands.append(Candidate(
                    "入球大小", "HIL", _fmt_cond(ln.get("condition")), side,
                    f"{'大' if side == 'H' else '細'} {_fmt_cond(ln.get('condition'))}",
                    o, fo, w, pu, o / fo - 1, is_main=bool(ln.get("main"))))
    if fit_c:
        mu, phi, crmse, nc = fit_c
        cp = corner_pmf(mu, phi)
        for ln in _lines_of(odds, "CHL"):
            if ln.get("status") not in (None, "AVAILABLE"):
                continue
            for side in ("H", "L"):
                o = ln["odds"].get(side)
                if not o or o <= 1.01:
                    continue
                w, pu = ou_prob(cp, ln.get("condition"), side)
                fo = fair_odds(w, pu)
                cands.append(Candidate(
                    "角球大小", "CHL", _fmt_cond(ln.get("condition")), side,
                    f"角球{'大' if side == 'H' else '細'} {_fmt_cond(ln.get('condition'))}",
                    o, fo, w, pu, o / fo - 1, is_main=bool(ln.get("main"))))
    return cands


# ---------------------------------------------------------------- 信心度 + 凱利

def confidence(c, fit_g, fit_c, minutes_to_ko, n_lines, drift=None):
    """0-100 信心分。"""
    s = 50.0
    # 1. 擬合質素:殘差越細,模型越可信
    rmse = (fit_g[3] if c.code in ("HDC", "HIL") and fit_g else
            (fit_c[2] if fit_c else 0.4))
    s += max(-22.0, min(18.0, (0.13 - rmse) * 160))
    # 2. 資料厚度:可用線數
    s += min(12.0, (n_lines - 4) * 1.6)
    # 3. Edge 幅度 (太大通常係數據問題,打折)
    e = abs(c.edge)
    if e > 0.18:
        s -= (e - 0.18) * 110
    else:
        s += min(14.0, e * 85)
    # 4. 主線流動性高,可信度高
    s += 7.0 if c.is_main else -6.0
    # 5. 越接近開賽,盤口資訊越完整
    if minutes_to_ko is not None:
        s += 9.0 if minutes_to_ko <= 8 else (4.0 if minutes_to_ko <= 35 else 0.0)
    # 6. 賠率極端 -> 減分
    if c.odds > 4.0 or c.odds < 1.30:
        s -= 9.0
    # 7. 盤口走勢:賠率向我方有利方向郁 = 逆市,減分;向不利方向郁 = 順市,加分
    if drift is not None:
        s += max(-10.0, min(10.0, -drift * 90))
    # 8. 角球模型本質波動較大
    if c.code == "CHL":
        s -= 5.0
    return max(0.0, min(100.0, s))


MIN_ODDS = 1.01   # 純數學下限(賠率必須 > 1)。已取消人為賠率門檻 —— 見下。

ODDS_CAP_KNEE = 0.80  # b = 賠率 - 1。b 低過呢個值,單場上限按 b/KNEE 線性遞減。


def odds_cap_mult(odds):
    """賠率遞減注碼上限乘數。

    取代舊有嘅硬性最低賠率門檻。理由:賠率門檻其實係一個偽裝咗嘅注碼
    上限,而且有武斷嘅懸崖(@1.51 可以落足 4%,@1.49 一蚊都唔落),同時
    針對錯咗變數 —— 真正嘅問題唔係短賠率校準容錯窄(呢個講法已被 EV
    敏感度分析推翻),而係注碼 = EV / b,b 細嘅時候同樣嘅模型誤差喺對數
    增長上嘅殺傷力大約放大十倍。

    所以直接針對注碼:b >= KNEE 用足上限;b < KNEE 按比例縮細。
      賠率 1.18 (b=0.18) → 22.5% 上限 → 0.90%
      賠率 1.35 (b=0.35) → 43.8% 上限 → 1.75%
      賠率 1.50 (b=0.50) → 62.5% 上限 → 2.50%
      賠率 1.80 (b=0.80) → 100%  上限 → 4.00%
    """
    b = max(0.0, float(odds) - 1.0)
    return min(1.0, b / ODDS_CAP_KNEE)


def kelly(c, bankroll, cap_pct=0.04, conf_floor=58.0,
          kelly_fraction=1.0 / 3.0, market_mult=None, min_odds=None):
    """分數凱利 + 單場上限。走水部分視作退本處理。

    兩層風險折讓:
      1. 機率收縮 —— 以信心度把模型機率向市場隱含機率靠。
      2. 凱利分數 —— 全凱利只在模型機率完全校準時最優;模型基礎層
         本質是盤口翻譯器,EV 系統性高估的風險高,故乘 kelly_fraction。
         market_mult 讓沒有獨立資料源的市場(角球)再打折。
    """
    floor_odds = MIN_ODDS if min_odds is None else float(min_odds)
    if c.confidence < conf_floor or c.edge <= 0 or c.odds < floor_odds:
        c.kelly_frac = 0.0
        c.stake = 0.0
        return c
    shrink = 0.35 + 0.65 * (c.confidence / 100.0)
    p = c.prob * shrink + (1 / c.odds) * (1 - shrink)
    denom = 1 - c.push
    if denom <= 1e-6:
        c.kelly_frac = 0.0
        c.stake = 0.0
        return c
    pe = min(0.98, p / denom)
    b = c.odds - 1
    f_full = max(0.0, (pe * b - (1 - pe)) / b) * denom   # 全凱利(走水已折)
    mult = 1.0
    if market_mult:
        mult = float(market_mult.get(c.code, 1.0))
    f = f_full * kelly_fraction * mult
    f = min(f, cap_pct * odds_cap_mult(c.odds))   # 賠率遞減單場上限
    c.kelly_frac = f
    c.kelly_full = round(f_full, 4)
    c.stake = round(bankroll * f / 10) * 10
    return c
