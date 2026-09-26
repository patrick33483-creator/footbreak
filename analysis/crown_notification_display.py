"""Display only: never evaluate a strategy, send, or write a betting record."""
import math
import sqlite3
from datetime import datetime, timedelta, timezone


def fixture_fields(match):
    fields = {k: match.get(k) for k in ("league", "home", "away")}
    if not all(fields.values()) and match.get("sid") is not None:
        try:
            with sqlite3.connect(
                "file:/opt/crown-radar-v2/data/crown.db?mode=ro", uri=True, timeout=2
            ) as db:
                row = db.execute(
                    "SELECT league,home,away FROM matches WHERE sid=?",
                    (str(match["sid"]),),
                ).fetchone()
                if row:
                    fields = {k: fields[k] or v for k, v in zip(fields, row)}
        except sqlite3.Error:
            pass
    return {k: str(v) if v else "資料未提供" for k, v in fields.items()}


def over_message(match, line, hongkong_odds, strategy, notes=()):
    line, hk = float(line), float(hongkong_odds)
    if not all(math.isfinite(v) for v in (line, hk)) or line <= 0 or hk <= 0:
        raise ValueError("Cannot display missing or invalid original selection odds")
    fields = fixture_fields(match)
    stamp = match.get("kickoff_utc_ms") or match.get("ko_utc_ms") or match.get("kickoff_utc")
    ko = datetime.fromtimestamp(float(stamp) / 1000, timezone(timedelta(hours=8)))
    text = [
        "皇冠賽前通知",
        f"聯賽：{fields['league']}",
        f"主隊：{fields['home']}",
        f"客隊：{fields['away']}",
        f"開賽：{ko:%Y-%m-%d %H:%M}（香港）",
        "",
        f"買乜：全場入球大 {line:g}",
        f"盤口：大 {line:g}" + ("（大 2.5／3）" if line == 2.75 else ""),
        f"皇冠賠率：{hk + 1:.2f}（十進制；港賠 {hk:.2f}）",
        f"策略：{strategy}",
    ]
    text.extend(str(n) for n in notes if n)
    return "\n".join(text)


def home_message(match, line, hongkong_odds, strategy, notes=()):
    line, hk = float(line), float(hongkong_odds)
    if not math.isfinite(line) or line != 0 or not math.isfinite(hk) or hk <= 0:
        raise ValueError("CE display requires original flat home price")
    fields = fixture_fields(match)
    ko = datetime.fromtimestamp(float(match["kickoff_utc_ms"]) / 1000,
                                timezone(timedelta(hours=8)))
    return "\n".join([
        "皇冠賽前通知",
        f"聯賽：{fields['league']}",
        f"主隊：{fields['home']}",
        f"客隊：{fields['away']}",
        f"開賽：{ko:%Y-%m-%d %H:%M}（香港）",
        "",
        f"買乜：買主隊 {fields['home']}",
        "盤口：平手 0（和局退回本金）",
        f"皇冠賠率：{hk + 1:.2f}（十進制；港賠 {hk:.2f}）",
        f"策略：{strategy}",
        *[str(n) for n in notes if n],
    ])
