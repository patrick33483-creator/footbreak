"""Idempotent Crown Telegram bet notifications."""
from __future__ import annotations

import json
import urllib.request
from typing import Any

from .common import iso_hkt, read_json, write_json_atomic
from .config import Settings
from .state import paths


def _quarter_line(value: Any, signed: bool = True) -> str:
    try:
        line = float(value)
    except (TypeError, ValueError):
        return str(value or "")
    sign = ("-" if line < 0 else "+" if line > 0 else "") if signed else ""
    amount = abs(line)
    quarters = round(amount * 4)
    whole, rem = divmod(quarters, 4)
    if rem == 0:
        body = str(whole)
    elif rem == 1:
        body = f"{whole}/{whole + 0.5:g}"
    elif rem == 2:
        body = f"{whole + 0.5:g}"
    else:
        body = f"{whole + 0.5:g}/{whole + 1}"
    return f"{sign}{body}"


def _bet_label(bet: dict[str, Any]) -> str:
    market = str(bet.get("market") or bet.get("code") or "")
    side = str(bet.get("side") or "")
    line = bet.get("line", bet.get("condition"))
    if market == "HDC":
        team = bet.get("home") if side == "H" else bet.get("away")
        # Stored handicap is from the home-team viewpoint.  Convert it to the
        # selected team's viewpoint before displaying an away-side bet.
        selected_line = -float(line) if side == "A" and line is not None else line
        return f"讓球 · {team} {_quarter_line(selected_line)}"
    if market == "HIL":
        return f"入球大細 · {'大' if side == 'H' else '細'} {_quarter_line(line, signed=False)}"
    return str(bet.get("label") or f"{market} {side} {line}")


def _load(config: Settings) -> dict[str, Any]:
    state = read_json(paths(config)["notify"], {"bets": []})
    state.setdefault("bets", [])
    return state


def _send(config: Settings, text: str) -> None:
    if not (config.telegram_enabled and config.telegram_bot_token and config.telegram_chat_id):
        return
    body = json.dumps({"chat_id": config.telegram_chat_id, "text": text}).encode()
    request = urllib.request.Request(f"https://api.telegram.org/bot{config.telegram_bot_token}/sendMessage", data=body,
                                     headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(request, timeout=20):
        pass


def notify_new(ledger: dict[str, Any], config: Settings) -> int:
    state, sent = _load(config), 0
    seen = set(state["bets"])
    for bet in ledger.get("bets", []):
        bid = str(bet.get("bet_id"))
        if bet.get("status") != "PENDING" or bid in seen:
            continue
        stake = float(bet.get("stake") or 0)
        odds = float(bet.get("odds") or 0)
        _send(
            config,
            "皇冠模擬注單\n"
            f"{bet['home']} vs {bet['away']}\n"
            f"投注：{_bet_label(bet)}\n"
            f"賠率：{odds:.2f}\n"
            f"注碼：HK${stake:,.0f}\n"
            "只作模擬，絕不實際投注。",
        )
        state["bets"].append(bid)
        seen.add(bid)
        sent += 1
    state["updated_at"] = iso_hkt()
    write_json_atomic(paths(config)["notify"], state)
    return sent
