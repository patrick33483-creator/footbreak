"""模擬倉帳本 — 記錄推介、注碼、結算與 ROI。

本金 $50,000。全凱利 + 單場 8% 上限,另加組合層級每日曝險上限 25%。
"""
import csv
import json
import os
from datetime import datetime

import hkjc_feed as H

DIR = os.path.dirname(os.path.abspath(__file__))
LEDGER = os.path.join(DIR, "portfolio.json")
BANKROLL0 = 50000.0
DAILY_CAP_PCT = 1.00       # 同一日全部注碼合計上限(用戶指定:不設限)
OPEN_CAP_PCT = 1.00        # 未結算注碼合計上限(用戶指定:不設限)

FIELDS = ["bet_id", "match_id", "kickoff", "league", "home", "away", "stage",
          "market", "code", "condition", "side", "label", "odds", "fair",
          "prob", "push", "edge", "confidence", "kelly_frac", "stake",
          "placed_at", "status", "result", "pnl", "note"]


def load():
    if not os.path.exists(LEDGER):
        return {"bankroll0": BANKROLL0, "bets": []}
    with open(LEDGER, encoding="utf8") as fh:
        return json.load(fh)


def save(led):
    with open(LEDGER, "w", encoding="utf8") as fh:
        json.dump(led, fh, ensure_ascii=False, indent=1)


def bet_key(match_id, code, condition, side):
    return f"{match_id}|{code}|{condition}|{side}"


def open_stake(led):
    return sum(b["stake"] for b in led["bets"] if b["status"] == "PENDING")


def day_stake(led, day):
    return sum(b["stake"] for b in led["bets"]
               if b["status"] == "PENDING" and b["kickoff"][:10] == day)


def equity(led):
    return led["bankroll0"] + sum(b.get("pnl") or 0 for b in led["bets"])


def apply_portfolio_caps(led, cand_stake, kickoff_day):
    """回傳經組合上限調整後的注碼 (可能為 0)。"""
    eq = equity(led)
    room_day = max(0.0, eq * DAILY_CAP_PCT - day_stake(led, kickoff_day))
    room_open = max(0.0, eq * OPEN_CAP_PCT - open_stake(led))
    allowed = min(cand_stake, room_day, room_open)
    if allowed < 200:          # 太細唔值得落
        return 0.0
    return round(allowed / 10) * 10


def record(led, hk_match, c, stage, note=""):
    """新增或更新一筆推介。同一盤口重複出現則更新(記最新一段)。"""
    mid = str(hk_match["id"])
    ko = H.parse_kickoff(hk_match)
    key = bet_key(mid, c.code, c.condition, c.side)
    day = ko.date().isoformat()
    existing = next((b for b in led["bets"] if b["bet_id"] == key), None)
    if existing and existing["status"] != "PENDING":
        return existing, "已結算,不改"
    stake = c.stake
    if existing:
        # 已落注:只在注碼上升時補注,下降不減(已成交)
        add = apply_portfolio_caps(led, max(0.0, stake - existing["stake"]), day)
        if add <= 0:
            existing["note"] = (existing.get("note") or "") + f" | {stage}維持"
            return existing, "維持"
        existing["stake"] += add
        existing["odds"] = c.odds
        existing["edge"] = c.edge
        existing["confidence"] = c.confidence
        existing["stage"] = stage
        existing["note"] = (existing.get("note") or "") + f" | {stage}加注${add:,.0f}"
        return existing, f"加注 ${add:,.0f}"
    stake = apply_portfolio_caps(led, stake, day)
    if stake <= 0:
        return None, "組合上限已滿"
    row = {
        "bet_id": key, "match_id": mid, "kickoff": ko.isoformat(),
        "league": hk_match["tournament"]["name_ch"],
        "home": hk_match["homeTeam"]["name_ch"],
        "away": hk_match["awayTeam"]["name_ch"],
        "stage": stage, "market": c.market, "code": c.code,
        "condition": c.condition, "side": c.side, "label": c.label,
        "odds": c.odds, "fair": round(c.fair, 3), "prob": round(c.prob, 4),
        "push": round(c.push, 4), "edge": round(c.edge, 4),
        "confidence": round(c.confidence, 1), "kelly_frac": round(c.kelly_frac, 4),
        "stake": stake, "placed_at": H.now_hkt().isoformat(),
        "status": "PENDING", "result": None, "pnl": None, "note": note,
    }
    led["bets"].append(row)
    return row, "新增"


# ------------------------------------------------------------------ 結算

def settle_one(bet, home_goals, away_goals, home_corners=None, away_corners=None):
    """按賽果計盈虧。回傳 (result, pnl) 或 None (資料不足)。"""
    import model as M
    parts = M.parse_condition(bet["condition"])
    if not parts:
        return None
    stake, odds = bet["stake"], bet["odds"]
    per = stake / len(parts)          # 四分盤拆兩半注
    total_pnl = 0.0
    outcomes = []
    for line in parts:
        if bet["code"] == "HDC":
            # condition 為主隊角度;主隊 margin + 讓球
            margin = (home_goals - away_goals) + line
            if bet["side"] == "A":
                margin = -margin
            if margin > 1e-9:
                total_pnl += per * (odds - 1); outcomes.append("W")
            elif abs(margin) < 1e-9:
                outcomes.append("P")
            else:
                total_pnl -= per; outcomes.append("L")
        else:
            if bet["code"] == "HIL":
                tot = home_goals + away_goals
            else:
                if home_corners is None or away_corners is None:
                    return None
                tot = home_corners + away_corners
            diff = tot - line
            if bet["side"] == "H":       # 大
                pass
            else:                        # 細
                diff = -diff
            if diff > 1e-9:
                total_pnl += per * (odds - 1); outcomes.append("W")
            elif abs(diff) < 1e-9:
                outcomes.append("P")
            else:
                total_pnl -= per; outcomes.append("L")
    tag = {"W": "全贏", "L": "全輸", "P": "走水"}
    if len(set(outcomes)) == 1:
        res = tag[outcomes[0]]
    elif set(outcomes) == {"W", "P"}:
        res = "半贏"
    elif set(outcomes) == {"L", "P"}:
        res = "半輸"
    else:
        res = "半贏半輸"
    return res, round(total_pnl, 2)


def summary(led):
    bets = led["bets"]
    done = [b for b in bets if b["status"] == "SETTLED"]
    pend = [b for b in bets if b["status"] == "PENDING"]
    turn = sum(b["stake"] for b in done)
    pnl = sum(b.get("pnl") or 0 for b in done)
    return {
        "本金": led["bankroll0"],
        "現時淨值": round(equity(led), 2),
        "已結算注數": len(done),
        "待結算注數": len(pend),
        "待結算曝險": round(sum(b["stake"] for b in pend), 2),
        "總投注額": round(turn, 2),
        "總盈虧": round(pnl, 2),
        "ROI": (round(pnl / turn, 4) if turn else None),
        "命中率": (round(sum(1 for b in done if (b.get("pnl") or 0) > 0) / len(done), 4)
                 if done else None),
    }


def export_csv(led, path):
    with open(path, "w", newline="", encoding="utf-8-sig") as fh:
        w = csv.DictWriter(fh, fieldnames=FIELDS, extrasaction="ignore")
        w.writeheader()
        for b in sorted(led["bets"], key=lambda x: x["kickoff"]):
            w.writerow(b)
    return path
