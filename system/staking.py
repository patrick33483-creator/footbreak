"""注碼階段控制 —— 以已結算樣本的校準表現決定凱利分數。

理念:全凱利只在模型機率完全校準時才是最優增長策略。本系統的基礎機率
由 Dixon-Coles / 負二項模型擬合 Pinnacle 去水盤得出,本質是「盤口翻譯
器」,EV 系統性高估的風險高,故起步用 1/3 凱利,並以實際結算數據作為
放大注碼的唯一依據。

階段:
  1  0–80 注                                   1/3 凱利,單場上限 4%
  2  ≥80 注 且 校準斜率 > 0.6                   1/2 凱利,單場上限 5%
  3  ≥200 注 且 實際 ROI > 模型預測 ROI 的 60%   2/3 凱利,單場上限 6%
  降級  實際命中率低於模型預測 8 個百分點以上      退回上一階段

角球(CHL)沒有任何獨立資料源(μ 100% 由盤口反推,調整層借用入球
彈性),EV 幻覺風險最高,故一律再乘 0.5。
"""
import json
import os

HERE = os.path.dirname(os.path.abspath(__file__))
LEDGER = os.path.join(HERE, "sim_ledger.json")

MARKET_MULT = {"CHL": 0.5}          # 角球額外折讓

STAGES = {
    1: {"fraction": 1.0 / 3.0, "cap": 0.04, "label": "階段一 · 建立樣本"},
    2: {"fraction": 0.50,      "cap": 0.05, "label": "階段二 · 校準通過"},
    3: {"fraction": 2.0 / 3.0, "cap": 0.06, "label": "階段三 · 已驗證"},
}

# 帳本用 status="SETTLED" + result 字串,呢度統一映射
HIT_W = {"Won": 1.0, "Half Won": 0.75, "Half Lost": 0.25,
         "Lost": 0.0, "Refunded": None}      # None = 走水,不計入命中率


def _prob(b):
    v = b.get("model_prob", b.get("prob"))
    return None if v is None else float(v)


def _edge(b):
    v = b.get("ev", b.get("edge"))
    return None if v is None else float(v)


def _hit(b):
    """命中權重;走水或未結算回傳 None。"""
    return HIT_W.get(b.get("result"))


def _settled(led):
    return [b for b in led.get("bets", [])
            if b.get("status") == "SETTLED" and b.get("result") in HIT_W]


def _decided(bets):
    return [b for b in bets if _hit(b) is not None]


def calibration(bets):
    """把注單按模型機率分桶,計實際命中率,再做加權最小平方迴歸求斜率。

    斜率 1.0 = 完美校準;0.5 = 實際 edge 只有模型自稱的一半。
    走水(PUSH)不計入命中率分母;半贏半輸各算 0.5 次命中。
    """
    edges = [(0.35, 0.45), (0.45, 0.50), (0.50, 0.55),
             (0.55, 0.60), (0.60, 0.70), (0.70, 1.01)]
    buckets = []
    for lo, hi in edges:
        sel = [b for b in _decided(bets)
               if _prob(b) is not None and lo <= _prob(b) < hi]
        if not sel:
            continue
        n = len(sel)
        hits = sum(_hit(b) for b in sel)
        buckets.append({
            "lo": lo, "hi": hi, "n": n,
            "pred": sum(_prob(b) for b in sel) / n,
            "actual": hits / n,
        })
    slope = None
    if len(buckets) >= 3:
        sw = sum(b["n"] for b in buckets)
        mx = sum(b["pred"] * b["n"] for b in buckets) / sw
        my = sum(b["actual"] * b["n"] for b in buckets) / sw
        num = sum(b["n"] * (b["pred"] - mx) * (b["actual"] - my) for b in buckets)
        den = sum(b["n"] * (b["pred"] - mx) ** 2 for b in buckets)
        if den > 1e-9:
            slope = num / den
    return buckets, slope


def performance(bets):
    """實際 ROI vs 模型預測 ROI,以及命中率落差。"""
    stake = sum(float(b.get("stake") or 0) for b in bets)
    pnl = sum(float(b.get("pnl") or 0) for b in bets)
    roi = pnl / stake if stake > 0 else None
    pred_roi = None
    ev = [_edge(b) for b in bets if _edge(b) is not None]
    if ev:
        pred_roi = sum(ev) / len(ev)
    dec = _decided(bets)
    hit_gap = None
    if dec:
        hits = sum(_hit(b) for b in dec)
        pr = [_prob(b) for b in dec if _prob(b) is not None]
        if pr:
            hit_gap = hits / len(dec) - sum(pr) / len(pr)
    return {"n": len(bets), "stake": stake, "pnl": pnl, "roi": roi,
            "pred_roi": pred_roi, "hit_gap": hit_gap}


def stage(led=None):
    """回傳目前應採用的注碼階段。"""
    if led is None:
        try:
            with open(LEDGER, encoding="utf-8") as f:
                led = json.load(f)
        except (OSError, ValueError):
            led = {"bets": []}
    bets = _settled(led)
    buckets, slope = calibration(bets)
    perf = performance(bets)
    n = perf["n"]

    lvl = 1
    if n >= 80 and slope is not None and slope > 0.6:
        lvl = 2
    if (n >= 200 and slope is not None and slope > 0.6
            and perf["roi"] is not None and perf["pred_roi"]
            and perf["roi"] > 0.6 * perf["pred_roi"]):
        lvl = 3
    # 降級:實際命中率低於模型預測 8 個百分點以上
    if perf["hit_gap"] is not None and perf["hit_gap"] < -0.08 and n >= 30:
        lvl = max(1, lvl - 1)
        demoted = True
    else:
        demoted = False

    st = dict(STAGES[lvl])
    st.update({"level": lvl, "n_settled": n, "slope": slope,
               "buckets": buckets, "perf": perf, "demoted": demoted,
               "market_mult": MARKET_MULT})
    return st


if __name__ == "__main__":
    import sys
    s = stage()
    s.pop("buckets", None) if "--brief" in sys.argv else None
    print(json.dumps(s, ensure_ascii=False, indent=1))
