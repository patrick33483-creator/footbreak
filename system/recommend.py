"""HKJC 足球投注推介引擎

以 Pinnacle 去水盤為銳利基準擬合 Dixon-Coles / 負二項角球模型,
再用該模型評估 HKJC 讓球 (HDC)、入球大小 (HIL)、總角球大小 (CHL) 的優勢值,
最後計分數凱利注碼 (階段控制,起步 1/3 凱利、單場上限 4%,本金 $50,000)。
"""
import json
import os
from dataclasses import asdict
from datetime import datetime, timedelta

import hkjc_feed as H
import model as M
import sharp as S
import staking as K

BANKROLL = 50000.0
CONF_FLOOR = 58.0
_STAGE = None


def stake_stage(refresh=False):
    """目前注碼階段(凱利分數、單場上限、市場折讓)。以已結算樣本決定。"""
    global _STAGE
    if _STAGE is None or refresh:
        _STAGE = K.stage()
    return _STAGE


CAP_PCT = stake_stage()["cap"]
MIN_EDGE = 0.02
SNAP_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "snapshots")
os.makedirs(SNAP_DIR, exist_ok=True)


def build_pairs(matches=None, fixtures=None):
    """把 HKJC 賽事對應到 Pinnacle fixture。回傳 [(hk_match, fixture, score)]"""
    if matches is None:
        matches = [m for m in H.fetch_matches() if m.get("status") == "PREEVENT"]
    if fixtures is None:
        fixtures = S.list_fixtures()
    out = []
    for m in matches:
        ko = H.parse_kickoff(m)
        if not ko:
            continue
        fx, sc = S.match_fixture(m, fixtures, ko)
        if fx:
            out.append((m, fx, sc))
    return out


def analyse(hk_match, pin_odds, minutes_to_ko, drift_map=None):
    """對單場賽事產生候選盤口 (已計信心度與注碼)。"""
    home = hk_match["homeTeam"]["name_ch"]
    away = hk_match["awayTeam"]["name_ch"]
    fg = M.fit_goals(pin_odds)
    fc = M.fit_corners(pin_odds)
    if not fg and not fc:
        return [], None, None
    hk = H.flatten_odds(hk_match)
    n_lines = sum(len(hk.get(k) or []) for k in ("HAD", "HDC", "HIL", "CHL"))
    cands = M.evaluate(hk, fg, fc, home, away)
    for c in cands:
        key = f"{c.code}|{c.condition}|{c.side}"
        drift = (drift_map or {}).get(key)
        c.confidence = M.confidence(c, fg, fc, minutes_to_ko, n_lines, drift)
        st = stake_stage()
        M.kelly(c, BANKROLL, st["cap"], CONF_FLOOR,
                kelly_fraction=st["fraction"], market_mult=st["market_mult"])
    return cands, fg, fc


def pick_best(cands):
    """每場每個市場最多揀一條:優勢值最高且信心度過關。"""
    best = {}
    for c in cands:
        if c.edge < MIN_EDGE or c.stake <= 0:
            continue
        cur = best.get(c.code)
        score = c.edge * (c.confidence / 100.0)
        if cur is None or score > cur[0]:
            best[c.code] = (score, c)
    return [c for _, c in sorted(best.values(), key=lambda x: -x[0])]


# ------------------------------------------------------------------ 快照

def snap_path(match_id, stage):
    return os.path.join(SNAP_DIR, f"{match_id}_{stage}.json")


def save_snapshot(match_id, stage, hk_odds, picks, fg, fc, kickoff):
    data = {
        "match_id": match_id,
        "stage": stage,
        "saved_at": H.now_hkt().isoformat(),
        "kickoff": kickoff.isoformat() if kickoff else None,
        "hk_odds": hk_odds,
        "fit_goals": list(fg) if fg else None,
        "fit_corners": list(fc) if fc else None,
        "picks": [asdict(c) for c in picks],
    }
    with open(snap_path(match_id, stage), "w", encoding="utf8") as fh:
        json.dump(data, fh, ensure_ascii=False)
    return data


def load_snapshot(match_id, stage):
    p = snap_path(match_id, stage)
    if not os.path.exists(p):
        return None
    with open(p, encoding="utf8") as fh:
        return json.load(fh)


def odds_lookup(hk_odds):
    out = {}
    for code, lines in (hk_odds or {}).items():
        if not isinstance(lines, list):
            continue
        for ln in lines:
            for side, price in (ln.get("odds") or {}).items():
                out[f"{code}|{ln.get('condition')}|{side}"] = price
    return out


def compute_drift(prev_hk_odds, cur_hk_odds):
    """賠率變化率 (正數 = 賠率上升 = 該邊被市場冷落)。"""
    prev, cur = odds_lookup(prev_hk_odds), odds_lookup(cur_hk_odds)
    out = {}
    for k, v in cur.items():
        p = prev.get(k)
        if p and v and p > 1 and v > 1:
            out[k] = v / p - 1
    return out
